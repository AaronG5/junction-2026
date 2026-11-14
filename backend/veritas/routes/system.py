"""The System page (B7): live status of the AI and the services, and shared settings."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import app_settings
from ..config import Settings
from ..db import connection
from ..deps import get_settings
from ..models import AppSettings, ErrorBody
from ..system import system_status

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/system")
def system(settings: SettingsDep):
    # Never fails: a service that's down is shown as stopped.
    return system_status(settings).to_json()


@router.get("/settings", responses={503: {"model": ErrorBody}})
def read_settings(settings: SettingsDep):
    with connection(settings) as conn:
        return app_settings.load(conn).to_json()


@router.put("/settings", responses={400: {"model": ErrorBody}, 503: {"model": ErrorBody}})
def write_settings(settings: SettingsDep, value: Annotated[AppSettings, Body()]):
    with connection(settings) as conn:
        try:
            return app_settings.save(conn, value).to_json()
        except app_settings.SettingsRefused as exc:
            raise HTTPException(400, str(exc)) from None
