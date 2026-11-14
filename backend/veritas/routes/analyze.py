"""Analyze a link (B6): start a job, then poll it.

POST /api/analyze {"url": ...}
  202 + the job (status queued/running), or 200 + a finished job when this post was
  analyzed before. Answered at once without a job: 400 not a post link, 404 post
  not collected and no scraper set up, 503 the AI or the database isn't running.
GET /api/analyze/{id}
  the job: status, step (0-4, 5 = done), and the report or a plain error.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import analysis
from ..ai import AiUnavailable
from ..db import connection
from ..deps import get_settings
from ..config import Settings
from ..models import ErrorBody

router = APIRouter()

ERRORS = {400: {"model": ErrorBody}, 404: {"model": ErrorBody}, 503: {"model": ErrorBody}}


def _runner(request: Request) -> analysis.JobRunner:
    return request.app.state.analysis


@router.post("/analyze", status_code=202, responses=ERRORS)
def start_analysis(request: Request, settings: Annotated[Settings, Depends(get_settings)],
                   url: Annotated[str, Body(embed=True, max_length=2048)]):
    runner = _runner(request)
    # The link is checked before the database is opened: a typo gets its answer even when MySQL is down.
    if analysis.parse_link(url) is None:
        raise HTTPException(400, analysis.NOT_A_LINK)
    try:
        with connection(settings) as conn:
            job, new = analysis.start(conn, runner.services, runner, url)
    except analysis.AnalysisRefused as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except AiUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    return JSONResponse(job, status_code=202 if new else 200)


@router.get("/analyze/{job_id}", responses={404: {"model": ErrorBody}})
def analysis_job(request: Request, settings: Annotated[Settings, Depends(get_settings)], job_id: str):
    try:
        with connection(settings) as conn:
            return analysis.get_job(conn, job_id, _runner(request).services.now())
    except analysis.AnalysisRefused as exc:
        raise HTTPException(exc.status, str(exc)) from None
