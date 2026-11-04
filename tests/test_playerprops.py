import io
import ipaddress
import socket
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.playerprops import is_playerprops_host

PUBLIC = ipaddress.ip_address("93.184.216.34")


def make_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


class Capture:
    def __init__(self, info):
        self.info = info
        self.params = None
        self.url = None
        self.download = None
        self.calls = 0

    def factory(self):
        outer = self

        class FakeYDL:
            def __init__(self, params):
                outer.calls += 1
                outer.params = params

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                outer.url = url
                outer.download = download
                info = outer.info
                if isinstance(info, Exception):
                    raise info
                return info

        return FakeYDL


def client_for(tmp_path, handler, monkeypatch, info, *, resolver=None, max_bytes=80 * 1024 * 1024):
    capture = Capture(info)
    monkeypatch.setattr(
        "app.playerprops._yt_dlp",
        lambda: SimpleNamespace(YoutubeDL=capture.factory()),
    )
    if resolver is None:
        resolver = lambda host: [PUBLIC]
    application = create_app(
        tmp_path / "lib",
        resolver=resolver,
        transport=httpx.MockTransport(handler),
        max_bytes=max_bytes,
    )
    client = TestClient(
        application,
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    return client, application, capture


def wav_handler(wav):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)

    return handler


@pytest.mark.parametrize(
    "host",
    [
        "playerprops.com",
        "PLAYERPROPS.COM",
        "www.playerprops.com",
        "a.b.playerprops.com",
        "playerprops.com.",
    ],
)
def test_playerprops_host_allows_the_site_and_its_subdomains(host):
    assert is_playerprops_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "playerprops.com.evil.net",
        "www.playerprops.com.evil.net",
        "notplayerprops.com",
        "evilplayerprops.com",
        "playerprops.com.evil.com",
        "playerprops.co",
        "playerprops.com.com",
        "prefixplayerprops.com",
        "soundcloud.com",
        "www.youtube.com",
        "",
    ],
)
def test_playerprops_host_rejects_lookalikes(host):
    assert not is_playerprops_host(host)


def test_playerprops_page_uses_yt_dlp_and_checks_the_media_file(tmp_path, monkeypatch):
    wav = make_wav()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("host"), request.extensions.get("sni_hostname")))
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=wav)

    client, application, capture = client_for(
        tmp_path,
        handler,
        monkeypatch,
        {
            "title": "Night Shift",
            "ext": "wav",
            "protocol": "https",
            "url": "https://cdn.example.com/media/track",
            "has_drm": False,
        },
    )
    response = client.post("/api/fetch", json={"url": "https://www.playerprops.com/watch/night"})
    assert response.status_code == 200, response.text
    assert capture.calls == 1
    assert capture.download is False
    assert capture.url == "https://www.playerprops.com/watch/night"
    assert capture.params["format"] == "bestaudio[ext=m4a]/bestaudio/best"
    assert capture.params["noplaylist"] is True
    assert capture.params["fixup"] == "never"
    assert capture.params["postprocessors"] == []
    assert capture.params["allowed_extractors"] == ["generic"]
    assert seen == [("93.184.216.34", "cdn.example.com", "cdn.example.com")]
    assert response.headers["x-pixel-filename"] == "Night%20Shift.m4a"
    assert response.headers["x-pixel-mime"] == "audio/mp4"
    art_len = int(response.headers["x-pixel-artwork-length"])
    assert response.content[art_len:] == wav
    assert list(application.state.scratch.iterdir()) == []


def test_other_hosts_do_not_use_yt_dlp(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    client, _application, capture = client_for(tmp_path, handler, monkeypatch, {})
    for url in (
        "https://cdn.example.com/song.wav",
        "https://www.youtube.com/watch?v=abc",
        "https://soundcloud.com/artist/track",
        "https://open.spotify.com/track/abc",
        "https://playerprops.com.evil.net/song.mp3",
        "https://playerprops.com.evil.net/watch",
    ):
        refused = client.post("/api/fetch", json={"url": url})
        assert refused.status_code == 400, url
        assert refused.json()["detail"] == "Only playerprops.com links are supported."
    assert capture.calls == 0


def test_playerprops_rejects_drm_private_media_and_streams(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    drm_client, drm_app, _capture = client_for(
        tmp_path / "drm",
        handler,
        monkeypatch,
        {"has_drm": True, "ext": "wav", "url": "https://cdn.example.com/song.wav", "protocol": "https"},
    )
    drm = drm_client.post("/api/fetch", json={"url": "https://playerprops.com/track/1"})
    assert drm.status_code == 400
    assert "DRM" in drm.json()["detail"]
    assert list(drm_app.state.scratch.iterdir()) == []

    private_client, private_app, _capture = client_for(
        tmp_path / "private",
        handler,
        monkeypatch,
        {"ext": "wav", "url": "http://127.0.0.1/secret.wav", "protocol": "http"},
    )
    private = private_client.post("/api/fetch", json={"url": "https://tracks.playerprops.com/secret"})
    assert private.status_code == 400
    assert "isn't allowed" in private.json()["detail"]
    assert list(private_app.state.scratch.iterdir()) == []

    def resolver(host):
        if host == "cdn.example.com":
            return [ipaddress.ip_address("10.1.1.1")]
        return [PUBLIC]

    dns_client, dns_app, _capture = client_for(
        tmp_path / "dns",
        handler,
        monkeypatch,
        {"ext": "wav", "url": "https://cdn.example.com/song.wav", "protocol": "https"},
        resolver=resolver,
    )
    dns = dns_client.post("/api/fetch", json={"url": "https://playerprops.com/track/2"})
    assert dns.status_code == 400
    assert "isn't allowed" in dns.json()["detail"]
    assert list(dns_app.state.scratch.iterdir()) == []

    stream_client, _stream_app, _capture = client_for(
        tmp_path / "stream",
        handler,
        monkeypatch,
        {"ext": "mp4", "url": "https://cdn.example.com/playlist.m3u8", "protocol": "m3u8_native"},
    )
    stream = stream_client.post("/api/fetch", json={"url": "https://playerprops.com/live"})
    assert stream.status_code == 400
    assert "ffmpeg" in stream.json()["detail"]


def test_playerprops_media_redirect_to_a_private_host_is_refused(tmp_path, monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret.wav"})

    client, application, _capture = client_for(
        tmp_path,
        handler,
        monkeypatch,
        {"ext": "wav", "title": "Hidden", "url": "https://cdn.example.com/start.wav", "protocol": "https"},
    )
    response = client.post("/api/fetch", json={"url": "https://playerprops.com/track/3"})
    assert response.status_code == 400
    assert "isn't allowed" in response.json()["detail"]
    assert calls["n"] == 1
    assert list(application.state.scratch.iterdir()) == []


def test_playerprops_saves_mp4_and_enforces_the_size_cap(tmp_path, monkeypatch):
    payload = b"not-a-media-header"

    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/octet-stream"}, content=payload)

    client, application, _capture = client_for(
        tmp_path / "any",
        html,
        monkeypatch,
        {"ext": "webm", "vcodec": "vp9", "url": "https://cdn.example.com/song.wav", "protocol": "https", "title": "Clip"},
    )
    saved = client.post("/api/fetch", json={"url": "https://playerprops.com/track/4"})
    assert saved.status_code == 200, saved.text
    assert saved.headers["x-pixel-mime"] == "audio/mp4"
    assert saved.headers["x-pixel-filename"] == "Clip.m4a"
    art_len = int(saved.headers["x-pixel-artwork-length"])
    assert saved.content[art_len:] == payload
    assert list(application.state.scratch.iterdir()) == []

    def big(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "audio/wav"}, content=b"RIFFxxxx" + b"\x00" * 200)

    big_client, big_app, _capture = client_for(
        tmp_path / "big",
        big,
        monkeypatch,
        {"ext": "wav", "url": "https://cdn.example.com/big.wav", "protocol": "https"},
        max_bytes=32,
    )
    limited = big_client.post("/api/fetch", json={"url": "https://playerprops.com/track/5"})
    assert limited.status_code == 413
    assert "Nothing was saved" in limited.json()["detail"]
    assert list(big_app.state.scratch.iterdir()) == []


def test_credentials_on_a_playerprops_link_are_refused(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    client, _application, capture = client_for(tmp_path, handler, monkeypatch, {})
    response = client.post("/api/fetch", json={"url": "https://user:secret@playerprops.com/track"})
    assert response.status_code == 400
    assert "password" in response.json()["detail"]
    assert capture.calls == 0


def test_playerprops_links_cannot_include_a_port(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    client, _application, capture = client_for(tmp_path, handler, monkeypatch, {})
    response = client.post("/api/fetch", json={"url": "https://playerprops.com:443/track"})
    assert response.status_code == 400
    assert "port" in response.json()["detail"]
    assert capture.calls == 0


def _pinned(handler, resolver=None, deadline=None):
    from app.playerprops import make_pinned_handler

    seen = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("host"), request.extensions.get("sni_hostname")))
        return handler(request)

    return make_pinned_handler(
        resolver=resolver or (lambda host: [PUBLIC]),
        deadline=time.monotonic() + 5 if deadline is None else deadline,
        transport=httpx.MockTransport(transport),
    ), seen


def test_real_request_url_is_checked_before_any_connection():
    from yt_dlp.networking import Request
    from yt_dlp.networking.exceptions import RequestError

    req = Request("http://127.0.0.1/secret")
    assert req.url.startswith("http://127.0.0.1")
    assert not hasattr(req, "full_url")
    handler, seen = _pinned(lambda request: httpx.Response(200, text="nope"))
    with pytest.raises(RequestError) as caught:
        handler.send(req)
    assert seen == []
    assert "isn't allowed" in str(caught.value.__cause__)


def test_pinned_handler_keeps_the_first_address_and_rechecks_redirects():
    from yt_dlp.networking import Request
    from yt_dlp.networking.exceptions import RequestError

    answers = [[PUBLIC], [ipaddress.ip_address("127.0.0.1")]]

    def resolver(host):
        assert host == "a.playerprops.com"
        return answers.pop(0)

    def page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

    handler, seen = _pinned(page, resolver=resolver)
    with pytest.raises(RequestError) as caught:
        handler.send(Request("https://a.playerprops.com/watch"))
    assert seen == [("93.184.216.34", "a.playerprops.com", "a.playerprops.com")]
    assert answers == [[ipaddress.ip_address("127.0.0.1")]]
    assert "isn't allowed" in str(caught.value.__cause__)


def test_pinned_handler_refuses_a_redirect_off_playerprops():
    from yt_dlp.networking import Request
    from yt_dlp.networking.exceptions import RequestError

    def page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://www.youtube.com/watch?v=abc"})

    handler, seen = _pinned(page)
    with pytest.raises(RequestError) as caught:
        handler.send(Request("https://www.playerprops.com/embed"))
    assert len(seen) == 1
    assert "YouTube" in str(caught.value.__cause__)


def test_extract_request_stops_when_the_deadline_has_passed():
    from yt_dlp.networking import Request
    from yt_dlp.networking.exceptions import RequestError

    def page(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    handler, seen = _pinned(page, deadline=time.monotonic() - 1)
    with pytest.raises(RequestError) as caught:
        handler.send(Request("https://playerprops.com/watch"))
    assert seen == []
    assert caught.value.__cause__.status == 504


def test_trickling_page_stops_at_the_deadline():
    from yt_dlp.networking import Request
    from yt_dlp.networking.exceptions import RequestError

    from app.playerprops import make_pinned_handler

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(40):
                time.sleep(0.2)
                yield b"0123456789"

        def close(self):
            return None

    class SlowTransport(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, stream=SlowBody())

    started = time.monotonic()
    handler = make_pinned_handler(
        resolver=lambda host: [PUBLIC],
        deadline=started + 0.5,
        transport=SlowTransport(),
    )
    with pytest.raises(RequestError) as caught:
        handler.send(Request("https://playerprops.com/watch"))
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert caught.value.__cause__.status == 504


def test_urlopen_does_not_connect_to_a_loopback_probe(tmp_path):
    import yt_dlp
    from yt_dlp.networking import Request

    from app.playerprops import _checking_ydl, ydl_params

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.3)
    port = listener.getsockname()[1]
    try:
        cls = _checking_ydl(yt_dlp.YoutubeDL, lambda host: [PUBLIC], time.monotonic() + 2, None)
        with cls(ydl_params(Path(tmp_path))) as ydl:
            with pytest.raises(Exception) as caught:
                ydl.urlopen(Request(f"http://127.0.0.1:{port}/secret"))
        assert "isn't allowed" in str(caught.value)
        with pytest.raises(socket.timeout):
            listener.accept()
    finally:
        listener.close()


def test_only_the_generic_extractor_is_enabled(tmp_path):
    import yt_dlp

    from app.playerprops import _checking_ydl, ydl_params

    cls = _checking_ydl(yt_dlp.YoutubeDL, lambda host: [PUBLIC], time.monotonic() + 2, None)
    with cls(ydl_params(tmp_path)) as ydl:
        names = sorted(name.lower() for name in ydl._ies)
    assert names == ["generic"]
