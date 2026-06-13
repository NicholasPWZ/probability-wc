"""HTTP client for FotMob's public JSON API (https://www.fotmob.com/api/data).

We use curl_cffi to impersonate a real Chrome TLS fingerprint. Responses are
cached on disk (TTL) so repeated team / match lookups during aggregation are
cheap, and a small rate limiter + retry/backoff keeps us polite and resilient
to 429/5xx.

NOTE: SofaScore was the original source but it IP-blocks datacenter hosts
(returns a 403 "challenge"); FBref sits behind Cloudflare. FotMob's
``/api/data`` endpoints return clean JSON without those blocks.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi_requests

from app.config import get_settings

API_ROOT = "https://www.fotmob.com/api/data"

_BASE_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fotmob.com/",
}


class SofaScoreError(RuntimeError):
    """Raised when the data API cannot be reached or returns a non-recoverable error."""


class _RateLimiter:
    """Simple thread-safe minimum-interval limiter."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last = time.monotonic()


class SofaScoreClient:
    """Caching, rate-limited client for the SofaScore JSON API."""

    def __init__(self) -> None:
        settings = get_settings()
        self._cache_ttl = settings.cache_ttl
        self._cache_dir = Path(settings.cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # Spread out requests; ~3 req/s ceiling.
        self._limiter = _RateLimiter(min_interval=0.33)
        self._session = cffi_requests.Session(impersonate="chrome", headers=_BASE_HEADERS, timeout=20)

    # -- cache helpers ----------------------------------------------------
    def _cache_path(self, path: str) -> Path:
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]
        return self._cache_dir / f"{digest}.json"

    def _read_cache(self, path: str, ttl: int | None) -> Any | None:
        ttl = self._cache_ttl if ttl is None else ttl
        if ttl <= 0:
            return None
        fp = self._cache_path(path)
        if not fp.exists():
            return None
        if (time.time() - fp.stat().st_mtime) > ttl:
            return None
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, path: str, payload: Any) -> None:
        try:
            self._cache_path(path).write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    # -- request ----------------------------------------------------------
    def get(self, path: str, *, ttl: int | None = None, force: bool = False) -> Any:
        """GET an API path (relative to API_ROOT), returning decoded JSON.

        Uses the disk cache unless ``force`` is True. A 404 is returned as
        ``None`` (e.g. statistics for a match that has not been played).
        """
        path = path.lstrip("/")
        if not force:
            cached = self._read_cache(path, ttl)
            if cached is not None:
                return cached

        url = f"{API_ROOT}/{path}"
        last_exc: Exception | None = None
        for attempt in range(4):
            self._limiter.wait()
            try:
                resp = self._session.get(url)
            except Exception as exc:  # network / curl error
                last_exc = exc
                time.sleep(0.5 * (2**attempt))
                continue

            if resp.status_code == 404:
                self._write_cache(path, None)
                return None
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                last_exc = SofaScoreError(f"{resp.status_code} for {path}")
                time.sleep(0.8 * (2**attempt))
                continue
            if resp.status_code != 200:
                raise SofaScoreError(f"Unexpected {resp.status_code} for {path}")

            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise SofaScoreError(f"Invalid JSON for {path}") from exc
            self._write_cache(path, data)
            return data

        raise SofaScoreError(f"Failed to fetch {path}: {last_exc}")


_client: SofaScoreClient | None = None
_client_lock = threading.Lock()


def get_client() -> SofaScoreClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = SofaScoreClient()
    return _client
