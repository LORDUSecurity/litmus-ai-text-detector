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


class Purger:
    def __init__(self) -> None:
        self.enabled = False
        self.error: str | None = None
        self.rows_purged = 0
        self.purge_errors = 0
        self.startup_max_id = 0
        self.newest_id = 0
        self.quiet_sweeps = 0
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        try:
            self._conn = sqlite3.connect(N8N_DB, timeout=10.0, check_same_thread=False)
            self._conn.execute("PRAGMA busy_timeout = 4000")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self.startup_max_id = self._max_id()
            self.newest_id = self.startup_max_id
            self.enabled = True
        except sqlite3.Error as exc:
            self.enabled = False
            self.error = f"cannot open n8n database at {N8N_DB}: {exc}"
            return
        self._thread = threading.Thread(target=self._run, name="n8n-purge", daemon=True)
        self._thread.start()

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
