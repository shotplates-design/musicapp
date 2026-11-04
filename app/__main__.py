"""Run the player.

PIXEL_HOST defaults to 127.0.0.1. Any other host has no login: anyone who can
reach the process can add and delete tracks and use the fetcher. create_app
prints that warning when the bind address is not loopback.
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
