"""Download a direct audio file the listener has rights to.

This module fetches one http(s) URL that already points at an audio file.
It does not talk to YouTube, Spotify, Apple Music, or any other streaming
service, and it does not extract audio from web pages.

The hostname is resolved once. The TCP connection uses that validated address
so a later DNS answer cannot redirect the download. The Host header and the
TLS SNI / certificate name stay the original hostname.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx


# Vercel Hobby still caps a function response at 4.5 MB. Streaming does not
# raise that cap (https://vercel.com/docs/functions/limitations), so the file
# we stream back stays at 4 MiB, under 4.5 MB whether that figure is decimal
# or binary.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
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
# Decimal, octal (leading 0), or hex (0x) parts, 1 to 4 of them. Catches
# 2130706433, 0177.0.0.1, 0x7f.1, and 127.1 before they reach a resolver.
_OBFUSCATED_IPV4 = re.compile(
    r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}\Z",
    re.IGNORECASE,
)


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
    info: dict | None = None


def prepare_public_url(url: str) -> str:
    """Validate an http(s) URL and return a canonical form (no fragment).

    The host must not be a streaming catalog, a local name, or a private
    address literal. This does not require the path to name an audio file.
    """
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
    _public_literal(host)
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise LinkError("That link isn't a direct audio file.")
    ascii_host = _ascii_host(host)
    return urlunsplit((scheme, _netloc(ascii_host, parts.port), parts.path or "/", parts.query, ""))


def normalize_audio_url(url: str) -> str:
    """Validate a direct audio URL and return a canonical form (no fragment)."""
    canonical = prepare_public_url(url)
    extension = _extension(urlsplit(canonical).path)
    if extension not in ALLOWED_TYPES:
        raise LinkError(
            "That link isn't a direct audio file. Use an MP3, M4A, AAC, OGG, Opus, WAV, or FLAC URL."
        )
    return canonical


def guard_remote_url(url: str, resolver) -> str:
    """Return a canonical URL only when every resolved address is public."""
    canonical = prepare_public_url(url)
    host = urlsplit(canonical).hostname or ""
    resolve_public(host, resolver)
    return canonical


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


def resolve_public(host: str, resolver) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return global addresses for host. A literal is not resolved again.

    Every address from one lookup must be global. A name that answers with
    both a public and a private address is rejected.
    """
    literal = _public_literal(host)
    if literal is not None:
        return [literal]
    try:
        addresses = list(resolver(host))
    except LinkError:
        raise
    except OSError as exc:
        raise LinkError("Could not find that host.") from exc
    checked = []
    for address in addresses:
        address = _unmap(address)
        if not address.is_global:
            raise LinkError("That address isn't allowed.")
        checked.append(address)
    if not checked:
        raise LinkError("That address isn't allowed.")
    return checked


async def download_audio(
    url: str,
    dest: Path,
    *,
    resolver,
    transport: httpx.BaseTransport | None,
    max_bytes: int,
    mime_type: str = "application/octet-stream",
    filename: str | None = None,
) -> DownloadedAudio:
    """Download one public file. The caller names the type; the bytes are not inspected."""
    if max_bytes < 1:
        raise LinkError("The size limit is too small.", status=413)
    current = prepare_public_url(url)
    timeout = httpx.Timeout(10.0, read=30.0)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            host = parts.hostname or ""
            addresses = resolve_public(host, resolver)
            pinned = _pinned_url(parts, addresses[0])
            async with client.stream(
                "GET",
                pinned,
                headers={
                    "Host": _host_header(host, parts.port, parts.scheme),
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        raise LinkError("The server redirected without a new link. Nothing was saved.")
                    current = prepare_public_url(urljoin(current, location.strip()))
                    continue
                if response.status_code != 200:
                    raise LinkError(f"The server returned HTTP {response.status_code}. Nothing was saved.")
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
                chosen = filename or _filename_from_path(parts.path, "m4a")
                return DownloadedAudio(mime_type=mime_type or "audio/mp4", filename=chosen, extension="m4a")
    raise LinkError("Too many redirects. Nothing was saved.")


def _size_message(max_bytes: int) -> str:
    if max_bytes >= 1024 * 1024 and max_bytes % (1024 * 1024) == 0:
        amount = f"{max_bytes // (1024 * 1024)} MB"
    else:
        amount = f"{max_bytes} bytes"
    message = f"That file is over the {amount} limit. Nothing was saved."
    if max_bytes == DEFAULT_MAX_BYTES:
        message = (
            f"That file is over the 4 MB limit. A streamed response still cannot "
            f"exceed Vercel Hobby's 4.5 MB cap, so nothing was saved."
        )
    return message


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


def _pinned_url(parts, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))


def _host_header(host: str, port: int | None, scheme: str) -> str:
    formatted = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    if port and port != default:
        return f"{formatted}:{port}"
    return formatted


def _ip_literal(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _unmap(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _public_literal(host: str):
    """Return a global IP literal, raise if it is not public, or None for a name."""
    ip = _ip_literal(host)
    if ip is None:
        ip = _parse_obfuscated_ipv4(host)
    if ip is None:
        return None
    ip = _unmap(ip)
    if not ip.is_global:
        raise LinkError("That address isn't allowed.")
    return ip


def _parse_obfuscated_ipv4(host: str):
    if not _OBFUSCATED_IPV4.fullmatch(host):
        return None
    values = []
    for part in host.split("."):
        try:
            if part.lower().startswith("0x"):
                values.append(int(part, 16))
            elif len(part) > 1 and part.startswith("0") and all(ch in "01234567" for ch in part):
                values.append(int(part, 8))
            else:
                values.append(int(part, 10))
        except ValueError as exc:
            raise LinkError("That address isn't allowed.") from exc
    try:
        return ipaddress.IPv4Address(_inet_aton(values))
    except (ValueError, ipaddress.AddressValueError) as exc:
        raise LinkError("That address isn't allowed.") from exc


def _inet_aton(parts: list[int]) -> int:
    """Pack 1–4 numeric parts the way inet_aton does (127.1 -> 127.0.0.1)."""
    if len(parts) == 1:
        limits = (0xFFFFFFFF,)
    elif len(parts) == 2:
        limits = (0xFF, 0xFFFFFF)
    elif len(parts) == 3:
        limits = (0xFF, 0xFF, 0xFFFF)
    elif len(parts) == 4:
        limits = (0xFF, 0xFF, 0xFF, 0xFF)
    else:
        raise ValueError("not an IPv4 literal")
    for value, limit in zip(parts, limits):
        if value < 0 or value > limit:
            raise ValueError("part out of range")
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return (parts[0] << 24) | parts[1]
    if len(parts) == 3:
        return (parts[0] << 24) | (parts[1] << 16) | parts[2]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def _ascii_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise LinkError("That address isn't allowed.") from exc


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
