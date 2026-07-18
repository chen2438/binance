"""Client for the public data.binance.vision archive bucket.

Only public historical data is fetched; no API key, no account access, no order
placement. The bucket is also listed directly (S3 XML) rather than read through
``fapi/exchangeInfo``, because exchangeInfo returns **currently listed** symbols
only. Screening research built on that would carry survivorship bias, so the
universe is taken from every symbol that ever published data.
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import requests

BUCKET_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
DATA_URL = "https://data.binance.vision"
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

# Dated delivery futures carry an underscore suffix (e.g. BTCUSDT_240628).
# Perpetuals never do, so this is the discriminator between the two.
_PERPETUAL = re.compile(r"^[A-Z0-9]+$")

USER_AGENT = "candlepilot/0.1 (backtest research; public archive only)"

# Connection pool size when none is given. Kept above the default worker count so a
# plain VisionClient() is not the bottleneck.
DEFAULT_POOL_SIZE = 16


class DownloadError(RuntimeError):
    """Raised when an archive cannot be fetched or fails verification."""


# Datasets keyed by a bar interval; their paths carry an extra interval segment and
# their filenames use the interval rather than the dataset name.
INTERVAL_DATASETS = frozenset(
    {"klines", "markPriceKlines", "indexPriceKlines", "premiumIndexKlines"}
)


@dataclass(frozen=True)
class Archive:
    """One downloadable monthly or daily archive."""

    symbol: str
    kind: str  # e.g. "klines", "markPriceKlines", "fundingRate"
    period: str  # "YYYY-MM" for monthly, "YYYY-MM-DD" for daily
    interval: str | None = None  # bar interval; None for non-interval datasets

    @property
    def granularity(self) -> str:
        return "daily" if len(self.period) == 10 else "monthly"

    @property
    def is_interval_dataset(self) -> bool:
        return self.kind in INTERVAL_DATASETS

    @property
    def filename(self) -> str:
        if self.is_interval_dataset:
            return f"{self.symbol}-{self.interval}-{self.period}.zip"
        return f"{self.symbol}-{self.kind}-{self.period}.zip"

    @property
    def url(self) -> str:
        parts = ["data", "futures", "um", self.granularity, self.kind, self.symbol]
        if self.is_interval_dataset:
            parts.append(self.interval or "")
        return f"{DATA_URL}/{'/'.join(parts)}/{self.filename}"

    @property
    def relative_path(self) -> Path:
        parts = [self.kind, self.symbol]
        if self.is_interval_dataset:
            parts.insert(1, self.interval or "")
        return Path(*parts) / self.filename


class VisionClient:
    """Thin HTTP client with retries over the public archive bucket."""

    def __init__(
        self,
        *,
        retries: int = 4,
        backoff: float = 1.5,
        timeout: int = 120,
        pool_size: int = DEFAULT_POOL_SIZE,
    ):
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

        # requests defaults to a 10-connection pool. Running more workers than that
        # makes every surplus connection get discarded and re-established, so a large
        # ingest pays a TLS handshake per archive instead of reusing keep-alive.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max(pool_size, 1), pool_maxsize=max(pool_size, 1)
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _get(self, url: str, *, allow_missing: bool = False) -> bytes | None:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self._session.get(url, timeout=self.timeout)
                if response.status_code == 404:
                    if allow_missing:
                        return None
                    raise DownloadError(f"archive not found: {url}")
                response.raise_for_status()
                return response.content
            except DownloadError:
                raise
            except Exception as error:  # network flakiness, 5xx, timeouts
                last_error = error
                if attempt < self.retries - 1:
                    time.sleep(self.backoff**attempt)
        raise DownloadError(f"failed to fetch {url}: {last_error}")

    def list_symbols(self, *, quote: str = "USDT", interval: str = "1m") -> list[str]:
        """List every symbol that ever published klines, including delisted ones."""
        prefix = f"data/futures/um/monthly/klines/"
        symbols: list[str] = []
        marker = ""
        while True:
            url = f"{BUCKET_URL}?delimiter=/&prefix={prefix}&max-keys=1000"
            if marker:
                url += f"&marker={marker}"
            payload = self._get(url)
            root = ET.fromstring(payload or b"")

            found = [
                node.text.rstrip("/").rsplit("/", 1)[-1]
                for node in root.findall("s3:CommonPrefixes/s3:Prefix", _S3_NS)
                if node.text
            ]
            symbols.extend(found)

            truncated = (root.findtext("s3:IsTruncated", namespaces=_S3_NS) or "").lower()
            if truncated != "true" or not found:
                break
            marker = f"{prefix}{found[-1]}/"

        selected = [
            symbol
            for symbol in symbols
            if symbol.endswith(quote) and _PERPETUAL.fullmatch(symbol)
        ]
        return sorted(set(selected))

    def perpetual_status(self, *, quote: str = "USDT") -> dict[str, str]:
        """Map symbol -> exchangeInfo status for currently listed perpetuals.

        Read-only public market metadata; no key, no account access.

        The status matters and is not binary. Binance reports ``SETTLING`` for a
        perpetual being wound down: it is absent from the trading set but still
        publishing data today. Collapsing that into "delisted" both overstates the
        delisted count and mislabels symbols that still have live bars.
        """
        payload = self._get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        import json

        info = json.loads(payload or b"{}")
        return {
            item["symbol"]: item.get("status", "UNKNOWN")
            for item in info.get("symbols", [])
            if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == quote
        }

    def live_perpetuals(self, *, quote: str = "USDT") -> list[str]:
        """Symbols currently in the tradeable set (``status == TRADING``)."""
        return sorted(
            symbol
            for symbol, status in self.perpetual_status(quote=quote).items()
            if status == "TRADING"
        )

    def fetch(self, archive: Archive, *, allow_missing: bool = True) -> bytes | None:
        """Download an archive, verify its checksum, and return the inner CSV bytes.

        Returns ``None`` when the archive does not exist, which is normal: symbols
        only publish data for months they were actually listed.
        """
        payload = self._get(archive.url, allow_missing=allow_missing)
        if payload is None:
            return None

        expected = self._get(f"{archive.url}.CHECKSUM", allow_missing=True)
        if expected is not None:
            digest = hashlib.sha256(payload).hexdigest()
            wanted = expected.decode("utf-8", "replace").split()[0].strip()
            if digest != wanted:
                raise DownloadError(
                    f"checksum mismatch for {archive.filename}: "
                    f"expected {wanted}, got {digest}"
                )

        try:
            with zipfile.ZipFile(BytesIO(payload)) as bundle:
                names = bundle.namelist()
                if len(names) != 1:
                    raise DownloadError(
                        f"{archive.filename} holds {len(names)} members, expected 1"
                    )
                return bundle.read(names[0])
        except zipfile.BadZipFile as error:
            raise DownloadError(f"{archive.filename} is not a valid zip: {error}") from error
