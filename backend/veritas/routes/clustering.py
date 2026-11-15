"""Clustering from the dashboard (the Narratives screen's "Update clusters" button).

GET  /api/clustering             the state: idle / running / done / too_few / failed (+ message)
POST /api/clustering {"days": 7} start a run over the posts of the last N days (1-365):
                                 202 + state; 200 + state when one is already running;
                                 503 when the clustering packages aren't installed.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from ..clustering import ClusteringUnavailable
from ..clustering_job import DEFAULT_DAYS, MAX_DAYS, MIN_DAYS, ClusteringJob
from ..models import ErrorBody

router = APIRouter()


def _job(request: Request) -> ClusteringJob:
    return request.app.state.clustering


@router.get("/clustering")
def clustering_state(request: Request):
    return _job(request).state()


@router.post("/clustering", status_code=202, responses={503: {"model": ErrorBody}})
def start_clustering(request: Request,
                     days: Annotated[int, Body(embed=True, ge=MIN_DAYS, le=MAX_DAYS)] = DEFAULT_DAYS):
    try:
        state, started = _job(request).start(days)
    except ClusteringUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    return state if started else JSONResponse(state, status_code=200)
