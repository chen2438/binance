"""Build the multi-symbol daily panel that screening runs on.

Screening runs on **daily** bars while execution runs on 1m. That split is not a
shortcut: a daily archive is ~865x smaller than the 1m archive for the same month,
which is the difference between screening the whole 787-symbol universe and screening
whichever handful happened to be convenient. Convenience-picked universes are how
survivorship bias gets back in after being designed out.

The panel is long-form (``symbol``, ``date``, columns) because symbols exist over
different date ranges — a wide frame would fabricate rows for symbols that had not
listed yet, and those NaNs are exactly what a careless rank turns into signal.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..data.store import ParquetStore

log = logging.getLogger("candlepilot.screen.panel")

PANEL_COLUMNS = [
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "count",
    "funding_rate",
]


def build_panel(
    store: ParquetStore,
    symbols: list[str] | None = None,
    *,
    interval: str = "1d",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Assemble a long-form daily panel indexed by (date, symbol).

    Symbols with no ingested data are skipped with a warning rather than silently
    dropped, because a missing symbol is indistinguishable from a delisted one in
    the results and only one of those is legitimate.
    """
    if symbols is None:
        universe = store.load_universe()
        if universe.empty:
            raise ValueError("no universe; run `candlepilot universe` first")
        symbols = universe["symbol"].tolist()

    frames = []
    missing = []
    for symbol in symbols:
        bars = store.load_klines(symbol, interval, start=start, end=end)
        if bars.empty:
            missing.append(symbol)
            continue

        bars = bars.copy()
        bars["symbol"] = symbol
        bars["funding_rate"] = _daily_funding(store, symbol, bars.index)
        frames.append(bars.reset_index().rename(columns={"open_time_utc": "date"}))

    if missing:
        log.warning("no ingested data for %d symbols, e.g. %s", len(missing), missing[:5])
    if not frames:
        raise ValueError("panel is empty; ingest daily klines first")

    panel = pd.concat(frames, ignore_index=True)
    panel = panel.loc[:, ["date"] + PANEL_COLUMNS]
    panel = panel.sort_values(["date", "symbol"], ignore_index=True)
    return panel.set_index(["date", "symbol"])


def _daily_funding(store: ParquetStore, symbol: str, index: pd.DatetimeIndex) -> pd.Series:
    """Sum a day's funding settlements onto that day's bar."""
    funding = store.load_funding(symbol)
    if funding.empty:
        return pd.Series(0.0, index=index)

    daily = funding["last_funding_rate"].resample("1D").sum()
    return daily.reindex(index).fillna(0.0)
