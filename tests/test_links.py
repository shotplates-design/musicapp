import io
import ipaddress
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


PUBLIC = ipaddress.ip_address("93.184.216.34")


def make_wav(seconds=0.5, rate=8000) -> bytes:
    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def make_tagged_mp3(tmp_path: Path) -> bytes:
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * (417 - 4)
    path = tmp_path / "seed.mp3"
    path.write_bytes(frame * 8)
    from mutagen.id3 import APIC, TIT2, TPE1
    from mutagen.mp3 import MP3

    audio = MP3(path)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text="Night Drive"))
    audio.tags.add(TPE1(encoding=3, text="Pixel Band"))
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"\xff\xd8\xff\xd9"))
    audio.save()
    return path.read_bytes()


def client_for(tmp_path, handler, *, resolver=None, max_bytes=80 * 1024 * 1024):
    if resolver is None:
        resolver = lambda host: [PUBLIC]
    application = create_app(
        tmp_path / "lib",
        resolver=resolver,
        transport=httpx.MockTransport(handler),
        max_bytes=max_bytes,
    )
    return TestClient(application), application


def test_saves_direct_audio_reads_tags_and_sorts_newest_first(tmp_path, monkeypatch):
    wav = make_wav()
    mp3 = make_tagged_mp3(tmp_path)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("My_Song.wav"):
            return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)
        if request.url.path.endswith("night.mp3"):
            return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=mp3)
        raise AssertionError(request.url)

    ticks = iter([1_000, 5_000])
    monkeypatch.setattr("app.library.now_ms", lambda: next(ticks))
    client, application = client_for(tmp_path, handler)

    first = client.post("/api/tracks", json={"url": "https://cdn.example.com/My_Song.wav?token=1"})
    assert first.status_code == 201, first.text
    saved = first.json()["track"]
    assert saved["title"] == "My Song"
    assert saved["artist"] == "Unknown artist"
    assert saved["source"] == "link"
    assert saved["mimeType"] == "audio/wav"
    assert 0.4 < saved["duration"] < 0.6
    assert saved["byteSize"] == len(wav)
    assert saved["artworkSize"] == 0

    second = client.post("/api/tracks", json={"url": "https://cdn.example.com/night.mp3"})
    assert second.status_code == 201, second.text
    tagged = second.json()["track"]
    assert tagged["title"] == "Night Drive"
    assert tagged["artist"] == "Pixel Band"
    assert tagged["artworkSize"] == 4
    assert tagged["artworkMime"] == "image/jpeg"
    assert tagged["duration"] > 0

    listing = client.get("/api/tracks")
    assert listing.status_code == 200
    body = listing.json()
    assert [track["id"] for track in body["tracks"]] == [tagged["id"], saved["id"]]
    assert body["totalBytes"] == saved["byteSize"] + tagged["byteSize"] + tagged["artworkSize"]

    audio = client.get(f"/api/tracks/{saved['id']}/audio", headers={"Range": "bytes=0-3"})
    assert audio.status_code == 206
    assert audio.content == b"RIFF"
    assert audio.headers["accept-ranges"] == "bytes"
    assert "inline" in audio.headers["content-disposition"]

    art = client.get(f"/api/tracks/{tagged['id']}/artwork")
    assert art.status_code == 200
    assert art.headers["content-type"].startswith("image/jpeg")
    assert art.content == b"\xff\xd8\xff\xd9"

    duplicate = client.post("/api/tracks", json={"url": "https://cdn.example.com/night.mp3"})
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    assert duplicate.json()["track"]["id"] == tagged["id"]
    assert calls["n"] == 2

    deleted = client.delete(f"/api/tracks/{tagged['id']}")
    assert deleted.status_code == 200
    assert client.get(f"/api/tracks/{tagged['id']}/audio").status_code == 404
    assert client.get(f"/api/tracks/{tagged['id']}/artwork").status_code == 404
    remaining = client.get("/api/tracks").json()["tracks"]
    assert [track["id"] for track in remaining] == [saved["id"]]
    media = list(application.state.library.media.iterdir())
    assert len(media) == 1
    assert media[0].name.endswith(".wav")


def test_rejects_streaming_services_private_addresses_and_non_audio(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html></html>")

    client, application = client_for(tmp_path, handler)
    blocked = [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc.mp3",
        "https://music.youtube.com/watch?v=abc.mp3",
        "https://open.spotify.com/track/abc.mp3",
        "https://music.apple.com/us/album/night.m4a",
        "https://audio-ssl.itunes.apple.com/song.m4a",
    ]
    for url in blocked:
        response = client.post("/api/tracks", json={"url": url})
        assert response.status_code == 400, url
        assert "YouTube, Spotify, and Apple Music" in response.json()["detail"]

    private_urls = [
        "http://127.0.0.1/a.mp3",
        "http://10.1.1.1/a.mp3",
        "http://192.168.1.9/a.wav",
        "http://169.254.169.254/latest/meta.mp3",
        "http://localhost/a.mp3",
        "file:///tmp/a.mp3",
        "https://user:secret@cdn.example.com/a.mp3",
    ]
    for url in private_urls:
        response = client.post("/api/tracks", json={"url": url})
        assert response.status_code == 400, url
        assert "Nothing was saved" not in response.json()["detail"] or "allowed" in response.json()["detail"].lower() or "aren't allowed" in response.json()["detail"]

    page = client.post("/api/tracks", json={"url": "https://cdn.example.com/download"})
    assert page.status_code == 400
    assert "direct audio file" in page.json()["detail"]

    html = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.mp3"})
    assert html.status_code == 400
    assert "didn't return audio" in html.json()["detail"]
    assert calls["n"] == 1

    def private_dns(host):
        return [ipaddress.ip_address("10.1.1.1")]

    dns_client, _dns_app = client_for(
        tmp_path / "dns",
        handler,
        resolver=private_dns,
    )
    dns = dns_client.post("/api/tracks", json={"url": "https://cdn.example.com/secret.mp3"})
    assert dns.status_code == 400
    assert "isn't allowed" in dns.json()["detail"]

    redirect_calls = {"n": 0}

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        redirect_calls["n"] += 1
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret.wav"})

    redirect_client, redirect_app = client_for(tmp_path / "redir", redirect_handler)
    redirected = redirect_client.post("/api/tracks", json={"url": "https://cdn.example.com/start.mp3"})
    assert redirected.status_code == 400
    assert "isn't allowed" in redirected.json()["detail"]
    assert redirect_app.state.library.list_tracks() == []
    assert redirect_calls["n"] == 1
    assert list(redirect_app.state.library.media.iterdir()) == []
    assert application.state.library.list_tracks() == []


def test_enforces_size_limit_and_does_not_keep_a_partial_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=b"RIFFxxxx" + b"\x00" * 200)

    client, application = client_for(tmp_path, handler, max_bytes=32)
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/big.wav"})
    assert response.status_code == 413
    assert "limit" in response.json()["detail"]
    assert "Nothing was saved" in response.json()["detail"]
    assert application.state.library.list_tracks() == []
    assert list(application.state.library.media.iterdir()) == []


def test_missing_url_field(tmp_path):
    client, _application = client_for(tmp_path, lambda request: httpx.Response(500))
    response = client.post("/api/tracks", json={})
    assert response.status_code == 422


def test_no_streaming_rippers_in_requirements():
    text = Path("requirements.txt").read_text(encoding="utf-8").lower()
    for banned in ("yt-dlp", "youtube-dl", "pytube", "spotdl", "ytmusic"):
        assert banned not in text


def test_delete_unknown_track(tmp_path):
    client, _application = client_for(tmp_path, lambda request: httpx.Response(500))
    response = client.delete("/api/tracks/not-a-uuid")
    assert response.status_code == 404
