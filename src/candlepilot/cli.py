"""Command line entry point for CandlePilot data ingestion."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from .data.ingest import build_universe, ingest_symbols
from .data.store import DEFAULT_ROOT, ParquetStore


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="store root")
    parser.add_argument("-v", "--verbose", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="candlepilot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    universe = sub.add_parser("universe", help="refresh the symbol universe")
    universe.add_argument("--quote", default="USDT")
    _add_common(universe)

    ingest = sub.add_parser("ingest", help="download and land historical archives")
    group = ingest.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbols", nargs="+", help="explicit symbol list")
    group.add_argument("--all", action="store_true", help="every symbol in the universe")
    ingest.add_argument("--start", required=True, help="first month, YYYY-MM")
    ingest.add_argument("--end", required=True, help="last month, YYYY-MM")
    ingest.add_argument("--interval", default="1m")
    ingest.add_argument(
        "--kinds",
        nargs="+",
        default=["klines", "markPriceKlines", "fundingRate"],
        choices=["klines", "markPriceKlines", "fundingRate"],
    )
    ingest.add_argument("--workers", type=int, default=8)
    ingest.add_argument("--live-only", action="store_true", help="skip delisted symbols")
    _add_common(ingest)

    status = sub.add_parser("status", help="show ingested coverage")
    status.add_argument("--interval", default="1m")
    _add_common(status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    store = ParquetStore(args.root)

    if args.command == "universe":
        frame = build_universe(quote=args.quote, store=store)
        live = int(frame["is_live"].fillna(False).sum())
        print(f"{len(frame)} archived symbols, {live} live, {len(frame) - live} delisted")
        print(f"wrote {store.root / 'universe.parquet'}")
        return 0

    if args.command == "ingest":
        if args.all:
            universe = store.load_universe()
            if universe.empty:
                print("universe is empty; run `candlepilot universe` first", file=sys.stderr)
                return 1
            if args.live_only:
                universe = universe[universe["is_live"].fillna(False)]
            symbols = universe["symbol"].tolist()
        else:
            symbols = args.symbols

        report = ingest_symbols(
            symbols,
            start=args.start,
            end=args.end,
            interval=args.interval,
            kinds=tuple(args.kinds),
            store=store,
            workers=args.workers,
        )
        print(report)
        return 1 if report.failed else 0

    if args.command == "status":
        summary = store.summary(args.interval)
        if summary.empty:
            print("nothing ingested yet")
            return 0
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(summary.to_string(index=False))
        total_mb = summary["bytes"].sum() / 1e6
        print(f"\n{len(summary)} symbols, {summary['files'].sum()} files, {total_mb:.1f} MB")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
