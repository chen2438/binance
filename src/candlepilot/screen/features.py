"""Point-in-time features for screening.

Every feature here is computed with a rolling window and then **shifted by one day**,
so the value carried on date ``T`` is derived only from data through ``T-1``. Without
that shift a rule that ranks on "today's volume" is really ranking on information that
only existed after the day closed, and the resulting backtest cannot be reproduced.

The shift happens once, centrally, in ``compute_features`` — not in each feature — so
a new feature cannot forget it.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

# Trading days per year, for annualising daily realized volatility. Perps trade
# continuously, so there are no market holidays to net out.
DAYS_PER_YEAR = 365


def _liquidity(group: pd.DataFrame, window: int) -> pd.Series:
    """Median daily turnover: whether a position can be entered at all."""
    return group["quote_volume"].rolling(window, min_periods=window).median()


def _realized_vol(group: pd.DataFrame, window: int) -> pd.Series:
    returns = np.log(group["close"]).diff()
    return returns.rolling(window, min_periods=window).std() * np.sqrt(DAYS_PER_YEAR)


def _momentum(group: pd.DataFrame, window: int) -> pd.Series:
    return group["close"] / group["close"].shift(window) - 1


def _funding_carry(group: pd.DataFrame, window: int) -> pd.Series:
    """Mean daily funding. Persistent funding is a real cash flow, not a footnote."""
    return group["funding_rate"].rolling(window, min_periods=window).mean()


def _range_ratio(group: pd.DataFrame, window: int) -> pd.Series:
    """Mean daily high-low range over close: how much intraday room a strategy has."""
    daily_range = (group["high"] - group["low"]) / group["close"]
    return daily_range.rolling(window, min_periods=window).mean()


def _dollar_range(group: pd.DataFrame, window: int) -> pd.Series:
    """Turnover x range, a crude proxy for tradeable intraday opportunity."""
    product = group["quote_volume"] * (group["high"] - group["low"]) / group["close"]
    return product.rolling(window, min_periods=window).median()


FEATURES: dict[str, Callable[[pd.DataFrame, int], pd.Series]] = {
    "liquidity": _liquidity,
    "realized_vol": _realized_vol,
    "momentum": _momentum,
    "funding_carry": _funding_carry,
    "range_ratio": _range_ratio,
    "dollar_range": _dollar_range,
}


def compute_features(
    panel: pd.DataFrame,
    *,
    window: int = 30,
    features: dict[str, Callable] | None = None,
    min_history: int = 30,
) -> pd.DataFrame:
    """Compute point-in-time features for every (date, symbol) in the panel.

    Returns a frame indexed like ``panel`` with one column per feature, plus
    ``age_days`` and ``eligible``. All values on date ``T`` derive from data
    through ``T-1``.
    """
    features = features or FEATURES
    panel = panel.sort_index()

    # Computed per symbol rather than via groupby.apply: each symbol's series is
    # shifted in isolation, which is what stops one symbol inheriting another's
    # last row at the seam between them.
    parts = []
    for symbol, group in panel.groupby(level="symbol", sort=False):
        series = group.droplevel("symbol").sort_index()
        frame = pd.DataFrame(index=series.index)
        for name, function in features.items():
            frame[name] = function(series, window)

        # Bars observed so far: how long the symbol has actually been listed.
        frame["age_days"] = np.arange(1, len(series) + 1, dtype="float64")

        # The single shift that makes every feature point-in-time.
        frame = frame.shift(1)
        frame["symbol"] = symbol
        parts.append(frame.reset_index())

    result = pd.concat(parts, ignore_index=True)
    result = result.set_index(["date", "symbol"]).sort_index()

    feature_columns = [c for c in result.columns if c != "age_days"]
    result["eligible"] = result[feature_columns].notna().all(axis=1) & (
        result["age_days"] >= min_history
    )
    return result
