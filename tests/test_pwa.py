from fastapi.testclient import TestClient

from app.main import create_app


def test_ios_shell(tmp_path):
    client = TestClient(create_app(tmp_path / "lib"))
    html = client.get("/").text
    assert client.get("/index.html").status_code == 200
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html
    assert 'name="mobile-web-app-capable" content="yes"' in html
    assert 'name="apple-mobile-web-app-status-bar-style" content="black-translucent"' in html
    assert 'name="apple-mobile-web-app-title" content="Pixel Music"' in html
    assert "viewport-fit=cover" in html
    assert 'rel="apple-touch-icon"' in html
    assert "manifest.json" in html
    assert "Only add audio you have rights to." in html
    assert 'playsinline' in html

    manifest = client.get("/manifest.json")
    assert manifest.status_code == 200
    assert "manifest" in manifest.headers["content-type"]
    body = manifest.json()
    assert body["display"] == "standalone"
    assert body["start_url"] == "./"
    assert body["background_color"] == "#1a1030"
    assert body["theme_color"] == "#1a1030"
    assert any(icon["src"].endswith("apple-touch-icon.png") for icon in body["icons"])
    assert any(icon["purpose"] == "maskable" for icon in body["icons"])

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert "javascript" in worker.headers["content-type"]
    assert "no-cache" in worker.headers["cache-control"]
    assert "/api/" in worker.text
    assert "apple-touch-icon.png" in worker.text
    assert "./index.html" in worker.text

    icon = client.get("/icons/apple-touch-icon.png")
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"
    assert icon.content.startswith(b"\x89PNG\r\n\x1a\n")

    css = client.get("/css/app.css")
    assert css.status_code == 200
    assert "#1a1030" in css.text
    assert "#ff4d8d" in css.text
    assert "Press Start 2P" in css.text
    assert "safe-area-inset" in css.text

    script = client.get("/js/app.js")
    assert script.status_code == 200
    assert "mediaSession" in script.text
    assert "setActionHandler" in script.text
    font = client.get("/fonts/PressStart2P-Regular.woff2")
    assert font.status_code == 200
    assert len(font.content) > 1000
