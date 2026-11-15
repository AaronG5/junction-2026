"""B8: the launcher's new steps, with fakes (no processes started, nothing installed)."""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from veritas.config import ROOT

sys.path.insert(0, str(ROOT))  # the launcher package (local_ai/) lives at the repository root

from local_ai import __main__ as launcher  # noqa: E402
from local_ai import backend, paths, services, state, ui  # noqa: E402
from local_ai.hardware import GPU, Hardware  # noqa: E402
from local_ai.selector import Candidate  # noqa: E402


class Done(SimpleNamespace):
    pass


def completed(code=0, out="", err=""):
    return Done(returncode=code, stdout=out, stderr=err)


# --------------------------------------------------------------------------- steps and order

def test_steps_run_in_a_sensible_order():
    titles = [t for t, _ in launcher.STEPS]
    assert titles.index("Starting the databases (MySQL + Qdrant)") < titles.index("Preparing the database")
    assert titles.index("Preparing the database") < titles.index("Setting up vector search")
    assert titles.index("Loading the demo data") > titles.index("Preparing the database")
    assert titles[-1] == "Starting the dashboard"


def _ctx(**args):
    defaults = dict(demo_data=False, no_backend=False, skip_docker=False)
    return launcher.Context(args=SimpleNamespace(**{**defaults, **args}), state={})


def _net_args(**args):
    return SimpleNamespace(**{**dict(host=None, local=False, expose_databases=False), **args})


def test_demo_data_only_with_the_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(backend, "load_demo_data", lambda: calls.append(1) or "Demo data loaded: 7 claims")
    launcher.step_demo(_ctx())
    assert calls == []
    launcher.step_demo(_ctx(demo_data=True))
    assert calls == [1]


def test_backend_step_can_be_skipped(monkeypatch):
    started = []
    monkeypatch.setattr(backend, "start_backend", lambda *a, **k: started.append(1))
    monkeypatch.setattr(backend, "read_env", lambda: {"BACKEND_PORT": "8000"})
    launcher.step_backend(_ctx(no_backend=True))
    assert started == []
    ctx = _ctx()
    launcher.step_backend(ctx)
    assert started == [1] and ctx.results["dashboard"] == "http://127.0.0.1:8000"


def test_the_dashboard_address_is_the_one_users_can_open(monkeypatch):
    """Bound to every address, the launcher shows this computer's own address."""
    monkeypatch.delenv("BIND_HOST", raising=False)
    monkeypatch.setattr(backend, "read_env", lambda: {"BACKEND_PORT": "8000", "BIND_HOST": "0.0.0.0"})
    monkeypatch.setattr(services, "lan_ip", lambda: "192.0.2.10")
    assert backend.dashboard_url() == "http://192.0.2.10:8000"
    # The health check always goes to this computer, wherever the server listens.
    assert backend.backend_url() == "http://127.0.0.1:8000"


# --------------------------------------------------------------------------- loading the models

@pytest.fixture
def swap(monkeypatch, tmp_path):
    """A fake AI server: `answers` drives what each warm-up request returns."""
    log = {"posts": [], "answers": {}, "states": ""}
    monkeypatch.setattr(services, "read_env", lambda: {"LLAMA_SWAP_PORT": "8080"})
    monkeypatch.setattr(services.paths, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(services, "_running_states", lambda: log["states"])
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def post(url, payload, timeout=30):
        log["posts"].append(payload["model"])
        answer = log["answers"].get(payload["model"], (200, b"{}"))
        return answer.pop(0) if isinstance(answer, list) else answer

    monkeypatch.setattr(services, "http_post_json", post)
    return log


def test_both_models_are_loaded_by_asking_them_the_apps_own_question(swap):
    assert services.wait_models_loaded() is True
    # A real request, not a status poll: that is what makes llama-swap load a
    # model an earlier run left stopped.
    assert swap["posts"] == ["chat", "embedding"]


def test_a_model_that_is_still_starting_is_waited_for(swap):
    swap["answers"]["chat"] = [(None, b""), (503, b"loading"), (200, b"{}")]
    swap["states"] = "gemma-4-E4B-it: starting"
    assert services.load_model("chat", timeout=60) is True
    assert swap["posts"] == ["chat", "chat", "chat"]


def test_an_unknown_model_fails_at_once_instead_of_waiting(swap):
    swap["answers"]["chat"] = (404, b'{"error":"model not found"}')
    assert services.load_model("chat", timeout=900) is False
    assert swap["posts"] == ["chat"]  # no retries: waiting cannot fix the config


def test_a_model_that_never_loads_gives_up_and_points_at_the_log(swap, monkeypatch, capsys):
    # Every call to the clock moves it 10 s: each answer looks like a slow load, never a crash.
    monkeypatch.setattr(time, "time", itertools.count(0, 10).__next__)
    swap["answers"]["chat"] = (503, b"still loading")
    (swap_log := services.paths.LOGS_DIR / "llama-swap.log").write_text(
        "llama-server exited with code 1\nfailed to allocate buffer\n", encoding="utf-8")
    assert services.load_model("chat", timeout=600) is False
    out = capsys.readouterr().out
    assert str(swap_log) in out and "failed to allocate buffer" in out


def test_a_model_whose_process_dies_at_once_is_not_retried_for_ten_minutes(swap, capsys):
    """What the server showed: llama-swap answers 500 instantly, hundreds of times.
    Three instant failures are proof enough; after that only the log gets longer."""
    swap["answers"]["chat"] = (500, b"upstream command exited prematurely")
    (services.paths.LOGS_DIR / "llama-swap.log").write_text(
        "[INFO] starting gemma-3-1b-it\n"
        "gemma-3-1b-it: error while loading shared libraries: libgomp.so.1: cannot open shared object file\n"
        "[WARN] group: starting gemma-3-1b-it failed: upstream command exited prematurely\n"
        "[WARN] non-200 response, recording partial metrics: status=500, path=/v1/chat/completions\n" * 50,
        encoding="utf-8")
    assert services.load_model("chat", timeout=600) is False
    assert swap["posts"] == ["chat"] * 3
    out = capsys.readouterr().out
    assert "exits at once" in out and "upstream command exited prematurely" in out
    assert "libgomp.so.1" in out            # llama-server's own line, dug out from under the noise
    assert "non-200 response" not in out    # the noise itself isn't repeated


def test_a_slow_error_is_a_load_in_progress_not_a_crash(swap, monkeypatch):
    """A 503 that took 20 s to come back is llama-swap holding the request while the
    model loads; that must keep waiting, not count towards 'exits at once'."""
    monkeypatch.setattr(time, "time", itertools.count(0, 10).__next__)
    swap["answers"]["chat"] = [(503, b"loading")] * 5 + [(200, b"{}")]
    assert services.load_model("chat", timeout=600) is True
    assert swap["posts"] == ["chat"] * 6


def test_the_step_tells_a_broken_binary_from_a_broken_model(monkeypatch, capsys):
    monkeypatch.setattr(launcher.services, "start_llama_swap", lambda *a: True)
    monkeypatch.setattr(launcher.services, "swap_public_url", lambda env=None: "http://127.0.0.1:8080")
    monkeypatch.setattr(launcher.services, "wait_models_loaded", lambda: False)
    monkeypatch.setattr(launcher.runtime, "server_runs",
                        lambda server: (False, "SIGILL: it uses CPU instructions this computer does not have"))
    ctx = _ctx(no_ai_server=False)
    ctx.server, ctx.swap_binary, ctx.config_hash = "llama-server", "llama-swap", "h"
    launcher.step_ai_server(ctx)
    out = capsys.readouterr().out
    assert "cannot run on this computer" in out and "SIGILL" in out
    assert "ai" not in ctx.results

    monkeypatch.setattr(launcher.runtime, "server_runs", lambda server: (True, "version: 7000 (abc123)"))
    launcher.step_ai_server(ctx)
    assert "problem is with loading the model" in capsys.readouterr().out


@pytest.mark.parametrize("code,out,ok,expect", [
    (0, "version: 7000 (abc123)\nbuilt with gcc", True, "version: 7000"),
    (-4, "", False, "CPU instructions"),
    (127, "error while loading shared libraries: libgomp.so.1", False, "libgomp.so.1"),
    (1, "", False, "exit code 1"),
])
def test_server_runs_explains_why_llama_server_cannot_start(monkeypatch, code, out, ok, expect):
    from local_ai import runtime
    monkeypatch.setattr(runtime, "_run_server", lambda server, *a, **k: (code, out))
    got_ok, detail = runtime.server_runs("llama-server")
    assert got_ok is ok and expect in detail


def test_one_broken_model_does_not_hide_the_other(swap):
    swap["answers"]["chat"] = (404, b"no such model")
    assert services.wait_models_loaded(timeout=60) is False
    assert swap["posts"] == ["chat", "embedding"]  # the embedding model is still loaded


def test_an_ai_server_left_behind_on_the_port_is_called_out(monkeypatch, tmp_path, capsys):
    """Two llama-swaps means two copies of the models in memory - say so."""
    monkeypatch.setattr(services, "read_env", lambda: {"LLAMA_SWAP_PORT": "8080"})
    monkeypatch.setattr(services, "set_env_values", lambda values: None)
    monkeypatch.setattr(services, "port_free", lambda port: False)
    monkeypatch.setattr(services, "next_free_port", lambda port, taken: 8081)
    monkeypatch.setattr(services, "http_get", lambda url, timeout=3: (200, b'{"running": []}'))
    monkeypatch.setattr(services, "llama_swap_running", lambda: True)
    monkeypatch.setattr(services.paths, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(services.paths, "SWAP_PID", tmp_path / "swap.pid")
    monkeypatch.setattr(services.subprocess, "Popen", lambda cmd, **kw: SimpleNamespace(pid=7, poll=lambda: None))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert services.start_llama_swap("llama-swap", "hash1") is True
    out = capsys.readouterr().out
    assert "8081" in out and "left behind" in out and "--stop" in out


# --------------------------------------------------------------------------- where things listen

@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """A throwaway .env, so the tests never touch the real one."""
    monkeypatch.setattr(paths, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("BIND_HOST", raising=False)
    monkeypatch.delenv("DB_BIND_HOST", raising=False)
    state.ensure_env()
    return tmp_path / ".env"


def test_a_new_install_listens_on_this_computer_only(env_file):
    assert state.read_env()["BIND_HOST"] == "127.0.0.1"
    assert state.read_env()["DB_BIND_HOST"] == "127.0.0.1"


def test_the_host_choice_is_saved_for_every_service(env_file):
    launcher.apply_network_settings(_net_args(host="0.0.0.0"))
    env = state.read_env()
    # The dashboard and the AI server are on the network; the databases are not.
    assert env["BIND_HOST"] == "0.0.0.0"
    assert env["DB_BIND_HOST"] == "127.0.0.1"
    assert services.bind_host(env) == "0.0.0.0" and services.db_bind_host(env) == "127.0.0.1"

    # The choice sticks without repeating the flag, and --expose-databases adds those.
    launcher.apply_network_settings(_net_args(expose_databases=True))
    env = state.read_env()
    assert (env["BIND_HOST"], env["DB_BIND_HOST"]) == ("0.0.0.0", "0.0.0.0")

    # --local puts everything back on this computer.
    launcher.apply_network_settings(_net_args(local=True))
    env = state.read_env()
    assert (env["BIND_HOST"], env["DB_BIND_HOST"]) == ("127.0.0.1", "127.0.0.1")


def test_launch_sh_passes_its_default_in_the_environment(env_file, monkeypatch):
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    launcher.apply_network_settings(_net_args())
    assert state.read_env()["BIND_HOST"] == "0.0.0.0"
    # ...and a flag typed by the user still wins over it.
    launcher.apply_network_settings(_net_args(local=True))
    assert state.read_env()["BIND_HOST"] == "127.0.0.1"
    launcher.apply_network_settings(_net_args(host="192.0.2.10"))
    assert state.read_env()["BIND_HOST"] == "192.0.2.10"


@pytest.mark.skipif(os.name == "nt", reason="launch.sh is the Linux/macOS launcher")
def test_launch_sh_serves_the_network_but_keeps_what_env_says(tmp_path):
    """./launch.sh defaults to 0.0.0.0 and passes everything else through."""
    script = tmp_path / "launch.sh"
    script.write_bytes((ROOT / "launch.sh").read_bytes())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"  # stands in for uv: prints what the launcher would get
    uv.write_text('#!/bin/sh\necho "BIND_HOST=$BIND_HOST"\necho "args=$*"\n', encoding="utf-8")
    uv.chmod(0o755)

    def run():
        env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
        env.pop("BIND_HOST", None)
        done = subprocess.run(["bash", str(script), "--demo-data"], capture_output=True,
                              text=True, env=env, cwd=tmp_path, timeout=60)
        assert done.returncode == 0, done.stderr
        return done.stdout

    out = run()
    assert "BIND_HOST=0.0.0.0" in out
    assert "args=run --directory" in out and "--demo-data" in out  # flags passed through

    (tmp_path / ".env").write_text("# local install\nBIND_HOST=127.0.0.1\n", encoding="utf-8")
    assert "BIND_HOST=127.0.0.1" in run()


def test_migrations_run_once_per_launch(monkeypatch):
    runs = []
    monkeypatch.setattr(backend, "run_tool", lambda script, *a, **k: runs.append(script) or (0, "The database is up to date."))
    launcher.step_database(_ctx())
    assert runs == ["migrate.py"]


# --------------------------------------------------------------------------- the backend's tools

def test_migrate_waits_for_mysql_to_start(monkeypatch):
    answers = iter([(1, "Migration stopped. The database isn't reachable at 127.0.0.1:3307."),
                    (1, "Migration stopped. The database isn't reachable at 127.0.0.1:3307."),
                    (0, "Applied: 0005_analysis_jobs")])
    monkeypatch.setattr(backend, "run_tool", lambda *a, **k: next(answers))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert backend.migrate() == "Applied: 0005_analysis_jobs"


def test_a_real_migration_error_stops_at_once(monkeypatch):
    runs = []
    monkeypatch.setattr(backend, "run_tool", lambda *a, **k: runs.append(1) or (1, "Migration stopped. 0006_x.sql, statement 2 failed"))
    with pytest.raises(ui.StepFailed, match="0006_x.sql"):
        backend.migrate()
    assert len(runs) == 1


def test_waiting_for_mysql_gives_up(monkeypatch):
    monkeypatch.setattr(backend, "run_tool", lambda *a, **k: (1, "The database isn't reachable at x."))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(time, "time", lambda: next(clock))
    with pytest.raises(ui.StepFailed, match="isn't reachable"):
        backend.migrate(wait_seconds=90)


def test_run_tool_uses_this_python_and_reports_output(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["cwd"] = cmd, kw["cwd"]
        return completed(1, "", "Vector search setup stopped. Qdrant isn't reachable")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    code, out = backend.run_tool("setup_vectors.py")
    assert code == 1 and "isn't reachable" in out
    assert seen["cmd"][0] == sys.executable and seen["cmd"][1].endswith(os.path.join("backend", "setup_vectors.py"))
    assert seen["cwd"] == str(ROOT)


def test_demo_data_failure_is_a_plain_step_failure(monkeypatch):
    monkeypatch.setattr(backend, "run_tool", lambda *a, **k: (1, "Demo data not changed. This database already holds real data"))
    with pytest.raises(ui.StepFailed, match="already holds real data"):
        backend.load_demo_data()


# --------------------------------------------------------------------------- the dashboard build

@pytest.fixture
def frontend(tmp_path, monkeypatch):
    fe = tmp_path / "frontend"
    (fe / "src").mkdir(parents=True)
    (fe / "src" / "main.tsx").write_text("x", encoding="utf-8")
    (fe / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(backend, "FRONTEND_DIR", fe)
    monkeypatch.setattr(backend, "DIST_INDEX", fe / "dist" / "index.html")
    return fe


def _build(fe, age=0):
    (fe / "dist").mkdir(exist_ok=True)
    index = fe / "dist" / "index.html"
    index.write_text("<html>", encoding="utf-8")
    t = time.time() + age
    os.utime(index, (t, t))


def test_an_up_to_date_build_is_kept(frontend, monkeypatch):
    _build(frontend, age=+100)
    monkeypatch.setattr(backend.subprocess, "run", lambda *a, **k: pytest.fail("npm must not run"))
    assert backend.build_frontend(npm="npm") == "current"


def test_an_out_of_date_build_is_rebuilt(frontend, monkeypatch):
    _build(frontend, age=-100)
    calls = []
    monkeypatch.setattr(backend.subprocess, "run", lambda cmd, **k: calls.append(cmd[1:]) or completed())
    assert backend.build_frontend(npm="npm") == "built"
    assert calls == [["ci", "--no-audit", "--no-fund"], ["run", "build"]]  # no node_modules yet
    (frontend / "node_modules").mkdir()
    calls.clear()
    assert backend.build_frontend(npm="npm") == "built"
    assert calls == [["run", "build"]]


def test_a_changed_source_file_makes_the_build_out_of_date(frontend):
    _build(frontend, age=+100)
    assert backend.build_is_current()
    later = time.time() + 200
    os.utime(frontend / "src" / "main.tsx", (later, later))
    assert not backend.build_is_current()


def test_without_node(frontend):
    assert backend.build_frontend(npm="") == "missing"
    _build(frontend, age=-100)
    assert backend.build_frontend(npm="") == "prebuilt"


def test_a_failed_build_stops_the_launcher(frontend, monkeypatch):
    (frontend / "node_modules").mkdir()
    monkeypatch.setattr(backend.subprocess, "run", lambda *a, **k: completed(1))
    with pytest.raises(ui.StepFailed, match="npm run build"):
        backend.build_frontend(npm="npm")


# --------------------------------------------------------------------------- the backend server

@pytest.fixture
def server(tmp_path, monkeypatch):
    """Fakes for processes and ports; records what happens."""
    log = {"started": [], "stopped": [], "env": {"BACKEND_PORT": "8000"}, "running": False, "alive": set()}
    monkeypatch.setattr(backend, "BACKEND_PID", tmp_path / "backend.pid")
    monkeypatch.setattr(backend.paths, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(backend.paths, "STATE_DIR", tmp_path)
    monkeypatch.setattr(backend, "read_env", lambda: log["env"])
    monkeypatch.setattr(backend, "port_free", lambda port: True)
    monkeypatch.setattr(backend, "_pid_alive", lambda pid: pid in log["alive"])
    monkeypatch.setattr(backend, "backend_running", lambda env=None: log["running"])
    monkeypatch.setattr(backend, "wait_port_released", lambda port: None)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class Proc:
        pid = 4242

        def __init__(self, cmd, **kw):
            log["started"].append(cmd)
            log["alive"].add(4242)
            log["running"] = True

        def poll(self):
            return None

    monkeypatch.setattr(backend.subprocess, "Popen", Proc)
    monkeypatch.setattr(backend.subprocess, "run", lambda cmd, **k: log["stopped"].append(cmd) or log["alive"].discard(4242) or completed())
    return log


def test_starts_the_backend_and_remembers_it(server):
    assert backend.start_backend(wanted_hash="h1") == "started"
    assert server["started"] == [[sys.executable, str(backend.BACKEND_DIR / "run.py")]]
    assert json.loads(backend.BACKEND_PID.read_text(encoding="utf-8")) == {"pid": 4242, "hash": "h1", "port": "8000"}


def test_keeps_a_running_backend_when_nothing_changed(server):
    backend.start_backend(wanted_hash="h1")
    assert backend.start_backend(wanted_hash="h1") == "kept"
    assert len(server["started"]) == 1 and server["stopped"] == []


def test_restarts_only_when_its_config_changed(server):
    backend.start_backend(wanted_hash="h1")
    assert backend.start_backend(wanted_hash="h2") == "started"
    assert len(server["started"]) == 2 and len(server["stopped"]) == 1


def test_a_backend_that_dies_at_once_is_reported(server, monkeypatch):
    class Dead:
        pid = 1

        def __init__(self, cmd, **kw):
            pass

        def poll(self):
            return 1

    monkeypatch.setattr(backend.subprocess, "Popen", Dead)
    with pytest.raises(ui.StepFailed, match="stopped right away"):
        backend.start_backend(wanted_hash="h1")


def test_a_busy_port_moves_the_backend(server, monkeypatch):
    saved = {}
    monkeypatch.setattr(backend, "port_free", lambda port: port != "8000")
    monkeypatch.setattr(backend, "next_free_port", lambda port, taken: 8001)
    monkeypatch.setattr(backend, "set_env_values", lambda values: saved.update(values) or server["env"].update(values))
    monkeypatch.setattr(backend, "config_hash", lambda: "after-move")
    backend.start_backend(wanted_hash="h1")
    assert saved == {"BACKEND_PORT": "8001"}
    assert json.loads(backend.BACKEND_PID.read_text(encoding="utf-8"))["port"] == "8001"


def test_stop_leaves_no_backend_behind(server):
    backend.start_backend(wanted_hash="h1")
    assert backend.stop_backend() is True
    assert not backend.BACKEND_PID.exists() and 4242 not in server["alive"]
    assert backend.stop_backend() is False  # nothing to stop


def test_launcher_stop_also_stops_the_backend(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(backend, "stop_backend", lambda: calls.append("backend") or True)
    monkeypatch.setattr(launcher.services, "stop_llama_swap", lambda: calls.append("ai"))
    monkeypatch.setattr(launcher.services, "stop_docker_services", lambda: calls.append("docker"))
    launcher.cmd_stop(SimpleNamespace(skip_docker=False))
    assert calls == ["backend", "ai", "docker"]
    assert "Dashboard stopped." in capsys.readouterr().out


def test_config_hash_follows_env_and_code(tmp_path, monkeypatch):
    be = tmp_path / "backend"
    (be / "veritas").mkdir(parents=True)
    (be / "prompts").mkdir()
    (be / "veritas" / "a.py").write_text("x = 1", encoding="utf-8")
    (be / "run.py").write_text("run", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("BACKEND_PORT=8000\n", encoding="utf-8")
    monkeypatch.setattr(backend, "BACKEND_DIR", be)
    monkeypatch.setattr(backend.paths, "ENV_FILE", env)
    first = backend.config_hash()
    assert backend.config_hash() == first
    env.write_text("BACKEND_PORT=8001\n", encoding="utf-8")
    second = backend.config_hash()
    assert second != first
    (be / "veritas" / "a.py").write_text("x = 2", encoding="utf-8")
    assert backend.config_hash() != second


# --------------------------------------------------------------------------- what the System page gets

def test_hardware_summary_has_no_private_details():
    hw = Hardware(os="windows", arch="x64", cpu_cores=16, ram_total_gb=31.73, ram_available_gb=20, disk_free_gb=313.24,
                  gpus=[GPU("nvidia", "NVIDIA GeForce RTX 4070 Laptop GPU", 7.996)], backend="cuda")
    assert backend.hardware_summary(hw) == {"gpu": "NVIDIA GeForce RTX 4070 Laptop GPU", "vram_gb": 8.0, "ram_gb": 31.7, "disk_free_gb": 313.2}
    no_gpu = Hardware(os="linux", arch="x64", cpu_cores=4, ram_total_gb=8, ram_available_gb=4, disk_free_gb=50)
    assert backend.hardware_summary(no_gpu)["gpu"] is None


def test_upgrade_is_a_better_model_that_fits():
    def cand(mid, quality, fits=True):
        return Candidate({"id": mid, "quality": quality, "size_gb": quality * 2.0, "mmproj": None}, fits, "gpu")
    ranked = [cand("big", 9, fits=False), cand("better", 5), cand("chosen", 3), cand("small", 1)]
    size = lambda m: m["size_gb"]  # noqa: E731
    assert backend.upgrade_for(ranked, "chosen", size)["model"] == "better"
    assert backend.upgrade_for(ranked, "better", size) is None   # nothing better fits
    assert backend.upgrade_for(ranked, "unknown", size) is None


@pytest.mark.parametrize("answer, running", [
    ((200, b'{"status":"ok","version":"0.1.0"}'), True),
    ((200, b"<html>something else on this port</html>"), False),
    ((503, b""), False),
    ((None, b""), False),
])
def test_backend_running_reads_the_health_answer(monkeypatch, answer, running):
    monkeypatch.setattr(backend, "http_get", lambda url, timeout=3: answer)  # the real http_get returns bytes
    assert backend.backend_running({"BACKEND_PORT": "8000"}) is running
