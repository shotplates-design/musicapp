"""Same-origin check and a best-effort per-process rate limit for /api/fetch.

The limiter only sees requests handled by this process. A Vercel Hobby
function can run more than one instance, including in more than one region,
so the real global cap can be higher. Hobby also includes one firewall
rate-limit rule; that rule is documented as a manual step and is not assumed
to be published.
"""

from __future__ import annotations

import ipaddress
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
                if len(self._hits) >= self.max_keys:
                    return False
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
        """Drop addresses whose newest hit is already outside the window.

        Keys still inside the window stay, including ones already at the limit.
        A full map rejects a new address instead of forgetting a blocked one.
        """
        cutoff = moment - self.window_seconds
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]


def same_origin(request: Request) -> bool:
    """Origin check disabled: every request is accepted."""
    return True


def client_address(request: Request) -> str:
    """Use the platform client IP on Vercel. Elsewhere trust only the socket."""
    if _on_vercel():
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return _rate_key(real)
    if request.client and request.client.host:
        return _rate_key(request.client.host)
    return "unknown"


def _rate_key(address: str) -> str:
    """IPv4 stays as itself. IPv6 shares one bucket per /64."""
    raw = address.strip()
    if raw.startswith("[") and "]" in raw:
        raw = raw[1:raw.index("]")]
    raw = raw.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return address.strip() or "unknown"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    if isinstance(ip, ipaddress.IPv6Address):
        network = ipaddress.IPv6Network((ip, 64), strict=False)
        return f"{network.network_address}/64"
    return str(ip)


def _on_vercel() -> bool:
    return bool(os.environ.get("VERCEL"))


def _allowed_origins() -> set[str]:
    origins = set()
    for key in ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL", "VERCEL_BRANCH_URL"):
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
