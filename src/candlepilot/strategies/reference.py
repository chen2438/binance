"""A reference strategy that exists to exercise the engine.

This is **not** a researched edge and is not proposed as one. It is the simplest
thing that produces entries, stops and exits on real data so the engine can be
verified end to end and so the cost sweep has something to chew on. Treat any
backtest number it produces as a test fixture, not a finding.
"""

from __future__ import annotations

from ..backtest.engine import BarContext, Intent


class DonchianBreakout:
    """Long the N-bar high, stop at the N-bar low, flat after `max_hold` bars."""

    def __init__(self, lookback: int = 60, max_hold: int = 240, stop_atr_mult: float = 1.0):
        self.lookback = lookback
        self.max_hold = max_hold
        self.stop_atr_mult = stop_atr_mult

    def on_bar(self, ctx: BarContext) -> Intent | None:
        if ctx.i < self.lookback:
            return None

        if ctx.position is not None:
            if ctx.i - ctx.position.entry_index >= self.max_hold:
                return Intent("exit")
            return None

        window = ctx.history.iloc[-self.lookback :]
        high = float(window["high"].iloc[:-1].max())
        low = float(window["low"].iloc[:-1].min())
        close = float(ctx.bar["close"])

        if close > high and close > low:
            stop = close - (close - low) * self.stop_atr_mult
            if stop < close:
                return Intent("long", stop_price=stop)
        return None
