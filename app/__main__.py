"""Run the player.

PIXEL_HOST defaults to 127.0.0.1. Songs stay in the browser. Anyone who can
open the site can ask this process to fetch a file URL.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("PIXEL_HOST", "127.0.0.1")
    port = int(os.environ.get("PIXEL_PORT", "8000"))
    kwargs = {"host": host, "port": port}
    cert = os.environ.get("PIXEL_SSL_CERTFILE")
    key = os.environ.get("PIXEL_SSL_KEYFILE")
    if cert and key:
        kwargs["ssl_certfile"] = cert
        kwargs["ssl_keyfile"] = key
    uvicorn.run("app.main:app", **kwargs)


if __name__ == "__main__":
    main()
