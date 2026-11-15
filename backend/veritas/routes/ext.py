"""Browser-extension API: narrative check for posts the user scrolls past (contract: extension/API.md).

POST /api/ext/batch     {"client", "items": [{id, url, author, author_name, text, lang, time, kind, quoted_id}]}
                        -> {"results": [...]} one per item; fast-path fields at once, status "pending" while the
                        judge runs
GET  /api/ext/result?ids=a,b   -> {"results": [...]} (poll pending ids)
GET  /api/ext/status    -> the narrative check's bank sizes, thresholds, judge on/off (dashboard System page)
POST /api/ext/feedback  {tweet_id, verdict_shown, user_says, note?} -> {"ok": true}
POST /api/ext/save      {"client", "items"} -> {"queued": n}: only store the posts (no check)
                        Posts from /batch and /save are kept in MySQL + Qdrant (veritas/ext_store.py).
POST /api/ext/deep      {"item", "shared"?, "context"?} -> the in-depth analysis job (patterns, truth check, trace)
GET  /api/ext/deep/{id} -> the job (poll until status is "done" or "error")
The engine lives in veritas/narrative.py; the in-depth analysis in veritas/deep.py. Labels are "potentially propaganda" at most, never an attribution.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..ai import LocalAI

router = APIRouter(prefix="/ext")

MAX_ITEMS = 32
RATE = 240            # requests per client per minute (batch + result + feedback)
_hits: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


class Item(BaseModel):
    id: str = Field(max_length=40)
    url: str | None = Field(default=None, max_length=500)
    author: str | None = Field(default=None, max_length=100)
    author_name: str | None = Field(default=None, max_length=200)
    text: str = Field(default="", max_length=10000)
    lang: str | None = Field(default=None, max_length=20)
    time: str | None = Field(default=None, max_length=40)
    kind: str | None = Field(default=None, max_length=20)
    quoted_id: str | None = Field(default=None, max_length=40)
    platform: str | None = Field(default=None, max_length=20)     # "facebook" on Facebook items
    group: str | None = Field(default=None, max_length=300)       # the Facebook group it was posted in


class SharedPost(BaseModel):
    """The original of a shared (Facebook) or quoted (X) post."""
    author: str | None = Field(default=None, max_length=100)
    author_name: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=500)
    time: str | None = Field(default=None, max_length=40)
    text: str | None = Field(default=None, max_length=10000)
    platform: str | None = Field(default=None, max_length=20)


class DeepRequest(BaseModel):
    """POST /api/ext/deep: the post, plus what the fast check already found about it (the result's
    content.narrative, content.state_media_echo and source_match) so the trace can use it."""
    item: Item
    shared: SharedPost | None = None
    context: dict = Field(default_factory=dict)


class Batch(BaseModel):
    client: str | None = Field(default=None, max_length=40)
    items: list[Item] = Field(max_length=MAX_ITEMS)


class Feedback(BaseModel):
    tweet_id: str = Field(max_length=40)
    verdict_shown: str = Field(max_length=200)
    user_says: Literal["wrong", "right"]
    note: str | None = Field(default=None, max_length=1000)


def _limit(request: Request) -> None:
    key = request.client.host if request.client else "?"
    now = time.monotonic()
    with _rate_lock:
        q = _hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE:
            raise HTTPException(429, "Too many requests. Slow down and try again in a minute.")
        q.append(now)


def _engine(request: Request):
    # Imported here so the app still starts on machines without numpy; only these endpoints need it.
    try:
        from .. import narrative
    except ImportError:
        raise HTTPException(503, "The narrative check isn't installed on this server (numpy is missing).") from None
    settings = request.app.state.settings
    return narrative.get_engine(lambda: LocalAI.from_settings(settings))


def _keep(request: Request, items: list[dict]) -> int:
    """Store the posts in MySQL + Qdrant in the background (veritas/ext_store.py); never fails the request."""
    from .. import ext_store
    try:
        store = ext_store.get_store(request.app.state.settings)
        return store.add(items) if store else 0
    except Exception:  # noqa: BLE001 - saving is best effort
        return 0


@router.post("/batch")
def batch(request: Request, body: Batch):
    _limit(request)
    items = [i.model_dump() for i in body.items]
    results = _engine(request).batch(items)
    _keep(request, items)
    return {"results": results}


@router.post("/save")
def save(request: Request, body: Batch):
    """Only store the posts (no check): the extension's copy to Veritas Lens on the reader's computer,
    when its automatic check runs on another server."""
    _limit(request)
    return {"queued": _keep(request, [i.model_dump() for i in body.items])}


@router.get("/result")
def result(request: Request, ids: Annotated[str, Query(max_length=40 * 41)]):
    _limit(request)
    wanted = [i for i in ids.split(",") if i][:40]
    return {"results": _engine(request).get(wanted)}


@router.get("/status")
def status(request: Request):
    """The narrative check at a glance, for the dashboard. Answers even when numpy is missing (available: false)."""
    try:
        engine = _engine(request)
    except HTTPException as exc:
        return {"available": False, "reason": exc.detail, "claims": 0, "echoClaims": 0, "telegramPosts": 0,
                "thresholds": {}, "judge": False, "embeddingModel": None, "dim": None}
    return engine.status()


@router.post("/feedback")
def feedback(request: Request, body: Feedback):
    _limit(request)
    _engine(request).feedback(body.model_dump())
    return {"ok": True}


# --------------------------------------------------------------------------- in-depth analysis (veritas/deep.py)

NO_TEXT = "This post has no text to analyze."
NO_JOB = "That analysis isn't available any more. Start it again."


def _context(raw: dict) -> dict:
    """Only the fast-check fields the trace uses, size-limited (they come from the client)."""
    out: dict = {}
    if isinstance(raw.get("narrative"), dict):
        out["narrative"] = {k: raw["narrative"].get(k) for k in ("text", "source", "sim")}
    if isinstance(raw.get("state_media_echo"), list):
        out["state_media_echo"] = [{k: e.get(k) for k in ("text", "source", "n_aired", "first", "last", "sim")}
                                   for e in raw["state_media_echo"][:3] if isinstance(e, dict)]
    sm = raw.get("source_match")
    if isinstance(sm, dict) and isinstance(sm.get("telegram"), list):
        out["source_match"] = {"strong": bool(sm.get("strong")), "telegram": [
            {k: t.get(k) for k in ("channel", "url", "date", "sim", "text")} for t in sm["telegram"][:3] if isinstance(t, dict)]}
    for v in out.get("state_media_echo", []) + out.get("source_match", {}).get("telegram", []) + [out.get("narrative") or {}]:
        for k, x in v.items():
            if isinstance(x, str):
                v[k] = x[:1000]
    return out


@router.post("/deep")
def deep_start(request: Request, body: DeepRequest):
    """Start (or reuse) the in-depth analysis of one post: patterns, truth check, trace."""
    from .. import deep
    from ..ai import AiUnavailable

    _limit(request)
    item = body.item.model_dump()
    if not (item.get("text") or "").strip():
        raise HTTPException(400, NO_TEXT)
    if body.shared:
        item["shared"] = body.shared.model_dump()
    try:
        return deep.get_jobs(request.app.state.settings).start(item, _context(body.context))
    except AiUnavailable as exc:
        raise HTTPException(503, str(exc)) from None


@router.get("/deep/{job_id}")
def deep_get(request: Request, job_id: str):
    from .. import deep

    _limit(request)
    job = deep.get_jobs(request.app.state.settings).get(job_id)
    if job is None:
        raise HTTPException(404, NO_JOB)
    return job
