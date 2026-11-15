"""Launcher steps for the Veritas Lens app itself (B8): database schema, vector
collections, the dashboard build, optional demo data, and the backend server.

The backend's own tools do the work (backend/migrate.py, setup_vectors.py,
demo_data.py, run.py); they are run as separate processes with the same Python,
so the launcher never imports the backend.
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from . import paths, ui
from .services import (http_get, next_free_port, on_the_network, port_free, reachable_host,
                       wait_port_released, _other_ports)
from .state import read_env, set_env_values

BACKEND_DIR = paths.ROOT / "backend"
FRONTEND_DIR = paths.ROOT / "frontend"
DIST_INDEX = FRONTEND_DIR / "dist" / "index.html"
BACKEND_PID = paths.STATE_DIR / "backend.pid"
BACKEND_LOG = "backend.log"
# Files whose change makes the dashboard build out of date.
FRONTEND_INPUTS = ("src", "public", "index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json")


def backend_url(env=None):
    """Loopback address, used for the health check: the launcher and the server
    are on the same computer, whatever BIND_HOST the server listens on."""
    env = env or read_env()
    return f"http://127.0.0.1:{env.get('BACKEND_PORT', '8000')}"


def dashboard_url(env=None):
    """The address to tell the user about: this computer's network address when
    the dashboard is bound to the network, loopback otherwise."""
    env = env or read_env()
    return f"http://{reachable_host(env)}:{env.get('BACKEND_PORT', '8000')}"


def run_tool(script, *args, timeout=600):
    """Run one of backend/*.py with this Python. Returns (exit code, output)."""
    try:
        proc = subprocess.run([sys.executable, str(BACKEND_DIR / script), *args], cwd=str(paths.ROOT),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"{script} took longer than {timeout} s."
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# ---- database schema and vector collections ------------------------------------------

def migrate(wait_seconds=90):
    """Apply database migrations. MySQL may still be starting, so retry a while."""
    deadline = time.time() + wait_seconds
    while True:
        code, out = run_tool("migrate.py")
        if code == 0:
            return out
        if "isn't reachable" not in out or time.time() > deadline:
            raise ui.StepFailed(out or "The database migrations failed.")
        time.sleep(3)


def setup_vectors(wait_seconds=60):
    deadline = time.time() + wait_seconds
    while True:
        code, out = run_tool("setup_vectors.py")
        if code == 0:
            return out
        if "isn't reachable" not in out or time.time() > deadline:
            raise ui.StepFailed(out or "Setting up vector search failed.")
        time.sleep(3)


def load_demo_data():
    code, out = run_tool("demo_data.py")
    if code != 0:
        raise ui.StepFailed(out or "The demo data could not be loaded.")
    return out


# Embedding tens of thousands of posts takes a while on a small GPU; the import
# commits as it goes, so a timeout only means "run the launcher again to continue".
IMPORT_TIMEOUT = 6 * 3600


def import_data(timeout=IMPORT_TIMEOUT):
    """Load data/parsed/ (the team's Telegram posts and news articles) and make their vectors."""
    code, out = run_tool("import_data.py", timeout=timeout)
    if code != 0:
        raise ui.StepFailed(out or "The collected data could not be loaded.")
    return out.splitlines()[-1] if out else "Collected data loaded."


# ---- the dashboard (frontend build) ---------------------------------------------------

def _newest_input_mtime():
    newest = 0.0
    for name in FRONTEND_INPUTS:
        p = FRONTEND_DIR / name
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
        elif p.is_dir():
            for root, _dirs, files in os.walk(p):
                for f in files:
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
    return newest


def build_is_current():
    return DIST_INDEX.is_file() and DIST_INDEX.stat().st_mtime >= _newest_input_mtime()


def _npm():
    return shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")


def packages_are_current():
    """True when node_modules is there and no newer than the lock file.

    npm stamps node_modules/.package-lock.json on every install, so an older
    stamp (or none) means the lock file gained packages since: pulling a branch
    that adds a dependency leaves a node_modules that can't build."""
    modules = FRONTEND_DIR / "node_modules"
    if not modules.is_dir():
        return False
    lock = FRONTEND_DIR / "package-lock.json"
    if not lock.is_file():
        return True
    stamp = modules / ".package-lock.json"
    installed = stamp.stat().st_mtime if stamp.is_file() else modules.stat().st_mtime
    return installed >= lock.stat().st_mtime


def build_frontend(npm=None):
    """Build the dashboard when Node.js is here and the build is out of date.
    Returns what happened: "current", "built", "prebuilt" (no Node, using the
    existing build) or "missing" (no Node and no build)."""
    if build_is_current():
        return "current"
    npm = npm if npm is not None else _npm()
    if not npm:
        return "prebuilt" if DIST_INDEX.is_file() else "missing"
    if not packages_are_current():
        code = subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=str(FRONTEND_DIR)).returncode
        if code != 0:
            raise ui.StepFailed("Installing the dashboard's packages failed (npm ci).")
    code = subprocess.run([npm, "run", "build"], cwd=str(FRONTEND_DIR)).returncode
    if code != 0:
        raise ui.StepFailed("Building the dashboard failed (npm run build).")
    return "built"


# ---- the backend server ------------------------------------------------------------------

def config_hash():
    """Changes when anything the running backend depends on changes: .env and its code."""
    h = hashlib.sha256()
    env_file = paths.ENV_FILE
    h.update(env_file.read_bytes() if env_file.is_file() else b"")
    for p in sorted((BACKEND_DIR / "veritas").rglob("*.py")) + [BACKEND_DIR / "run.py"] + sorted((BACKEND_DIR / "prompts").glob("*.md")):
        if "__pycache__" in p.parts or not p.is_file():
            continue
        h.update(str(p.relative_to(BACKEND_DIR)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _read_pid():
    try:
        data = json.loads(BACKEND_PID.read_text(encoding="utf-8"))
        return int(data["pid"]), data.get("hash"), str(data.get("port"))
    except (OSError, ValueError, KeyError, TypeError):
        return None, None, None


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out and "python" in out.lower()
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def backend_running(env=None):
    status, body = http_get(backend_url(env) + "/api/health")  # body is bytes
    return status == 200 and b'"ok"' in (body or b"")


def stop_backend():
    """Stop the backend this launcher started. Returns True when one was running."""
    pid, _, port = _read_pid()
    was_running = _pid_alive(pid)
    if was_running:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.5)
        if port:
            wait_port_released(port)
    BACKEND_PID.unlink(missing_ok=True)
    return was_running


def start_backend(wanted_hash=None, wait_seconds=30):
    """Start the backend in the background, or keep the running one when nothing changed."""
    env = read_env()
    port = env.get("BACKEND_PORT", "8000")
    wanted_hash = wanted_hash or config_hash()
    pid, running_hash, running_port = _read_pid()
    if _pid_alive(pid):
        if running_hash == wanted_hash and running_port == port and backend_running(env):
            ui.ok(f"Dashboard already running at {dashboard_url(env)}")
            return "kept"
        ui.info("The app or its settings changed; restarting the dashboard server...")
        stop_backend()

    if not port_free(port):
        new = next_free_port(port, _other_ports(env, "BACKEND_PORT"))
        ui.warn(f"Port {port} is already used by another program; using {new} for the dashboard.")
        set_env_values({"BACKEND_PORT": str(new)})
        port = str(new)
        wanted_hash = config_hash()  # .env changed

    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(paths.LOGS_DIR / BACKEND_LOG, "ab")
    cmd = [sys.executable, str(BACKEND_DIR / "run.py")]
    # The server reads .env itself, but an environment variable would win over
    # it; hand it the address .env holds, so both always agree.
    child_env = {**os.environ, "BIND_HOST": env.get("BIND_HOST", "127.0.0.1")}
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags, cwd=str(paths.ROOT), env=child_env)
    else:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(paths.ROOT), env=child_env)
    BACKEND_PID.write_text(json.dumps({"pid": proc.pid, "hash": wanted_hash, "port": port}), encoding="utf-8")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            raise ui.StepFailed(f"The dashboard server stopped right away. Details: {paths.LOGS_DIR / BACKEND_LOG}")
        if backend_running(read_env()):
            env = read_env()
            ui.ok(f"Dashboard running at {dashboard_url(env)}")
            if on_the_network(env.get("BIND_HOST", "127.0.0.1")):
                ui.info("Anyone who can reach this computer can open it: there is no login.")
            return "started"
        time.sleep(0.5)
    raise ui.StepFailed(f"The dashboard server didn't answer in time. Details: {paths.LOGS_DIR / BACKEND_LOG}")


# ---- what the System page shows about this computer -----------------------------------------

def hardware_summary(hw):
    """The part of the hardware the backend's System page shows (no paths, nothing private)."""
    gpu = hw.best_gpu
    return {
        "gpu": gpu.name if gpu else None,
        "vram_gb": round(gpu.vram_gb, 1) if gpu else None,
        "ram_gb": round(hw.ram_total_gb, 1),
        "disk_free_gb": round(hw.disk_free_gb, 1),
    }


def upgrade_for(ranked, chosen_id, download_gb):
    """A better model than the chosen one that still fits, if any (best first in `ranked`)."""
    chosen = next((c for c in ranked if c.model["id"] == chosen_id), None)
    if chosen is None:
        return None
    for c in ranked:
        if c.fits and c.model["quality"] > chosen.model["quality"]:
            return {"model": c.model["id"], "download_gb": round(download_gb(c.model), 1),
                    "note": "It gives better analyses but answers a little slower."}
    return None
