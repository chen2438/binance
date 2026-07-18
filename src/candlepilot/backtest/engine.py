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
from .execution import Bar, SymbolExecutor
from .position import DEFAULT_MMR, Position


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
        # Shared with the portfolio backtest so both fill identically.
        self.executor = SymbolExecutor(
            cost_model=self.costs,
            risk_fraction=risk_fraction,
            max_leverage=max_leverage,
            mmr=mmr,
        )

    # ------------------------------------------------------------------ internals

    def _bar(self, row: pd.Series) -> Bar:
        return Bar(
            time=row.name,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            mark_low=float(row["mark_low"]),
            mark_high=float(row["mark_high"]),
            mark_close=float(row["mark_close"]),
            funding_rate=float(row["funding_rate"]),
            tradeable=bool(row["tradeable"]),
        )

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

        for i in range(len(frame)):
            row = frame.iloc[i]
            bar = self._bar(row)

            # 1. Fill the previous bar's decision at this bar's open.
            if pending is not None:
                if pending.action == "exit" and position is not None:
                    fields, net = self.executor.close_position(
                        position, bar.open, bar, i, "signal", symbol=self.symbol
                    )
                    trades.append(Trade(**fields))
                    equity += net
                    position = None
                elif pending.action in ("long", "short") and position is None:
                    if bar.tradeable and pending.stop_price is not None:
                        position = self.executor.open_position(
                            side=1 if pending.action == "long" else -1,
                            bar=bar,
                            index=i,
                            equity=equity,
                            stop_price=pending.stop_price,
                            target_price=pending.target_price,
                            tags=pending.tags,
                        )
                    elif not bar.tradeable:
                        # No usable mark price means liquidation cannot be evaluated;
                        # entering anyway would silently disable the safety check.
                        skipped += 1
                pending = None

            # 2. Funding settles against the open position.
            if position is not None:
                self.executor.settle_funding(position, bar)

            # 3./4. Liquidation and exit levels, worst-path ordering.
            if position is not None:
                resolved = self.executor.resolve_exit(position, bar)
                if resolved is not None:
                    price, reason = resolved
                    fields, net = self.executor.close_position(
                        position, price, bar, i, reason, symbol=self.symbol
                    )
                    trades.append(Trade(**fields))
                    equity += net
                    if reason == "liquidation":
                        liquidations += 1
                    position = None

            # 5. Strategy sees the closed bar and decides for the next one.
            unrealized = position.unrealized(bar.close) if position else 0.0
            ctx = BarContext(i=i, bar=row, position=position, equity=equity, _frame=frame)
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
