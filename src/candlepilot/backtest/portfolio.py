"""Multi-symbol portfolio backtest driven by a rolling screen.

Three policy decisions shape this layer, and each is a place where a backtest can
quietly flatter itself:

* **Leaving the pool does not force a close.** A symbol dropping out of the screen
  stops *new* entries but leaves open positions alone. Forcing closes would let the
  rebalance cadence overwrite the strategy's own exit logic, so a weekly screen would
  silently cap holding periods at a week and the measured edge would be the screen's,
  not the strategy's.
* **Delisting does force a close.** When a symbol's data ends while a position is
  open, it is closed at the last available price and tagged ``delisted``. Letting the
  position evaporate would drop its loss on the floor — which is exactly the
  survivorship bias the data layer went to such trouble to avoid.
* **Equity is shared.** Risk is sized against total portfolio equity, so positions
  compete for capital and a drawdown in one symbol shrinks every other position.
  Running per-symbol backtests and summing them would assume infinite capital.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel
from .engine import BarContext, Intent, Strategy, Trade
from .execution import Bar, SymbolExecutor
from .position import DEFAULT_MMR, Position

log = logging.getLogger("candlepilot.portfolio")


@dataclass
class _Feed:
    """One symbol's bars as arrays, with a cursor into the global timeline."""

    symbol: str
    frame: pd.DataFrame
    times: np.ndarray
    cursor: int = 0
    position: Position | None = None
    pending: Intent | None = None
    strategy: object = None

    @property
    def exhausted(self) -> bool:
        return self.cursor >= len(self.times)

    @property
    def last_time(self):
        return self.times[-1]


@dataclass
class PortfolioResult:
    equity_curve: pd.Series
    trades: list[Trade]
    initial_equity: float
    cost_model: CostModel
    liquidations: int = 0
    delisted_exits: int = 0
    skipped_untradeable: int = 0
    skipped_at_capacity: int = 0
    skipped_out_of_pool: int = 0
    pool_history: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def trades_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.__dict__ for t in self.trades])

    def by_symbol(self) -> pd.DataFrame:
        frame = self.trades_frame
        if frame.empty:
            return pd.DataFrame()
        return (
            frame.groupby("symbol")
            .agg(
                trades=("net_pnl", "size"),
                net_pnl=("net_pnl", "sum"),
                fees=("fees", "sum"),
                funding=("funding", "sum"),
                wins=("net_pnl", lambda s: int((s > 0).sum())),
            )
            .sort_values("net_pnl")
        )


class PortfolioBacktest:
    """Runs one strategy across a time-varying pool of symbols on shared equity."""

    def __init__(
        self,
        bars: dict[str, pd.DataFrame],
        pool: pd.DataFrame,
        *,
        cost_model: CostModel | None = None,
        initial_equity: float = 10_000.0,
        risk_fraction: float = 0.01,
        max_leverage: float = 20.0,
        max_positions: int = 5,
        mmr: float = DEFAULT_MMR,
        close_on_pool_exit: bool = False,
    ):
        if max_leverage > 20:
            raise ValueError("max_leverage above 20x exceeds the project's stated cap")
        if not bars:
            raise ValueError("no symbol bars supplied")

        self.bars = bars
        self.pool = pool
        self.costs = cost_model or CostModel()
        self.initial_equity = initial_equity
        self.max_positions = max_positions
        self.close_on_pool_exit = close_on_pool_exit
        self.executor = SymbolExecutor(
            cost_model=self.costs,
            risk_fraction=risk_fraction,
            max_leverage=max_leverage,
            mmr=mmr,
        )

    # ------------------------------------------------------------------ pool state

    def _pool_schedule(self) -> tuple[np.ndarray, list[dict[str, int]]]:
        """Rebalance timestamps and the symbol -> side mapping in force from each.

        A pool without a ``side`` column assigns 0, meaning "in the pool, direction
        left to the strategy".
        """
        if self.pool.empty:
            return np.array([], dtype="datetime64[ns]"), []

        frame = self.pool
        sides = frame["side"] if "side" in frame.columns else pd.Series(0, index=frame.index)
        tagged = frame.assign(_side=sides)
        grouped = (
            tagged.groupby("date")
            .apply(lambda g: dict(zip(g["symbol"], g["_side"])), include_groups=False)
            .sort_index()
        )
        return grouped.index.to_numpy(), grouped.tolist()

    # ------------------------------------------------------------------------ run

    def run(self, strategy_factory) -> PortfolioResult:
        feeds = {
            symbol: _Feed(
                symbol=symbol,
                frame=frame,
                times=frame.index.to_numpy(),
                strategy=strategy_factory(),
            )
            for symbol, frame in self.bars.items()
            if not frame.empty
        }

        timeline = np.unique(np.concatenate([feed.times for feed in feeds.values()]))
        pool_times, pool_sets = self._pool_schedule()

        equity = self.initial_equity
        trades: list[Trade] = []
        curve = np.empty(len(timeline), dtype="float64")

        liquidations = delisted = skipped_mark = skipped_cap = skipped_pool = 0
        pool_cursor = -1
        active_pool: dict[str, int] = {}

        for step, now in enumerate(timeline):
            # Advance the screen: the pool selected at or before `now` is in force.
            while pool_cursor + 1 < len(pool_times) and pool_times[pool_cursor + 1] <= now:
                pool_cursor += 1
                active_pool = pool_sets[pool_cursor]

            unrealized = 0.0
            open_count = sum(1 for f in feeds.values() if f.position is not None)

            for feed in feeds.values():
                if feed.exhausted or feed.times[feed.cursor] != now:
                    # No bar for this symbol at this timestamp. If its data has ended
                    # while a position is open, that position must be settled, not
                    # forgotten.
                    if feed.exhausted and feed.position is not None:
                        fields, net = self._force_close(feed, "delisted")
                        trades.append(Trade(**fields))
                        equity += net
                        # Clearing the position is what stops this from re-firing on
                        # every remaining timeline step and booking the same loss
                        # once per bar of every other symbol still trading.
                        feed.position = None
                        delisted += 1
                        open_count -= 1
                    elif feed.position is not None:
                        unrealized += feed.position.unrealized(
                            float(feed.frame["close"].iloc[feed.cursor - 1])
                        )
                    continue

                i = feed.cursor
                row = feed.frame.iloc[i]
                bar = _to_bar(row)
                in_pool = feed.symbol in active_pool
                pool_side = int(active_pool.get(feed.symbol, 0))

                # 1. Fill the previous bar's decision.
                if feed.pending is not None:
                    action = feed.pending.action
                    if action == "exit" and feed.position is not None:
                        fields, net = self.executor.close_position(
                            feed.position, bar.open, bar, i, "signal", symbol=feed.symbol
                        )
                        trades.append(Trade(**fields))
                        equity += net
                        feed.position = None
                        open_count -= 1
                    elif action in ("long", "short") and feed.position is None:
                        if not in_pool:
                            skipped_pool += 1
                        elif open_count >= self.max_positions:
                            skipped_cap += 1
                        elif not bar.tradeable:
                            skipped_mark += 1
                        elif feed.pending.stop_price is not None:
                            feed.position = self.executor.open_position(
                                side=1 if action == "long" else -1,
                                bar=bar,
                                index=i,
                                equity=equity,
                                stop_price=feed.pending.stop_price,
                                target_price=feed.pending.target_price,
                                tags=feed.pending.tags,
                            )
                            if feed.position is not None:
                                open_count += 1
                    feed.pending = None

                # 2. Funding.
                if feed.position is not None:
                    self.executor.settle_funding(feed.position, bar)

                # 3. Exits, including a forced close if the screen demands it.
                if feed.position is not None:
                    resolved = self.executor.resolve_exit(feed.position, bar)
                    if resolved is None and self.close_on_pool_exit and not in_pool:
                        resolved = (bar.close, "pool_exit")
                    if resolved is not None:
                        price, reason = resolved
                        fields, net = self.executor.close_position(
                            feed.position, price, bar, i, reason, symbol=feed.symbol
                        )
                        trades.append(Trade(**fields))
                        equity += net
                        if reason == "liquidation":
                            liquidations += 1
                        feed.position = None
                        open_count -= 1

                # 4. Strategy decides for this symbol's next bar.
                ctx = BarContext(
                    i=i,
                    bar=row,
                    position=feed.position,
                    equity=equity,
                    _frame=feed.frame,
                    pool_side=pool_side,
                )
                intent = feed.strategy.on_bar(ctx)
                if intent is not None:
                    feed.pending = intent

                if feed.position is not None:
                    unrealized += feed.position.unrealized(bar.close)
                feed.cursor += 1

            curve[step] = equity + unrealized

        # Settle anything still open at the end of the backtest.
        for feed in feeds.values():
            if feed.position is not None:
                fields, net = self._force_close(feed, "end_of_data")
                trades.append(Trade(**fields))
                equity += net
                feed.position = None

        # The final curve point was marked to market, which does not include the exit
        # costs those closes just paid. Restating it keeps the curve reconcilable with
        # the trade list instead of overstating the finish by the unpaid fees.
        if len(curve):
            curve[-1] = equity

        return PortfolioResult(
            equity_curve=pd.Series(curve, index=pd.DatetimeIndex(timeline), name="equity"),
            trades=trades,
            initial_equity=self.initial_equity,
            cost_model=self.costs,
            liquidations=liquidations,
            delisted_exits=delisted,
            skipped_untradeable=skipped_mark,
            skipped_at_capacity=skipped_cap,
            skipped_out_of_pool=skipped_pool,
            pool_history=self.pool,
        )

    def _force_close(self, feed: _Feed, reason: str) -> tuple[dict, float]:
        """Close at the symbol's last available bar."""
        i = len(feed.frame) - 1
        row = feed.frame.iloc[i]
        bar = _to_bar(row)
        return self.executor.close_position(
            feed.position, bar.close, bar, i, reason, symbol=feed.symbol
        )


def _to_bar(row: pd.Series) -> Bar:
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
