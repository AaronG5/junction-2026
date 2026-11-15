"""Start the Providence backend:  uv run python backend/run.py

Listens on BIND_HOST:BACKEND_PORT from .env (default 127.0.0.1:8000 = this
computer only). BIND_HOST=0.0.0.0 serves the network too, which is what the
launcher writes for a server install (./launch.sh --host 0.0.0.0).
Settings are read once, here, and handed to the app.
"""
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veritas.config import Settings  # noqa: E402
from veritas.main import create_app  # noqa: E402


def main() -> None:
    settings = Settings.load()
    if settings.on_the_network:
        print(f"Listening on {settings.host}:{settings.port} - reachable from the network. "
              "The dashboard has no login of its own, so on a public server put it behind "
              "a reverse proxy that adds HTTPS and a password.", flush=True)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
