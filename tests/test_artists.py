import ipaddress
from pathlib import Path
from urllib.parse import unquote

import httpx
from fastapi.testclient import TestClient

from app.artists import (
    UNKNOWN_ARTIST,
    add_song_to_artist_playlist,
    detect_artist,
    find_artist_playlist,
)
from app.main import create_app
from app.tags import read_tags


PUBLIC = ipaddress.ip_address("93.184.216.34")


def test_detection_prefers_yt_dlp_fields_in_order():
    assert detect_artist(info={"artist": "  A  ", "creator": "B", "uploader": "C"}) == "A"
    assert detect_artist(info={"artist": " ", "creator": " B ", "uploader": "C"}, tag_artist="Tag") == "B"
    assert detect_artist(info={"uploader": " Channel "}, tag_artist="Tag", title="Other - Song") == "Channel"


def test_embedded_tag_beats_a_split_title():
    assert detect_artist(info={}, tag_artist="  Pixel Band ", title="Other - Song") == "Pixel Band"


def test_title_split_is_used_when_info_and_tags_are_blank():
    assert detect_artist(info={"artist": "", "creator": None}, tag_artist=" ", title="Pixel Band - Night Drive") == "Pixel Band"
    assert detect_artist(title="Pixel Band – Night Drive") == "Pixel Band"
    assert detect_artist(title="Night Drive") == UNKNOWN_ARTIST
    assert detect_artist(title=" - Night Drive") == UNKNOWN_ARTIST


def test_mutagen_artist_is_used_before_the_title(tmp_path):
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * (417 - 4)
    path = tmp_path / "seed.mp3"
    path.write_bytes(frame * 4)
    from mutagen.id3 import TIT2, TPE1
    from mutagen.mp3 import MP3

    audio = MP3(path)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text="Night Drive"))
    audio.tags.add(TPE1(encoding=3, text="Pixel Band"))
    audio.save()
    tags = read_tags(path)
    artist = detect_artist(
        info={"title": "Someone Else - Night Drive"},
        tag_artist=tags["artist"],
        titles=(tags["title"], "Someone Else - Night Drive"),
    )
    assert artist == "Pixel Band"


def test_existing_artist_playlist_is_reused_case_insensitively():
    playlists = [{
        "id": "pl-radiohead",
        "name": "Radiohead",
        "artistKey": "radiohead",
        "trackIds": ["song-1"],
    }]
    assert find_artist_playlist(playlists, "  RADIOHEAD ")["id"] == "pl-radiohead"
    chosen = add_song_to_artist_playlist(playlists, "  RADIOHEAD ", "song-2")
    assert chosen["id"] == "pl-radiohead"
    assert chosen["name"] == "Radiohead"
    assert chosen["trackIds"] == ["song-1", "song-2"]
    assert len(playlists) == 1
    add_song_to_artist_playlist(playlists, "radiohead", "song-2")
    assert playlists[0]["trackIds"] == ["song-1", "song-2"]


def test_playlist_is_created_only_when_no_normalized_match_exists():
    playlists = [{"id": "pl-1", "name": "Pixel Band", "trackIds": ["a"]}]
    added = add_song_to_artist_playlist(playlists, "pixel   band", "b")
    assert added["id"] == "pl-1"
    assert len(playlists) == 1
    assert playlists[0]["trackIds"] == ["a", "b"]
    created = add_song_to_artist_playlist(playlists, "Other Artist", "c")
    assert len(playlists) == 2
    assert created["id"] != "pl-1"
    assert created["name"] == "Other Artist"
    assert created["trackIds"] == ["c"]


def test_blank_artists_share_the_unknown_playlist():
    playlists = []
    add_song_to_artist_playlist(playlists, "   ", "a")
    add_song_to_artist_playlist(playlists, "Unknown Artist", "b")
    assert len(playlists) == 1
    assert playlists[0]["name"] == UNKNOWN_ARTIST
    assert playlists[0]["trackIds"] == ["a", "b"]


def _client(tmp_path, monkeypatch):
    application = create_app(
        tmp_path / "lib",
        resolver=lambda host: [PUBLIC],
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        max_bytes=1024 * 1024,
    )
    return TestClient(
        application,
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )


def _fake_ydl(info, filename="Night Drive.m4a", payload=b"abc"):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            folder = Path(self.opts["outtmpl"]).parent
            (folder / filename).write_bytes(payload)
            for hook in self.opts.get("progress_hooks") or []:
                hook({"status": "finished", "info_dict": info})
            return 0

    return FakeYDL


def test_fetch_returns_the_yt_dlp_artist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.playerprops.yt_dlp.YoutubeDL",
        _fake_ydl({"artist": "  Pixel Band ", "creator": "Ignored", "uploader": "Ignored", "title": "Other - Song"}),
    )
    response = _client(tmp_path, monkeypatch).post("/api/fetch", json={"url": "https://playerprops.com/watch"})
    assert response.status_code == 200, response.text
    assert unquote(response.headers["x-pixel-artist"]) == "Pixel Band"


def test_fetch_parses_the_title_when_yt_dlp_has_no_artist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.playerprops.yt_dlp.YoutubeDL",
        _fake_ydl({"artist": "", "creator": None, "uploader": "  ", "title": "Pixel Band - Night Drive"}),
    )
    response = _client(tmp_path, monkeypatch).post("/api/fetch", json={"url": "https://playerprops.com/watch"})
    assert response.status_code == 200, response.text
    assert unquote(response.headers["x-pixel-artist"]) == "Pixel Band"


def test_fetch_falls_back_to_unknown_artist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.playerprops.yt_dlp.YoutubeDL",
        _fake_ydl({"title": "Night Drive"}, filename="Night Drive.m4a"),
    )
    response = _client(tmp_path, monkeypatch).post("/api/fetch", json={"url": "https://playerprops.com/watch"})
    assert response.status_code == 200, response.text
    assert unquote(response.headers["x-pixel-artist"]) == UNKNOWN_ARTIST


def test_fetch_uses_mutagen_when_yt_dlp_has_no_artist(tmp_path, monkeypatch):
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * (417 - 4)
    path = tmp_path / "seed.mp3"
    path.write_bytes(frame * 4)
    from mutagen.id3 import TIT2, TPE1
    from mutagen.mp3 import MP3

    audio = MP3(path)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text="Tagged Title"))
    audio.tags.add(TPE1(encoding=3, text="Tag Artist"))
    audio.save()
    payload = path.read_bytes()
    monkeypatch.setattr(
        "app.playerprops.yt_dlp.YoutubeDL",
        _fake_ydl({"title": "Someone Else - Song"}, filename="song.m4a", payload=payload),
    )
    response = _client(tmp_path, monkeypatch).post("/api/fetch", json={"url": "https://playerprops.com/watch"})
    assert response.status_code == 200, response.text
    assert unquote(response.headers["x-pixel-artist"]) == "Tag Artist"
    assert unquote(response.headers["x-pixel-title"]) == "Tagged Title"
