"""Clustering started from the dashboard (the "Update clusters" button on Narratives).

Runs in one background thread, one run at a time: pressing the button again while it
runs just reports the running one. The state lives in memory; the results are in MySQL.
  update   (the button) adds the posts that aren't clustered yet, without recomputing the
           rest (veritas/clustering_update.py); the very first run groups every post.
  rebuild  groups everything again from scratch (all posts, or the last N days), like
           `uv run python backend/cluster.py --rebuild`.
Either way, the typical and edge posts of every cluster that changed then get their
highlights and claims (veritas/cluster_insight.py), and the copies between posts, the
source profiles and the trending stories are brought up to date (veritas/derive.py).
While it runs, the message says what it is doing.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from .ai import AiError, AiUnavailable, LocalAI
from .cluster_insight import enrich_clusters, unread_clusters
from .clustering import ClusteringUnavailable, _libs, run_clustering
from .clustering_update import update_clustering
from .derive import derive_all
from .config import Settings
from .db import DatabaseUnavailable, connect
from .factcheck import gemini_for
from .gemini import Gemini, GeminiError, GeminiUnavailable
from .vectors import Qdrant, VectorStoreError, VectorStoreUnavailable

log = logging.getLogger("veritas")

MIN_DAYS, MAX_DAYS = 1, 365
Mode = Literal["update", "rebuild"]
SERVER_ERROR = "Clustering failed because of a problem on the server. Details are in the backend log."
PLAIN_ERRORS = (ClusteringUnavailable, DatabaseUnavailable, VectorStoreUnavailable, VectorStoreError,
                AiUnavailable, AiError, GeminiUnavailable, GeminiError)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def summary(result: dict) -> tuple[str, str]:
    """(status, message) for a finished run's report."""
    insight = result.get("insight") or {}
    read = f" Read {insight['posts']} typical posts." if insight.get("posts") else ""
    derived = result.get("derived") or {}
    if derived.get("stories") or derived.get("sources"):
        read += f" Updated {derived.get('sources', 0)} sources and {derived.get('stories', 0)} stories."
    checked = insight.get("checked") or {}
    if checked:
        order = ("false", "misleading", "true", "unverified")
        read += f" Fact-checked {sum(checked.values())} claims: " + ", ".join(
            f"{checked[v]} {v}" for v in order if checked.get(v)) + "."
    if result.get("status") in ("too_few", "up_to_date"):
        return result["status"], (result.get("message") or "") + read
    if result.get("mode") == "update":
        new = result.get("new_clusters", 0)
        return "done", (f"Added {result.get('posts', 0):,} new posts: {result.get('joined', 0):,} joined existing clusters"
                        f"{f', {new} new narratives' if new else ''}.{read}")
    return "done", f"{len(result.get('clusters', []))} clusters from {result.get('posts', 0):,} posts.{read}"


@dataclass
class ClusteringJob:
    """status: idle (never run since the backend started), running, done, up_to_date, too_few, failed."""
    settings: Settings
    run: Callable[[str, int | None, Callable[[str], None]], dict] | None = None   # tests swap the real work
    status: str = "idle"
    message: str | None = None
    mode: str | None = None
    days: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def state(self) -> dict:
        with self._lock:
            return {"status": self.status, "message": self.message, "mode": self.mode, "days": self.days,
                    "startedAt": _iso(self.started_at), "finishedAt": _iso(self.finished_at)}

    def start(self, mode: Mode = "update", days: int | None = None) -> tuple[dict, bool]:
        """Start a run unless one is running. Returns (state, started). days: rebuild only (None = all posts)."""
        if days is not None and not MIN_DAYS <= days <= MAX_DAYS:
            raise ValueError(f"days must be between {MIN_DAYS} and {MAX_DAYS}.")
        if self.run is None:
            _libs()  # missing packages: say so now, not after a minute of waiting
        with self._lock:
            if self.status == "running":
                started = False
            else:
                self.status, self.message, self.mode, self.days = "running", None, mode, days
                self.started_at, self.finished_at = _now(), None
                threading.Thread(target=self._work, args=(mode, days), name="clustering", daemon=True).start()
                started = True
        return self.state(), started

    def _say(self, message: str) -> None:
        with self._lock:
            if self.status == "running":
                self.message = message

    def _work(self, mode: str, days: int | None) -> None:
        try:
            status, message = summary((self.run or self._real_run)(mode, days, self._say))
        except PLAIN_ERRORS as exc:
            status, message = "failed", str(exc)
        except Exception:
            log.exception("Clustering from the dashboard failed")
            status, message = "failed", SERVER_ERROR
        with self._lock:
            self.status, self.message, self.finished_at = status, message, _now()

    def _real_run(self, mode: str, days: int | None, say: Callable[[str], None]) -> dict:
        conn = connect(self.settings, read_timeout=3600)
        try:
            gemini = gemini_for(conn, self.settings)
            try:
                with Qdrant.from_settings(self.settings) as qdrant, LocalAI.from_settings(self.settings, timeout=300) as ai:
                    return cluster_and_read(conn, qdrant, ai, mode=mode, days=days, say=say, gemini=gemini)
            finally:
                if gemini is not None:
                    gemini.close()
        finally:
            conn.close()


def cluster_and_read(conn, qdrant: Qdrant, ai: LocalAI, *, mode: str = "update", days: int | None = None,
                     say: Callable[[str], None] = lambda m: None, read: bool = True, gemini: Gemini | None = None) -> dict:
    """One clustering run (update or rebuild), then highlights and claims for the clusters it touched."""
    if mode == "rebuild":
        say("Grouping all posts again from scratch. This can take a few minutes.")
        until = _now()
        result = run_clustering(conn, qdrant, ai, since=until - timedelta(days=days) if days else None, until=until)
    else:
        result = update_clustering(conn, qdrant, ai, progress=say)
    if read and result.get("run_id") and result.get("status") in ("done", "up_to_date"):
        # the clusters this run changed, and any whose typical posts weren't read before
        ids = {c["cluster_id"] for c in result.get("clusters", [])} | set(unread_clusters(conn, result["run_id"], gemini is not None))
        if ids:
            result["insight"] = enrich_clusters(conn, ai, qdrant, ids, progress=say, gemini=gemini)
    if read:
        result["derived"] = derive_all(conn, qdrant, translator(gemini or ai), progress=say)
    return result


def translator(engine):
    """Text -> (English, prompt label, model), with Gemini when it is set up, else the local AI."""
    from .analyzers import translate

    def run(text: str) -> tuple[str, str, str]:
        answer = translate(engine, text)
        return answer.value.translation.strip(), answer.prompt, answer.model
    return run
