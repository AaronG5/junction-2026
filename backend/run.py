"""Start the Veritas Lens backend:  uv run python backend/run.py

Listens on 127.0.0.1 only (never on the network), on BACKEND_PORT from .env (default 8000).
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
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
