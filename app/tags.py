"""Read title, artist, artwork, and duration without re-encoding the audio."""

from __future__ import annotations

import math
import wave
from pathlib import Path


def read_tags(path: Path) -> dict:
    title = ""
    artist = ""
    duration = None
    artwork = None
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        MutagenFile = None  # type: ignore

    audio = None
    if MutagenFile is not None:
        try:
            audio = MutagenFile(path)
        except Exception:
            audio = None
    if audio is not None:
        info = getattr(audio, "info", None)
        length = getattr(info, "length", None) if info is not None else None
        if isinstance(length, (int, float)) and math.isfinite(float(length)) and float(length) > 0:
            duration = float(length)
        tags = getattr(audio, "tags", None)
        if tags is not None:
            title = _tag_text(tags, ("TIT2", "\xa9nam", "TITLE", "title"))
            artist = _tag_text(tags, ("TPE1", "\xa9ART", "ARTIST", "artist"))
        artwork = _artwork(audio, tags if audio is not None else None)

    if duration is None and path.suffix.lower() == ".wav":
        duration = _wav_duration(path)
    return {"title": title, "artist": artist, "duration": duration, "artwork": artwork}


def _tag_text(tags, keys: tuple[str, ...]) -> str:
    for key in keys:
        if key not in tags:
            continue
        text = _as_text(tags[key])
        if text:
            return text
    return ""


def _as_text(value) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", None)
    if text is not None:
        return _as_text(text)
    if isinstance(value, (list, tuple)):
        return _as_text(value[0]) if value else ""
    return str(value).replace("\x00", "").strip()


def _artwork(audio, tags):
    if tags is not None:
        try:
            from mutagen.id3 import APIC
        except ImportError:
            APIC = ()  # type: ignore
        try:
            from mutagen.mp4 import MP4Cover
        except ImportError:
            MP4Cover = ()  # type: ignore
        values = []
        try:
            values = list(tags.values())
        except Exception:
            values = []
        for value in values:
            items = value if isinstance(value, list) else [value]
            for item in items:
                data = getattr(item, "data", None)
                if APIC and isinstance(item, APIC) and data:
                    return bytes(data), (item.mime or "image/jpeg")
                if MP4Cover and isinstance(item, MP4Cover) and len(item):
                    mime = "image/png" if item.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
                    return bytes(item), mime
    pictures = getattr(audio, "pictures", None) or []
    if pictures and getattr(pictures[0], "data", None):
        picture = pictures[0]
        return bytes(picture.data), (picture.mime or "image/jpeg")
    return None


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (wave.Error, EOFError, OSError):
        return None
    if not rate or frames <= 0:
        return None
    return frames / float(rate)
