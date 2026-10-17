"""Port of the Node gateway's execution-row purger.

n8n writes the webhook's inbound request record — including any BYOK header — into
its SQLite database when an execution starts, and turning run-data saving off does
not remove it (measured 2026-10-06). So the process that fronts the API also deletes
those rows a second later.

Two details carried over from the Node original, both learned the hard way:
  * `PRAGMA foreign_keys = ON`, or `execution_data` survives the parent delete.
  * scope by `max(id)` captured at startup, not by timestamp — n8n stores `startedAt`
    in the machine's local frame while Python's UTC clock is not, so a time-based
    floor silently matches nothing.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time

N8N_DB = os.environ.get("N8N_DB", "C:/Users/adam/n8n-runtime/.n8n-data/.n8n/database.sqlite")
WORKFLOW_ID = os.environ.get("DETECTOR_WORKFLOW_ID", "ai-detection-ai-only")
MAX_AGE_SECONDS = max(1, int(os.environ.get("MAX_AGE_SECONDS", "5")))
SWEEP_INTERVAL = max(0.5, float(os.environ.get("SWEEP_INTERVAL_SECONDS", "2")))
QUIET_SWEEPS_BEFORE_LAST = max(2, math.ceil(MAX_AGE_SECONDS / SWEEP_INTERVAL))
# How long the purger will wait for n8n to create its own database on a cold deploy, where the
# backend reaches gunicorn long before n8n finishes migrating.
N8N_DB_WAIT_SECONDS = max(30, int(os.environ.get("N8N_DB_WAIT_SECONDS", "900")))


class Purger:
    def __init__(self) -> None:
        self.enabled = False
        self.error: str | None = None
        self.rows_purged = 0
        self.leftovers_purged = 0
        self.purge_errors = 0
        self.startup_max_id = 0
        self.newest_id = 0
        self.quiet_sweeps = 0
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        if not os.path.exists(N8N_DB):
            # `sqlite3.connect()` CREATES the file it is given. The backend container runs as root
            # and n8n runs as uid 1000, so a purger that opens first leaves a root-owned
            # database.sqlite that n8n can only read -- and n8n crash-loops on
            # "SQLITE_READONLY: attempt to write a readonly database" with the workflow never
            # registered. That is what #106 hit on the first real `docker compose up`: everything
            # looked right in `ps` (backend healthy, web healthy) until n8n's RestartCount started
            # climbing. Waiting for n8n to create its own database costs one purge cycle and is the
            # only ordering-safe answer, because it is n8n that owns the file.
            self.enabled = False
            self.error = f"n8n has not created {N8N_DB} yet; not touching it"
            threading.Thread(target=self._wait_for_n8n, name="n8n-purge-wait",
                             daemon=True).start()
            return
        try:
            self._conn = sqlite3.connect(N8N_DB, timeout=10.0, check_same_thread=False)
            self._conn.execute("PRAGMA busy_timeout = 4000")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self.startup_max_id = self._max_id()
            self.newest_id = self.startup_max_id
            self.enabled = True
            # The wait path above writes an explanatory `error`, and a successful retry has to clear
            # it: /gateway-health reports `purge_enabled` and `purge_error` side by side, so leaving
            # the stale line made a healthy purger read as "enabled, but something is wrong".
            self.error = None
        except sqlite3.Error as exc:
            self.enabled = False
            self.error = f"cannot open n8n database at {N8N_DB}: {exc}"
            return
        try:
            self.leftovers_purged = self._purge_leftovers()
        except sqlite3.Error:
            self.purge_errors += 1
        self._thread = threading.Thread(target=self._run, name="n8n-purge", daemon=True)
        self._thread.start()

    def _purge_leftovers(self) -> int:
        """Delete this workflow's execution rows that predate this process.

        Every sweep deletes `id > startup_max_id`, so a row written before the backend (re)started
        was never touched again. On the live container that meant two rows survived a recreate, and
        `execution_data.data` for each held the webhook's `x-primary-api-key` in clear text -- n8n
        persists the inbound request even with run-data saving off (measured 2026-10-06, and again
        here by reading the volume back). So every key that was mid-flight when the deploy restarted
        stayed on disk for the life of the volume. The delete is scoped to this workflow, and
        `execution_data.executionId` is `ON DELETE CASCADE`, so the blob goes with its parent row.
        """
        cur = self._conn.execute(  # type: ignore[union-attr]
            "DELETE FROM execution_entity WHERE workflowId = ? AND id <= ?",
            (WORKFLOW_ID, self.startup_max_id),
        )
        self._conn.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def _wait_for_n8n(self) -> None:
        """Retry `start()` until n8n creates its database, then stop waiting.

        On a cold `docker compose up` the backend reaches gunicorn in a few seconds and n8n needs a
        minute or more to run its migrations, so an existence check alone would leave
        `purge_enabled: false` for the life of the container -- which is the BYOK-in-n8n-rows
        guarantee silently missing. Polling costs one stat per 5 s on a daemon thread.
        """
        deadline = time.time() + N8N_DB_WAIT_SECONDS
        while time.time() < deadline:
            if self._stop.wait(5.0):
                return          # shutdown, or a test that started a wait it does not need
            if os.path.exists(N8N_DB):
                self.start()      # may still fail mid-migration; retry until it takes
                if self.enabled:
                    return
        self.error = f"{N8N_DB} never appeared within {N8N_DB_WAIT_SECONDS}s; purge stayed off"

    def _max_id(self) -> int:
        row = self._conn.execute(  # type: ignore[union-attr]
            "SELECT COALESCE(MAX(id), 0) AS id FROM execution_entity WHERE workflowId = ?",
            (WORKFLOW_ID,),
        ).fetchone()
        return int(row[0])

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sweep()
            self._stop.wait(SWEEP_INTERVAL)

    def sweep(self) -> int:
        if not self.enabled or self._conn is None:
            return 0
        with self._lock:
            try:
                current = self._max_id()
                self.quiet_sweeps = self.quiet_sweeps + 1 if current == self.newest_id else 0
                self.newest_id = current
                cur = self._conn.execute(
                    "DELETE FROM execution_entity WHERE workflowId = ? AND id > ? AND id < ?",
                    (WORKFLOW_ID, self.startup_max_id, current),
                )
                changes = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                if self.quiet_sweeps >= QUIET_SWEEPS_BEFORE_LAST:
                    cur = self._conn.execute(
                        "DELETE FROM execution_entity WHERE workflowId = ? AND id > ?",
                        (WORKFLOW_ID, self.startup_max_id),
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        changes += cur.rowcount
                self._conn.commit()
                if changes:
                    self.rows_purged += changes
                return changes
            except sqlite3.Error:
                self.purge_errors += 1
                return 0

    def status(self) -> dict:
        return {
            "purge_enabled": self.enabled,
            "rows_purged": self.rows_purged,
            "purge_errors": self.purge_errors,
            "max_age_seconds": MAX_AGE_SECONDS,
            "startup_max_id": self.startup_max_id,
            "startup_rows_purged": self.leftovers_purged,
            "newest_id": self.newest_id,
            "quiet_sweeps": self.quiet_sweeps,
            "quiet_sweeps_before_last": QUIET_SWEEPS_BEFORE_LAST,
            "workflow_id": WORKFLOW_ID,
            "n8n_db": N8N_DB if self.enabled else None,
            "purge_error": self.error,
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def wait(seconds: float) -> None:
    time.sleep(seconds)
