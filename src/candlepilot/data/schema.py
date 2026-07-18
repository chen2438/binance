"""Column layouts and parsing for Binance Vision CSV archives.

Two archive quirks are handled here, both observed in the real bucket:

* Older archives (e.g. ``BTCUSDT-1m-2020-01``) have **no header row**, while newer
  ones (2024 onward) do. The header is detected rather than assumed.
* Timestamps are milliseconds since epoch. Binance has changed this unit before on
  other datasets, so the parsed range is asserted instead of trusted.
"""

from __future__ import annotations

import io

import pandas as pd

# Epoch-millisecond bounds used to catch a silent unit change (e.g. microseconds).
# 2015-01-01 .. 2100-01-01, wide enough to never reject legitimate data.
_MIN_EPOCH_MS = 1_420_070_400_000
_MAX_EPOCH_MS = 4_102_444_800_000

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]

KLINE_DTYPES = {
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "quote_volume": "float64",
    "count": "int64",
    "taker_buy_volume": "float64",
    "taker_buy_quote_volume": "float64",
}

# Columns kept in the parquet store; `ignore` and `close_time` are redundant.
KLINE_OUTPUT_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
]

FUNDING_COLUMNS = ["calc_time", "funding_interval_hours", "last_funding_rate"]

FUNDING_DTYPES = {
    "funding_interval_hours": "int64",
    "last_funding_rate": "float64",
}


class SchemaError(ValueError):
    """Raised when an archive does not match the expected layout."""


def _has_header(raw: bytes, first_column: str) -> bool:
    head = raw[: len(first_column) + 2].decode("utf-8", errors="replace")
    return head.lstrip().lower().startswith(first_column)


def _check_epoch_ms(series: pd.Series, label: str) -> None:
    if series.empty:
        return
    lo, hi = int(series.min()), int(series.max())
    if lo < _MIN_EPOCH_MS or hi > _MAX_EPOCH_MS:
        raise SchemaError(
            f"{label} outside the expected epoch-millisecond range "
            f"(got {lo}..{hi}); the archive unit may have changed"
        )


def parse_klines(raw: bytes) -> pd.DataFrame:
    """Parse a raw kline CSV payload into a normalized DataFrame."""
    frame = pd.read_csv(
        io.BytesIO(raw),
        header=0 if _has_header(raw, "open_time") else None,
        names=KLINE_COLUMNS,
        dtype=KLINE_DTYPES,
    )
    if frame.empty:
        return frame.reindex(columns=KLINE_OUTPUT_COLUMNS)

    frame["open_time"] = frame["open_time"].astype("int64")
    _check_epoch_ms(frame["open_time"], "kline open_time")

    frame = frame.loc[:, KLINE_OUTPUT_COLUMNS]
    frame = frame.drop_duplicates(subset="open_time", keep="last")
    return frame.sort_values("open_time", ignore_index=True)


def parse_funding(raw: bytes) -> pd.DataFrame:
    """Parse a raw funding-rate CSV payload into a normalized DataFrame."""
    frame = pd.read_csv(
        io.BytesIO(raw),
        header=0 if _has_header(raw, "calc_time") else None,
        names=FUNDING_COLUMNS,
        dtype=FUNDING_DTYPES,
    )
    if frame.empty:
        return frame.reindex(columns=FUNDING_COLUMNS)

    frame["calc_time"] = frame["calc_time"].astype("int64")
    _check_epoch_ms(frame["calc_time"], "funding calc_time")

    frame = frame.drop_duplicates(subset="calc_time", keep="last")
    return frame.sort_values("calc_time", ignore_index=True)
