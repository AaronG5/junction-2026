"""The Narratives screen's "Update clusters" button: GET/POST /api/clustering (veritas/clustering_job.py).

The real work (run_clustering) has its own tests in test_b9_clustering.py; here it is swapped
for a fake so the job's states, the one-at-a-time rule and the error messages are checked fast.
"""
import threading
import time

import pytest

from veritas import clustering_job
from veritas.ai import AiUnavailable
from veritas.clustering import ClusteringUnavailable


def wait_until_finished(client, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = client.get("/api/clustering").json()
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("the clustering job never finished")


def use_fake(client, run):
    client.app.state.clustering.run = run


def test_idle_before_any_run(client):
    assert client.get("/api/clustering").json() == {
        "status": "idle", "message": None, "days": None, "startedAt": None, "finishedAt": None}


def test_a_run_goes_from_running_to_done(client):
    gate = threading.Event()
    seen = []

    def run(days):
        seen.append(days)
        gate.wait(5)
        return {"status": "done", "posts": 1234, "clusters": [{}, {}, {}]}

    use_fake(client, run)
    res = client.post("/api/clustering", json={"days": 30})
    assert res.status_code == 202
    assert res.json()["status"] == "running" and res.json()["days"] == 30 and res.json()["startedAt"]
    assert client.get("/api/clustering").json()["status"] == "running"
    gate.set()
    state = wait_until_finished(client)
    assert state["status"] == "done"
    assert state["message"] == "3 clusters from 1,234 posts."
    assert state["finishedAt"]
    assert seen == [30]


def test_only_one_run_at_a_time(client):
    gate = threading.Event()
    calls = []

    def run(days):
        calls.append(days)
        gate.wait(5)
        return {"status": "done", "posts": 0, "clusters": []}

    use_fake(client, run)
    assert client.post("/api/clustering", json={"days": 7}).status_code == 202
    again = client.post("/api/clustering", json={"days": 90})
    assert again.status_code == 200
    assert again.json()["status"] == "running" and again.json()["days"] == 7
    gate.set()
    wait_until_finished(client)
    assert calls == [7]
    # Finished: the button works again.
    assert client.post("/api/clustering", json={"days": 90}).status_code == 202
    wait_until_finished(client)
    assert calls == [7, 90]


def test_default_is_seven_days(client):
    seen = []
    use_fake(client, lambda days: seen.append(days) or {"status": "done", "posts": 0, "clusters": []})
    assert client.post("/api/clustering").status_code == 202
    wait_until_finished(client)
    assert seen == [7]


def test_too_few_posts_passes_the_message_on(client):
    use_fake(client, lambda days: {"status": "too_few", "posts": 3,
                                   "message": "Only 3 posts in this window; at least 20 are needed. No clusters made."})
    client.post("/api/clustering", json={"days": 7})
    state = wait_until_finished(client)
    assert state["status"] == "too_few"
    assert state["message"].startswith("Only 3 posts in this window")


def test_a_plain_error_is_shown_as_is(client):
    def run(days):
        raise AiUnavailable("The local AI isn't running. Start it with the launcher.")

    use_fake(client, run)
    client.post("/api/clustering", json={"days": 7})
    state = wait_until_finished(client)
    assert state == {**state, "status": "failed", "message": "The local AI isn't running. Start it with the launcher."}


def test_an_unexpected_error_is_not_leaked(client):
    def run(days):
        raise RuntimeError("secret internals /home/x")

    use_fake(client, run)
    client.post("/api/clustering", json={"days": 7})
    state = wait_until_finished(client)
    assert state["status"] == "failed"
    assert state["message"] == clustering_job.SERVER_ERROR
    assert "secret" not in state["message"]


@pytest.mark.parametrize("days", [0, 366, -1, "a week", 1.5])
def test_days_out_of_range_is_refused(client, days):
    use_fake(client, lambda d: pytest.fail("must not start"))
    res = client.post("/api/clustering", json={"days": days})
    assert res.status_code == 422
    assert client.get("/api/clustering").json()["status"] == "idle"


def test_missing_packages_are_reported_before_starting(client, monkeypatch):
    def missing():
        raise ClusteringUnavailable("Clustering needs extra packages. Install them with:  uv sync --extra clustering")

    monkeypatch.setattr(clustering_job, "_libs", missing)
    res = client.post("/api/clustering", json={"days": 7})
    assert res.status_code == 503
    assert "uv sync --extra clustering" in res.json()["detail"]
    assert client.get("/api/clustering").json()["status"] == "idle"
