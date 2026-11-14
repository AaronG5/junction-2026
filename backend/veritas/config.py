"""Backend settings, read from the repository's .env (written by the launcher).

Environment variables override .env, which is handy for tests and deployment.
Passwords are kept out of repr(), so settings can be logged safely.

The server listens on BIND_HOST (`.env`, default 127.0.0.1 = this computer only).
Set it to 0.0.0.0 to serve a whole server to the network; the launcher's
`--host` flag writes it. Host-header checking follows: a loopback server keeps
the strict 127.0.0.1/localhost list (that is what blocks DNS rebinding from
websites), a network server accepts any host name unless ALLOWED_HOSTS says
which ones.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Addresses that mean "this computer only".
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def allowed_hosts_for(host: str, configured: str = "") -> tuple[str, ...]:
    """Which Host headers requests may carry. ALLOWED_HOSTS wins when set."""
    names = tuple(h.strip() for h in configured.split(",") if h.strip())
    if names:
        return names
    if is_loopback(host):
        return ("127.0.0.1", "localhost")
    # Reachable from the network: the browser addresses it by server name or IP,
    # which this process cannot know, so any host name is accepted.
    return ("*",)


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

    @property
    def on_the_network(self) -> bool:
        """True when the server is reachable from outside this computer."""
        return not is_loopback(self.host)

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

        # BACKEND_HOST is the server's own override; BIND_HOST is what the
        # launcher writes for every service at once.
        host = get("BACKEND_HOST", get("BIND_HOST", "127.0.0.1"))
        return cls(
            host=host,
            allowed_hosts=allowed_hosts_for(host, get("ALLOWED_HOSTS", "")),
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
