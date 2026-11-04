"""Same-origin check and a best-effort per-process rate limit for /api/fetch.

The limiter only sees requests handled by this process. A Vercel Hobby
function can run more than one instance, including in more than one region,
so the real global cap can be higher. Hobby also includes one firewall
rate-limit rule; that rule is documented as a manual step and is not assumed
to be published.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import Request

MAX_RATE_KEYS = 1024


class SlidingRateLimit:
    def __init__(self, limit: int = 10, window_seconds: float = 60.0, max_keys: int = MAX_RATE_KEYS):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(moment)
            hits = self._hits.get(key)
            if hits is None:
                while self._hits and len(self._hits) >= self.max_keys:
                    self._evict_oldest()
                hits = deque()
                self._hits[key] = hits
            cutoff = moment - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(moment)
            return True

    def _sweep(self, moment: float) -> None:
        cutoff = moment - self.window_seconds
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]

    def _evict_oldest(self) -> None:
        oldest = min(self._hits, key=lambda key: self._hits[key][-1] if self._hits[key] else float("-inf"))
        del self._hits[oldest]


def same_origin(request: Request) -> bool:
    """True only when Origin is this deployment, not a Host the client invented."""
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    origin = (request.headers.get("origin") or "").strip()
    if site != "same-origin" or not origin:
        return False
    parsed = urlsplit(origin)
    if not _origin_shape_ok(parsed):
        return False
    if _on_vercel():
        return _canonical(parsed) in _allowed_origins()
    host = (request.headers.get("host") or "").strip().lower()
    return parsed.scheme == request.url.scheme and parsed.netloc.lower() == host


def client_address(request: Request) -> str:
    """Use the platform client IP on Vercel. Elsewhere trust only the socket."""
    if _on_vercel():
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _on_vercel() -> bool:
    return bool(os.environ.get("VERCEL"))


def _allowed_origins() -> set[str]:
    origins = set()
    for key in ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        origin = _https_host_origin(os.environ.get(key) or "")
        if origin:
            origins.add(origin)
    extra = _https_origin(os.environ.get("PIXEL_ORIGIN") or "")
    if extra:
        origins.add(extra)
    return origins


def _https_host_origin(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    return _https_origin(raw)


def _https_origin(value: str) -> str | None:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not _origin_shape_ok(parsed):
        return None
    return _canonical(parsed)


def _origin_shape_ok(parsed) -> bool:
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    return parsed.path in {"", "/"}


def _canonical(parsed) -> str:
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}"
