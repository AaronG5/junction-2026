"""Browser-extension API: narrative check for posts the user scrolls past (contract: extension/API.md).

POST /api/ext/batch     {"client", "items": [{id, url, author, author_name, text, lang, time, kind, quoted_id}]}
                        -> {"results": [...]} one per item; fast-path fields at once, status "pending" while the
                        judge runs
GET  /api/ext/result?ids=a,b   -> {"results": [...]} (poll pending ids)
POST /api/ext/feedback  {tweet_id, verdict_shown, user_says, note?} -> {"ok": true}
The engine lives in veritas/narrative.py. Labels are "potentially propaganda" at most, never an attribution.
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


@router.post("/batch")
def batch(request: Request, body: Batch):
    _limit(request)
    return {"results": _engine(request).batch([i.model_dump() for i in body.items])}


@router.get("/result")
def result(request: Request, ids: Annotated[str, Query(max_length=40 * 41)]):
    _limit(request)
    wanted = [i for i in ids.split(",") if i][:40]
    return {"results": _engine(request).get(wanted)}


@router.post("/feedback")
def feedback(request: Request, body: Feedback):
    _limit(request)
    _engine(request).feedback(body.model_dump())
    return {"ok": True}
