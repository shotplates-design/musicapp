"""Serve the pixel player and the direct-link library."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.download import DEFAULT_MAX_BYTES, LinkError, download_audio, normalize_audio_url
from app.library import Library, TrackNotFound, title_from_filename, total_bytes
from app.tags import read_tags

STATIC = Path(__file__).resolve().parent / "static"


class LinkIn(BaseModel):
    url: str


def create_app(
    library_dir: Path | None = None,
    *,
    resolver=None,
    transport=None,
    max_bytes: int | None = None,
) -> FastAPI:
    root = Path(library_dir) if library_dir is not None else Path(os.environ.get("PIXEL_LIBRARY", "library"))
    library = Library(root)
    app = FastAPI(title="Pixel Music", docs_url=None, redoc_url=None)
    app.state.library = library
    app.state.resolver = resolver
    app.state.transport = transport
    app.state.max_bytes = int(os.environ.get("PIXEL_MAX_BYTES", DEFAULT_MAX_BYTES) if max_bytes is None else max_bytes)

    @app.exception_handler(LinkError)
    async def link_error(_request, exc: LinkError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.get("/api/tracks")
    def list_tracks():
        tracks = library.list_tracks()
        return JSONResponse(
            {"tracks": tracks, "totalBytes": total_bytes(tracks)},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/tracks")
    async def add_track(body: LinkIn):
        normalized = normalize_audio_url(body.url)
        existing = library.find_by_source_url(normalized)
        if existing:
            return JSONResponse({"track": existing, "created": False}, status_code=200)
        tmp = library.media / f".partial-{uuid.uuid4().hex}"
        try:
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
            artwork = tags.get("artwork")
            art_bytes = artwork[0] if artwork and artwork[0] else None
            art_mime = artwork[1] if artwork else None
            track, created = library.adopt_file(
                tmp,
                source_url=normalized,
                filename=downloaded.filename,
                mime_type=downloaded.mime_type,
                title=tags.get("title") or title_from_filename(downloaded.filename),
                artist=tags.get("artist") or "Unknown artist",
                duration=tags.get("duration"),
                artwork=art_bytes,
                artwork_mime=art_mime,
            )
        finally:
            tmp.unlink(missing_ok=True)
        return JSONResponse({"track": track, "created": created}, status_code=201 if created else 200)

    @app.delete("/api/tracks/{track_id}")
    def delete_track(track_id: str):
        try:
            library.delete(track_id)
        except TrackNotFound:
            raise HTTPException(status_code=404, detail="That track isn't in the library.") from None
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.get("/api/tracks/{track_id}/audio")
    def track_audio(track_id: str):
        track = _track_or_404(library, track_id)
        path = library.audio_path(track)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="That audio file is missing from storage.")
        return FileResponse(
            path,
            media_type=track.get("mimeType") or "application/octet-stream",
            filename=track.get("filename") or path.name,
            content_disposition_type="inline",
        )

    @app.get("/api/tracks/{track_id}/artwork")
    def track_artwork(track_id: str):
        track = _track_or_404(library, track_id)
        path = library.artwork_path(track)
        if not track.get("artworkSize") or not path.is_file():
            raise HTTPException(status_code=404, detail="That track has no artwork.")
        return FileResponse(path, media_type=track.get("artworkMime") or "application/octet-stream")

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


def _track_or_404(library: Library, track_id: str) -> dict:
    try:
        return library.get(track_id)
    except TrackNotFound:
        raise HTTPException(status_code=404, detail="That track isn't in the library.") from None


app = create_app()
