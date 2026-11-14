"""Every filesystem location the launcher uses, in one place.

All paths hang off the repository root (the folder containing launch.sh), so
the launcher behaves the same no matter which directory it is started from.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_file_value(key):
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


# Models can be large; let users put them on another disk via .env / env var.
MODELS_DIR = Path(os.environ.get("JUNCTION_MODELS_DIR")
                  or _env_file_value("JUNCTION_MODELS_DIR")
                  or ROOT / "models")
EMBEDDINGS_DIR = MODELS_DIR / "embeddings"

BIN_DIR = ROOT / "bin"            # llama.cpp + llama-swap binaries
DOWNLOADS_DIR = BIN_DIR / "_downloads"
LOGS_DIR = ROOT / "logs"
STATE_DIR = ROOT / ".local_ai"    # launcher state, generated configs, pid files
STATE_FILE = STATE_DIR / "state.json"
SWAP_CONFIG = STATE_DIR / "llama-swap.yaml"
SWAP_PID = STATE_DIR / "llama-swap.pid"

ENV_FILE = ROOT / ".env"
COMPOSE_FILE = ROOT / "docker" / "docker-compose.yml"
CATALOG_FILE = Path(__file__).resolve().parent / "models.json"


def ensure_dirs():
    for d in (MODELS_DIR, EMBEDDINGS_DIR, BIN_DIR, DOWNLOADS_DIR, LOGS_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)
