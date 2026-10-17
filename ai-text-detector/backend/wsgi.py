"""WSGI entry point: `flask --app backend.wsgi run --port 8787`.

The purge thread belongs to a serving process, not to an import, so it starts here. So does
the discovery worker: it spends a user's API budget on real provider calls, which is not
something an import (or a test client) should be able to trigger.
"""
from . import discovery
from .app import app, purger

purger.start()
discovery.worker.start()

__all__ = ["app"]
