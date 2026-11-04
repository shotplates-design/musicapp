"""Read a playerprops.com page with yt-dlp and download the audio file.

Only that host, and its subdomains, may use yt-dlp. The match is the parsed
hostname, exactly `playerprops.com` or a name that ends in `.playerprops.com`.
`playerprops.com.evil.net` does not match. Every other host is refused.

yt-dlp only resolves the page. The file it points at is downloaded with the
public-address, size, and deadline checks. The saved file is always `.m4a`.
There is no ffmpeg and no post-processing. Anything yt-dlp writes is confined
to a temporary directory and removed before this function returns.
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.download import (
    DEFAULT_MAX_BYTES,
    MAX_REDIRECTS,
    REDIRECTS,
    LinkError,
    _host_header,
    _pinned_url,
    _size_message,
    download_audio,
    guard_remote_url,
    prepare_public_url,
    resolve_public,
)
from app.library import safe_filename

PLAYERPROPS_HOST = "playerprops.com"
M4A_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
_STREAM_PROTOCOLS = ("m3u8", "dash", "ism", "f4m")


def is_playerprops_host(host: str) -> bool:
    """True for playerprops.com and its subdomains, not for lookalike names."""
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return False
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return name == PLAYERPROPS_HOST or name.endswith("." + PLAYERPROPS_HOST)


def is_playerprops_url(url: str) -> bool:
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return False
    return is_playerprops_host(parts.hostname)


def canonical_playerprops_url(url: str) -> str:
    canonical = prepare_public_url(url)
    parts = urlsplit(canonical)
    if parts.port is not None:
        raise LinkError("Playerprops links can't include a port.")
    if not is_playerprops_host(parts.hostname or ""):
        raise LinkError("That link isn't on playerprops.com.")
    return canonical


async def download_playerprops(url, dest, *, resolver, transport, max_bytes, deadline):
    """Resolve one playerprops page, then download its public audio file."""
    page = canonical_playerprops_url(url)
    guard_remote_url(page, resolver)
    info = await asyncio.to_thread(_extract, page, resolver, deadline, transport, max_bytes)
    _reject_drm(info)
    _reject_playlist(info)
    _reject_stream(info)
    media = str(info.get("url") or "").strip()
    if not media:
        raise LinkError("That page didn't include a file. Nothing was saved.")
    title = str(info.get("title") or "").strip() or "track"
    return await download_audio(
        media,
        dest,
        resolver=resolver,
        transport=transport,
        max_bytes=max_bytes,
        mime_type="audio/mp4",
        filename=safe_filename(title, "m4a"),
    )


def _yt_dlp():
    import yt_dlp

    return yt_dlp


def ydl_params(directory: Path) -> dict:
    """Options for one page. No playlist, no ffmpeg, no files left behind."""
    folder = str(directory)
    return {
        "format": M4A_FORMAT,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "postprocessors": [],
        "fixup": "never",
        "ffmpeg_location": None,
        "cachedir": False,
        "socket_timeout": 10,
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "outtmpl": str(directory / "%(id)s.%(ext)s"),
        "paths": {"home": folder, "temp": folder},
        "allowed_extractors": ["generic"],
    }


def _extract(url: str, resolver, deadline, transport=None, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    yt_dlp = _yt_dlp()
    with tempfile.TemporaryDirectory(prefix="pixel-ydl-") as tmp:
        params = ydl_params(Path(tmp))
        ydl_cls = _checking_ydl(yt_dlp.YoutubeDL, resolver, deadline, transport, max_bytes)
        try:
            with ydl_cls(params) as ydl:
                info = ydl.extract_info(url, download=False)
        except LinkError:
            raise
        except Exception as exc:
            cause = _linked_error(exc)
            if isinstance(cause, LinkError):
                raise cause from exc
            if "drm" in str(exc).lower():
                raise LinkError("That track is DRM-protected. Nothing was saved.") from exc
            raise LinkError("Couldn't read that playerprops page. Nothing was saved.") from exc
    if not isinstance(info, dict):
        raise LinkError("Couldn't read that playerprops page. Nothing was saved.")
    return info


def _linked_error(exc: BaseException):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LinkError):
            return current
        current = getattr(current, "__cause__", None) or getattr(current, "cause", None)
    return None


def _checking_ydl(base, resolver, deadline, transport, max_bytes=DEFAULT_MAX_BYTES):
    class CheckingYDL(base):
        def __init__(self, params):
            super().__init__(params)
            self._pixel_resolver = resolver
            self._pixel_deadline = deadline
            self._pixel_transport = transport
            self._pixel_max_bytes = max_bytes

        def urlopen(self, req):
            director = self._request_director
            if "_Pinned" not in director.handlers:
                director.add_handler(make_pinned_handler(
                    resolver=self._pixel_resolver,
                    deadline=self._pixel_deadline,
                    transport=self._pixel_transport,
                    max_bytes=self._pixel_max_bytes,
                    logger=director.logger,
                ))
                director.preferences.add(
                    lambda handler, _request: 10000 if handler.RH_KEY == "_Pinned" else 0,
                )
            try:
                return super().urlopen(req)
            except Exception as exc:
                cause = _linked_error(exc)
                if isinstance(cause, LinkError):
                    raise cause from exc
                raise

    return CheckingYDL


class _SilentLog:
    def debug(self, *args, **kwargs):
        return None

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def stdout(self, *args, **kwargs):
        return None

    def stderr(self, *args, **kwargs):
        return None


def make_pinned_handler(*, resolver, deadline, transport=None, logger=None, max_bytes: int = DEFAULT_MAX_BYTES):
    """HTTP handler that connects to one checked public address.

    yt-dlp's Request exposes `.url`, not `.full_url`. Every hop is checked
    again, including redirects, and the TCP connection uses the address from
    that check so a later DNS answer cannot retarget it.
    """
    from yt_dlp.networking.common import RequestHandler, Response
    from yt_dlp.networking.exceptions import RequestError

    class _PinnedRH(RequestHandler):
        _SUPPORTED_URL_SCHEMES = None
        _SUPPORTED_PROXY_SCHEMES = None
        _SUPPORTED_FEATURES = None

        def __init__(self, *, resolver, deadline, transport, max_bytes, **kwargs):
            super().__init__(**kwargs)
            self._resolver = resolver
            self._deadline = deadline
            self._transport = transport
            self._max_bytes = max_bytes

        def _validate(self, request):
            return None

        def _send(self, request):
            try:
                # yt_dlp.networking.Request exposes .url. It has no .full_url.
                return self._open(request.url, request.method)
            except LinkError as exc:
                raise RequestError(exc.message, cause=exc) from exc
            except RequestError:
                raise
            except Exception as exc:
                raise RequestError("Couldn't read that playerprops page. Nothing was saved.", cause=exc) from exc

        def _open(self, url: str, method: str):
            if not isinstance(url, str) or not url:
                raise LinkError("That page can only be read from playerprops.com. Nothing was saved.")
            current = url
            with httpx.Client(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for _hop in range(MAX_REDIRECTS + 1):
                    self._check_deadline()
                    canonical = canonical_playerprops_url(current)
                    parts = urlsplit(canonical)
                    host = parts.hostname or ""
                    addresses = resolve_public(host, self._resolver)
                    pinned = _pinned_url(parts, addresses[0])
                    # A per-read timeout is not the request deadline. Keep each
                    # socket wait short, and check the wall clock on every chunk.
                    self._check_deadline()
                    try:
                        remaining = self._deadline - time.monotonic()
                        if remaining <= 0:
                            raise LinkError("That download took too long. Nothing was saved.", status=504)
                        with client.stream(
                            method,
                            pinned,
                            headers={
                                "Host": _host_header(host, parts.port, parts.scheme),
                                "User-Agent": "PixelMusic/0.1",
                                "Accept-Encoding": "identity",
                            },
                            extensions={"sni_hostname": host},
                            timeout=httpx.Timeout(min(remaining, 1.0)),
                        ) as response:
                            if response.status_code in REDIRECTS:
                                location = response.headers.get("location")
                                if not location:
                                    raise LinkError("The server redirected without a new link. Nothing was saved.")
                                current = urljoin(canonical, location.strip())
                                if response.status_code in {301, 302, 303}:
                                    method = "GET"
                                continue
                            if response.status_code != 200:
                                raise LinkError(f"The server returned HTTP {response.status_code}. Nothing was saved.")
                            body = self._read_body(response)
                            built = Response(
                                fp=io.BytesIO(body),
                                url=canonical,
                                headers=dict(response.headers),
                                status=response.status_code,
                            )
                    except httpx.TimeoutException as exc:
                        raise LinkError("That download took too long. Nothing was saved.", status=504) from exc
                    return built
            raise LinkError("Too many redirects. Nothing was saved.")

        def _check_deadline(self) -> None:
            if time.monotonic() >= self._deadline:
                raise LinkError("That download took too long. Nothing was saved.", status=504)

        def _read_body(self, response: httpx.Response) -> bytes:
            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > self._max_bytes:
                raise LinkError(_size_message(self._max_bytes), status=413)
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                self._check_deadline()
                if not chunk:
                    continue
                total += len(chunk)
                if total > self._max_bytes:
                    raise LinkError(_size_message(self._max_bytes), status=413)
                chunks.append(chunk)
            return b"".join(chunks)

    return _PinnedRH(
        logger=logger or _SilentLog(),
        resolver=resolver,
        deadline=deadline,
        transport=transport,
        max_bytes=max_bytes,
        timeout=10,
    )


def _reject_drm(info: dict) -> None:
    if info.get("has_drm"):
        raise LinkError("That track is DRM-protected. Nothing was saved.")
    for fmt in info.get("requested_formats") or []:
        if isinstance(fmt, dict) and fmt.get("has_drm"):
            raise LinkError("That track is DRM-protected. Nothing was saved.")


def _reject_playlist(info: dict) -> None:
    if info.get("_type") in {"playlist", "multi_video"}:
        raise LinkError("Paste one playerprops track, not a playlist. Nothing was saved.")


def _reject_stream(info: dict) -> None:
    protocol = str(info.get("protocol") or "").lower()
    if any(name in protocol for name in _STREAM_PROTOCOLS):
        raise LinkError("That track is a stream this server can't save without ffmpeg. Nothing was saved.")
    requested = [fmt for fmt in (info.get("requested_formats") or []) if isinstance(fmt, dict)]
    if len(requested) > 1:
        raise LinkError("That track is a stream this server can't save without ffmpeg. Nothing was saved.")


