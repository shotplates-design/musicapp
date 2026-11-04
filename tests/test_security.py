import asyncio
import io
import ipaddress
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from app.download import LinkError, normalize_audio_url, signature_matches
from app.main import create_app

PUBLIC = ipaddress.ip_address("93.184.216.34")
PUBLIC_V6 = ipaddress.ip_address("2001:4860:4860::8888")


def make_wav(seconds=0.2, rate=8000) -> bytes:
    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def client_for(tmp_path, handler, *, resolver=None, max_bytes=80 * 1024 * 1024, **app_kwargs):
    if resolver is None:
        resolver = lambda host: [PUBLIC]
    application = create_app(
        tmp_path / "lib",
        resolver=resolver,
        transport=httpx.MockTransport(handler),
        max_bytes=max_bytes,
        **app_kwargs,
    )
    return TestClient(
        application,
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    ), application


def test_pins_the_validated_address_when_dns_changes(tmp_path):
    wav = make_wav()
    answers = iter([[PUBLIC], [ipaddress.ip_address("127.0.0.1")]])
    seen = []

    def resolver(host):
        return next(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((
            request.url.host,
            request.headers.get("host"),
            request.extensions.get("sni_hostname"),
        ))
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)

    client, _application = client_for(tmp_path, handler, resolver=resolver)
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 200, response.text
    assert seen == [("93.184.216.34", "cdn.example.com", "cdn.example.com")]
    art_len = int(response.headers["x-pixel-artwork-length"])
    assert response.content[art_len:] == wav
    assert next(answers) == [ipaddress.ip_address("127.0.0.1")]


def test_rejects_a_lookup_that_includes_a_private_address(tmp_path):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(500)

    def resolver(host):
        return [PUBLIC, ipaddress.ip_address("10.0.0.1")]

    client, application = client_for(tmp_path, handler, resolver=resolver)
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.mp3"})
    assert response.status_code == 400
    assert "isn't allowed" in response.json()["detail"]
    assert called["n"] == 0
    assert list(application.state.scratch.iterdir()) == []


def test_connects_to_a_public_ipv6_answer(tmp_path):
    wav = make_wav()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["header"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)

    client, _application = client_for(tmp_path, handler, resolver=lambda host: [PUBLIC_V6])
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 200, response.text
    assert seen == {
        "host": "2001:4860:4860::8888",
        "header": "cdn.example.com",
        "sni": "cdn.example.com",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://[fd00::1]/a.mp3",
        "http://[fc00::1]/a.mp3",
        "http://[::1]/a.mp3",
        "http://[::ffff:127.0.0.1]/a.mp3",
        "http://[::ffff:10.1.1.1]/a.mp3",
        "http://[::ffff:192.168.0.1]/a.mp3",
        "http://[::ffff:169.254.169.254]/a.mp3",
        "http://0x7f.0.0.1/a.mp3",
        "http://0x7f.1/a.mp3",
        "http://0x7f000001/a.mp3",
        "http://0177.0.0.1/a.mp3",
        "http://2130706433/a.mp3",
        "http://127.1/a.mp3",
        "http://127.0.1/a.wav",
    ],
)
def test_rejects_non_global_address_spellings(url):
    with pytest.raises(LinkError, match="isn't allowed"):
        normalize_audio_url(url)


def test_allows_a_global_numeric_address():
    assert normalize_audio_url("http://8.8.8.8/a.mp3").startswith("http://8.8.8.8/")
    assert normalize_audio_url("http://[2001:4860:4860::8888]/a.wav").startswith("http://[2001:4860:4860::8888]/")


def test_signature_accepts_each_audio_type():
    assert signature_matches(b"ID3" + b"\x00" * 8, "mp3")
    assert signature_matches(bytes([0xFF, 0xFB, 0x90, 0x00]), "mp3")
    assert signature_matches(b"\x00\x00\x00\x18ftypM4A ", "m4a")
    assert signature_matches(b"OggS" + b"\x00" * 8, "ogg")
    assert signature_matches(b"OggS" + b"\x00" * 8, "opus")
    assert signature_matches(b"fLaC" + b"\x00" * 8, "flac")
    assert signature_matches(b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE", "wav")
    assert signature_matches(bytes([0xFF, 0xF1, 0x50, 0x80]), "aac")
    assert signature_matches(bytes([0xFF, 0xF9, 0x50, 0x80]), "aac")
    assert not signature_matches(b"<html>nope</html>", "wav")
    assert not signature_matches(b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE", "mp3")
    assert not signature_matches(bytes([0xFF, 0xFB, 0x90, 0x00]), "aac")


def test_rejects_a_body_whose_bytes_do_not_match_the_content_type(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=b"<html>not audio</html>")

    client, application = client_for(tmp_path, handler)
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 400
    assert "doesn't match" in response.json()["detail"]
    assert list(application.state.scratch.iterdir()) == []


def test_rejects_when_the_body_is_longer_than_a_lying_content_length(tmp_path):
    wav = make_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/wav", "Content-Length": "10"},
            content=wav,
        )

    client, application = client_for(tmp_path, handler, max_bytes=80)
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 413
    assert "Nothing was saved" in response.json()["detail"]
    assert list(application.state.scratch.iterdir()) == []


def test_counts_bytes_when_content_length_is_omitted(tmp_path):
    wav = make_wav()

    def over(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)
        del response.headers["content-length"]
        return response

    client, application = client_for(tmp_path, over, max_bytes=80)
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/big.wav"})
    assert response.status_code == 413
    assert list(application.state.scratch.iterdir()) == []

    def under(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)
        del response.headers["content-length"]
        return response

    client, application = client_for(tmp_path / "ok", under, max_bytes=len(wav) + 10)
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 200, response.text
    art_len = int(response.headers["x-pixel-artwork-length"])
    assert response.content[art_len:] == wav


def test_stored_size_is_the_body_not_a_short_content_length(tmp_path):
    wav = make_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/wav", "Content-Length": "5"},
            content=wav,
        )

    client, _application = client_for(tmp_path, handler, max_bytes=len(wav) + 10)
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 200, response.text
    art_len = int(response.headers["x-pixel-artwork-length"])
    assert response.content[art_len:] == wav


def test_fetch_rejects_other_origins_and_limits_each_address(tmp_path):
    wav = make_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)

    client, application = client_for(tmp_path, handler)
    missing = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"}, headers={"Origin": "", "Sec-Fetch-Site": ""})
    assert missing.status_code == 403
    assert "this app" in missing.json()["detail"]

    cross = client.post(
        "/api/fetch",
        json={"url": "https://cdn.example.com/song.wav"},
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross.status_code == 403

    for _ in range(10):
        ok = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"})
        assert ok.status_code == 200, ok.text
    limited = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.wav"})
    assert limited.status_code == 429
    assert "Wait a minute" in limited.json()["detail"]
    assert list(application.state.scratch.iterdir()) == []


def test_fetch_deadline_stops_a_slow_download(tmp_path, monkeypatch):
    async def hang(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr("app.main.download_audio", hang)
    application = create_app(tmp_path / "slow", fetch_timeout=0.05)
    client = TestClient(
        application,
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    response = client.post("/api/fetch", json={"url": "https://cdn.example.com/song.mp3"})
    assert response.status_code == 504
    assert "took too long" in response.json()["detail"]
    assert list(application.state.scratch.iterdir()) == []
