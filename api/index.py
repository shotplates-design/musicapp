"""Vercel loads this module. The app only checks a link and streams the file."""

from app.main import app

__all__ = ["app"]
