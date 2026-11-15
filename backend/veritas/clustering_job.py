"""Clustering started from the dashboard (the "Update clusters" button on Narratives).

The same work as `uv run python backend/cluster.py`, run in one background thread.
Only one run at a time: pressing the button again while it runs just reports the
running one. The state lives in memory; the finished run itself is in MySQL.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .ai import AiError, AiUnavailable, LocalAI
from .clustering import ClusteringUnavailable, _libs, run_clustering
from .config import Settings
from .db import DatabaseUnavailable, connect
from .vectors import Qdrant, VectorStoreError, VectorStoreUnavailable

log = logging.getLogger("veritas")

MIN_DAYS, MAX_DAYS, DEFAULT_DAYS = 1, 365, 7
SERVER_ERROR = "Clustering failed because of a problem on the server. Details are in the backend log."
PLAIN_ERRORS = (ClusteringUnavailable, DatabaseUnavailable, VectorStoreUnavailable, VectorStoreError,
                AiUnavailable, AiError)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


@dataclass
class ClusteringJob:
    """status: idle (never run since the backend started), running, done, too_few, failed."""
    settings: Settings
    run: Callable[[int], dict] | None = None   # tests swap the real work
    status: str = "idle"
    message: str | None = None
    days: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def state(self) -> dict:
        with self._lock:
            return {"status": self.status, "message": self.message, "days": self.days,
                    "startedAt": _iso(self.started_at), "finishedAt": _iso(self.finished_at)}

    def start(self, days: int = DEFAULT_DAYS) -> tuple[dict, bool]:
        """Start a run unless one is running. Returns (state, started)."""
        if not MIN_DAYS <= days <= MAX_DAYS:
            raise ValueError(f"days must be between {MIN_DAYS} and {MAX_DAYS}.")
        if self.run is None:
            _libs()  # missing packages: say so now, not after a minute of waiting
        with self._lock:
            if self.status == "running":
                started = False
            else:
                self.status, self.message, self.days = "running", None, days
                self.started_at, self.finished_at = _now(), None
                threading.Thread(target=self._work, args=(days,), name="clustering", daemon=True).start()
                started = True
        return self.state(), started

    def _work(self, days: int) -> None:
        try:
            result = (self.run or self._real_run)(days)
            status = "done" if result.get("status") == "done" else "too_few"
            message = result.get("message") if status == "too_few" else (
                f"{len(result.get('clusters', []))} clusters from {result.get('posts', 0):,} posts.")
        except PLAIN_ERRORS as exc:
            status, message = "failed", str(exc)
        except Exception:
            log.exception("Clustering from the dashboard failed")
            status, message = "failed", SERVER_ERROR
        with self._lock:
            self.status, self.message, self.finished_at = status, message, _now()

    def _real_run(self, days: int) -> dict:
        until = _now()
        conn = connect(self.settings, read_timeout=3600)
        try:
            with Qdrant.from_settings(self.settings) as qdrant, LocalAI.from_settings(self.settings, timeout=300) as ai:
                return run_clustering(conn, qdrant, ai, since=until - timedelta(days=days), until=until)
        finally:
            conn.close()
