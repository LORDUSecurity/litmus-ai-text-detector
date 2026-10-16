"""WSGI entry point: `flask --app backend.wsgi run --port 8787`.

The purge thread belongs to a serving process, not to an import, so it starts here.
"""
from .app import app, purger

purger.start()

__all__ = ["app"]
