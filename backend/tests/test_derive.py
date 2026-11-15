"""Copies, source profiles and stories derived from the collected posts (veritas/derive.py),
and the screens that show them: Sources, a source's report, the landing page's trending
stories, a story, and "paste a link". Synthetic posts, throwaway database and Qdrant."""
from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("umap", reason="clustering packages not installed (uv sync --extra clustering)")

from conftest import make_client  # noqa: E402
from test_b9_clustering import WEEK1, add_posts, blobs, world  # noqa: E402,F401
from test_clustering_update import LATER, FakeAI  # noqa: E402
from veritas.clustering import run_clustering  # noqa: E402
from veritas.db import connection  # noqa: E402
from veritas.derive import build_sources, build_stories, build_story, derive_all, find_copies, plain  # noqa: E402

HOURS3 = timedelta(hours=3)


def fake_translate(text):
    return f"EN: {text}", "translate.v1", "fake"


def setup(w, with_copies=True):
    """Account w1 posts three stories (posts 0-89, near-identical within a group) and 10 unrelated
    posts (90-99, one per hour); account w2 copies those 10, each 3 hours after its original."""
    vectors, truth = blobs()
    ids1 = add_posts(w, vectors, truth, WEEK1, "w1")
    ids2 = add_posts(w, vectors[90:], truth[90:], WEEK1 + timedelta(hours=90) + HOURS3, "w2") if with_copies else []
    with connection(w.settings) as conn:
        result = run_clustering(conn, w.qdrant, FakeAI().client(), since=WEEK1, until=LATER, now=LATER)
    with connection(w.settings) as conn, conn.cursor() as cur:
        cur.execute("UPDATE posts SET url = CONCAT('https://t.me/', external_id)")
    return ids1, ids2, result


@pytest.mark.integration
class TestDerived:
    def test_copies_go_from_the_earlier_post_to_the_later_one_and_are_found_once(self, world):
        w = world
        ids1, ids2, _ = setup(w)
        with connection(w.settings) as conn:
            found = find_copies(conn, w.qdrant)
            again = find_copies(conn, w.qdrant)
        rel = w.rows("SELECT source_post_id AS s, target_post_id AS t, relation_type AS k FROM post_relations")
        assert again == 0 and found == len(rel) > 0                                # each post compared once
        pairs = {(r["s"], r["t"]) for r in rel if r["k"] == "copy"}
        assert pairs == set(zip(ids1[90:], ids2))                                 # the original -> the copy, 3 h later
        assert not any(r["s"] in ids2 and r["t"] in ids1 for r in rel)            # never backwards
        assert not any(r["s"] in ids1 and r["t"] in ids1 for r in rel)            # an account repeating itself isn't copying

    def test_source_profiles(self, world):
        w = world
        setup(w)
        with connection(w.settings) as conn:
            find_copies(conn, w.qdrant)
            assert build_sources(conn, now=LATER) == 2
        client = make_client(w.settings)
        by_id = {s["id"]: s for s in client.get("/api/sources").json()["sources"]}
        assert set(by_id) == {"chan_w1", "chan_w2"}
        copier, original = by_id["chan_w2"], by_id["chan_w1"]
        assert (copier["nearCopyPct"], copier["originalPct"]) == (100.0, 0.0)
        assert (original["originalPct"], original["nearCopyPct"]) == (100.0, 0.0)
        assert copier["posts30d"] == 10 and copier["mainTopic"] == ""            # its posts fit no narrative
        assert original["mainTopic"].startswith("Story of group ")
        assert copier["kind"].startswith("Channel")
        report = client.get("/api/sources/chan_w2").json()
        assert report["upstream"] == [{"name": "@chan_w1", "kind": "Telegram channel", "posts": 10, "typicalDelay": "3 h"}]
        assert report["basedOn"] == "Based on 10 posts collected since November 2026."
        assert report["stats"]["postsPerDay"] == round(10 / 30, 1)
        assert len(report["daily"]) == 30 and sum(d["posts"] for d in report["daily"]) == 10
        assert len(report["recent"]) == 5 and report["recent"][0]["url"].startswith("https://t.me/")

    def test_trending_stories_one_per_narrative(self, world):
        w = world
        setup(w)
        with connection(w.settings) as conn:
            out = derive_all(conn, w.qdrant, fake_translate)
            keys_before = [r["story_key"] for r in w.rows("SELECT story_key FROM stories ORDER BY story_key")]
            build_stories(conn, fake_translate)                                    # again: the same keys, replaced
        assert out["stories"] == 3 == len(keys_before)
        assert [r["story_key"] for r in w.rows("SELECT story_key FROM stories ORDER BY story_key")] == keys_before
        client = make_client(w.settings)
        stories = client.get("/api/trending", params={"range": "90"}).json()["stories"]
        assert len(stories) == 3
        group0 = next(s for s in stories if s["narrative"]["name"].startswith("Story of group 0"))
        assert group0["narrative"]["headline"] == "Yes. It's one of 30 posts telling the same story."
        assert group0["post"]["translation"].startswith("EN: group 0 post")
        assert group0["travel"]["headline"] == "We haven't found copies of it yet." and group0["travel"]["accounts"] == 1
        assert client.get(f"/api/stories/{group0['id']}").json() == group0

    def test_paste_a_link_tells_any_collected_posts_story(self, world):
        w = world
        _ids1, ids2, _ = setup(w)
        with connection(w.settings) as conn:
            find_copies(conn, w.qdrant)
        client = make_client(w.settings)
        url = w.rows("SELECT url FROM posts WHERE post_id = %s", (ids2[0],))[0]["url"]
        res = client.get("/api/trace", params={"url": url})
        assert res.status_code == 200
        story = res.json()
        assert story["id"] == f"post-{ids2[0]}"
        assert story["origin"]["headline"] == "It started with @chan_w1, 3 h earlier."
        assert [n["kind"] for n in story["origin"]["chain"]] == ["Telegram channel · Lithuanian", "The post you picked"]
        assert story["travel"]["copies"] == 1 and story["travel"]["accounts"] == 2 and story["travel"]["timeLabel"] == "3 h"
        assert story["travel"]["headline"] == "Within 3 h it was copied 1 time, in one language."
        assert client.get(f"/api/stories/post-{ids2[0]}").json()["origin"] == story["origin"]   # the tour can open it
        assert client.get("/api/trace", params={"url": "https://t.me/nobody/1"}).status_code == 404
        assert client.get("/api/stories/post-999999").status_code == 404
        assert client.get("/api/stories/post-0").status_code == 404

    def test_a_post_without_copies_cluster_or_claims(self, world):
        w = world
        ids = add_posts(w, blobs(sizes=(), scattered=1)[0], [-1], WEEK1, "solo")
        with connection(w.settings) as conn:
            story = build_story(conn, ids[0])
        assert story["origin"]["headline"].startswith("We found no earlier version")
        assert story["travel"]["headline"] == "We haven't found copies of it yet." and story["travel"]["copies"] == 0
        assert story["narrative"] == {"headline": "Not yet: it doesn't fit any narrative we track.", "name": "", "stats": ""}
        assert story["claim"]["verdict"] == "unverified" and story["status"] == "unverified"
        assert story["post"]["translation"] == ""                                 # no translator given: none made


def test_markdown_is_removed():
    assert plain("**To the President**\n# Title\n- item `x`") == "To the President\nTitle\nitem x"
