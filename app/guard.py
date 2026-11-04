"""Same-origin check and a best-effort per-process rate limit for /api/fetch.

The limiter only sees requests handled by this process. A Vercel Hobby
function can run more than one instance, including in more than one region,
so the real global cap can be higher. Hobby also includes one firewall
rate-limit rule; that rule is documented as a manual step and is not assumed
to be published.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import Request


class SlidingRateLimit:
    def __init__(self, limit: int = 10, window_seconds: float = 60.0):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            cutoff = moment - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                if not hits:
                    self._hits.pop(key, None)
                return False
            hits.append(moment)
            return True


def same_origin(request: Request) -> bool:
    """True only when the browser says this POST came from this app."""
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    origin = (request.headers.get("origin") or "").strip()
    if site != "same-origin" or not origin:
        return False
    parsed = urlsplit(origin)
    host = (request.headers.get("host") or "").strip().lower()
    if parsed.scheme != request.url.scheme or parsed.netloc.lower() != host:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path not in {"", "/"}:
        return False
    return True


def client_address(request: Request) -> str:
    """Prefer the address the platform sets, then the socket peer."""
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    if request.client and request.client.host:
        return request.client.host
    return "unknown"
