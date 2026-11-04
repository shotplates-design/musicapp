"""Download a direct audio file the listener has rights to.

This module fetches one http(s) URL that already points at an audio file.
It does not talk to YouTube, Spotify, Apple Music, or any other streaming
service, and it does not extract audio from web pages.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit, urljoin

import httpx


DEFAULT_MAX_BYTES = 80 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2000
USER_AGENT = "PixelMusic/0.1 (personal library; direct audio files only)"

# Hosts that only serve a streaming catalog. Direct file links never live here.
STREAMING_SUFFIXES = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "googlevideo.com",
    "spotify.com",
    "scdn.co",
    "spotifycdn.com",
    "music.apple.com",
    "itunes.apple.com",
)

ALLOWED_TYPES = {
    "mp3": {"audio/mpeg", "audio/mp3"},
    "m4a": {"audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac", "audio/x-aac"},
    "aac": {"audio/aac", "audio/mp4", "audio/x-aac"},
    "ogg": {"audio/ogg", "audio/opus", "audio/vorbis"},
    "opus": {"audio/ogg", "audio/opus"},
    "wav": {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"},
    "flac": {"audio/flac", "audio/x-flac"},
}

REDIRECTS = {301, 302, 303, 307, 308}
_NUMERIC_HOST = re.compile(r"\d+(?:\.\d+){0,3}")


class LinkError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class DownloadedAudio:
    mime_type: str
    filename: str
    extension: str


def normalize_audio_url(url: str) -> str:
    """Validate a direct audio URL and return a canonical form (no fragment)."""
    raw = str(url or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise LinkError("Paste a direct audio file link (MP3, M4A, AAC, OGG, Opus, WAV, or FLAC).")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise LinkError("Only http and https links to an audio file are allowed.")
    if parts.username or parts.password:
        raise LinkError("Links with a username or password aren't allowed.")
    host = parts.hostname
    if not host:
        raise LinkError("That link doesn't have a host.")
    host = host.lower().rstrip(".")
    if _is_streaming_host(host):
        raise LinkError(
            "This app only downloads direct audio files you have rights to. "
            "YouTube, Spotify, and Apple Music links aren't supported."
        )
    if _is_local_name(host):
        raise LinkError("That address isn't allowed.")
    ip = _ip_literal(host)
    if ip is not None and not ip.is_global:
        raise LinkError("That address isn't allowed.")
    if ip is None and _NUMERIC_HOST.fullmatch(host):
        raise LinkError("That address isn't allowed.")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise LinkError("That link isn't a direct audio file.")
    extension = _extension(parts.path)
    if extension not in ALLOWED_TYPES:
        raise LinkError(
            "That link isn't a direct audio file. Use an MP3, M4A, AAC, OGG, Opus, WAV, or FLAC URL."
        )
    return urlunsplit((scheme, _netloc(host, parts.port), parts.path or "/", parts.query, ""))


def default_resolver(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LinkError("Could not find that host.") from exc
    found = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return found


def ensure_public_dns(host: str, resolver) -> None:
    """Reject hosts that resolve to loopback, private, or link-local addresses."""
    literal = _ip_literal(host)
    addresses = [literal] if literal is not None else list(resolver(host))
    if not addresses or any(not address.is_global for address in addresses):
        raise LinkError("That address isn't allowed.")


async def download_audio(
    url: str,
    dest: Path,
    *,
    resolver,
    transport: httpx.BaseTransport | None,
    max_bytes: int,
) -> DownloadedAudio:
    if max_bytes < 1:
        raise LinkError("The size limit is too small.", status=413)
    current = normalize_audio_url(url)
    timeout = httpx.Timeout(10.0, read=30.0)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            ensure_public_dns(parts.hostname or "", resolver)
            extension = _extension(parts.path)
            async with client.stream(
                "GET",
                current,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "audio/*",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if response.status_code in REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        raise LinkError("The server redirected without a new link. Nothing was saved.")
                    current = normalize_audio_url(urljoin(current, location.strip()))
                    continue
                if response.status_code != 200:
                    raise LinkError(f"The server returned HTTP {response.status_code}. Nothing was saved.")
                mime = _matching_mime(response.headers.get("content-type", ""), extension)
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise LinkError(_size_message(max_bytes), status=413)
                written = 0
                try:
                    with dest.open("wb") as handle:
                        async for chunk in response.aiter_bytes(65536):
                            if not chunk:
                                continue
                            written += len(chunk)
                            if written > max_bytes:
                                raise LinkError(_size_message(max_bytes), status=413)
                            handle.write(chunk)
                except LinkError:
                    dest.unlink(missing_ok=True)
                    raise
                if written <= 0:
                    dest.unlink(missing_ok=True)
                    raise LinkError("That file was empty. Nothing was saved.")
                filename = _filename_from_path(parts.path, extension)
                return DownloadedAudio(mime_type=mime, filename=filename, extension=extension)
    raise LinkError("Too many redirects. Nothing was saved.")


def _matching_mime(header: str, extension: str) -> str:
    mime = header.split(";", 1)[0].strip().lower()
    allowed = ALLOWED_TYPES.get(extension, set())
    if mime in allowed:
        return mime
    raise LinkError("The server didn't return audio. Nothing was saved.")


def _size_message(max_bytes: int) -> str:
    if max_bytes >= 1024 * 1024 and max_bytes % (1024 * 1024) == 0:
        amount = f"{max_bytes // (1024 * 1024)} MB"
    else:
        amount = f"{max_bytes} bytes"
    return f"That file is over the {amount} limit. Nothing was saved."


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()


def _filename_from_path(path: str, extension: str) -> str:
    from app.library import safe_filename

    name = unquote(path.rsplit("/", 1)[-1]) or f"track.{extension}"
    return safe_filename(name, extension)


def _netloc(host: str, port: int | None) -> str:
    formatted = f"[{host}]" if ":" in host else host
    if port:
        return f"{formatted}:{port}"
    return formatted


def _ip_literal(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_local_name(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    return host.endswith(".localhost") or host.endswith(".local")


def _is_streaming_host(host: str) -> bool:
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return True
    for suffix in STREAMING_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False
