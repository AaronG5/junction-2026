"""Settings shared by everyone using this install (B7): where answers come from
(data mode) and whether analyses are shared. Stored in the `settings` table, so
they survive restarts and are the same in every browser. (The colour theme stays
per browser, in the frontend.)

Rule: sharing can't be on when nothing may leave this computer ("local" mode).
"""
from __future__ import annotations

import json

from .models import AppSettings

KEY = "data_source"
DEFAULTS = AppSettings(data_mode="hybrid", share_analyses=False)
SHARING_WHILE_LOCAL = ("Sharing can't be turned on while Veritas Lens works only on this computer. "
                       "Choose Hybrid or Central server first.")


class SettingsRefused(Exception):
    """The new settings aren't allowed. The message is shown to users as is."""


def load(conn) -> AppSettings:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE setting_key = %s", (KEY,))
        row = cur.fetchone()
    if row is None:
        return DEFAULTS
    try:
        stored = json.loads(row["value"]) if isinstance(row["value"], (str, bytes)) else row["value"]
        merged = {**DEFAULTS.to_json(), **{k: v for k, v in stored.items() if k in ("dataMode", "shareAnalyses")}}
        value = AppSettings.model_validate(merged)
    except (ValueError, TypeError, AttributeError):
        return DEFAULTS  # unreadable row: fall back rather than break every page
    if value.data_mode == "local" and value.share_analyses:
        return value.model_copy(update={"share_analyses": False})
    return value


def save(conn, value: AppSettings) -> AppSettings:
    if value.data_mode == "local" and value.share_analyses:
        raise SettingsRefused(SHARING_WHILE_LOCAL)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO settings (setting_key, value) VALUES (%s, %s)"
                    " ON DUPLICATE KEY UPDATE value = VALUES(value)", (KEY, json.dumps(value.to_json())))
    conn.commit()
    return value
