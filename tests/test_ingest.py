"""Tests for archive planning — the monthly/daily boundary is the tricky part."""

from __future__ import annotations

from datetime import date

from candlepilot.data.ingest import month_range, plan_archives
from candlepilot.data.vision import Archive


def test_month_range_is_inclusive() -> None:
    assert month_range("2024-11", "2025-02") == ["2024-11", "2024-12", "2025-01", "2025-02"]


def test_closed_months_use_monthly_archives() -> None:
    archives = plan_archives(
        "BTCUSDT", start="2024-01", end="2024-03", today=date(2026, 7, 18)
    )
    assert [a.period for a in archives] == ["2024-01", "2024-02", "2024-03"]
    assert all(a.granularity == "monthly" for a in archives)


def test_current_month_uses_daily_archives_up_to_last_closed_day() -> None:
    """The running day has no archive yet, so it must not be planned."""
    archives = plan_archives(
        "BTCUSDT", start="2026-07", end="2026-07", today=date(2026, 7, 18)
    )
    assert all(a.granularity == "daily" for a in archives)
    assert [a.period for a in archives] == [
        f"2026-07-{day:02d}" for day in range(1, 18)
    ]


def test_first_of_month_plans_nothing_for_current_month() -> None:
    archives = plan_archives(
        "BTCUSDT", start="2026-07", end="2026-07", today=date(2026, 7, 1)
    )
    assert archives == []


def test_future_months_are_skipped() -> None:
    archives = plan_archives(
        "BTCUSDT", start="2026-08", end="2026-12", today=date(2026, 7, 18)
    )
    assert archives == []


def test_mixed_range_spans_monthly_then_daily() -> None:
    archives = plan_archives(
        "BTCUSDT", start="2026-05", end="2026-07", today=date(2026, 7, 18)
    )
    monthly = [a for a in archives if a.granularity == "monthly"]
    daily = [a for a in archives if a.granularity == "daily"]
    assert [a.period for a in monthly] == ["2026-05", "2026-06"]
    assert len(daily) == 17


def test_archive_url_and_path_for_klines() -> None:
    archive = Archive("BTCUSDT", "klines", "2024-06", "1m")
    assert archive.url == (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        "BTCUSDT/1m/BTCUSDT-1m-2024-06.zip"
    )
    assert archive.relative_path.as_posix() == "klines/1m/BTCUSDT/BTCUSDT-1m-2024-06.zip"


def test_archive_url_for_daily_funding() -> None:
    archive = Archive("BTCUSDT", "fundingRate", "2026-07-05")
    assert archive.granularity == "daily"
    assert archive.url == (
        "https://data.binance.vision/data/futures/um/daily/fundingRate/"
        "BTCUSDT/BTCUSDT-fundingRate-2026-07-05.zip"
    )


def test_mark_price_archive_url_carries_interval() -> None:
    archive = Archive("BTCUSDT", "markPriceKlines", "2024-06", "1m")
    assert archive.is_interval_dataset
    assert archive.url == (
        "https://data.binance.vision/data/futures/um/monthly/markPriceKlines/"
        "BTCUSDT/1m/BTCUSDT-1m-2024-06.zip"
    )
    assert archive.relative_path.as_posix() == (
        "markPriceKlines/1m/BTCUSDT/BTCUSDT-1m-2024-06.zip"
    )


def test_funding_is_not_an_interval_dataset() -> None:
    archive = Archive("BTCUSDT", "fundingRate", "2024-06")
    assert not archive.is_interval_dataset
    assert archive.filename == "BTCUSDT-fundingRate-2024-06.zip"
