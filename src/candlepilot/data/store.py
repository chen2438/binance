"""Parquet store for ingested archives.

Layout mirrors the upstream bucket so an ingested period maps to exactly one file,
which is what makes re-runs incremental:

    <root>/klines/<interval>/<SYMBOL>/<SYMBOL>-<interval>-<period>.parquet
    <root>/fundingRate/<SYMBOL>/<SYMBOL>-fundingRate-<period>.parquet
    <root>/universe.parquet
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .vision import Archive

DEFAULT_ROOT = Path("data")


class ParquetStore:
    def __init__(self, root: Path | str = DEFAULT_ROOT):
        self.root = Path(root)

    def path_for(self, archive: Archive) -> Path:
        return self.root / archive.relative_path.with_suffix(".parquet")

    def has(self, archive: Archive) -> bool:
        return self.path_for(archive).exists()

    def write(self, archive: Archive, frame: pd.DataFrame) -> Path:
        path = self.path_for(archive)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file first so an interrupted run never leaves a
        # truncated parquet that a later run would treat as already ingested.
        staging = path.with_suffix(".parquet.tmp")
        frame.to_parquet(staging, index=False, compression="zstd")
        staging.replace(path)
        return path

    # ------------------------------------------------------------------ reading

    def load_klines(
        self,
        symbol: str,
        interval: str = "1m",
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        """Load a symbol's klines as a UTC-indexed DataFrame."""
        directory = self.root / "klines" / interval / symbol
        files = sorted(directory.glob(f"{symbol}-{interval}-*.parquet"))
        if not files:
            return pd.DataFrame()

        frame = pd.concat((pd.read_parquet(path) for path in files), ignore_index=True)
        frame = frame.drop_duplicates(subset="open_time", keep="last")
        frame = frame.sort_values("open_time", ignore_index=True)
        frame.index = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
        frame.index.name = "open_time_utc"
        return frame.loc[start:end]

    def load_funding(
        self,
        symbol: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        directory = self.root / "fundingRate" / symbol
        files = sorted(directory.glob(f"{symbol}-fundingRate-*.parquet"))
        if not files:
            return pd.DataFrame()

        frame = pd.concat((pd.read_parquet(path) for path in files), ignore_index=True)
        frame = frame.drop_duplicates(subset="calc_time", keep="last")
        frame = frame.sort_values("calc_time", ignore_index=True)
        frame.index = pd.to_datetime(frame["calc_time"], unit="ms", utc=True)
        frame.index.name = "calc_time_utc"
        return frame.loc[start:end]

    # ------------------------------------------------------------------ universe

    def write_universe(self, frame: pd.DataFrame) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "universe.parquet"
        frame.to_parquet(path, index=False)
        return path

    def load_universe(self) -> pd.DataFrame:
        path = self.root / "universe.parquet"
        return pd.read_parquet(path) if path.exists() else pd.DataFrame()

    # -------------------------------------------------------------------- status

    def summary(self, interval: str = "1m") -> pd.DataFrame:
        """Per-symbol ingested coverage, for `candlepilot data status`."""
        directory = self.root / "klines" / interval
        if not directory.exists():
            return pd.DataFrame()

        rows = []
        for symbol_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
            files = sorted(symbol_dir.glob("*.parquet"))
            if not files:
                continue
            periods = [path.stem.rsplit("-", 1)[-1] for path in files]
            monthly = [p for p in periods if len(p) == 7]
            rows.append(
                {
                    "symbol": symbol_dir.name,
                    "files": len(files),
                    "first_period": min(periods),
                    "last_period": max(periods),
                    "months": len(monthly),
                    "bytes": sum(path.stat().st_size for path in files),
                }
            )
        return pd.DataFrame(rows)
