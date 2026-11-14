"""Read-only endpoints for the dashboard screens (B3).

Query parameters are what the frontend sends (frontend/src/lib/filters.ts,
filtersToParams): platform, from, to, status, and q for claim search. A missing
parameter means "no limit"; "all" is accepted too. Anything else is a 422 with a
readable message.

Database work is plain blocking code, so these are `def` endpoints: FastAPI runs
them in a thread pool and a slow query never blocks other requests.
"""
from __future__ import annotations

from typing import Annotated, Literal

import pymysql
from fastapi import APIRouter, Depends, HTTPException, Query

from .. import queries
from ..config import Settings
from ..db import connection
from ..deps import get_db, get_settings
from ..filters import range_from_params
from ..links import is_supported_link
from ..models import Day, ErrorBody

router = APIRouter()

Db = Annotated[pymysql.connections.Connection, Depends(get_db)]
PlatformParam = Annotated[Literal["all", "Telegram", "Facebook"], Query()]
SourcePlatformParam = Annotated[Literal["all", "Telegram", "Facebook", "Website"], Query()]
StatusParam = Annotated[Literal["all", "true", "false", "misleading", "unverified"], Query()]
FromParam = Annotated[Day | None, Query(alias="from")]
ToParam = Annotated[Day | None, Query()]

ERRORS_404 = {404: {"model": ErrorBody}}


def _filters(platform: str, from_: str | None, to: str | None, status: str) -> queries.ListFilters:
    return queries.ListFilters(platform=platform, range=range_from_params(from_, to), status=status)


def _json(model):
    return model.to_json()


@router.get("/narratives")
def narratives(db: Db, platform: PlatformParam = "all", from_: FromParam = None, to: ToParam = None,
               status: StatusParam = "all"):
    return _json(queries.narratives(db, _filters(platform, from_, to, status)))


@router.get("/claims")
def claims(db: Db, platform: PlatformParam = "all", from_: FromParam = None, to: ToParam = None,
           status: StatusParam = "all", q: Annotated[str, Query(max_length=200)] = ""):
    return _json(queries.claims(db, _filters(platform, from_, to, status), q))


@router.get("/sources")
def sources(db: Db, platform: SourcePlatformParam = "all"):
    return _json(queries.sources(db, platform))


@router.get("/sources/{source_id}", responses=ERRORS_404)
def source(db: Db, source_id: str):
    try:
        return _json(queries.source_report(db, source_id))
    except queries.NotFound as exc:
        raise HTTPException(404, str(exc)) from None


@router.get("/trending")
def trending(db: Db, platform: PlatformParam = "all", from_: FromParam = None, to: ToParam = None,
             status: StatusParam = "all"):
    return _json(queries.trending(db, _filters(platform, from_, to, status)))


@router.get("/stories/{story_id}", responses=ERRORS_404)
def story(db: Db, story_id: str):
    try:
        return _json(queries.story(db, story_id))
    except queries.NotFound as exc:
        raise HTTPException(404, str(exc)) from None


@router.get("/trace", responses={400: {"model": ErrorBody}, **ERRORS_404})
def trace(settings: Annotated[Settings, Depends(get_settings)], url: Annotated[str, Query(max_length=2048)]):
    # The link is checked before the database is opened: a typo gets its answer even when MySQL is down.
    if not is_supported_link(url):
        raise HTTPException(400, queries.NOT_A_LINK)
    try:
        with connection(settings) as db:
            return _json(queries.trace(db, url))
    except queries.NotFound as exc:
        raise HTTPException(404, str(exc)) from None
