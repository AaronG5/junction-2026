"""B9: clustering, on synthetic data with known groups (no real AI needed).

Needs the optional packages (uv sync --extra clustering); skipped without them.
Integration tests use a throwaway database and throwaway Qdrant collections.
"""
from __future__ import annotations

import dataclasses
import json
import math
import secrets
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterator

import httpx
import pytest

pytest.importorskip("umap", reason="clustering packages not installed (uv sync --extra clustering)")
np = pytest.importorskip("numpy")

import cluster as cluster_cli  # noqa: E402
from conftest import make_client, scratch_database  # noqa: E402
from veritas import clustering  # noqa: E402
from veritas.ai import LocalAI  # noqa: E402
from veritas.clustering import group, match_narratives, run_clustering  # noqa: E402
from veritas.config import Settings  # noqa: E402
from veritas.content import store_post_vector  # noqa: E402
from veritas.db import connection  # noqa: E402
from veritas.db.migrations import migrate  # noqa: E402
from veritas.ids import post_uuid  # noqa: E402
from veritas.vectors import Qdrant, VectorStoreUnavailable  # noqa: E402

D = 768


def unit(v):
    return v / np.linalg.norm(v)


def blobs(seed=1, sizes=(30, 30, 30), scattered=10, spread=0.15, directions=(0, 1, 2)):
    """Groups around fixed directions, plus scattered posts. Returns (vectors, true group per vector)."""
    rng = np.random.default_rng(seed)
    vectors, truth = [], []
    for k, (size, axis) in enumerate(zip(sizes, directions)):
        for _ in range(size):
            vectors.append(unit(np.eye(D)[axis] + spread * unit(rng.normal(size=D))).tolist())
            truth.append(k)
    for _ in range(scattered):
        vectors.append(unit(rng.normal(size=D)).tolist())
        truth.append(-1)
    return vectors, truth


@pytest.fixture(scope="module")
def grouped():
    vectors, truth = blobs()
    return vectors, truth, group(vectors)


# --------------------------------------------------------------------------- the grouping itself

def test_three_clear_groups_are_found_and_scattered_posts_fit_none(grouped):
    _vectors, truth, g = grouped
    found = {k: Counter(g.labels[i] for i, t in enumerate(truth) if t == k) for k in (0, 1, 2, -1)}
    for k in (0, 1, 2):
        assert len(found[k]) == 1 and -1 not in found[k], found  # each group is exactly one cluster
    assert len({next(iter(found[k])) for k in (0, 1, 2)}) == 3   # three different clusters
    assert found[-1] == Counter({-1: 10})                         # scattered posts: noise
    assert len(g.centres) == 3


def test_the_same_input_always_gives_the_same_result(grouped):
    vectors, _truth, g = grouped
    again = group(vectors)
    assert again.labels == g.labels and again.map_xy == g.map_xy and again.rank == g.rank


def test_typical_posts_are_closest_to_the_centre_and_edge_posts_farthest(grouped):
    vectors, _truth, g = grouped
    for label, centre in g.centres.items():
        members = [i for i, lab in enumerate(g.labels) if lab == label]
        by_rank = sorted(members, key=lambda i: g.rank[i])
        assert [g.rank[i] for i in by_rank] == list(range(1, len(members) + 1))
        sims = [float(np.dot(vectors[i], centre)) for i in by_rank]
        assert sims == sorted(sims, reverse=True)  # rank 1 = most similar, last = least
        assert g.distance[by_rank[0]] == pytest.approx(1 - sims[0])
    assert all(g.rank[i] is None and math.isnan(g.distance[i]) for i, lab in enumerate(g.labels) if lab == -1)


def test_map_positions_fit_the_map(grouped):
    _v, _t, g = grouped
    assert all(2 <= x <= 98 and 2 <= y <= 98 for x, y in g.map_xy)
    assert all(round(x, 2) == x and round(y, 2) == y for x, y in g.map_xy)


def test_no_groups_in_pure_noise():
    vectors, _ = blobs(sizes=(), scattered=40, seed=5)
    assert set(group(vectors).labels) == {-1}


def test_narratives_are_matched_one_to_one_best_first():
    a, b, c = np.eye(D)[0], np.eye(D)[1], np.eye(D)[2]
    new = {0: unit(a + 0.1 * b).tolist(), 1: unit(b).tolist(), 2: c.tolist()}
    old = {10: a.tolist(), 11: unit(b + 0.2 * a).tolist(), 12: unit(np.eye(D)[7]).tolist()}
    assert match_narratives(new, old) == {0: 10, 1: 11}       # c matches nothing
    assert match_narratives({0: a.tolist(), 1: a.tolist()}, {10: a.tolist()}) in ({0: 10}, {1: 10})  # one old -> one new
    assert match_narratives(new, {}) == {}


# --------------------------------------------------------------------------- the job, with a database and Qdrant

class NamingAI:
    def __init__(self):
        self.calls = 0

    def __call__(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        self.calls += 1
        body = json.loads(request.content)
        first = body["messages"][1]["content"].split("1. ", 1)[1].split("\n", 1)[0]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"name": f"Story of {first[:20]}", "summary": "Posts saying the same thing."})}}]})

    def client(self):
        return LocalAI("http://ai.test/v1", transport=httpx.MockTransport(self))


@dataclasses.dataclass
class World:
    settings: Settings
    qdrant: Qdrant
    ai: NamingAI

    def rows(self, sql, args=()):
        with connection(self.settings) as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())


@pytest.fixture
def world(tmp_path) -> Iterator[World]:
    q = Qdrant.from_settings(Settings.load(), prefix=f"vt_{secrets.token_hex(4)}_")
    try:
        q.aliases()
    except VectorStoreUnavailable as exc:
        q.close()
        pytest.skip(f"Qdrant not available: {exc}")
    with scratch_database() as db:
        settings = dataclasses.replace(db, frontend_dist=tmp_path, qdrant_port=Settings.load().qdrant_port)
        with connection(settings) as conn:
            migrate(conn)
        try:
            q.setup()
            yield World(settings, q, NamingAI())
        finally:
            q.drop_all()
            q.close()


WEEK1 = datetime(2026, 11, 2, tzinfo=timezone.utc)


def add_posts(w: World, vectors, truth, start: datetime, tag: str) -> list[int]:
    ids = []
    with connection(w.settings) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO accounts (uuid, platform_id, external_id, handle, display_name, account_type) VALUES"
                        " (%s, 2, %s, %s, 'Chan', 'channel') ON DUPLICATE KEY UPDATE account_id = LAST_INSERT_ID(account_id)",
                        (uuid.uuid4().bytes, f"chan_{tag}", f"chan_{tag}"))
            account = cur.lastrowid
        for i, (vec, t) in enumerate(zip(vectors, truth)):
            ext = f"{tag}/{i}"
            with conn.cursor() as cur:
                cur.execute("INSERT INTO posts (uuid, platform_id, account_id, container_account_id, external_id, text, language, published_at)"
                            " VALUES (%s, 2, %s, %s, %s, %s, %s, %s)",
                            (post_uuid("telegram", ext).bytes, account, account, ext, f"group {t} post {i}",
                             ["lt", "ru"][i % 2], (start + timedelta(hours=i)).replace(tzinfo=None)))
                ids.append(cur.lastrowid)
            store_post_vector(conn, w.qdrant, ids[-1], vec)
    return ids


@pytest.mark.integration
class TestClusteringJob:
    def test_a_run_stores_everything_the_narratives_screen_shows(self, world):
        w = world
        vectors, truth = blobs()
        ids = add_posts(w, vectors, truth, WEEK1, "w1")
        with connection(w.settings) as conn, conn.cursor() as cur:  # a false claim in group 0's posts
            cur.execute("INSERT INTO claims (claim_id, uuid, text, verdict) VALUES (1, %s, 'A claim', 'false')", (uuid.uuid4().bytes,))
            for pid in ids[:10]:
                cur.execute("INSERT INTO post_claims (post_id, claim_id) VALUES (%s, 1)", (pid,))
        with connection(w.settings) as conn:
            result = run_clustering(conn, w.qdrant, w.ai.client(), since=WEEK1, until=WEEK1 + timedelta(days=7),
                                    now=WEEK1 + timedelta(days=7))
        assert result["status"] == "done" and result["posts"] == 100 and result["unclustered"] == 10
        assert [c["size"] for c in result["clusters"]] == [30, 30, 30]
        assert w.ai.calls == 3

        run = w.rows("SELECT * FROM clustering_runs")[0]
        assert (run["status"], run["post_count"], run["cluster_count"], run["unclustered_count"]) == ("done", 100, 3, 10)
        clusters = w.rows("SELECT * FROM clusters ORDER BY cluster_id")
        assert len(clusters) == 3 and all(c["narrative_id"] for c in clusters) and all(c["change_pct"] is None for c in clusters)
        stats = json.loads(clusters[0]["stats"])
        assert len(stats["typical"]) == 2 and len(stats["edge"]) == 1 and stats["platforms"] == ["Telegram"]
        assert {lang["code"] for lang in stats["languages"]} == {"lt", "ru"}
        assert sum(lang["share"] for lang in stats["languages"]) == pytest.approx(100)
        assert len(w.rows("SELECT * FROM post_clusters")) == 100
        assert len(w.rows("SELECT * FROM post_clusters WHERE cluster_id IS NULL")) == 10
        assert w.rows("SELECT COUNT(*) AS n FROM narratives")[0]["n"] == 3
        assert w.qdrant.count("clusters") == 3

        # the screens read it
        body = make_client(w.settings).get("/api/narratives").json()
        assert body["run"] == {"date": "2026-11-09", "posts": 100, "clusters": 3, "unclustered": 10}
        assert [c["size"] for c in body["clusters"]] == [30, 30, 30]
        assert len(body["points"]) == 100 and all(p["clusterId"] is None for p in body["points"][:10])
        group0 = next(c for c in body["clusters"] if c["typical"][0]["text"].startswith("group 0"))
        assert group0["status"] == "false"  # its main claim's verdict
        assert group0["name"].startswith("Story of group 0")
        # a new post close to a group is assigned to it without re-clustering
        near = w.qdrant.nearest_cluster(vectors[0], min_score=0.75, run_id=run["run_id"])
        assert near.payload["cluster_id"] == group0["id"]
        assert w.qdrant.similar_posts(vectors[0], limit=1)[0].payload["cluster_id"] == group0["id"]

    def test_a_newer_week_keeps_each_story_in_its_narrative(self, world):
        w = world
        vectors, truth = blobs(seed=1)
        add_posts(w, vectors, truth, WEEK1, "w1")
        with connection(w.settings) as conn:
            run_clustering(conn, w.qdrant, w.ai.client(), since=WEEK1, until=WEEK1 + timedelta(days=7), now=WEEK1 + timedelta(days=7))
        first = {c["label"]: c["narrative_id"] for c in w.rows("SELECT label, narrative_id FROM clusters")}

        week2 = WEEK1 + timedelta(days=7)
        vectors2, truth2 = blobs(seed=2, sizes=(40, 30, 15), scattered=8)  # same stories, other posts and sizes
        add_posts(w, vectors2, truth2, week2, "w2")
        with connection(w.settings) as conn:
            result = run_clustering(conn, w.qdrant, w.ai.client(), since=week2, until=week2 + timedelta(days=7), now=week2 + timedelta(days=7))
        assert [c["size"] for c in result["clusters"]] == [40, 30, 15]
        assert all(c["same_narrative_as"] for c in result["clusters"])
        assert sorted(c["change_pct"] for c in result["clusters"]) == [-50.0, 0.0, 33.3]
        latest = w.rows("SELECT narrative_id FROM clusters WHERE run_id = (SELECT MAX(run_id) FROM clustering_runs)")
        assert {r["narrative_id"] for r in latest} == set(first.values())
        assert w.rows("SELECT COUNT(*) AS n FROM narratives")[0]["n"] == 3  # no new narratives

    def test_too_few_posts_give_no_clusters(self, world):
        w = world
        vectors, truth = blobs(sizes=(5, 5), scattered=2)
        add_posts(w, vectors, truth, WEEK1, "few")
        with connection(w.settings) as conn:
            result = run_clustering(conn, w.qdrant, w.ai.client(), since=WEEK1, until=WEEK1 + timedelta(days=7))
        assert result["status"] == "too_few" and result["posts"] == 12 and "No clusters made" in result["message"]
        assert w.rows("SELECT * FROM clustering_runs") == [] and w.ai.calls == 0

    def test_only_posts_in_the_window_are_used(self, world):
        w = world
        vectors, truth = blobs(sizes=(30, 30), scattered=0)
        add_posts(w, vectors, truth, WEEK1 - timedelta(days=30), "old")
        with connection(w.settings) as conn:
            result = run_clustering(conn, w.qdrant, w.ai.client(), since=WEEK1, until=WEEK1 + timedelta(days=7))
        assert result["status"] == "too_few" and result["posts"] == 0

    def test_a_failure_marks_the_run_failed(self, world, monkeypatch):
        w = world
        vectors, truth = blobs()
        add_posts(w, vectors, truth, WEEK1, "w1")

        def boom(*a, **k):
            raise RuntimeError("grouping broke")

        monkeypatch.setattr(clustering, "group", boom)
        with connection(w.settings) as conn, pytest.raises(RuntimeError):
            run_clustering(conn, w.qdrant, w.ai.client(), since=WEEK1, until=WEEK1 + timedelta(days=7))
        assert [r["status"] for r in w.rows("SELECT status FROM clustering_runs")] == ["failed"]
        assert make_client(w.settings).get("/api/narratives").json()["clusters"] == []  # the screens ignore failed runs


def test_cli_explains_missing_packages(monkeypatch, capsys):
    def missing():
        raise clustering.ClusteringUnavailable("Clustering needs extra packages. Install them with:  uv sync --extra clustering")

    from contextlib import nullcontext
    monkeypatch.setattr(clustering, "_libs", missing)
    monkeypatch.setattr(cluster_cli, "connection", lambda settings, **k: nullcontext())  # no real services needed
    monkeypatch.setattr(cluster_cli, "gemini_for", lambda conn, settings: None)
    monkeypatch.setattr(cluster_cli.Qdrant, "from_settings", classmethod(lambda cls, s, **k: nullcontext()))
    monkeypatch.setattr(cluster_cli.LocalAI, "from_settings", classmethod(lambda cls, s, **k: nullcontext()))
    from veritas import clustering_job
    monkeypatch.setattr(clustering_job, "run_clustering", lambda *a, **k: clustering.group([[1.0] * D] * 3))
    assert cluster_cli.main(["--rebuild", "--days", "7"]) == 1
    err = capsys.readouterr().err
    assert "uv sync --extra clustering" in err and "Traceback" not in err


@pytest.mark.parametrize("days", ["0", "400"])
def test_cli_checks_days(days, capsys):
    assert cluster_cli.main(["--rebuild", "--days", days]) == 1
    assert "--days must be between 1 and 365" in capsys.readouterr().err


def test_cli_days_only_with_rebuild(capsys):
    assert cluster_cli.main(["--days", "7"]) == 1
    assert "--days only applies with --rebuild" in capsys.readouterr().err


@pytest.mark.llm
def test_live_gemma_names_a_group():
    ai = LocalAI.from_settings(Settings.load(), timeout=300)
    try:
        ai.check()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"local AI not available: {exc}")
    name, summary = clustering._name(ai, [
        "Elektros kainos kils 40% dėl ES sankcijų",
        "Из-за санкций ЕС свет в Литве подорожает вдвое",
        "EU sanctions will double electricity prices this winter",
    ])
    assert 0 < len(name.split()) <= 12 and summary


class ScriptedAI:
    """Answers the naming prompt from a list (strings as they are, dicts as JSON)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        self.calls += 1
        a = self.answers.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": a if isinstance(a, str) else json.dumps(a)}}]})

    def client(self):
        return LocalAI("http://ai.test/v1", transport=httpx.MockTransport(self))


def test_a_long_name_and_summary_are_trimmed_not_refused():
    ai = ScriptedAI({"name": "word " * 60, "summary": "This is a long summary. " * 40})
    name, summary = clustering._name(ai.client(), ["Post one", "Post two"])
    assert ai.calls == 1 and len(name) <= 120 and name.endswith("…") and len(summary) <= 400


def test_naming_that_keeps_failing_falls_back_to_the_most_typical_post():
    ai = ScriptedAI("not json", {"name": 5})
    name, summary = clustering._name(ai.client(), ["Elektros kainos kils 40% dėl ES sankcijų, sako kanalas", "x"])
    assert ai.calls == 2 and summary == ""
    assert name == "Elektros kainos kils 40% dėl ES sankcijų, sako…"
