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
    return TestClient(application), application


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
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 201, response.text
    assert seen == [("93.184.216.34", "cdn.example.com", "cdn.example.com")]
    assert response.json()["track"]["byteSize"] == len(wav)
    assert next(answers) == [ipaddress.ip_address("127.0.0.1")]


def test_rejects_a_lookup_that_includes_a_private_address(tmp_path):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(500)

    def resolver(host):
        return [PUBLIC, ipaddress.ip_address("10.0.0.1")]

    client, application = client_for(tmp_path, handler, resolver=resolver)
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.mp3"})
    assert response.status_code == 400
    assert "isn't allowed" in response.json()["detail"]
    assert called["n"] == 0
    assert application.state.library.list_tracks() == []


def test_connects_to_a_public_ipv6_answer(tmp_path):
    wav = make_wav()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["header"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)

    client, _application = client_for(tmp_path, handler, resolver=lambda host: [PUBLIC_V6])
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 201, response.text
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
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 400
    assert "doesn't match" in response.json()["detail"]
    assert application.state.library.list_tracks() == []
    assert list(application.state.library.media.iterdir()) == []


def test_rejects_when_the_body_is_longer_than_a_lying_content_length(tmp_path):
    wav = make_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/wav", "Content-Length": "10"},
            content=wav,
        )

    client, application = client_for(tmp_path, handler, max_bytes=80)
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 413
    assert "Nothing was saved" in response.json()["detail"]
    assert application.state.library.list_tracks() == []


def test_counts_bytes_when_content_length_is_omitted(tmp_path):
    wav = make_wav()

    def over(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)
        del response.headers["content-length"]
        return response

    client, application = client_for(tmp_path, over, max_bytes=80)
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/big.wav"})
    assert response.status_code == 413
    assert application.state.library.list_tracks() == []

    def under(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)
        del response.headers["content-length"]
        return response

    client, application = client_for(tmp_path / "ok", under, max_bytes=len(wav) + 10)
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 201, response.text
    assert response.json()["track"]["byteSize"] == len(wav)


def test_stored_size_is_the_body_not_a_short_content_length(tmp_path):
    wav = make_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/wav", "Content-Length": "5"},
            content=wav,
        )

    client, _application = client_for(tmp_path, handler, max_bytes=len(wav) + 10)
    response = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.wav"})
    assert response.status_code == 201, response.text
    assert response.json()["track"]["byteSize"] == len(wav)


def test_exposed_server_requires_a_token_and_localhost_does_not(tmp_path):
    with pytest.raises(RuntimeError, match="PIXEL_TOKEN"):
        create_app(tmp_path / "open", bind_host="0.0.0.0")

    wav = make_wav()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)

    client, _application = client_for(tmp_path, handler, bind_host="0.0.0.0", token="s3cret")
    assert client.get("/").status_code == 200
    assert client.get("/api/tracks").status_code == 401
    assert client.post("/api/tracks", json={"url": "https://cdn.example.com/song.wav"}).status_code == 401

    headers = {"Authorization": "Bearer s3cret"}
    created = client.post("/api/tracks", json={"url": "https://cdn.example.com/song.wav"}, headers=headers)
    assert created.status_code == 201, created.text
    track_id = created.json()["track"]["id"]
    assert client.get("/api/tracks", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/tracks", headers=headers).status_code == 200
    assert client.get(f"/api/tracks/{track_id}/audio").status_code == 401
    assert client.get(f"/api/tracks/{track_id}/artwork").status_code == 401
    audio = client.get(f"/api/tracks/{track_id}/audio", headers=headers)
    assert audio.status_code == 200
    client.cookies.set("pixel_token", "s3cret")
    cookie = client.get(f"/api/tracks/{track_id}/audio")
    assert cookie.status_code == 200
    client.cookies.clear()
    query = client.get(f"/api/tracks/{track_id}/audio?token=s3cret")
    assert query.status_code == 200
    assert client.delete(f"/api/tracks/{track_id}").status_code == 401
    assert client.delete(f"/api/tracks/{track_id}", headers=headers).status_code == 200

    local, _local_app = client_for(tmp_path / "local", handler, bind_host="127.0.0.1")
    assert local.get("/api/tracks").status_code == 200

    tunneled, _tunneled_app = client_for(tmp_path / "tunnel", handler, bind_host="127.0.0.1", token="s3cret")
    assert tunneled.get("/api/tracks").status_code == 401
    assert tunneled.get("/api/tracks", headers=headers).status_code == 200
