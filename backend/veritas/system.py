"""The System page: what runs on this computer, and whether it's up (B7).

- The AI: which models llama-swap has loaded (asked live), where they run and the
  hardware (from the launcher's state file, .local_ai/state.json, which the
  launcher writes when it picks a model), and a stronger model that would fit.
- The three services, each checked live with a short timeout. A service that's
  down shows as stopped; the page itself always loads.
Nothing secret is ever included (no passwords, keys or file paths).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .config import ROOT, Settings
from .db import DatabaseUnavailable, connect
from .models import AiStatus, Service, SystemStatus, Upgrade

STATE_FILE = ROOT / ".local_ai" / "state.json"
CHECK_TIMEOUT = 2.0
EMBEDDING_NOTE = "768 numbers per post · 100+ languages"
PLACEMENT = {"cuda": "Runs on the graphics card", "vulkan": "Runs on the graphics card",
             "metal": "Runs on the graphics chip", "cpu": "Runs on the processor"}


def read_state(path: Path = STATE_FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _llama(settings: Settings) -> dict | None:
    """llama-swap's model list by alias, or None when it isn't running."""
    try:
        res = httpx.get(f"http://127.0.0.1:{settings.llama_swap_port}/v1/models", timeout=CHECK_TIMEOUT)
        res.raise_for_status()
        models = res.json().get("data", [])
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return {m.get("id"): m for m in models if isinstance(m, dict)}


def _database(settings: Settings) -> bool:
    try:
        conn = connect(settings, read_timeout=int(CHECK_TIMEOUT) + 1)
    except DatabaseUnavailable:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return cur.fetchone()["ok"] == 1
    except Exception:  # noqa: BLE001 - any failure means "not working"
        return False
    finally:
        conn.close()


def _qdrant(settings: Settings) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{settings.qdrant_port}/", timeout=CHECK_TIMEOUT).status_code == 200
    except httpx.HTTPError:
        return False


def _model_id(models: dict | None, alias: str) -> tuple[str | None, dict]:
    entry = (models or {}).get(alias) or {}
    meta = (entry.get("meta") or {}).get("llamaswap") or {}
    return (meta.get("modelID") or (entry.get("id") if entry else None)), meta


def _hardware(state: dict) -> tuple[str, str]:
    hw = state.get("hardware")
    if not isinstance(hw, dict):
        return "Not detected yet", "Start Veritas Lens with the launcher to detect it."
    gpu, vram = hw.get("gpu"), hw.get("vram_gb")
    name = f"{gpu} · {vram:g} GB" if gpu and isinstance(vram, (int, float)) else (gpu or "No graphics card (processor only)")
    parts = []
    if isinstance(hw.get("ram_gb"), (int, float)):
        parts.append(f"{round(hw['ram_gb'])} GB memory")
    if isinstance(hw.get("disk_free_gb"), (int, float)):
        parts.append(f"{round(hw['disk_free_gb'])} GB free disk")
    return name, " · ".join(parts) or "Memory and disk not detected"


def _upgrade(state: dict, current: str | None) -> Upgrade | None:
    up = state.get("upgrade")
    if not isinstance(up, dict) or not up.get("model") or up.get("model") == current:
        return None
    try:
        return Upgrade(model=str(up["model"]), download_gb=float(up.get("download_gb", 0)),
                       note=str(up.get("note") or "It gives better analyses but answers a little slower."))
    except (TypeError, ValueError):
        return None


def system_status(settings: Settings, state: dict | None = None) -> SystemStatus:
    state = read_state() if state is None else state
    with ThreadPoolExecutor(max_workers=3) as pool:  # three checks at once: the page waits ~2 s at most
        llama_f, db_f, qdrant_f = pool.submit(_llama, settings), pool.submit(_database, settings), pool.submit(_qdrant, settings)
        models, db_up, qdrant_up = llama_f.result(), db_f.result(), qdrant_f.result()

    chat, chat_meta = _model_id(models, "chat")
    embedding, _ = _model_id(models, "embedding")
    chat = chat or state.get("chat_model") or "Not chosen yet"
    note = PLACEMENT.get(state.get("backend"), "Where it runs isn't known yet")
    if chat_meta.get("vision"):
        note += " · reads images"
    hardware, hardware_note = _hardware(state)
    return SystemStatus(
        ai=AiStatus(running=models is not None, chat_model=chat, chat_model_note=note,
                    embedding_model=embedding or "embeddinggemma-300m", embedding_note=EMBEDDING_NOTE,
                    hardware=hardware, hardware_note=hardware_note, upgrade=_upgrade(state, chat)),
        services=[
            Service(name="AI server", role="llama-swap · OpenAI-compatible API",
                    address=f"127.0.0.1:{settings.llama_swap_port}", running=models is not None),
            Service(name="Database", role="MySQL 8.4 · posts, sources, claims",
                    address=f"{settings.mysql_host}:{settings.mysql_port}", running=db_up),
            Service(name="Vector search", role="Qdrant · similar-post search",
                    address=f"127.0.0.1:{settings.qdrant_port}", running=qdrant_up),
        ],
    )
