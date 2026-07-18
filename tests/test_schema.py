"""Parsing tests covering the archive-format quirks seen in the real bucket."""

from __future__ import annotations

import pytest

from candlepilot.data.schema import SchemaError, parse_funding, parse_klines

HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore\n"
)
ROW_A = "1717200000000,67577.90,67620.50,67572.00,67608.70,129.521,1717200059999,8754783.34,1732,94.507,6387683.53,0\n"
ROW_B = "1717200060000,67608.70,67650.00,67600.00,67640.10,88.100,1717200119999,5957000.00,900,40.000,2705000.00,0\n"


def test_parses_archive_without_header() -> None:
    """2020-era archives ship no header row; the first line is data."""
    frame = parse_klines((ROW_A + ROW_B).encode())
    assert len(frame) == 2
    assert frame["open_time"].tolist() == [1717200000000, 1717200060000]
    assert frame["close"].iloc[0] == pytest.approx(67608.70)


def test_parses_archive_with_header() -> None:
    """2024-era archives ship a header row, which must not become a data row."""
    frame = parse_klines((HEADER + ROW_A + ROW_B).encode())
    assert len(frame) == 2
    assert frame["open_time"].dtype == "int64"
    assert frame["close"].iloc[0] == pytest.approx(67608.70)


def test_header_and_headerless_agree() -> None:
    with_header = parse_klines((HEADER + ROW_A + ROW_B).encode())
    without = parse_klines((ROW_A + ROW_B).encode())
    assert with_header.equals(without)


def test_drops_redundant_columns() -> None:
    frame = parse_klines((HEADER + ROW_A).encode())
    assert "ignore" not in frame.columns
    assert "close_time" not in frame.columns


def test_deduplicates_and_sorts_by_open_time() -> None:
    frame = parse_klines((HEADER + ROW_B + ROW_A + ROW_B).encode())
    assert frame["open_time"].tolist() == [1717200000000, 1717200060000]
    assert frame["open_time"].is_monotonic_increasing


def test_rejects_unexpected_timestamp_unit() -> None:
    """A silent ms->us change upstream must fail loudly, not shift every bar."""
    microseconds = ROW_A.replace("1717200000000", "1717200000000000", 1)
    with pytest.raises(SchemaError, match="epoch-millisecond"):
        parse_klines((HEADER + microseconds).encode())


def test_parses_funding_rate() -> None:
    raw = b"calc_time,funding_interval_hours,last_funding_rate\n1717200000000,8,0.00010000\n"
    frame = parse_funding(raw)
    assert frame["funding_interval_hours"].iloc[0] == 8
    assert frame["last_funding_rate"].iloc[0] == pytest.approx(0.0001)


def test_funding_rejects_unexpected_timestamp_unit() -> None:
    raw = b"calc_time,funding_interval_hours,last_funding_rate\n17172000000000000,8,0.0001\n"
    with pytest.raises(SchemaError, match="epoch-millisecond"):
        parse_funding(raw)


def test_mark_klines_keep_only_ohlc() -> None:
    """Mark-price archives zero out every volume field; keeping them invites misuse."""
    from candlepilot.data.schema import parse_mark_klines

    row = "1717200000000,67570.93,67613.44,67570.93,67602.74,0,1717200059999,0,60,0,0,0\n"
    frame = parse_mark_klines((HEADER + row).encode())
    assert list(frame.columns) == ["open_time", "open", "high", "low", "close"]
    assert frame["high"].iloc[0] == pytest.approx(67613.44)
