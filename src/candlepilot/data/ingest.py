"""Ingestion orchestration: decide what to fetch, fetch it, land it as parquet.

Period selection follows how the bucket actually publishes:

* A **monthly** archive appears only after the month closes, so closed months are
  fetched monthly (one request per symbol-month).
* The current month is covered by **daily** archives, and only up to the last
  closed UTC day — the running day has no archive yet.

Both are immutable once published, so an already-landed parquet is skipped and
re-runs are incremental.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from .schema import parse_funding, parse_klines, parse_mark_klines
from .store import ParquetStore
from .vision import INTERVAL_DATASETS, Archive, DownloadError, VisionClient

_PARSERS = {
    "klines": parse_klines,
    "markPriceKlines": parse_mark_klines,
    "fundingRate": parse_funding,
}

log = logging.getLogger("candlepilot.ingest")


@dataclass
class IngestReport:
    written: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"written={self.written} skipped={self.skipped} "
            f"missing={self.missing} failed={self.failed}"
        )


def month_range(start: str, end: str) -> list[str]:
    """Inclusive list of ``YYYY-MM`` strings."""
    periods = pd.period_range(start=start, end=end, freq="M")
    return [str(period) for period in periods]


def plan_archives(
    symbol: str,
    *,
    kind: str = "klines",
    interval: str | None = "1m",
    start: str,
    end: str,
    today: date | None = None,
) -> list[Archive]:
    """Enumerate the archives covering ``start``..``end`` for one symbol."""
    today = today or date.today()
    current_month = f"{today.year:04d}-{today.month:02d}"

    archives: list[Archive] = []
    for period in month_range(start, end):
        if period < current_month:
            archives.append(Archive(symbol, kind, period, interval))
            continue

        # Current (or future) month: daily archives up to the last closed UTC day.
        if period > current_month:
            continue
        last_closed = today - timedelta(days=1)
        day = date(today.year, today.month, 1)
        while day <= last_closed:
            archives.append(Archive(symbol, kind, day.isoformat(), interval))
            day += timedelta(days=1)
    return archives


def _ingest_one(
    archive: Archive,
    client: VisionClient,
    store: ParquetStore,
    report: IngestReport,
) -> None:
    if store.has(archive):
        report.skipped += 1
        return

    raw = client.fetch(archive)
    if raw is None:
        # Normal: the symbol was not listed during this period.
        report.missing += 1
        return

    frame = _PARSERS[archive.kind](raw)
    if frame.empty:
        report.missing += 1
        return

    store.write(archive, frame)
    report.written += 1


def ingest_symbols(
    symbols: list[str],
    *,
    start: str,
    end: str,
    interval: str = "1m",
    kinds: tuple[str, ...] = ("klines", "markPriceKlines", "fundingRate"),
    store: ParquetStore | None = None,
    client: VisionClient | None = None,
    workers: int = 8,
    today: date | None = None,
) -> IngestReport:
    """Ingest klines and/or funding rates for a list of symbols."""
    store = store or ParquetStore()
    client = client or VisionClient()
    report = IngestReport()

    archives: list[Archive] = []
    for symbol in symbols:
        for kind in kinds:
            archives.extend(
                plan_archives(
                    symbol,
                    kind=kind,
                    interval=interval if kind in INTERVAL_DATASETS else None,
                    start=start,
                    end=end,
                    today=today,
                )
            )

    log.info("planned %d archives across %d symbols", len(archives), len(symbols))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_ingest_one, archive, client, store, report): archive
            for archive in archives
        }
        for done in as_completed(futures):
            archive = futures[done]
            try:
                done.result()
            except DownloadError as error:
                report.failed += 1
                log.warning("%s: %s", archive.filename, error)
            except Exception as error:  # parsing/IO defects should not abort the run
                report.failed += 1
                log.warning("%s: unexpected failure: %s", archive.filename, error)

    return report


def build_universe(
    *,
    quote: str = "USDT",
    store: ParquetStore | None = None,
    client: VisionClient | None = None,
) -> pd.DataFrame:
    """Build the symbol universe from the bucket, flagging delisted symbols.

    The bucket lists every symbol that ever published data; exchangeInfo lists only
    those trading now. The difference is exactly the delisted set, which must stay
    in the universe or screening research inherits survivorship bias.
    """
    store = store or ParquetStore()
    client = client or VisionClient()

    archived = client.list_symbols(quote=quote)
    try:
        live = set(client.live_perpetuals(quote=quote))
    except DownloadError as error:
        log.warning("could not read exchangeInfo, marking liveness unknown: %s", error)
        live = None

    frame = pd.DataFrame({"symbol": archived})
    frame["is_live"] = (
        frame["symbol"].isin(live) if live is not None else pd.NA
    ).astype("boolean")
    frame["quote"] = quote
    store.write_universe(frame)
    return frame
