"""Shared test setup.

Test types (see docs/backend-plan.md):
  plain        always run
  integration  need Docker MySQL + Qdrant; skipped when they aren't reachable
  llm          need llama-swap; skipped when it isn't reachable
  scrape       use paid API credits; run only when asked for with  -m scrape
"""
from __future__ import annotations

import re
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
