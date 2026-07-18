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

    screen = sub.add_parser("screen", help="run a point-in-time symbol screen")
    screen.add_argument("--symbols", nargs="+", help="restrict to these symbols")
    screen.add_argument("--rank-by", default="dollar_range", help="feature to rank on")
    screen.add_argument("--ascending", action="store_true")
    screen.add_argument("--top", type=int, default=20)
    screen.add_argument("--window", type=int, default=30, help="feature lookback in days")
    screen.add_argument("--min-history", type=int, default=30)
    screen.add_argument("--min-liquidity", type=float, default=1e6, help="median daily USDT")
    screen.add_argument("--rebalance", default="W-MON")
    screen.add_argument("--start")
    screen.add_argument("--end")
    _add_common(screen)

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
        print(f"{len(frame)} archived symbols")
        if "status" in frame.columns and frame["status"].notna().any():
            for status, count in frame["status"].value_counts().items():
                print(f"  {status:<10} {count}")
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

    if args.command == "screen":
        from .screen import Screener, build_panel, compute_features, top_n
        from .screen.screener import turnover

        panel = build_panel(
            store, args.symbols, interval="1d", start=args.start, end=args.end
        )
        features = compute_features(
            panel, window=args.window, min_history=args.min_history
        )
        rule = top_n(
            args.rank_by,
            n=args.top,
            ascending=args.ascending,
            filters={"liquidity": (">=", args.min_liquidity)},
        )
        screener = Screener(rule, rebalance=args.rebalance)
        selections = screener.run(features)

        symbols_seen = panel.index.get_level_values("symbol").nunique()
        print(f"panel: {symbols_seen} symbols, {len(panel):,} symbol-days")
        print(f"rebalances: {len(selections)} ({args.rebalance})")
        churn = turnover(selections)
        if len(churn) > 1:
            print(f"median turnover per rebalance: {churn.iloc[1:].median():.1%}")

        latest = selections[-1] if selections else None
        if latest:
            date = latest.date.date()
            print(f"\nlatest pool ({date}, {latest.candidates} eligible candidates):")
            for rank, symbol in enumerate(latest.symbols, 1):
                print(f"  {rank:2d}. {symbol}")
        return 0

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
