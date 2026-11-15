"""Cluster updates without recomputing (veritas/clustering_update.py), and what the screens show
about each cluster's typical posts: highlights and claims (veritas/cluster_insight.py).

Synthetic posts with known groups, a throwaway database and throwaway Qdrant collections, and a
fake local AI. Needs the clustering packages (uv sync --extra clustering); skipped without them.
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import numpy as np
import pytest

pytest.importorskip("umap", reason="clustering packages not installed (uv sync --extra clustering)")

from conftest import make_client  # noqa: E402
from test_b9_clustering import D, WEEK1, World, add_posts, blobs, unit, world  # noqa: E402,F401
from veritas import cluster_insight, clustering_update  # noqa: E402
from veritas.ai import LocalAI  # noqa: E402
from veritas.cluster_insight import counts, enrich_clusters, segments  # noqa: E402
from veritas.clustering import run_clustering  # noqa: E402
from veritas.clustering_update import UP_TO_DATE, update_clustering  # noqa: E402
from veritas.db import connection  # noqa: E402

LATER = WEEK1 + timedelta(days=7)


class FakeAI:
    """Names clusters, finds a highlight and a claim in each post, and embeds claims."""

    def __init__(self):
        self.calls: dict[str, int] = {}

    def __call__(self, request):
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        if path.endswith("/embeddings"):
            self.calls["embed"] = self.calls.get("embed", 0) + 1
            rows = []
            for i, text in enumerate(body["input"]):
                v = np.zeros(D)
                v[100 + (sum(map(ord, text)) % 50)] = 1.0   # the same claim text -> the same vector
                rows.append({"index": i, "embedding": v.tolist()})
            return httpx.Response(200, json={"data": rows})
        name = body["response_format"]["json_schema"]["name"]
        self.calls[name] = self.calls.get(name, 0) + 1
        user = body["messages"][1]["content"]
        if name == "cluster_name":
            first = user.split("1. ", 1)[1].split("\n", 1)[0]
            answer = {"name": f"Story of {first[:20]}", "summary": "Posts saying the same thing."}
        elif name == "patterns":
            group = user.split("group ", 1)[1].split(" ", 1)[0]
            answer = {"patterns": [
                {"quote": f"group {group}", "technique": "Loaded language", "tier": "clear", "why": "A label."},
                {"quote": "post", "technique": "Doubt", "tier": "possible", "why": "Maybe."},
                {"quote": "not in the post", "technique": "Slogan", "tier": "likely", "why": "Made up."},
            ], "overall": "It labels a group."}
        elif name == "claims":
            group = user.split("group ", 1)[1].split(" ", 1)[0]
            answer = {"claims": [{"claim": f"Group {group} is behind it", "quote": f"group {group}"}]}
        else:
            raise AssertionError(f"unexpected prompt {name}")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}]})

    def client(self):
        return LocalAI("http://ai.test/v1", transport=httpx.MockTransport(self))


def first_run(w: World, ai: FakeAI):
    vectors, truth = blobs()
    add_posts(w, vectors, truth, WEEK1, "w1")
    with connection(w.settings) as conn:
        result = run_clustering(conn, w.qdrant, ai.client(), since=WEEK1, until=LATER, now=LATER)
    assert result["status"] == "done"
    return result


def near(axis: int, n: int, seed: int, spread=0.15):
    rng = np.random.default_rng(seed)
    return [unit(np.eye(D)[axis] + spread * unit(rng.normal(size=D))).tolist() for _ in range(n)]


def placed(w: World, run_id: int) -> dict[int, tuple]:
    return {r["post_id"]: (r["cluster_id"], float(r["map_x"]), float(r["map_y"]), r["distance_to_centroid"])
            for r in w.rows("SELECT * FROM post_clusters WHERE run_id = %s", (run_id,))}


def label_of(w: World, cluster_id: int) -> str:
    return w.rows("SELECT label FROM clusters WHERE cluster_id = %s", (cluster_id,))[0]["label"]


@pytest.mark.integration
class TestUpdates:
    def test_the_first_update_is_a_full_run_over_every_post(self, world):
        w, ai = world, FakeAI()
        vectors, truth = blobs()
        add_posts(w, vectors, truth, WEEK1 - timedelta(days=200), "old")  # long ago: no window applies
        with connection(w.settings) as conn:
            result = update_clustering(conn, w.qdrant, ai.client(), now=LATER)
        assert result["mode"] == "full" and result["status"] == "done" and result["posts"] == 100
        assert [c["size"] for c in result["clusters"]] == [30, 30, 30]
        # every post is marked as clustered in Qdrant, so the next update finds nothing new
        with connection(w.settings) as conn:
            again = update_clustering(conn, w.qdrant, ai.client(), now=LATER)
        assert again["status"] == "up_to_date" and again["message"] == UP_TO_DATE

    def test_new_posts_join_their_clusters_and_nothing_else_moves(self, world, monkeypatch):
        w, ai = world, FakeAI()
        result = first_run(w, ai)
        run_id = result["run_id"]
        before = placed(w, run_id)
        group0 = next(c["cluster_id"] for c in result["clusters"] if label_of(w, c["cluster_id"]).startswith("Story of group 0"))
        named = ai.calls["cluster_name"]

        def no_regrouping(*a, **k):
            raise AssertionError("an update with nothing new to group must not run UMAP")

        monkeypatch.setattr(clustering_update, "group", no_regrouping)
        new_ids = add_posts(w, near(0, 5, seed=9, spread=0.12), [0] * 5, LATER, "w2")
        with connection(w.settings) as conn:
            update = update_clustering(conn, w.qdrant, ai.client(), now=LATER + timedelta(days=1))
        assert (update["mode"], update["status"], update["posts"], update["joined"], update["new_clusters"]) == \
            ("update", "done", 5, 5, 0)
        assert update["run_id"] == run_id and update["clusters"] == [{"cluster_id": group0}]
        assert ai.calls["cluster_name"] == named                       # no new names asked for

        after = placed(w, run_id)
        assert {k: v for k, v in after.items() if k in before} == before   # nothing placed earlier moved
        assert all(after[i][0] == group0 for i in new_ids)
        xs = [v[1] for v in before.values() if v[0] == group0]
        ys = [v[2] for v in before.values() if v[0] == group0]
        for i in new_ids:   # placed among its cluster's dots (plus the small jitter)
            assert min(xs) - 2 <= after[i][1] <= max(xs) + 2 and min(ys) - 2 <= after[i][2] <= max(ys) + 2

        cluster = w.rows("SELECT * FROM clusters WHERE cluster_id = %s", (group0,))[0]
        assert cluster["size"] == 35 and cluster["change_pct"] is None   # the first run had no earlier week
        assert [r["rank_in_cluster"] for r in w.rows("SELECT rank_in_cluster FROM post_clusters WHERE cluster_id = %s"
                                                     " ORDER BY rank_in_cluster", (group0,))] == list(range(1, 36))
        run = w.rows("SELECT * FROM clustering_runs")[0]
        assert (run["post_count"], run["cluster_count"], run["unclustered_count"]) == (105, 3, 10)
        assert json.loads(run["params"])["updates"][-1]["new_posts"] == 5
        assert len(w.rows("SELECT * FROM clustering_runs")) == 1          # updated in place

        body = make_client(w.settings).get("/api/narratives").json()
        assert body["run"]["posts"] == 105 and len(body["points"]) == 105
        assert sorted(c["size"] for c in body["clusters"]) == [30, 30, 35]

        with connection(w.settings) as conn:
            assert update_clustering(conn, w.qdrant, ai.client())["status"] == "up_to_date"

    def test_a_new_story_becomes_a_new_cluster(self, world, monkeypatch):
        w, ai = world, FakeAI()
        result = first_run(w, ai)
        run_id = result["run_id"]
        before = placed(w, run_id)
        new_ids = add_posts(w, near(3, 25, seed=4), [3] * 25, LATER, "w2")
        grouped_sizes = []
        real_group = clustering_update.group
        monkeypatch.setattr(clustering_update, "group", lambda vectors, **k: grouped_sizes.append(len(vectors)) or real_group(vectors, **k))
        with connection(w.settings) as conn:
            update = update_clustering(conn, w.qdrant, ai.client(), now=LATER + timedelta(days=1))
        assert grouped_sizes == [25 + 10]     # only the new posts and the earlier noise, never the clustered 90
        assert update["new_clusters"] == 1 and update["joined"] == 0
        after = placed(w, run_id)
        new_cluster = {after[i][0] for i in new_ids}
        assert len(new_cluster) == 1 and None not in new_cluster
        assert label_of(w, new_cluster.pop()).startswith("Story of group 3")
        assert {k: v for k, v in after.items() if k in before} == before   # the old noise stays noise, nothing moves
        assert sorted(r["size"] for r in w.rows("SELECT size FROM clusters")) == [25, 30, 30, 30]
        assert w.rows("SELECT COUNT(*) AS n FROM narratives")[0]["n"] == 4
        assert w.qdrant.count("clusters") == 4
        assert all(2 <= after[i][1] <= 98 and 2 <= after[i][2] <= 98 for i in new_ids)

    def test_a_loosely_related_post_does_not_join(self, world):
        """Similar enough for the old floor (cosine 0.5), but much farther out than the cluster's members."""
        w, ai = world, FakeAI()
        first_run(w, ai)
        rng = np.random.default_rng(3)
        loose = [unit(np.eye(D)[0] + 0.9 * unit(rng.normal(size=D))).tolist() for _ in range(3)]   # cosine ~0.74
        with connection(w.settings) as conn:
            add_posts(w, loose, [-1] * 3, LATER, "loose")
            update = update_clustering(conn, w.qdrant, ai.client(), now=LATER + timedelta(days=1))
        assert update["posts"] == 3 and update["joined"] == 0

    def test_posts_in_the_run_without_their_mark_are_not_new(self, world):
        """Runs made before the mark existed, or a post embedded again: marked again, not re-clustered."""
        w, ai = world, FakeAI()
        result = first_run(w, ai)
        points = [p[0] for p in w.qdrant.vectors("posts")]
        w.qdrant._request("POST", f"/collections/{w.qdrant.name('posts')}/points/payload/delete?wait=true",
                          {"keys": ["clustered_run"], "points": points})
        before = placed(w, result["run_id"])
        with connection(w.settings) as conn:
            update = update_clustering(conn, w.qdrant, ai.client())
        assert update["status"] == "up_to_date"
        assert placed(w, result["run_id"]) == before
        assert all(p[2].get("clustered_run") == result["run_id"] for p in w.qdrant.vectors("posts"))


@pytest.mark.integration
class TestTypicalPosts:
    def test_typical_posts_get_highlights_and_claims_once(self, world):
        w, ai = world, FakeAI()
        result = first_run(w, ai)
        ids = [c["cluster_id"] for c in result["clusters"]]
        with connection(w.settings) as conn:
            report = enrich_clusters(conn, ai.client(), w.qdrant, ids)
        assert report == {"posts": 9, "skipped": 0, "clusters": 3}   # 2 typical + 1 edge per cluster
        assert ai.calls["patterns"] == 9 and ai.calls["claims"] == 9

        stats = json.loads(w.rows("SELECT stats FROM clusters WHERE cluster_id = %s", (ids[0],))[0]["stats"])
        ref = stats["typical"][0]
        assert ref["highlights"] == {"clear": 1, "likely": 0, "possible": 1}   # the made-up quote is dropped
        assert "".join(s["text"] for s in ref["segments"]) == ref["text"]
        marked = [s for s in ref["segments"] if "tier" in s]
        assert [(s["technique"], s["tier"]) for s in marked] == [("Loaded language", "clear"), ("Doubt", "possible")]

        # claims: one per group, linked to their cluster, which takes it as its main claim
        claims = w.rows("SELECT claim_id, text, cluster_id, post_count, verdict FROM claims ORDER BY text")
        assert [c["text"] for c in claims] == ["Group 0 is behind it", "Group 1 is behind it", "Group 2 is behind it"]
        assert all(c["post_count"] == 3 and c["verdict"] == "unverified" for c in claims)
        mains = {r["cluster_id"]: r["main_claim_id"] for r in w.rows("SELECT cluster_id, main_claim_id FROM clusters")}
        assert {c["cluster_id"] for c in claims} == set(ids)
        assert all(mains[c["cluster_id"]] == c["claim_id"] for c in claims)

        # the screens show them
        client = make_client(w.settings)
        body = client.get("/api/narratives").json()
        typical = body["clusters"][0]["typical"][0]
        assert typical["highlights"]["clear"] == 1 and typical["segments"]
        assert client.get("/api/claims").json()["total"] == 3

        # nothing is read twice
        with connection(w.settings) as conn:
            enrich_clusters(conn, ai.client(), w.qdrant, ids)
        assert ai.calls["patterns"] == 9 and ai.calls["claims"] == 9

    def test_an_update_keeps_highlights_of_posts_that_stay_typical(self, world):
        w, ai = world, FakeAI()
        result = first_run(w, ai)
        ids = [c["cluster_id"] for c in result["clusters"]]
        with connection(w.settings) as conn:
            enrich_clusters(conn, ai.client(), w.qdrant, ids)
        before = json.loads(w.rows("SELECT stats FROM clusters WHERE cluster_id = %s", (ids[0],))[0]["stats"])
        add_posts(w, near(0, 3, seed=11, spread=0.12), [0] * 3, LATER, "w2")
        with connection(w.settings) as conn:
            update = update_clustering(conn, w.qdrant, ai.client(), now=LATER + timedelta(days=1))
        assert update["joined"] == 3
        changed = update["clusters"][0]["cluster_id"]
        stats = json.loads(w.rows("SELECT stats FROM clusters WHERE cluster_id = %s", (changed,))[0]["stats"])
        read_before = {r["id"]: r for r in before["typical"] + before["edge"]} if changed == ids[0] else {}
        stayed = [r for r in stats["typical"] + stats["edge"] if r["id"] in read_before]
        assert all(r["highlights"] == read_before[r["id"]]["highlights"] for r in stayed)   # kept, no new reading


def test_segments_and_counts():
    text = "They lie. Everyone knows they lie."
    patterns = [{"quote": "Everyone knows", "technique": "Bandwagon", "tier": "clear", "why": "a"},
                {"quote": "They lie", "technique": "Name-calling", "tier": "likely", "why": "b"},
                {"quote": "lie. Every", "technique": "Overlap", "tier": "possible", "why": "c"},   # overlaps: dropped
                {"quote": "missing", "technique": "X", "tier": "clear", "why": "d"}]
    parts = segments(text, patterns)
    assert "".join(p["text"] for p in parts) == text
    assert [(p["text"], p.get("tier")) for p in parts] == [
        ("They lie", "likely"), (". ", None), ("Everyone knows", "clear"), (" they lie.", None)]
    assert segments("", []) == [{"text": ""}]
    assert counts(patterns) == {"clear": 2, "likely": 1, "possible": 1}
    assert cluster_insight.TIERS == ("clear", "likely", "possible")


@pytest.mark.integration
def test_clusters_made_before_get_read_on_the_next_update(world):
    """Clusters from before highlights existed are read on the next update, even with no new posts."""
    from veritas.cluster_insight import unread_clusters
    from veritas.clustering_job import cluster_and_read, summary
    w, ai = world, FakeAI()
    result = first_run(w, ai)
    with connection(w.settings) as conn:
        assert sorted(unread_clusters(conn, result["run_id"])) == sorted(c["cluster_id"] for c in result["clusters"])
        out = cluster_and_read(conn, w.qdrant, ai.client())
        assert out["status"] == "up_to_date" and out["insight"]["posts"] == 9
        assert summary(out) == ("up_to_date", "No new posts since the last update. Read 9 typical posts.")
        assert unread_clusters(conn, result["run_id"]) == []
        again = cluster_and_read(conn, w.qdrant, ai.client())
    assert "insight" not in again and ai.calls["patterns"] == 9
