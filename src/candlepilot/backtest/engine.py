"""Event-driven backtest loop for a single symbol.

Three ordering rules carry most of the correctness:

1. **Signals act on the next bar's open.** A strategy decides on bar ``i``'s close
   and fills at bar ``i+1``'s open, so no decision can consume its own outcome.
2. **Intrabar path is assumed adverse.** At 1m resolution the path inside a bar is
   unknown; when several levels sit inside one bar's range, the ones that hurt fire
   first, and the nearest adverse level fires before a farther one. Resolving ties
   the other way produces a systematic optimistic bias that grows as stops tighten —
   exactly where intraday strategies live.
3. **Liquidation is checked on mark price, exits on trade price.** These are
   different series for a reason; see ``dataset.build_bars``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from .costs import CostModel
from .position import DEFAULT_MMR, Position, size_for_risk


@dataclass(frozen=True)
class Intent:
    """What the strategy wants done at the next bar's open."""

    action: str  # "long", "short", "exit"
    stop_price: float | None = None
    target_price: float | None = None
    tags: dict = field(default_factory=dict)


@dataclass
class BarContext:
    """What a strategy may see: everything up to and including the current bar."""

    i: int
    bar: pd.Series
    position: Position | None
    equity: float
    _frame: pd.DataFrame

    @property
    def history(self) -> pd.DataFrame:
        """Bars 0..i inclusive. Deliberately the only window on the past."""
        return self._frame.iloc[: self.i + 1]


class Strategy(Protocol):
    def on_bar(self, ctx: BarContext) -> Intent | None: ...


@dataclass
class Trade:
    symbol: str
    side: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    margin: float
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    exit_reason: str
    bars_held: int

    @property
    def return_on_margin(self) -> float:
        return self.net_pnl / self.margin if self.margin else 0.0

    @property
    def leverage(self) -> float:
        return self.qty * self.entry_price / self.margin if self.margin else float("inf")


@dataclass
class BacktestResult:
    symbol: str
    equity_curve: pd.Series
    trades: list[Trade]
    initial_equity: float
    cost_model: CostModel
    liquidations: int = 0
    skipped_untradeable: int = 0

    @property
    def trades_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.__dict__ for t in self.trades])


class Backtest:
    """Single-symbol, single-position backtest."""

    def __init__(
        self,
        frame: pd.DataFrame,
        *,
        symbol: str = "",
        cost_model: CostModel | None = None,
        initial_equity: float = 10_000.0,
        risk_fraction: float = 0.01,
        max_leverage: float = 20.0,
        mmr: float = DEFAULT_MMR,
    ):
        if max_leverage > 20:
            raise ValueError("max_leverage above 20x exceeds the project's stated cap")
        self.frame = frame
        self.symbol = symbol
        self.costs = cost_model or CostModel()
        self.initial_equity = initial_equity
        self.risk_fraction = risk_fraction
        self.max_leverage = max_leverage
        self.mmr = mmr

    # ------------------------------------------------------------------ internals

    def _open(self, intent: Intent, bar: pd.Series, i: int, equity: float) -> Position | None:
        side = 1 if intent.action == "long" else -1
        raw_price = float(bar["open"])
        price = self.costs.fill_price(raw_price, side, opening=True)

        stop = intent.stop_price
        if stop is None:
            return None
        qty, margin = size_for_risk(
            equity,
            price,
            stop,
            risk_fraction=self.risk_fraction,
            max_leverage=self.max_leverage,
            mmr=self.mmr,
        )
        if qty <= 0:
            return None

        position = Position(
            side=side,
            entry_price=price,
            qty=qty,
            margin=margin,
            entry_index=i,
            entry_time=bar.name,
            stop_price=stop,
            target_price=intent.target_price,
            mmr=self.mmr,
            tags=dict(intent.tags),
        )
        position.fees_paid += self.costs.fee(qty * price)
        return position

    def _resolve_exit(self, position: Position, bar: pd.Series) -> tuple[float, str] | None:
        """Pick which level a bar triggers, assuming the worst plausible path.

        Adverse levels are resolved in the order price would reach them, and a level
        the bar **gapped through** fills at the open rather than at the level — a
        stop is a trigger, not a guaranteed price. Ignoring that is what makes naive
        backtests survive crashes they would not have survived: the gap-through is
        the realistic route to liquidation, since correct sizing keeps the stop
        nearer than the liquidation price in every ordinary bar.
        """
        side = position.side
        open_price, low, high = float(bar["open"]), float(bar["low"]), float(bar["high"])
        liq = position.liquidation_price()

        def fill_for(level: float) -> float:
            """Gapped-through levels fill at the open, which is worse."""
            return min(level, open_price) if side > 0 else max(level, open_price)

        adverse: list[tuple[float, str]] = []

        if position.stop_price is not None:
            stop = position.stop_price
            if (low <= stop) if side > 0 else (high >= stop):
                adverse.append((stop, "stop"))

        liquidated = bool(bar["tradeable"]) and position.is_liquidated(
            float(bar["mark_low"]), float(bar["mark_high"])
        )
        if liquidated:
            adverse.append((liq, "liquidation"))

        if adverse:
            # A long is walked down through the higher level first; a short up.
            adverse.sort(key=lambda item: -item[0] if side > 0 else item[0])
            level, reason = adverse[0]
            fill = fill_for(level)
            # No exit can print beyond the liquidation price: the exchange would
            # have force-closed the position before that fill was reachable.
            if liquidated and ((fill < liq) if side > 0 else (fill > liq)):
                return liq, "liquidation"
            return fill, reason

        if position.target_price is not None:
            target = position.target_price
            if (high >= target) if side > 0 else (low <= target):
                # Filled at the level, never at a favourable gap.
                return target, "target"

        return None

    def _close(
        self,
        position: Position,
        price: float,
        bar: pd.Series,
        i: int,
        reason: str,
    ) -> tuple[Trade, float]:
        fill = self.costs.fill_price(price, position.side, opening=False)
        if reason == "liquidation":
            # A liquidation is a forced market exit; the exchange also keeps the
            # remaining margin, so the loss is floored at the margin posted.
            fill = price
        gross = position.side * position.qty * (fill - position.entry_price)
        exit_fee = self.costs.fee(position.qty * fill)
        fees = position.fees_paid + exit_fee
        net = gross - fees - position.funding_paid
        if reason == "liquidation":
            net = max(net, -position.margin)

        trade = Trade(
            symbol=self.symbol,
            side=position.side,
            entry_time=position.entry_time,
            exit_time=bar.name,
            entry_price=position.entry_price,
            exit_price=fill,
            qty=position.qty,
            margin=position.margin,
            gross_pnl=gross,
            fees=fees,
            funding=position.funding_paid,
            net_pnl=net,
            exit_reason=reason,
            bars_held=i - position.entry_index,
        )
        return trade, net

    # ----------------------------------------------------------------------- run

    def run(self, strategy: Strategy) -> BacktestResult:
        frame = self.frame
        equity = self.initial_equity
        position: Position | None = None
        pending: Intent | None = None

        trades: list[Trade] = []
        curve = []
        liquidations = 0
        skipped = 0

        opens = frame["open"].to_numpy()
        columns = frame.columns

        for i in range(len(frame)):
            bar = frame.iloc[i]

            # 1. Fill the previous bar's decision at this bar's open.
            if pending is not None:
                if pending.action == "exit" and position is not None:
                    trade, net = self._close(position, float(opens[i]), bar, i, "signal")
                    trades.append(trade)
                    equity += net
                    position = None
                elif pending.action in ("long", "short") and position is None:
                    if bool(bar["tradeable"]):
                        position = self._open(pending, bar, i, equity)
                    else:
                        # No usable mark price means liquidation cannot be evaluated;
                        # entering anyway would silently disable the safety check.
                        skipped += 1
                pending = None

            # 2. Funding settles against the open position.
            if position is not None:
                rate = float(bar["funding_rate"])
                if rate:
                    mark = float(bar["mark_close"])
                    position.funding_paid += position.side * position.qty * mark * rate

            # 3./4. Liquidation and exit levels, worst-path ordering.
            if position is not None:
                resolved = self._resolve_exit(position, bar)
                if resolved is not None:
                    price, reason = resolved
                    trade, net = self._close(position, price, bar, i, reason)
                    trades.append(trade)
                    equity += net
                    if reason == "liquidation":
                        liquidations += 1
                    position = None

            # 5. Strategy sees the closed bar and decides for the next one.
            unrealized = position.unrealized(float(bar["close"])) if position else 0.0
            ctx = BarContext(i=i, bar=bar, position=position, equity=equity, _frame=frame)
            intent = strategy.on_bar(ctx)
            if intent is not None:
                pending = intent

            curve.append(equity + unrealized)

        return BacktestResult(
            symbol=self.symbol,
            equity_curve=pd.Series(curve, index=frame.index, name="equity"),
            trades=trades,
            initial_equity=self.initial_equity,
            cost_model=self.costs,
            liquidations=liquidations,
            skipped_untradeable=skipped,
        )
