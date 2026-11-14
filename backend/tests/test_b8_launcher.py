"""B8: the launcher's new steps, with fakes (no processes started, nothing installed)."""
from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from veritas.config import ROOT

sys.path.insert(0, str(ROOT))  # the launcher package (local_ai/) lives at the repository root

from local_ai import __main__ as launcher  # noqa: E402
from local_ai import backend, ui  # noqa: E402
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
    launcher.step_backend(_ctx(no_backend=True))
    assert started == []
    ctx = _ctx()
    launcher.step_backend(ctx)
    assert started == [1] and ctx.results["dashboard"].startswith("http://127.0.0.1:")


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
