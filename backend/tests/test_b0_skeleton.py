"""B0: the app skeleton. API basics, JSON errors, serving the frontend safely, settings."""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from conftest import make_client
from veritas import __version__
from veritas.config import ROOT, Settings, read_env_file
from veritas.main import FILE_NOT_FOUND, METHOD_NOT_ALLOWED, NOT_FOUND, SERVER_ERROR, create_app, safe_relative_path

SECRET = "s3cret-password-123"


class AnalyzeBody(BaseModel):  # module level: test-local models aren't visible to FastAPI here
    url: str


# ----------------------------------------------------------------------- API basics

def test_health(client) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def test_health_answers_head_requests(client) -> None:
    r = client.head("/api/health")
    assert r.status_code == 200
    assert r.content == b""


@pytest.mark.parametrize("path", ["/api/nope", "/api/narratives/x/y", "/api", "/api/"])
@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_unknown_api_address_is_a_json_404(client, path: str, method: str) -> None:
    r = getattr(client, method)(path)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": NOT_FOUND}


def test_an_endpoints_own_404_message_reaches_the_user(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/api/sources/{source_id}")
    def source(source_id: str) -> None:
        raise HTTPException(404, "There is no report for this source yet.")

    r = make_client(settings, app).get("/api/sources/nobody")
    assert r.status_code == 404
    assert r.json() == {"detail": "There is no report for this source yet."}


def test_endpoints_added_after_create_app_are_reachable(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/api/later")
    def later() -> dict[str, bool]:
        return {"ok": True}

    assert make_client(settings, app).get("/api/later").json() == {"ok": True}


def test_wrong_method_is_a_plain_sentence(client) -> None:
    r = client.post("/api/health")
    assert r.status_code == 405
    assert r.json() == {"detail": METHOD_NOT_ALLOWED}


def test_posting_to_an_app_address_is_a_json_404(client) -> None:
    r = client.post("/dashboard/claims")
    assert r.status_code == 404
    assert r.json() == {"detail": FILE_NOT_FOUND}


def test_api_description_is_available_but_swagger_ui_is_off(client) -> None:
    assert client.get("/api/openapi.json").status_code == 200
    r = client.get("/api/docs")  # Swagger UI would load scripts from a CDN; Veritas Lens works offline
    assert r.status_code == 404 and r.json() == {"detail": NOT_FOUND}


def test_unexpected_error_is_json_without_a_traceback(settings: Settings, caplog) -> None:
    app = create_app(settings)

    @app.get("/api/boom")
    def boom() -> None:
        raise RuntimeError("internal detail that must not leak")

    with caplog.at_level(logging.ERROR, logger="veritas"):
        r = make_client(settings, app).get("/api/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": SERVER_ERROR}
    assert "internal detail" not in r.text and "Traceback" not in r.text
    assert "internal detail" in caplog.text  # ...but it is logged for us


def test_crafted_addresses_cant_forge_log_lines(settings: Settings, caplog) -> None:
    app = create_app(settings)

    @app.get("/api/boom/{rest}")  # a {rest:path} route never matches a newline
    def boom(rest: str) -> None:
        raise RuntimeError("x")

    with caplog.at_level(logging.ERROR, logger="veritas"):
        make_client(settings, app).get("/api/boom/a%0AFAKE LOG LINE")
    assert "\nFAKE LOG LINE" not in caplog.text
    assert "\\nFAKE LOG LINE" in caplog.text


def test_validation_errors_are_readable(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/api/echo")
    def echo(n: int) -> dict[str, int]:
        return {"n": n}

    @app.post("/api/analyze-test")
    def analyze(body: AnalyzeBody) -> dict[str, str]:
        return {"url": body.url}

    c = make_client(settings, app)
    r = c.get("/api/echo?n=abc")
    assert r.status_code == 422 and r.json()["detail"].startswith("The request isn't valid. n:")
    r = c.post("/api/analyze-test", content="{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json() == {"detail": "The request body isn't valid JSON."}
    r = c.post("/api/analyze-test", json={})
    assert r.status_code == 422 and r.json() == {"detail": "The request isn't valid. url: Field required"}


# --------------------------------------------------------- only local host names

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "localhost:5173", "127.0.0.1:8000"])
def test_local_host_names_are_accepted(client, host: str) -> None:
    assert client.get("/api/health", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "attacker.com:8000", "127.0.0.1.evil.example"])
def test_other_host_names_are_refused_to_block_dns_rebinding(client, host: str) -> None:
    r = client.get("/api/health", headers={"host": host})
    assert r.status_code == 400
    assert "ok" not in r.text


# ------------------------------------------------------------------- the frontend

@pytest.mark.parametrize("path", [
    "/", "/dashboard/claims", "/dashboard/sources/kanalas_b", "/story/nato-border",
    "/dashboard/sources/some.page", "/story/v1.2", "/index.html",
])
def test_app_addresses_get_the_frontend_uncached(client, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<div id=root>" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_query_strings_dont_matter(client) -> None:
    r = client.get("/dashboard/analyze?url=https%3A%2F%2Ft.me%2Fx%2F1")
    assert r.status_code == 200 and "<div id=root>" in r.text


def test_head_on_an_app_address(client) -> None:
    r = client.head("/dashboard/claims")
    assert r.status_code == 200 and r.content == b""


@pytest.mark.parametrize("path,content_type", [
    ("/assets/index-abc123.js", "text/javascript"),
    ("/assets/plex-400.woff2", "font/woff2"),
])
def test_assets_have_fixed_types_and_long_cache(client, path: str, content_type: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(content_type)
    assert "immutable" in r.headers["cache-control"]


def test_root_files_are_served_without_long_cache(client) -> None:
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "immutable" not in r.headers.get("cache-control", "")


@pytest.mark.parametrize("path", ["/assets/missing.js", "/missing.png", "/dashboard/old.css", "/assets/anything"])
def test_missing_files_are_a_real_404_not_the_app(client, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert r.json() == {"detail": FILE_NOT_FOUND}


# Paths as they reach the server (httpx would normalise a raw "/../", so these are encoded).
TRAVERSALS = [
    "/..%2F..%2F.env", "/%2e%2e/%2e%2e/.env", "/assets/..%2F..%2Fsecret.txt", "/%2e%2e%5C%2e%2e%5Csecret.txt",
    "/assets%5C..%5C..%5Csecret.txt", "/C:/Windows/win.ini", "/C:%5CWindows%5Cwin.ini",
    "/%5C%5Cattacker.invalid%5Cshare%5Cx", "/x%00y",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_files_outside_the_frontend_folder_are_never_served(fake_dist: Path, path: str) -> None:
    (fake_dist.parent / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
    (fake_dist.parent / ".env").write_text("MYSQL_PASSWORD=TOP-SECRET", encoding="utf-8")
    r = make_client(Settings(frontend_dist=fake_dist)).get(path)
    assert r.status_code == 404
    assert "TOP-SECRET" not in r.text


def test_network_paths_never_touch_the_filesystem(fake_dist: Path, monkeypatch) -> None:
    """On Windows, even resolving a UNC path opens a network connection (and can leak
    the login hash). The check must reject it before any filesystem call."""
    client = make_client(Settings(frontend_dist=fake_dist))
    seen: list[str] = []
    real_resolve = Path.resolve

    def spy(self, *args, **kwargs):
        seen.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy)
    assert client.get("/%5C%5Cattacker.invalid%5Cshare%5Cx").status_code == 404
    assert not any("attacker" in s for s in seen)


@pytest.mark.parametrize("url_path", [
    "\\\\host\\share\\x", "/\\\\host\\share", "C:/Windows/win.ini", "a/../b", "..", "/assets/../x", "a\x00b", "a:b", "~/x",
])
def test_unsafe_paths_are_rejected_before_any_file_lookup(url_path: str) -> None:
    assert safe_relative_path(url_path) is None


@pytest.mark.parametrize("url_path,expected", [
    ("/", ""), ("/assets/index-abc.js", "assets/index-abc.js"), ("/dashboard/claims", "dashboard/claims"), ("/a//b/", "a/b"),
])
def test_safe_paths_are_kept(url_path: str, expected: str) -> None:
    result = safe_relative_path(url_path)
    assert result is not None
    assert result.parts == (tuple(expected.split("/")) if expected else ())


@pytest.mark.parametrize("make_dist", ["missing", "empty"])
@pytest.mark.parametrize("path", ["/dashboard/narratives", "/assets/index.js"])
def test_without_a_built_frontend_the_page_explains_how_to_build_it(tmp_path: Path, make_dist: str, path: str) -> None:
    dist = tmp_path / "dist"
    if make_dist == "empty":
        (dist / "assets").mkdir(parents=True)  # partially built: no index.html
    client = make_client(Settings(frontend_dist=dist))
    r = client.get(path)
    assert r.status_code == 503
    assert "npm run build" in r.text
    assert client.get("/api/health").status_code == 200  # the API still works


# ------------------------------------------------------------------------ settings

def test_settings_defaults_without_env(tmp_path: Path) -> None:
    s = Settings.load(env_file=tmp_path / "missing.env", environ={})
    assert (s.host, s.port, s.mysql_port) == ("127.0.0.1", 8000, 3306)
    assert (s.mysql_database, s.mysql_user) == ("veritas_lens", "veritas_lens")
    assert s.frontend_dist == ROOT / "frontend" / "dist"


def test_settings_from_env_file_and_environment_overrides(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\ufeff# comment\nexport MYSQL_PORT=3307\nMYSQL_PASSWORD='quoted-pass'\nBACKEND_PORT=8123\n\nNOT A LINE\n"
        'QDRANT_PORT="6335"\nMYSQL_USER=half"quoted\n',
        encoding="utf-8",
    )
    s = Settings.load(env_file=env, environ={"BACKEND_PORT": "9000", "MYSQL_DATABASE": ""})
    assert s.mysql_port == 3307            # 'export' prefix and BOM handled
    assert s.mysql_password == "quoted-pass"
    assert s.qdrant_port == 6335
    assert s.mysql_user == 'half"quoted'   # unbalanced quotes are kept as written
    assert s.port == 9000                  # the environment wins over .env
    assert s.mysql_database == "veritas_lens"  # an empty environment variable doesn't count


def test_relative_frontend_path_is_relative_to_the_repository(tmp_path: Path) -> None:
    s = Settings.load(env_file=tmp_path / "x.env", environ={"FRONTEND_DIST": "frontend/dist"})
    assert s.frontend_dist == ROOT / "frontend" / "dist"


@pytest.mark.parametrize("value,message", [("eighty", "must be a number"), ("70000", "must be between 1 and 65535"), ("0", "must be between 1 and 65535")])
def test_a_bad_port_gives_a_clear_error(tmp_path: Path, value: str, message: str) -> None:
    with pytest.raises(ValueError, match=f"BACKEND_PORT {message}"):
        Settings.load(env_file=tmp_path / "x.env", environ={"BACKEND_PORT": value})


def test_read_env_file_handles_missing_file(tmp_path: Path) -> None:
    assert read_env_file(tmp_path / "nope.env") == {}


def test_server_listens_on_localhost_only(monkeypatch, tmp_path: Path) -> None:
    """run.py must hand uvicorn 127.0.0.1, whatever the environment says."""
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("BACKEND_PORT", "8765")
    spec = importlib.util.spec_from_file_location("run_under_test", ROOT / "backend" / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    assert calls and calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 8765


def test_importing_the_app_has_no_side_effects() -> None:
    """Importing must not read .env or fail on bad settings; only create_app() does."""
    env = {**os.environ, "BACKEND_PORT": "not-a-port"}
    result = subprocess.run(
        [sys.executable, "-c", "import veritas.main as m; print(hasattr(m, 'app'))"],
        cwd=ROOT / "backend", env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_passwords_never_show_up(tmp_path: Path, fake_dist: Path, caplog) -> None:
    env = tmp_path / ".env"
    env.write_text(f"MYSQL_PASSWORD={SECRET}\n", encoding="utf-8")
    settings = Settings.load(env_file=env, environ={"FRONTEND_DIST": str(fake_dist)})
    assert settings.mysql_password == SECRET
    assert SECRET not in repr(settings) and SECRET not in str(settings)

    app = create_app(settings)

    @app.get("/api/boom")
    def boom() -> None:
        raise RuntimeError("database refused the connection")

    client = make_client(settings, app)
    with caplog.at_level(logging.DEBUG):
        for path in ["/api/health", "/api/nope", "/api/openapi.json", "/", "/api/boom"]:
            assert SECRET not in client.get(path).text
    assert SECRET not in caplog.text
