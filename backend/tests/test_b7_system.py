"""B7: the System page (live service status) and shared settings."""
from __future__ import annotations

import dataclasses
import json
import socket
import time
from typing import Iterator

import pytest

from conftest import make_client, scratch_database
from veritas import app_settings, system
from veritas.config import Settings
from veritas.db import connection
from veritas.db.migrations import migrate
from veritas.models import SystemStatus

SECRET = "pw-never-shown-b7"


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _all_down(tmp_path) -> Settings:
    return Settings(mysql_port=_closed_port(), qdrant_port=_closed_port(), llama_swap_port=_closed_port(),
                    mysql_password=SECRET, oxylabs_password=SECRET, context_dev_api_key=SECRET, frontend_dist=tmp_path)


STATE = {"chat_model": "gemma-4-E2B-it", "backend": "cuda",
         "hardware": {"gpu": "RTX 4070 Laptop", "vram_gb": 8, "ram_gb": 31.7, "disk_free_gb": 313.2},
         "upgrade": {"model": "gemma-4-26B-A4B-it", "download_gb": 15.4, "note": "Better analyses, a little slower."}}


# --------------------------------------------------------------------------- /api/system

def test_every_service_down_shows_stopped_and_the_page_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "read_state", lambda: STATE)
    client = make_client(_all_down(tmp_path))
    started = time.monotonic()
    res = client.get("/api/system")
    assert time.monotonic() - started < 10  # short timeouts, checked in parallel
    assert res.status_code == 200
    body = SystemStatus.model_validate(res.json())
    assert [(s.name, s.running) for s in body.services] == [("AI server", False), ("Database", False), ("Vector search", False)]
    assert body.ai.running is False
    assert body.ai.chat_model == "gemma-4-E2B-it"  # from the launcher's state when the AI is down
    assert body.ai.chat_model_note == "Runs on the graphics card"
    assert body.ai.hardware == "RTX 4070 Laptop · 8 GB" and body.ai.hardware_note == "32 GB memory · 313 GB free disk"
    assert body.ai.upgrade.model == "gemma-4-26B-A4B-it" and body.ai.upgrade.download_gb == 15.4


def test_no_secret_ever_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "read_state", lambda: {**STATE, "token": SECRET, "hardware": {"gpu": "x", "path": "C:/Users/x"}})
    text = make_client(_all_down(tmp_path)).get("/api/system").text
    assert SECRET not in text and "C:/Users" not in text


def test_without_launcher_state(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "read_state", lambda: {})
    ai = make_client(_all_down(tmp_path)).get("/api/system").json()["ai"]
    assert ai["chatModel"] == "Not chosen yet" and ai["hardware"] == "Not detected yet" and ai["upgrade"] is None


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", "null"])
def test_a_broken_state_file_is_ignored(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    assert system.read_state(path) == {}
    assert system.read_state(tmp_path / "missing.json") == {}


def test_upgrade_is_hidden_when_it_is_the_current_model_or_broken():
    assert system._upgrade({"upgrade": {"model": "m1", "download_gb": 3}}, "m1") is None
    assert system._upgrade({"upgrade": {"model": "m2", "download_gb": "lots"}}, "m1") is None
    assert system._upgrade({"upgrade": "m2"}, "m1") is None
    assert system._upgrade({"upgrade": {"model": "m2", "download_gb": 3}}, "m1").download_gb == 3


def test_vision_and_placement_notes():
    models = {"chat": {"id": "chat", "meta": {"llamaswap": {"modelID": "gemma-x", "vision": True}}}}
    assert system._model_id(models, "chat") == ("gemma-x", {"modelID": "gemma-x", "vision": True})
    assert system._model_id(None, "chat") == (None, {})


@pytest.mark.integration
def test_live_services_on_this_computer(tmp_path):
    """Against the services actually running here (skips what isn't)."""
    settings = dataclasses.replace(Settings.load(), frontend_dist=tmp_path)
    body = make_client(settings).get("/api/system").json()
    SystemStatus.model_validate(body)
    running = {s["name"]: s["running"] for s in body["services"]}
    if not any(running.values()):
        pytest.skip("no services running")
    if running["AI server"]:
        assert body["ai"]["running"] and body["ai"]["chatModel"] not in ("chat", "Not chosen yet")
    assert settings.mysql_password not in json.dumps(body)


# --------------------------------------------------------------------------- /api/settings

@pytest.fixture
def db(tmp_path) -> Iterator[Settings]:
    with scratch_database() as settings:
        with connection(settings) as conn:
            migrate(conn)
        yield dataclasses.replace(settings, frontend_dist=tmp_path)


@pytest.mark.integration
class TestSettings:
    def test_defaults_then_saved_values_survive_a_restart(self, db):
        assert make_client(db).get("/api/settings").json() == {"dataMode": "hybrid", "shareAnalyses": False}
        res = make_client(db).put("/api/settings", json={"dataMode": "central", "shareAnalyses": True})
        assert res.status_code == 200 and res.json() == {"dataMode": "central", "shareAnalyses": True}
        # a new app = a backend restart
        assert make_client(db).get("/api/settings").json() == {"dataMode": "central", "shareAnalyses": True}

    def test_sharing_in_local_only_mode_is_refused(self, db):
        client = make_client(db)
        client.put("/api/settings", json={"dataMode": "hybrid", "shareAnalyses": True})
        res = client.put("/api/settings", json={"dataMode": "local", "shareAnalyses": True})
        assert res.status_code == 400 and res.json() == {"detail": app_settings.SHARING_WHILE_LOCAL}
        assert client.get("/api/settings").json() == {"dataMode": "hybrid", "shareAnalyses": True}  # unchanged
        ok = client.put("/api/settings", json={"dataMode": "local", "shareAnalyses": False})
        assert ok.status_code == 200

    @pytest.mark.parametrize("body", [
        {"dataMode": "cloud", "shareAnalyses": False}, {"dataMode": "local"}, {"dataMode": "local", "shareAnalyses": "no"},
        {"dataMode": "local", "shareAnalyses": False, "theme": "dark"}, [], "local",
    ])
    def test_bad_bodies_are_422(self, db, body):
        res = make_client(db).put("/api/settings", json=body)
        assert res.status_code == 422 and res.json()["detail"].startswith("The request isn't valid.")

    def test_an_unreadable_or_contradictory_row_falls_back_safely(self, db):
        with connection(db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO settings (setting_key, value) VALUES ('data_source', '{\"dataMode\": \"local\", \"shareAnalyses\": true}')")
        assert make_client(db).get("/api/settings").json() == {"dataMode": "local", "shareAnalyses": False}
        with connection(db) as conn, conn.cursor() as cur:
            cur.execute("UPDATE settings SET value = '{\"dataMode\": 5}' WHERE setting_key = 'data_source'")
        assert make_client(db).get("/api/settings").json() == {"dataMode": "hybrid", "shareAnalyses": False}

    def test_database_down_is_503(self, tmp_path):
        client = make_client(_all_down(tmp_path))
        assert client.get("/api/settings").status_code == 503
        res = client.put("/api/settings", json={"dataMode": "local", "shareAnalyses": False})
        assert res.status_code == 503 and SECRET not in res.text
