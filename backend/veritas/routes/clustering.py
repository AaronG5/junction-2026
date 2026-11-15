"""Clustering from the dashboard (the Narratives screen's "Update clusters" button).

GET  /api/clustering   the state: idle / running / done / up_to_date / too_few / failed, with a
                       plain message (while running: what it is doing).
POST /api/clustering   {} adds the new posts to the clusters (veritas/clustering_update.py);
                       {"rebuild": true, "days": N} groups everything again (days 1-365, or all posts).
                       202 + state; 200 + state when one is already running;
                       503 when the clustering packages aren't installed.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from ..clustering import ClusteringUnavailable
from ..clustering_job import MAX_DAYS, MIN_DAYS, ClusteringJob
from ..models import ErrorBody

router = APIRouter()
DAYS_ONLY_FOR_REBUILD = "days only applies to a rebuild; an update always adds every new post."


def _job(request: Request) -> ClusteringJob:
    return request.app.state.clustering


@router.get("/clustering")
def clustering_state(request: Request):
    return _job(request).state()


@router.post("/clustering", status_code=202, responses={400: {"model": ErrorBody}, 503: {"model": ErrorBody}})
def start_clustering(request: Request,
                     rebuild: Annotated[bool, Body(embed=True, strict=True)] = False,
                     days: Annotated[int | None, Body(embed=True, ge=MIN_DAYS, le=MAX_DAYS, strict=True)] = None):
    if days is not None and not rebuild:
        raise HTTPException(400, DAYS_ONLY_FOR_REBUILD)
    try:
        state, started = _job(request).start("rebuild" if rebuild else "update", days)
    except ClusteringUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    return state if started else JSONResponse(state, status_code=200)
