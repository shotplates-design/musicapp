"""Serve the pixel player and the direct-link library."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.download import DEFAULT_MAX_BYTES, LinkError, download_audio, normalize_audio_url
from app.guard import SlidingRateLimit, client_address, same_origin
from app.library import title_from_filename
from app.tags import read_tags

STATIC = Path(__file__).resolve().parent / "static"
FETCH_TIMEOUT_SECONDS = 20.0
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60.0


class LinkIn(BaseModel):
    url: str


def create_app(
    library_dir: Path | None = None,
    *,
    resolver=None,
    transport=None,
    max_bytes: int | None = None,
    fetch_timeout: float = FETCH_TIMEOUT_SECONDS,
) -> FastAPI:
    scratch_root = Path(library_dir) if library_dir is not None else Path(tempfile.mkdtemp(prefix="pixel-music-"))
    scratch = scratch_root / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Pixel Music", docs_url=None, redoc_url=None)
    app.state.scratch = scratch
    app.state.resolver = resolver
    app.state.transport = transport
    app.state.fetch_timeout = fetch_timeout
    app.state.limiter = SlidingRateLimit(RATE_LIMIT, RATE_WINDOW_SECONDS)
    app.state.max_bytes = int(os.environ.get("PIXEL_MAX_BYTES", DEFAULT_MAX_BYTES) if max_bytes is None else max_bytes)

    @app.exception_handler(LinkError)
    async def link_error(_request, exc: LinkError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.post("/api/fetch")
    async def fetch_audio(body: LinkIn, request: Request):
        """Validate a direct file link and stream it back. Nothing is stored."""
        if not same_origin(request):
            raise LinkError("Fetch only works from this app.", status=403)
        normalized = normalize_audio_url(body.url)
        if not app.state.limiter.allow(client_address(request)):
            raise LinkError("Too many downloads from this address. Wait a minute and try again.", status=429)
        tmp = scratch / f"{uuid.uuid4().hex}.bin"
        try:
            async with asyncio.timeout(app.state.fetch_timeout):
                downloaded = await download_audio(
                    normalized,
                    tmp,
                    resolver=app.state.resolver or _resolver(),
                    transport=app.state.transport,
                    max_bytes=app.state.max_bytes,
                )
                try:
                    tags = read_tags(tmp)
                except Exception:
                    tags = {"title": "", "artist": "", "duration": None, "artwork": None}
        except TimeoutError:
            tmp.unlink(missing_ok=True)
            raise LinkError("That download took too long. Nothing was saved.", status=504) from None
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        audio_size = tmp.stat().st_size
        artwork = tags.get("artwork")
        art_bytes = b""
        art_mime = ""
        if artwork and artwork[0]:
            candidate = bytes(artwork[0])
            if len(candidate) + audio_size <= app.state.max_bytes:
                art_bytes = candidate
                art_mime = artwork[1] or "image/jpeg"
        duration = tags.get("duration")
        headers = {
            "Cache-Control": "no-store",
            "X-Pixel-Title": quote(tags.get("title") or title_from_filename(downloaded.filename), safe=""),
            "X-Pixel-Artist": quote(tags.get("artist") or "Unknown artist", safe=""),
            "X-Pixel-Duration": "" if not duration else str(duration),
            "X-Pixel-Filename": quote(downloaded.filename, safe=""),
            "X-Pixel-Mime": downloaded.mime_type,
            "X-Pixel-Source-Url": quote(normalized, safe=""),
            "X-Pixel-Artwork-Length": str(len(art_bytes)),
            "X-Pixel-Artwork-Mime": art_mime,
            "X-Pixel-Audio-Length": str(audio_size),
        }

        async def chunks():
            try:
                if art_bytes:
                    yield art_bytes
                with tmp.open("rb") as handle:
                    while True:
                        block = await asyncio.to_thread(handle.read, 65536)
                        if not block:
                            break
                        yield block
            finally:
                tmp.unlink(missing_ok=True)

        return StreamingResponse(chunks(), media_type="application/octet-stream", headers=headers)

    @app.get("/")
    @app.get("/index.html")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(
            STATIC / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/manifest.json")
    def manifest():
        return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")

    app.mount("/css", StaticFiles(directory=STATIC / "css"), name="css")
    app.mount("/js", StaticFiles(directory=STATIC / "js"), name="js")
    app.mount("/fonts", StaticFiles(directory=STATIC / "fonts"), name="fonts")
    app.mount("/icons", StaticFiles(directory=STATIC / "icons"), name="icons")
    return app


def _resolver():
    from app.download import default_resolver

    return default_resolver


app = create_app()
