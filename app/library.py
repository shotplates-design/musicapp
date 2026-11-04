"""On-disk library shaped like the pixel-music track store.

Each track record keeps the same fields the pixel-music client stores in
IndexedDB: title, artist, duration, mime type, audio bytes, artwork bytes,
filename, added time, source, and source URL. Newest tracks are listed first.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path


def now_ms() -> int:
    return int(time.time() * 1000)


def title_from_filename(name: str) -> str:
    base = str(name or "").replace("\\", "/").split("/")[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    base = " ".join(part for part in base.replace("_", " ").split())
    return base or "Untitled"


def safe_filename(name: str, ext: str) -> str:
    cleaned = str(name or "track")
    dot = cleaned.rfind(".")
    if dot > 0 and 2 <= len(cleaned) - dot - 1 <= 5 and cleaned[dot + 1 :].isalnum():
        cleaned = cleaned[:dot]
    forbidden = set('<>:"/\\|?*')
    cleaned = "".join(ch for ch in cleaned if ch not in forbidden and ord(ch) >= 32)
    cleaned = " ".join(cleaned.split()).strip()[:80] or "track"
    clean_ext = "".join(ch for ch in str(ext or "audio") if ch.isalnum()) or "audio"
    return f"{cleaned}.{clean_ext}"


def build_track_record(
    *,
    id: str,
    title: str,
    artist: str,
    duration: float | None,
    mime_type: str,
    byte_size: int,
    artwork_size: int = 0,
    artwork_mime: str | None = None,
    filename: str,
    added_at: int | None = None,
    source: str = "link",
    source_url: str = "",
) -> dict:
    seconds = duration
    if isinstance(seconds, (int, float)) and math.isfinite(float(seconds)) and float(seconds) > 0:
        seconds = float(seconds)
    else:
        seconds = None
    return {
        "id": id,
        "title": str(title or "").strip() or "Untitled",
        "artist": str(artist or "").strip() or "Unknown artist",
        "duration": seconds,
        "mimeType": mime_type or "application/octet-stream",
        "byteSize": max(0, int(byte_size or 0)),
        "artworkSize": max(0, int(artwork_size or 0)),
        "artworkMime": artwork_mime or None,
        "filename": filename or "audio",
        "addedAt": int(now_ms() if added_at is None else added_at),
        "source": source or "link",
        "sourceUrl": source_url or "",
    }


def sort_tracks(tracks: list[dict]) -> list[dict]:
    return sorted(
        tracks,
        key=lambda track: (-(int(track.get("addedAt") or 0)), str(track.get("title") or "")),
    )


def total_bytes(tracks: list[dict]) -> int:
    return sum((int(track.get("byteSize") or 0) + int(track.get("artworkSize") or 0)) for track in tracks or [])


class TrackNotFound(Exception):
    pass


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.media = self.root / "media"
        self.tracks_path = self.root / "tracks.json"
        self._lock = threading.Lock()
        self.media.mkdir(parents=True, exist_ok=True)

    def list_tracks(self) -> list[dict]:
        with self._lock:
            return sort_tracks(self._read())

    def total_bytes(self) -> int:
        return total_bytes(self.list_tracks())

    def get(self, track_id: str) -> dict:
        canonical = _canonical_id(track_id)
        with self._lock:
            for track in self._read():
                if track.get("id") == canonical:
                    return track
        raise TrackNotFound(canonical)

    def find_by_source_url(self, source_url: str) -> dict | None:
        with self._lock:
            for track in self._read():
                if track.get("sourceUrl") == source_url:
                    return track
        return None

    def adopt_file(
        self,
        tmp: Path,
        *,
        source_url: str,
        filename: str,
        mime_type: str,
        title: str,
        artist: str,
        duration: float | None,
        artwork: bytes | None,
        artwork_mime: str | None,
        added_at: int | None = None,
    ) -> tuple[dict, bool]:
        """Move a finished download into the library. Returns (track, created)."""
        with self._lock:
            for track in self._read():
                if source_url and track.get("sourceUrl") == source_url:
                    tmp.unlink(missing_ok=True)
                    return track, False
            track_id = str(uuid.uuid4())
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "audio"
            ext = "".join(ch for ch in ext if ch.isalnum()) or "audio"
            dest = self.media / f"{track_id}.{ext}"
            tmp.replace(dest)
            art_size = 0
            if artwork:
                (self.media / f"{track_id}.artwork").write_bytes(artwork)
                art_size = len(artwork)
            record = build_track_record(
                id=track_id,
                title=title,
                artist=artist,
                duration=duration,
                mime_type=mime_type,
                byte_size=dest.stat().st_size,
                artwork_size=art_size,
                artwork_mime=artwork_mime if artwork else None,
                filename=filename,
                added_at=added_at,
                source="link",
                source_url=source_url,
            )
            tracks = self._read()
            tracks.append(record)
            self._write(tracks)
            return record, True

    def delete(self, track_id: str) -> None:
        canonical = _canonical_id(track_id)
        with self._lock:
            tracks = self._read()
            kept = [track for track in tracks if track.get("id") != canonical]
            if len(kept) == len(tracks):
                raise TrackNotFound(canonical)
            for track in tracks:
                if track.get("id") == canonical:
                    self.audio_path(track).unlink(missing_ok=True)
                    self.artwork_path(track).unlink(missing_ok=True)
            self._write(kept)

    def audio_path(self, track: dict) -> Path:
        filename = str(track.get("filename") or "")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "audio"
        ext = "".join(ch for ch in ext if ch.isalnum()) or "audio"
        return self.media / f"{track['id']}.{ext}"

    def artwork_path(self, track: dict) -> Path:
        return self.media / f"{track['id']}.artwork"

    def _read(self) -> list[dict]:
        if not self.tracks_path.exists():
            return []
        data = json.loads(self.tracks_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise RuntimeError("tracks.json is not a list")
        return data

    def _write(self, tracks: list[dict]) -> None:
        tmp = self.tracks_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tracks, indent=2), encoding="utf-8")
        tmp.replace(self.tracks_path)


def _canonical_id(track_id: str) -> str:
    try:
        return str(uuid.UUID(str(track_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise TrackNotFound(str(track_id)) from exc
