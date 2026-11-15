"""The Narratives screen's "Update clusters" button: GET/POST /api/clustering (veritas/clustering_job.py).

The real work has its own tests (test_b9_clustering.py, test_clustering_update.py); here it is
swapped for a fake so the job's states, progress messages, the one-at-a-time rule and the
error messages are checked fast.
"""
import threading
import time

import pytest

from veritas import clustering_job
from veritas.ai import AiUnavailable
from veritas.clustering import ClusteringUnavailable
from veritas.clustering_job import summary
from veritas.routes.clustering import DAYS_ONLY_FOR_REBUILD


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


UPDATED = {"status": "done", "mode": "update", "posts": 1234, "joined": 1200, "new_clusters": 2,
           "clusters": [{"cluster_id": 1}], "insight": {"posts": 6}}


def test_idle_before_any_run(client):
    assert client.get("/api/clustering").json() == {
        "status": "idle", "message": None, "mode": None, "days": None, "startedAt": None, "finishedAt": None}


def test_the_button_runs_an_update_and_reports_progress(client):
    gate, said = threading.Event(), threading.Event()
    seen = []

    def run(mode, days, say):
        seen.append((mode, days))
        say("Adding 1,234 new posts to the clusters.")
        said.set()
        gate.wait(5)
        return UPDATED

    use_fake(client, run)
    res = client.post("/api/clustering", json={})
    assert res.status_code == 202
    assert res.json()["status"] == "running" and res.json()["mode"] == "update" and res.json()["startedAt"]
    said.wait(5)
    running = client.get("/api/clustering").json()
    assert running["status"] == "running" and running["message"] == "Adding 1,234 new posts to the clusters."
    gate.set()
    state = wait_until_finished(client)
    assert state["status"] == "done" and state["finishedAt"]
    assert state["message"] == "Added 1,234 new posts: 1,200 joined existing clusters, 2 new narratives. Read 6 typical posts."
    assert seen == [("update", None)]


def test_a_rebuild_can_be_asked_for(client):
    seen = []
    use_fake(client, lambda mode, days, say: seen.append((mode, days)) or {"status": "done", "mode": "full", "posts": 90,
                                                                          "clusters": [{}, {}, {}]})
    assert client.post("/api/clustering", json={"rebuild": True, "days": 30}).status_code == 202
    assert wait_until_finished(client)["message"] == "3 clusters from 90 posts."
    assert client.post("/api/clustering", json={"rebuild": True}).status_code == 202
    wait_until_finished(client)
    assert seen == [("rebuild", 30), ("rebuild", None)]


def test_days_only_with_a_rebuild(client):
    use_fake(client, lambda *a: pytest.fail("must not start"))
    res = client.post("/api/clustering", json={"days": 7})
    assert res.status_code == 400 and res.json()["detail"] == DAYS_ONLY_FOR_REBUILD


def test_only_one_run_at_a_time(client):
    gate = threading.Event()
    calls = []

    def run(mode, days, say):
        calls.append(mode)
        gate.wait(5)
        return UPDATED

    use_fake(client, run)
    assert client.post("/api/clustering", json={}).status_code == 202
    again = client.post("/api/clustering", json={"rebuild": True})
    assert again.status_code == 200 and again.json()["status"] == "running" and again.json()["mode"] == "update"
    gate.set()
    wait_until_finished(client)
    assert calls == ["update"]
    assert client.post("/api/clustering", json={}).status_code == 202   # finished: the button works again
    wait_until_finished(client)
    assert calls == ["update", "update"]


@pytest.mark.parametrize("result,status,message", [
    ({"status": "up_to_date", "message": "No new posts since the last update."}, "up_to_date", "No new posts since the last update."),
    ({"status": "too_few", "message": "Only 3 posts in this window; at least 20 are needed. No clusters made."}, "too_few",
     "Only 3 posts in this window; at least 20 are needed. No clusters made."),
    ({**UPDATED, "new_clusters": 0, "insight": {"posts": 0}}, "done", "Added 1,234 new posts: 1,200 joined existing clusters."),
])
def test_finished_states(client, result, status, message):
    use_fake(client, lambda mode, days, say: result)
    client.post("/api/clustering", json={})
    state = wait_until_finished(client)
    assert (state["status"], state["message"]) == (status, message)


def test_a_plain_error_is_shown_as_is(client):
    def run(mode, days, say):
        raise AiUnavailable("The local AI isn't running. Start it with the launcher.")

    use_fake(client, run)
    client.post("/api/clustering", json={})
    state = wait_until_finished(client)
    assert (state["status"], state["message"]) == ("failed", "The local AI isn't running. Start it with the launcher.")


def test_an_unexpected_error_is_not_leaked(client):
    def run(mode, days, say):
        raise RuntimeError("secret internals /home/x")

    use_fake(client, run)
    client.post("/api/clustering", json={})
    state = wait_until_finished(client)
    assert state["status"] == "failed" and state["message"] == clustering_job.SERVER_ERROR


@pytest.mark.parametrize("body", [{"rebuild": True, "days": 0}, {"rebuild": True, "days": 366}, {"rebuild": True, "days": "7"},
                                  {"rebuild": True, "days": 1.5}, {"rebuild": "yes"}, {"rebuild": 1}])
def test_bad_requests_are_refused(client, body):
    use_fake(client, lambda *a: pytest.fail("must not start"))
    assert client.post("/api/clustering", json=body).status_code == 422
    assert client.get("/api/clustering").json()["status"] == "idle"


def test_missing_packages_are_reported_before_starting(client, monkeypatch):
    def missing():
        raise ClusteringUnavailable("Clustering needs extra packages. Install them with:  uv sync --extra clustering")

    monkeypatch.setattr(clustering_job, "_libs", missing)
    res = client.post("/api/clustering", json={})
    assert res.status_code == 503 and "uv sync --extra clustering" in res.json()["detail"]
    assert client.get("/api/clustering").json()["status"] == "idle"


def test_summary_of_a_first_full_run():
    assert summary({"status": "done", "mode": "full", "posts": 1500, "clusters": [{}] * 12, "insight": {"posts": 36}}) == \
        ("done", "12 clusters from 1,500 posts. Read 36 typical posts.")
