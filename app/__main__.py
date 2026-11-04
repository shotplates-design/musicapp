"""Run the player. The bind address and the token stay in the environment.

PIXEL_HOST defaults to 127.0.0.1. Set PIXEL_TOKEN before using any other host,
or before publishing a tunnel to this port. The token is not read from a file
in the repo.
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
