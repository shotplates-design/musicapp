"""Make `pytest` import the app package from the repository root."""

from types import SimpleNamespace


def stub_playerprops(monkeypatch, media_url="https://cdn.example.com/song.mp4", **extra):
    """Make /api/fetch resolve a playerprops page to one media URL. No network."""
    info = {
        "url": media_url,
        "ext": "mp4",
        "protocol": "https",
        "title": "Track",
        "vcodec": "none",
    }
    info.update(extra)

    class FakeYDL:
        params = None
        url = None
        download = None

        def __init__(self, params):
            FakeYDL.params = params

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            FakeYDL.url = url
            FakeYDL.download = download
            return dict(info)

    monkeypatch.setattr("app.playerprops._yt_dlp", lambda: SimpleNamespace(YoutubeDL=FakeYDL))
    return FakeYDL
