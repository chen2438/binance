"""Assemble the aligned bar frame the engine consumes.

The three series do not line up on their own:

* Mark-price archives have gaps that the trade-price archives do not (BTCUSDT is
  missing 29 bars on 2020-01-19). A plain join silently yields NaN mark prices, and
  a NaN comparison is False — liquidation checks would quietly stop firing.
* Funding is a sparse event series (every 8h historically, 4h or 1h for some
  symbols), not a per-bar series.

Alignment is therefore explicit: the trade-price index is authoritative, mark price
is forward-filled within a bounded staleness, and bars whose mark price cannot be
established are marked untradeable rather than silently trusted.
"""

from __future__ import annotations

import pandas as pd

from ..data.store import ParquetStore

# How long a forward-filled mark price stays usable. Mark price tracks a spot index
# and moves continuously, so a short carry-over is fair; an hour-long one is not.
MAX_MARK_STALENESS = pd.Timedelta(minutes=15)


class DatasetError(ValueError):
    """Raised when the assembled dataset is unusable."""


def build_bars(
    store: ParquetStore,
    symbol: str,
    *,
    interval: str = "1m",
    start: str | None = None,
    end: str | None = None,
    max_mark_staleness: pd.Timedelta = MAX_MARK_STALENESS,
) -> pd.DataFrame:
    """Return trade prices, mark prices and per-bar funding on one index.

    Columns: ``open/high/low/close/volume/quote_volume`` (trade price),
    ``mark_open/mark_high/mark_low/mark_close``, ``funding_rate`` (0 outside
    settlement bars) and ``tradeable``.
    """
    klines = store.load_klines(symbol, interval, start=start, end=end)
    if klines.empty:
        raise DatasetError(f"no klines ingested for {symbol} {interval}")

    mark = store.load_mark_klines(symbol, interval, start=start, end=end)
    if mark.empty:
        raise DatasetError(
            f"no mark-price klines for {symbol}; liquidation cannot be evaluated. "
            f"Run: candlepilot ingest --symbols {symbol} --kinds markPriceKlines ..."
        )

    frame = klines.copy()

    mark = mark.rename(columns={c: f"mark_{c}" for c in ("open", "high", "low", "close")})
    mark_cols = ["mark_open", "mark_high", "mark_low", "mark_close"]
    aligned = mark[mark_cols].reindex(frame.index)

    # Bounded forward fill: carry the last known mark price across a short gap, but
    # never let a stale one authorise a liquidation check it cannot support.
    fresh = aligned["mark_close"].notna()
    last_seen = pd.Series(frame.index.where(fresh), index=frame.index).ffill()
    staleness = frame.index - pd.to_datetime(last_seen, utc=True)
    aligned = aligned.ffill()

    usable = aligned["mark_close"].notna() & (staleness <= max_mark_staleness)
    frame[mark_cols] = aligned
    frame["tradeable"] = usable.to_numpy()

    frame["funding_rate"] = _funding_per_bar(store, symbol, frame.index)
    return frame


def _funding_per_bar(store: ParquetStore, symbol: str, index: pd.DatetimeIndex) -> pd.Series:
    """Place each funding settlement on the bar that contains it."""
    funding = store.load_funding(symbol)
    series = pd.Series(0.0, index=index)
    if funding.empty:
        return series

    # searchsorted maps a settlement to the bar it falls inside, which is where the
    # cash flow actually hits an open position.
    positions = index.searchsorted(funding.index, side="right") - 1
    valid = positions >= 0
    if not valid.any():
        return series

    contributions = pd.Series(
        funding["last_funding_rate"].to_numpy()[valid],
        index=index[positions[valid]],
    )
    # A bar can contain more than one settlement only in degenerate data; sum to be safe.
    series = series.add(contributions.groupby(level=0).sum(), fill_value=0.0)
    return series.reindex(index).fillna(0.0)


def liquidity_profile(frame: pd.DataFrame) -> float:
    """Median per-bar quote volume, used to pick a slippage tier."""
    return float(frame["quote_volume"].median())
