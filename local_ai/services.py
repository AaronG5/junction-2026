"""Background services: Docker (MySQL + Qdrant) and the llama-swap process."""
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

from . import paths, ui
from .state import read_env, set_env_values

DOCKER_INSTALL_URL = "https://docs.docker.com/get-started/get-docker/"

# Addresses that mean "this computer only"; anything else is reachable from the network.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
ANY_HOSTS = ("0.0.0.0", "::", "[::]")


def bind_host(env=None):
    """Where the dashboard and the AI server listen. .env is what counts here:
    the launcher writes the choice there at the start of every run."""
    return (env or read_env()).get("BIND_HOST") or "127.0.0.1"


def db_bind_host(env=None):
    """Where Docker publishes MySQL and Qdrant (.env DB_BIND_HOST)."""
    return (env or read_env()).get("DB_BIND_HOST") or "127.0.0.1"


def on_the_network(host):
    return host not in LOOPBACK_HOSTS


def lan_ip():
    """This computer's address on its network. Sends nothing: a connected UDP
    socket only makes the system pick the route it would use."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("203.0.113.1", 9))  # TEST-NET-3, never actually routed
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def reachable_host(env=None):
    """The address to show users: for 0.0.0.0 that is this computer's own
    network address, which is what they can type into a browser."""
    host = bind_host(env)
    if not on_the_network(host):
        return "127.0.0.1"
    return lan_ip() if host in ANY_HOSTS else host


def port_free(port):
    """True if nothing listens on the port. Checks by connecting first: on
    Windows a test bind can succeed even when the port is taken (e.g. by
    Docker Desktop's port forwarder), so a bind test alone is not reliable."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", int(port))) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", int(port)))
            return True
        except OSError:
            return False


PORT_KEYS = ("MYSQL_PORT", "QDRANT_PORT", "QDRANT_GRPC_PORT", "LLAMA_SWAP_PORT")


def next_free_port(start, taken=()):
    """Next port after `start` that is free and not assigned to another service."""
    taken = {str(p) for p in taken}
    port = int(start) + 1
    while not port_free(port) or str(port) in taken:
        port += 1
    return port


def _other_ports(env, key):
    return [env[k] for k in PORT_KEYS if k != key and env.get(k)]


def wait_port_released(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_free(port):
            return True
        time.sleep(0.5)
    return False


def http_get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError):
        return None, b""


# ---- Docker -------------------------------------------------------------------

def _docker_ready():
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _compose_cmd():
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def _start_docker_daemon():
    system = platform.system()
    if system == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"):
            exe = os.path.join(base, "Docker", "Docker", "Docker Desktop.exe")
            if os.path.exists(exe):
                subprocess.Popen([exe], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                return True
    elif system == "Darwin":
        return subprocess.run(["open", "-a", "Docker"]).returncode == 0
    elif system == "Linux":
        ui.info("Docker is installed but not running. Start it with:  sudo systemctl start docker")
        ui.info("(and to avoid needing sudo: sudo usermod -aG docker $USER, then log in again)")
    return False


def _container_running(name):
    out = subprocess.run(["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                         capture_output=True, text=True).stdout
    return name in out.split()


def _resolve_port_conflicts(env):
    """If another program (e.g. another MySQL) already uses a port, move ours."""
    changed = False
    for key, container in (("MYSQL_PORT", "junction-mysql"), ("QDRANT_PORT", "junction-qdrant"),
                           ("QDRANT_GRPC_PORT", "junction-qdrant")):
        port = env.get(key)
        if not port:
            continue
        duplicate = port in _other_ports(env, key)
        busy = not port_free(port) and not _container_running(container)
        if duplicate or busy:
            new = str(next_free_port(port, _other_ports(env, key)))
            why = "is also assigned to another service" if duplicate else "is already used by another program"
            ui.warn(f"Port {port} {why}; using {new} for {key}.")
            env[key] = new
            changed = True
    if changed:
        set_env_values({k: env[k] for k in PORT_KEYS if k in env})
    return read_env()


def _wait_healthy(container, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}}", container],
                             capture_output=True, text=True).stdout.strip()
        if out == "healthy":
            return True
        time.sleep(3)
    return False


def start_docker_services():
    """Returns True if MySQL and Qdrant are running. Never raises: the AI part
    works without the databases, so problems are reported as warnings."""
    if not shutil.which("docker"):
        ui.warn("Docker is not installed, so the database services were skipped.")
        ui.info(f"Install Docker Desktop from {DOCKER_INSTALL_URL} and run the launcher again.")
        return False
    if not _docker_ready():
        ui.info("Starting Docker (this can take a minute)...")
        if _start_docker_daemon():
            for _ in range(60):
                if _docker_ready():
                    break
                time.sleep(3)
        if not _docker_ready():
            ui.warn("Docker is not running, so the database services were skipped. "
                    "Start Docker and run the launcher again.")
            return False
    compose = _compose_cmd()
    if not compose:
        ui.warn("Docker Compose is missing; please update Docker. Database services skipped.")
        return False

    ui.info("Starting MySQL and Qdrant (first run downloads the images)...")
    # Other containers with restart policies may grab ports while Docker is
    # still starting up, so a port conflict gets one more try with new ports.
    for attempt in range(2):
        env = _resolve_port_conflicts(read_env())
        result = subprocess.run([*compose, "--env-file", str(paths.ENV_FILE),
                                 "-f", str(paths.COMPOSE_FILE), "up", "-d"],
                                capture_output=True, text=True)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            break
        port_conflict = "already in use" in output or "port is already allocated" in output
        if not (port_conflict and attempt == 0):
            print(output)
            ui.warn("Could not start the database containers (see the Docker output above).")
            return False
        time.sleep(3)
    if not _wait_healthy("junction-mysql"):
        ui.warn("MySQL is taking unusually long to start; check: docker logs junction-mysql")
        return False
    status, _ = http_get(f"http://127.0.0.1:{env['QDRANT_PORT']}/readyz")
    for _ in range(20):
        if status == 200:
            break
        time.sleep(1)
        status, _ = http_get(f"http://127.0.0.1:{env['QDRANT_PORT']}/readyz")
    dbh = db_bind_host(env)
    ui.ok(f"MySQL on {dbh}:{env['MYSQL_PORT']}, Qdrant on {dbh}:{env['QDRANT_PORT']}")
    return True


def stop_docker_services():
    compose = _compose_cmd() if shutil.which("docker") else None
    if compose and _docker_ready():
        subprocess.run([*compose, "--env-file", str(paths.ENV_FILE), "-f", str(paths.COMPOSE_FILE), "stop"])


# ---- llama-swap -----------------------------------------------------------------

def swap_url(env=None):
    """Always the loopback address: used for health checks and by the backend,
    which runs on the same computer. Shown to users as swap_public_url()."""
    env = env or read_env()
    return f"http://127.0.0.1:{env.get('LLAMA_SWAP_PORT', '8080')}"


def swap_public_url(env=None):
    env = env or read_env()
    return f"http://{reachable_host(env)}:{env.get('LLAMA_SWAP_PORT', '8080')}"


def _read_pid():
    """(pid, config_hash, port) of the llama-swap this launcher started."""
    try:
        parts = paths.SWAP_PID.read_text().split() + [None, None]
        return int(parts[0]), parts[1], parts[2]
    except (OSError, ValueError, IndexError):
        return None, None, None


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return "llama-swap" in out.lower()
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_llama_swap():
    pid, _, port = _read_pid()
    if _pid_alive(pid):
        if os.name == "nt":  # /T also ends the llama-server child processes
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
    paths.SWAP_PID.unlink(missing_ok=True)


def llama_swap_running():
    status, _ = http_get(swap_url() + "/v1/models")
    return status == 200


def start_llama_swap(binary, config_hash):
    env = read_env()
    port = env.get("LLAMA_SWAP_PORT", "8080")
    pid, running_hash, running_port = _read_pid()
    if _pid_alive(pid):
        if running_hash == config_hash and running_port == port and llama_swap_running():
            ui.ok(f"AI server already running at {swap_url(env)}")
            return True
        ui.info("Configuration changed; restarting the AI server...")
        stop_llama_swap()

    if not port_free(port):
        new = next_free_port(port, _other_ports(env, "LLAMA_SWAP_PORT"))
        ui.warn(f"Port {port} is already used by another program; using {new} for the AI server.")
        set_env_values({"LLAMA_SWAP_PORT": str(new)})
        port = str(new)

    paths.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log = open(paths.LOGS_DIR / "llama-swap.log", "ab")
    cmd = [str(binary), "-config", str(paths.SWAP_CONFIG), "-listen", f"{bind_host(env)}:{port}"]
    if os.name == "nt":
        # Own hidden console (not DETACHED_PROCESS): it survives closing the
        # launcher window, and the llama-server children llama-swap spawns
        # inherit the hidden console instead of each opening a visible one.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags, cwd=str(paths.ROOT))
    else:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(paths.ROOT))
    paths.SWAP_PID.write_text(f"{proc.pid} {config_hash} {port}")

    for _ in range(60):
        if proc.poll() is not None:
            ui.err(f"The AI server stopped right away. Details: {paths.LOGS_DIR / 'llama-swap.log'}")
            return False
        if llama_swap_running():
            ui.ok(f"AI server running at {swap_public_url()} (OpenAI-compatible API, /v1)")
            return True
        time.sleep(0.5)
    ui.warn(f"The AI server has not answered yet. Details: {paths.LOGS_DIR / 'llama-swap.log'}")
    return False


def wait_models_loaded(timeout=900):
    """Wait until both preloaded models report ready (first load can take a while)."""
    url = swap_url() + "/running"
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = http_get(url)
        if status == 200:
            try:
                running = json.loads(body).get("running", [])
                states = [r.get("state") for r in running]
                if len(states) >= 2 and all(s == "ready" for s in states):
                    return True
            except (ValueError, AttributeError):
                pass
        time.sleep(2)
    return False
