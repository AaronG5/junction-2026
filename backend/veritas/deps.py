"""Things endpoints ask FastAPI for (dependencies). Endpoints never read app.state directly."""
from __future__ import annotations

from typing import Iterator

import pymysql
from fastapi import Depends, Request

from .config import Settings
from .db import connect


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[pymysql.connections.Connection]:
    """One database connection per request, closed afterwards.
    If MySQL is down, DatabaseUnavailable becomes a 503 with a plain message (main.py)."""
    conn = connect(settings)
    try:
        yield conn
    finally:
        conn.close()
