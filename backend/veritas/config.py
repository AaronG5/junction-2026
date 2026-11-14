"""Backend settings, read from the repository's .env (written by the launcher).

Environment variables override .env, which is handy for tests and deployment.
Passwords are kept out of repr(), so settings can be logged safely.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Ignores blanks and # comments, an optional leading
    `export `, and a BOM. Quotes are removed only when they match on both ends."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "veritas_lens"
    mysql_user: str = "veritas_lens"
    mysql_password: str = field(default="", repr=False)
    qdrant_port: int = 6333
    llama_swap_port: int = 8080
    # Scraper keys (B6). Empty = that scraper isn't used. Never shown (repr=False).
    oxylabs_username: str = field(default="", repr=False)
    oxylabs_password: str = field(default="", repr=False)
    context_dev_api_key: str = field(default="", repr=False)
    frontend_dist: Path = ROOT / "frontend" / "dist"
    # Host names requests may be addressed to (blocks DNS rebinding). Ports are ignored.
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")

    @classmethod
    def load(cls, env_file: Path | None = None, environ: dict[str, str] | None = None) -> "Settings":
        file_values = read_env_file(env_file if env_file is not None else ROOT / ".env")
        env = os.environ if environ is None else environ

        def get(key: str, default: str) -> str:
            # An empty environment variable doesn't count as set.
            return env.get(key) or file_values.get(key) or default

        def get_port(key: str, default: int) -> int:
            raw = get(key, str(default))
            try:
                port = int(raw)
            except ValueError as exc:
                raise ValueError(f"{key} must be a number, got {raw!r}") from exc
            if not 1 <= port <= 65535:
                raise ValueError(f"{key} must be between 1 and 65535, got {port}")
            return port

        dist = get("FRONTEND_DIST", "")
        dist_path = Path(dist) if dist else ROOT / "frontend" / "dist"
        if not dist_path.is_absolute():
            dist_path = ROOT / dist_path  # relative to the repository, not the current folder

        return cls(
            host="127.0.0.1",  # never exposed to the network; see docs/backend-plan.md
            port=get_port("BACKEND_PORT", 8000),
            mysql_host=get("MYSQL_HOST", "127.0.0.1"),
            mysql_port=get_port("MYSQL_PORT", 3306),
            mysql_database=get("MYSQL_DATABASE", "veritas_lens"),
            mysql_user=get("MYSQL_USER", "veritas_lens"),
            mysql_password=get("MYSQL_PASSWORD", ""),
            qdrant_port=get_port("QDRANT_PORT", 6333),
            llama_swap_port=get_port("LLAMA_SWAP_PORT", 8080),
            oxylabs_username=get("OXYLABS_USERNAME", ""),
            oxylabs_password=get("OXYLABS_PASSWORD", ""),
            context_dev_api_key=get("CONTEXT_DEV_API_KEY", ""),
            frontend_dist=dist_path,
        )
