"""The Veritas Lens web app: /api for data, everything else serves the built frontend.

- Errors are always JSON {"detail": "..."} in plain words, so the frontend can show
  them as they are. Unexpected errors never leak tracebacks to the browser.
- There are no catch-all routes. An unmatched address reaches the 404 handler, which
  answers /api with JSON and everything else with the frontend, so endpoints added
  later always take precedence.
- Only requests addressed to 127.0.0.1 / localhost are accepted (blocks DNS-rebinding
  attacks from websites, see docs/backend-plan.md).

Start the server with  uv run python backend/run.py
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .ai import LocalAI
from .analysis import JobRunner, Services
from .collectors import collector_for
from .config import Settings
from .db import DatabaseUnavailable
from .routes import ROUTERS
from .vectors import Qdrant

log = logging.getLogger("veritas")

NOT_FOUND = "There's nothing at this address in the Veritas Lens API."
FILE_NOT_FOUND = "Not found."
METHOD_NOT_ALLOWED = "This address doesn't accept that kind of request."
SERVER_ERROR = "Something went wrong on the server. Please try again."

# Content types set explicitly, so they don't depend on the machine's registry.
for _ext, _type in {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
    ".woff2": "font/woff2", ".woff": "font/woff", ".json": "application/json", ".map": "application/json",
    ".webmanifest": "application/manifest+json", ".ico": "image/x-icon", ".png": "image/png", ".webp": "image/webp",
}.items():
    mimetypes.add_type(_type, _ext)

# Paths with one of these endings are files: missing ones are a real 404.
# Anything else (e.g. /dashboard/sources/some.page) is an app address and gets index.html.
STATIC_EXTENSIONS = {
    ".js", ".mjs", ".css", ".map", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".txt", ".xml", ".webmanifest", ".html",
}

# Files the build puts in /assets have content hashes in their names, so they never change.
HASHED_ASSET_CACHE = "public, max-age=31536000, immutable"

FRONTEND_MISSING = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Veritas Lens</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 640px; margin: 64px auto; padding: 0 16px; line-height: 1.5">
<h1>Veritas Lens is running, but the dashboard isn't built yet</h1>
<p>Build it once, then reload this page:</p>
<pre style="background:#eee; padding:12px; border-radius:8px">cd frontend
npm install
npm run build</pre>
<p>The API itself is available at <a href="/api/health">/api/health</a>.</p>
</body></html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Nothing happens at import time; run.py and tests call this."""
    settings = settings or Settings.load()
    # Swagger UI is off: it loads its scripts from a CDN, and Veritas Lens must work offline.
    # The machine-readable API description stays at /api/openapi.json.
    app = FastAPI(title="Veritas Lens", version=__version__, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = settings
    # "Analyze a link" jobs run in a background thread (veritas/analysis.py). Tests swap the services.
    app.state.analysis = JobRunner(Services(
        settings=settings,
        ai=lambda: LocalAI.from_settings(settings),
        qdrant=lambda: Qdrant.from_settings(settings),
        collector=collector_for(settings),
    ))

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    _add_error_handlers(app, settings.frontend_dist)
    for router in ROUTERS:
        app.include_router(router, prefix="/api")
    return app


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _add_error_handlers(app: FastAPI, dist: Path) -> None:
    serve_frontend = _frontend_server(dist)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        path = request.scope["path"]
        no_route = exc.status_code == 404 and exc.detail == "Not Found"  # Starlette's default: nothing matched
        if no_route and not _is_api(path) and request.method in ("GET", "HEAD"):
            return serve_frontend(path)
        if no_route:
            detail = NOT_FOUND if _is_api(path) else FILE_NOT_FOUND
        elif exc.status_code == 405:
            detail = METHOD_NOT_ALLOWED
        else:
            # An endpoint's own message (e.g. 404 "There is no report for this source yet.") is kept.
            detail = exc.detail if isinstance(exc.detail, str) else "The request couldn't be handled."
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _describe_validation(exc)}, status_code=422)

    @app.exception_handler(DatabaseUnavailable)
    async def database_unavailable(request: Request, exc: DatabaseUnavailable) -> JSONResponse:
        # The message is already plain language and never contains the password.
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # %r escapes control characters, so a crafted address can't forge log lines.
        log.exception("Unhandled error on %s %r", request.method, request.scope["path"])
        return JSONResponse({"detail": SERVER_ERROR}, status_code=500)


def _describe_validation(exc: RequestValidationError) -> str:
    """Turn FastAPI's validation errors into one readable sentence."""
    problems = []
    for err in exc.errors():
        if err.get("type") == "json_invalid":
            return "The request body isn't valid JSON."
        loc = [p for p in err.get("loc", ()) if not isinstance(p, int)]
        where = ".".join(str(p) for p in loc[1:]) or "request body"
        problems.append(f"{where}: {err.get('msg', 'invalid value')}")
    return "The request isn't valid. " + "; ".join(problems)


def safe_relative_path(url_path: str) -> PurePosixPath | None:
    """The part of a URL path that may be looked up inside the frontend folder, or
    None if it could point anywhere else. Checked BEFORE touching the filesystem:
    on Windows even resolving a UNC path (\\\\host\\share) opens a network connection."""
    path = url_path.lstrip("/")
    if not path:
        return PurePosixPath("")
    if any(ch in path for ch in ("\\", ":", "\x00")):
        return None
    parts = PurePosixPath(path).parts
    if any(p in ("..", ".") or p.startswith("~") for p in parts):
        return None
    return PurePosixPath(*parts)


def _frontend_server(dist: Path):
    """Serve the built frontend. App addresses get index.html; missing files get a real 404."""
    root = dist.resolve()

    def frontend(url_path: str):
        index = root / "index.html"
        if not index.is_file():
            return HTMLResponse(FRONTEND_MISSING, status_code=503)

        rel = safe_relative_path(url_path)
        if rel is None:
            return JSONResponse({"detail": FILE_NOT_FOUND}, status_code=404)

        if rel.parts:
            candidate = (root / rel).resolve()
            # Second line of defence (e.g. a symlink inside dist pointing outside it).
            if not candidate.is_relative_to(root):
                return JSONResponse({"detail": FILE_NOT_FOUND}, status_code=404)
            if candidate.is_file() and candidate != index:
                hashed = rel.parts[0] == "assets"
                return FileResponse(candidate, headers={"Cache-Control": HASHED_ASSET_CACHE} if hashed else None)
            if rel.parts[0] == "assets" or rel.suffix.lower() in STATIC_EXTENSIONS - {".html"}:
                return JSONResponse({"detail": FILE_NOT_FOUND}, status_code=404)

        # index.html must never be cached, so a new build is picked up right away.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return frontend
