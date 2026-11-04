"""Read a playerprops.com page with yt-dlp and download the audio file.

Only that host, and its subdomains, may use yt-dlp. The match is the parsed
hostname, exactly `playerprops.com` or a name that ends in `.playerprops.com`.
`playerprops.com.evil.net` does not match. YouTube, Spotify, and every other
host stay on the direct-file path.

yt-dlp only resolves the page. The audio bytes are downloaded with the same
public-address and magic-byte checks as a direct file. There is no ffmpeg and
no post-processing. Anything yt-dlp writes is confined to a temporary directory
and removed before this function returns.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from app.download import (
    LinkError,
    download_audio,
    guard_remote_url,
    prepare_public_url,
)
from app.library import safe_filename

PLAYERPROPS_HOST = "playerprops.com"
_EXT_ALIAS = {"mp4": "m4a"}
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
    host = urlsplit(canonical).hostname or ""
    if not is_playerprops_host(host):
        raise LinkError("That link isn't on playerprops.com.")
    return canonical


async def download_playerprops(url, dest, *, resolver, transport, max_bytes):
    """Resolve one playerprops page, then download its public audio file."""
    page = canonical_playerprops_url(url)
    guard_remote_url(page, resolver)
    info = await asyncio.to_thread(_extract, page, resolver)
    _reject_drm(info)
    _reject_playlist(info)
    _reject_stream(info)
    media = str(info.get("url") or "").strip()
    if not media:
        raise LinkError("That page didn't include an audio file. Nothing was saved.")
    extension = _supported_ext(str(info.get("ext") or ""))
    guard_remote_url(media, resolver)
    title = str(info.get("title") or "").strip()
    filename = safe_filename(title, extension) if title else None
    return await download_audio(
        media,
        dest,
        resolver=resolver,
        transport=transport,
        max_bytes=max_bytes,
        extension=extension,
        filename=filename,
    )


def _yt_dlp():
    import yt_dlp

    return yt_dlp


def ydl_params(directory: Path) -> dict:
    """Options for one page. No playlist, no ffmpeg, no files left behind."""
    folder = str(directory)
    return {
        "format": "bestaudio/best",
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
    }


def _extract(url: str, resolver) -> dict:
    yt_dlp = _yt_dlp()
    with tempfile.TemporaryDirectory(prefix="pixel-ydl-") as tmp:
        params = ydl_params(Path(tmp))
        ydl_cls = _checking_ydl(yt_dlp.YoutubeDL, resolver)
        try:
            with ydl_cls(params) as ydl:
                info = ydl.extract_info(url, download=False)
        except LinkError:
            raise
        except Exception as exc:
            cause = exc.__cause__
            if isinstance(cause, LinkError):
                raise cause from exc
            if "drm" in str(exc).lower():
                raise LinkError("That track is DRM-protected. Nothing was saved.") from exc
            raise LinkError("Couldn't read that playerprops page. Nothing was saved.") from exc
    if not isinstance(info, dict):
        raise LinkError("Couldn't read that playerprops page. Nothing was saved.")
    return info


def _checking_ydl(base, resolver):
    class CheckingYDL(base):
        def urlopen(self, req):
            target = req if isinstance(req, str) else getattr(req, "full_url", "")
            if target:
                guard_remote_url(str(target), resolver)
            return super().urlopen(req)

    return CheckingYDL


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


def _supported_ext(ext: str) -> str:
    cleaned = ext.lower().strip().lstrip(".")
    cleaned = _EXT_ALIAS.get(cleaned, cleaned)
    from app.download import ALLOWED_TYPES

    if cleaned not in ALLOWED_TYPES:
        raise LinkError(
            "That page didn't include a supported audio file "
            "(MP3, M4A, AAC, OGG, Opus, WAV, or FLAC). Nothing was saved."
        )
    return cleaned
