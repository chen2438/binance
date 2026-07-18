"""Three classical hypotheses, at fixed conventional parameters.

These exist to answer one question before any rule search begins: *does this market
show any of the standard effects at this horizon, before optimisation?* Every
parameter below is a textbook default chosen in advance — 20-day momentum, 5-day
reversal, weekly funding carry, 2xATR stops. Nothing here is tuned, and nothing
should be tuned in place: the value of a baseline is that it was not fitted, so a
single fixed parameterisation keeps the multiple-testing correction meaningful.

If a hypothesis cannot clear costs at conventional settings, that is worth knowing
before spending a search budget on it. A baseline that fails is a direction ruled
out, which is the cheapest useful result in strategy research.
"""

from __future__ import annotations

import numpy as np

from ..backtest.engine import BarContext, Intent


def _atr(ctx: BarContext, window: int) -> float:
    """Average true range over the last ``window`` bars, ATR-style."""
    history = ctx.history
    if len(history) < window + 1:
        return float("nan")
    recent = history.iloc[-(window + 1) :]
    high = recent["high"].to_numpy()
    low = recent["low"].to_numpy()
    close = recent["close"].to_numpy()
    true_range = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])),
    )
    return float(true_range.mean())


class MomentumContinuation:
    """Time-series momentum: follow the sign of the trailing return.

    Convention: 20-bar lookback, 5-bar hold, 2xATR(14) stop. Long-only and
    short-enabled variants both trade, since a perp has no borrow constraint.
    """

    name = "momentum"

    def __init__(
        self,
        lookback: int = 20,
        hold: int = 5,
        atr_window: int = 14,
        stop_atr: float = 2.0,
        allow_short: bool = True,
    ):
        self.lookback = lookback
        self.hold = hold
        self.atr_window = atr_window
        self.stop_atr = stop_atr
        self.allow_short = allow_short

    def on_bar(self, ctx: BarContext) -> Intent | None:
        if ctx.position is not None:
            if ctx.i - ctx.position.entry_index >= self.hold:
                return Intent("exit")
            return None

        if ctx.i < max(self.lookback, self.atr_window) + 1:
            return None

        closes = ctx.history["close"].to_numpy()
        trailing = closes[-1] / closes[-1 - self.lookback] - 1
        atr = _atr(ctx, self.atr_window)
        if not np.isfinite(atr) or atr <= 0:
            return None

        price = float(closes[-1])
        if trailing > 0:
            return Intent("long", stop_price=price - self.stop_atr * atr)
        if trailing < 0 and self.allow_short:
            return Intent("short", stop_price=price + self.stop_atr * atr)
        return None


class MeanReversion:
    """Short-horizon reversal: fade the trailing move.

    Convention: 5-bar lookback, 5-bar hold, 2xATR(14) stop. Deliberately the mirror
    of the momentum baseline so the two are directly comparable on the same data.
    """

    name = "reversion"

    def __init__(
        self,
        lookback: int = 5,
        hold: int = 5,
        atr_window: int = 14,
        stop_atr: float = 2.0,
        allow_short: bool = True,
    ):
        self.lookback = lookback
        self.hold = hold
        self.atr_window = atr_window
        self.stop_atr = stop_atr
        self.allow_short = allow_short

    def on_bar(self, ctx: BarContext) -> Intent | None:
        if ctx.position is not None:
            if ctx.i - ctx.position.entry_index >= self.hold:
                return Intent("exit")
            return None

        if ctx.i < max(self.lookback, self.atr_window) + 1:
            return None

        closes = ctx.history["close"].to_numpy()
        trailing = closes[-1] / closes[-1 - self.lookback] - 1
        atr = _atr(ctx, self.atr_window)
        if not np.isfinite(atr) or atr <= 0:
            return None

        price = float(closes[-1])
        if trailing < 0:
            return Intent("long", stop_price=price - self.stop_atr * atr)
        if trailing > 0 and self.allow_short:
            return Intent("short", stop_price=price + self.stop_atr * atr)
        return None


class FundingCarry:
    """Take the other side of persistent funding.

    Positive funding means longs pay shorts, so the carry trade is short. This is a
    genuine cash flow rather than a price prediction, which makes it the one baseline
    whose edge does not require any directional forecast — and also the one most
    exposed to the price risk taken on to collect it.

    Convention: 7-bar mean funding, 7-bar hold, 3xATR(14) stop (wider, because the
    position is held for carry rather than direction).
    """

    name = "funding_carry"

    def __init__(
        self,
        lookback: int = 7,
        hold: int = 7,
        atr_window: int = 14,
        stop_atr: float = 3.0,
        threshold: float = 0.0,
    ):
        self.lookback = lookback
        self.hold = hold
        self.atr_window = atr_window
        self.stop_atr = stop_atr
        self.threshold = threshold

    def on_bar(self, ctx: BarContext) -> Intent | None:
        if ctx.position is not None:
            if ctx.i - ctx.position.entry_index >= self.hold:
                return Intent("exit")
            return None

        if ctx.i < max(self.lookback, self.atr_window) + 1:
            return None

        funding = ctx.history["funding_rate"].to_numpy()[-self.lookback :]
        mean_funding = float(funding.mean())
        atr = _atr(ctx, self.atr_window)
        if not np.isfinite(atr) or atr <= 0:
            return None

        price = float(ctx.history["close"].to_numpy()[-1])
        if mean_funding > self.threshold:
            # Longs are paying; collect it by being short.
            return Intent("short", stop_price=price + self.stop_atr * atr)
        if mean_funding < -self.threshold:
            return Intent("long", stop_price=price - self.stop_atr * atr)
        return None


BASELINES = {
    "momentum": MomentumContinuation,
    "reversion": MeanReversion,
    "funding_carry": FundingCarry,
}
