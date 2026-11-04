"""Detect a song's artist and file it under that artist's playlist.

Detection order:
1. yt-dlp info fields (artist, then creator, then uploader)
2. embedded file tags
3. a title shaped like "Artist - Song"
4. "Unknown Artist"

Playlist names are trimmed. Matching is case-insensitive, so "Radiohead" and
" radiohead " share one playlist. A new playlist is created only when none match.
"""

from __future__ import annotations

import uuid

UNKNOWN_ARTIST = "Unknown Artist"
_TITLE_SEPARATORS = (" - ", " – ", " — ")


def _clean(value) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _clean(item)
            if text:
                return text
        return ""
    if value is None:
        return ""
    return " ".join(str(value).replace("\x00", " ").split())


def normalize_artist(name: str | None) -> str:
    """Trim and collapse whitespace. Blank becomes Unknown Artist."""
    return _clean(name) or UNKNOWN_ARTIST


def artist_key(name: str | None) -> str:
    """Key used to match an existing playlist. Case-insensitive."""
    return normalize_artist(name).casefold()


def artist_from_info(info: dict | None) -> str:
    if not isinstance(info, dict):
        return ""
    for field in ("artist", "creator", "uploader"):
        text = _clean(info.get(field))
        if text:
            return text
    return ""


def artist_from_title(title: str | None) -> str:
    text = _clean(title)
    for separator in _TITLE_SEPARATORS:
        if separator not in text:
            continue
        left, right = text.split(separator, 1)
        left = left.strip()
        right = right.strip()
        if left and right:
            return left
    return ""


def detect_artist(
    *,
    info: dict | None = None,
    tag_artist: str | None = "",
    title: str | None = "",
    titles: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Return the artist using yt-dlp fields, tags, then the title."""
    found = artist_from_info(info) or _clean(tag_artist)
    if found:
        return found
    candidates = list(titles) if titles is not None else [title or ""]
    for candidate in candidates:
        found = artist_from_title(candidate)
        if found:
            return found
    return UNKNOWN_ARTIST


def _is_manual(playlist: dict) -> bool:
    return playlist.get("kind") == "manual"


def find_artist_playlist(playlists: list[dict], artist: str) -> dict | None:
    """Return the artist playlist whose normalized name matches, if any.

    User-named playlists are skipped so a manual playlist can share an
    artist's name without collecting that artist's new songs.
    """
    key = artist_key(artist)
    for playlist in playlists:
        if _is_manual(playlist):
            continue
        stored = playlist.get("artistKey") or artist_key(playlist.get("name") or "")
        if stored == key:
            return playlist
    return None


def add_song_to_artist_playlist(playlists: list[dict], artist: str, track_id: str) -> dict:
    """Add track_id to the artist's playlist, creating one only if none exists."""
    name = normalize_artist(artist)
    existing = find_artist_playlist(playlists, name)
    if existing is not None:
        ids = [str(item) for item in (existing.get("trackIds") or [])]
        if str(track_id) not in ids:
            ids.append(str(track_id))
        existing["trackIds"] = ids
        existing["artistKey"] = existing.get("artistKey") or artist_key(name)
        existing["kind"] = "artist"
        return existing
    created = {
        "id": str(uuid.uuid4()),
        "name": name,
        "kind": "artist",
        "artistKey": artist_key(name),
        "trackIds": [str(track_id)],
    }
    playlists.append(created)
    return created
