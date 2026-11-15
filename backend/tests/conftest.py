"""Shared test setup.

Test types (see docs/backend-plan.md):
  plain        always run
  integration  need Docker MySQL + Qdrant; skipped when they aren't reachable
  llm          need llama-swap; skipped when it isn't reachable
  scrape       use paid API credits; run only when asked for with  -m scrape
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from veritas.config import ROOT, Settings
from veritas.main import create_app

# Requests in tests are addressed to 127.0.0.1, like real ones (other host names are refused).
BASE_URL = "http://127.0.0.1"

SHARED = ROOT / "shared"
FIXTURES = SHARED / "fixtures"
CASES = SHARED / "cases"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    expr = config.getoption("markexpr") or ""
    if re.search(r"(?<!not )\bscrape\b", expr):
        return  # explicitly asked for, e.g. -m scrape
    skip = pytest.mark.skip(reason="uses paid scraper credits; run with -m scrape")
    for item in items:
        if "scrape" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_post_saving(monkeypatch):
    """/api/ext/batch keeps every post in MySQL + Qdrant (veritas/ext_store.py). Off in tests,
    so no test writes to your database; test_ext_store.py turns it on with a throwaway one."""
    monkeypatch.setenv("EXT_SAVE_POSTS", "off")


@pytest.fixture
def fake_dist(tmp_path: Path) -> Path:
    """A minimal built frontend: index.html, one hashed asset, a font and a root file."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Veritas Lens</title><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log('app')", encoding="utf-8")
    (dist / "assets" / "plex-400.woff2").write_bytes(b"wOF2fake")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    return dist


@pytest.fixture
def settings(fake_dist: Path) -> Settings:
    return Settings(mysql_password="s3cret-password-123", frontend_dist=fake_dist)


def make_client(settings: Settings, app=None) -> TestClient:
    return TestClient(app or create_app(settings), base_url=BASE_URL, raise_server_exceptions=False)


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return make_client(settings)


# --------------------------------------------------------------------------- throwaway databases

def root_settings() -> Settings:
    """The MySQL root login from .env (or MYSQL_ROOT_PASSWORD), no database selected."""
    import dataclasses
    import os

    from veritas.config import read_env_file

    env = read_env_file(ROOT / ".env") if (ROOT / ".env").is_file() else {}
    root_password = os.environ.get("MYSQL_ROOT_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD", "")
    return dataclasses.replace(Settings.load(), mysql_user="root", mysql_password=root_password, mysql_database="")


@contextmanager
def admin_connection():
    """A root connection; skips the test when MySQL isn't available."""
    from veritas.db import DatabaseUnavailable, connect

    try:
        conn = connect(root_settings(), database="")
    except DatabaseUnavailable as exc:
        pytest.skip(f"MySQL not available for integration tests: {exc}")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def scratch_database(name: str | None = None):
    """A brand-new empty database (veritas_test_<random>), dropped afterwards whatever happens.
    Yields Settings pointing at it (root login). Your veritas_lens database is never touched."""
    import dataclasses
    import secrets

    name = name or f"veritas_test_{secrets.token_hex(6)}"
    with admin_connection() as admin:
        try:
            with admin.cursor() as cur:
                cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            yield dataclasses.replace(root_settings(), mysql_database=name)
        finally:
            with admin.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
