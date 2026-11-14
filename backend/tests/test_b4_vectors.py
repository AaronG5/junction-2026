"""B4: vector search in Qdrant, with hand-made vectors (no AI needed).

Integration tests use their own collections (prefix vt_<random>_), deleted
afterwards; the app's own collections are never touched. Skipped when Qdrant
or MySQL isn't running.
"""
from __future__ import annotations

import json
import math
import secrets
import uuid
from typing import Iterator

import httpx
import pytest

import setup_vectors as setup_cli
from conftest import scratch_database
from veritas.config import Settings
from veritas.content import delete_posts, post_payload, remove_orphans, store_post_vector
from veritas.db import connection
from veritas.db.migrations import migrate
from veritas.ids import post_uuid
from veritas.vectors import Hit, Point, Qdrant, VectorStoreError, VectorStoreUnavailable, load_collections

DIM = 768


def vec(**parts: float) -> list[float]:
    """A 768-number vector with the given components (e0=1.0, e1=0.2, ...), unit length."""
    v = [0.0] * DIM
    for key, value in parts.items():
        v[int(key[1:])] = value
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def pid(n: int) -> str:
    return str(uuid.UUID(int=n))


# --------------------------------------------------------------------------- plain tests (fake Qdrant)

def _fake(handler) -> Qdrant:
    return Qdrant("http://qdrant.test", transport=httpx.MockTransport(handler))


def test_unreachable_qdrant_is_a_plain_error():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    with pytest.raises(VectorStoreUnavailable, match="isn't reachable at http://qdrant.test. Start it with the launcher."):
        _fake(refuse).count("posts")


def test_qdrant_errors_are_plain(monkeypatch):
    q = _fake(lambda r: httpx.Response(400, json={"status": {"error": "Wrong input: bad filter"}}))
    with pytest.raises(VectorStoreError, match="refused the request: Wrong input: bad filter"):
        q.count("posts")
    q = _fake(lambda r: httpx.Response(502, text="<html>gateway</html>"))
    with pytest.raises(VectorStoreError, match="HTTP 502"):
        q.count("posts")


@pytest.mark.parametrize("bad, message", [
    ([0.1] * 10, "must have 768 numbers, not 10"),
    ([float("nan")] + [0.0] * (DIM - 1), "NaN or infinity"),
    ([float("inf")] + [0.0] * (DIM - 1), "NaN or infinity"),
])
def test_wrong_vectors_are_refused_before_sending(bad, message):
    sent = []
    q = _fake(lambda r: sent.append(r) or httpx.Response(200, json={"result": {"points": []}}))
    with pytest.raises(ValueError, match=message):
        q.search("posts", bad)
    with pytest.raises(ValueError, match=message):
        q.upsert("posts", [Point(pid(1), bad)])
    assert sent == []


def test_point_ids_must_be_uuids():
    q = _fake(lambda r: httpx.Response(200, json={"result": {}}))
    with pytest.raises(ValueError):
        q.delete("posts", ["not-a-uuid"])
    with pytest.raises(ValueError):
        q.delete("posts", ["1 OR 1=1"])


def test_similar_posts_builds_the_filter():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"result": {"points": [{"id": pid(2), "score": 0.9, "payload": {"post_id": 2}}]}})

    hits = _fake(handler).similar_posts(vec(e0=1), limit=5, platforms=["telegram"], languages=["lt", "ru"],
                                        since="2026-11-01T00:00:00Z", exclude=[pid(1)])
    assert hits == [Hit(pid(2), 0.9, {"post_id": 2})]
    assert seen["using"] == "text" and seen["limit"] == 5
    assert seen["filter"] == {
        "must": [{"key": "platform", "match": {"any": ["telegram"]}},
                 {"key": "language", "match": {"any": ["lt", "ru"]}},
                 {"key": "published_at", "range": {"gte": "2026-11-01T00:00:00Z"}}],
        "must_not": [{"has_id": [pid(1)]}],
    }


def test_collections_file_matches_the_embedding_model():
    specs = load_collections()
    assert {s["alias"] for s in specs} == {"posts", "claims", "categories", "clusters"}
    for s in specs:
        assert s["config"]["vectors"]["text"] == {"size": 768, "distance": "Cosine"}
        assert s["name"] == f"{s['alias']}__embeddinggemma_300m"


def test_drop_all_refuses_the_real_collections():
    with pytest.raises(ValueError, match="only for prefixed"):
        _fake(lambda r: httpx.Response(200, json={"result": {}})).drop_all()


# --------------------------------------------------------------------------- integration

@pytest.fixture
def qdrant() -> Iterator[Qdrant]:
    q = Qdrant.from_settings(Settings.load(), prefix=f"vt_{secrets.token_hex(4)}_")
    try:
        q.aliases()
    except VectorStoreUnavailable as exc:
        q.close()
        pytest.skip(f"Qdrant not available for integration tests: {exc}")
    try:
        q.setup()
        yield q
    finally:
        q.drop_all()
        q.close()


@pytest.mark.integration
class TestQdrant:
    def test_setup_can_be_repeated_and_aliases_point_right(self, qdrant):
        assert qdrant.setup() == []  # already done by the fixture
        aliases = qdrant.aliases()
        for spec in load_collections():
            assert aliases[qdrant.name(spec["alias"])] == qdrant.name(spec["name"])
            info = qdrant._request("GET", f"/collections/{qdrant.name(spec['name'])}")
            have = {k: v["data_type"] for k, v in info["payload_schema"].items()}
            assert have == spec.get("payload_indexes", {})
        # nothing of ours leaked into the app's own names
        assert not any(a in aliases and aliases[a].startswith(qdrant.prefix) for a in ("posts", "claims"))

    def test_setup_moves_an_alias_that_points_elsewhere(self, qdrant):
        other = qdrant.name("posts__old_model")
        qdrant._request("PUT", f"/collections/{other}", {"vectors": {"text": {"size": 4, "distance": "Cosine"}}})
        try:
            qdrant._request("POST", "/collections/aliases", {"actions": [
                {"delete_alias": {"alias_name": qdrant.name("posts")}},
                {"create_alias": {"collection_name": other, "alias_name": qdrant.name("posts")}}]})
            assert qdrant.setup() == [f"alias {qdrant.name('posts')} -> {qdrant.name('posts__embeddinggemma_300m')}"]
        finally:
            qdrant._request("DELETE", f"/collections/{other}", allow_404=True)

    def test_setup_refuses_a_collection_made_for_another_model(self, qdrant):
        qdrant._request("DELETE", f"/collections/{qdrant.name('claims__embeddinggemma_300m')}")
        qdrant._request("PUT", f"/collections/{qdrant.name('claims__embeddinggemma_300m')}",
                        {"vectors": {"text": {"size": 384, "distance": "Cosine"}}})
        with pytest.raises(VectorStoreError, match="holds 384-number Cosine vectors; 768-number Cosine vectors are expected"):
            qdrant.setup()

    def test_setup_adds_a_missing_index(self, qdrant):
        name = qdrant.name("posts__embeddinggemma_300m")
        qdrant._request("DELETE", f"/collections/{name}/index/language?wait=true")
        assert qdrant.setup() == [f"indexed {name}.language"]

    def test_similar_posts_nearest_first_with_filters(self, qdrant):
        points = [
            Point(pid(1), vec(e0=1), {"post_id": 1, "platform": "telegram", "language": "lt", "published_at": "2026-11-10T08:00:00Z"}),
            Point(pid(2), vec(e0=1, e1=0.1), {"post_id": 2, "platform": "telegram", "language": "ru", "published_at": "2026-11-12T08:00:00Z"}),
            Point(pid(3), vec(e0=1, e1=0.5), {"post_id": 3, "platform": "facebook", "language": "lt", "published_at": "2026-10-01T08:00:00Z"}),
            Point(pid(4), vec(e0=1, e1=1.5), {"post_id": 4, "platform": "telegram", "language": "lt", "published_at": "2026-11-13T08:00:00Z"}),
            Point(pid(5), vec(e2=1), {"post_id": 5, "platform": "telegram", "language": "lt", "published_at": "2026-11-13T08:00:00Z"}),
        ]
        assert qdrant.upsert("posts", points) == 5
        q = vec(e0=1)
        ids = lambda hits: [h.payload["post_id"] for h in hits]  # noqa: E731
        hits = qdrant.similar_posts(q, limit=5)
        assert ids(hits) == [1, 2, 3, 4, 5]
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
        assert hits[0].score == pytest.approx(1.0, abs=1e-5) and hits[-1].score == pytest.approx(0.0, abs=1e-5)
        assert ids(qdrant.similar_posts(q, limit=2)) == [1, 2]
        assert ids(qdrant.similar_posts(q, platforms=["facebook"])) == [3]
        assert ids(qdrant.similar_posts(q, languages=["ru"])) == [2]
        assert ids(qdrant.similar_posts(q, since="2026-11-11T00:00:00Z")) == [2, 4, 5]
        assert ids(qdrant.similar_posts(q, until="2026-11-10T08:00:00Z")) == [1, 3]
        assert ids(qdrant.similar_posts(q, exclude=[pid(1)], limit=2)) == [2, 3]
        assert ids(qdrant.similar_posts(q, platforms=["telegram"], languages=["lt"], since="2026-11-11T00:00:00Z")) == [4, 5]

    def test_upsert_replaces_and_delete_removes(self, qdrant):
        qdrant.upsert("posts", [Point(pid(1), vec(e0=1), {"post_id": 1})])
        qdrant.upsert("posts", [Point(pid(1), vec(e3=1), {"post_id": 1, "language": "lt"})])
        assert qdrant.count("posts") == 1
        assert qdrant.similar_posts(vec(e3=1))[0].payload == {"post_id": 1, "language": "lt"}
        qdrant.delete("posts", [uuid.UUID(pid(1)).bytes])
        assert qdrant.count("posts") == 0
        qdrant.delete("posts", [pid(99)])  # deleting what isn't there is fine

    def test_nearest_cluster_or_none(self, qdrant):
        qdrant.upsert("clusters", [
            Point(pid(101), vec(e0=1), {"cluster_id": 12, "run_id": 1}),
            Point(pid(102), vec(e1=1), {"cluster_id": 7, "run_id": 1}),
            Point(pid(103), vec(e0=1, e2=0.05), {"cluster_id": 99, "run_id": 2}),
        ])
        near = qdrant.nearest_cluster(vec(e0=1, e1=0.3), min_score=0.6, run_id=1)
        assert near is not None and near.payload["cluster_id"] == 12
        assert qdrant.nearest_cluster(vec(e1=1, e0=0.2), min_score=0.6, run_id=1).payload["cluster_id"] == 7
        assert qdrant.nearest_cluster(vec(e5=1), min_score=0.6, run_id=1) is None  # fits no cluster
        assert qdrant.nearest_cluster(vec(e0=1), min_score=0.6, run_id=2).payload["cluster_id"] == 99

    def test_paging_through_every_id(self, qdrant):
        qdrant.upsert("posts", [Point(pid(i), vec(e0=1, e1=i / 100), {"post_id": i}) for i in range(1, 26)])
        assert sorted(qdrant.ids("posts", batch=7)) == sorted(pid(i) for i in range(1, 26))

    def test_cli_sets_up_and_reports(self, qdrant, monkeypatch, capsys):
        monkeypatch.setattr(setup_cli.Qdrant, "from_settings",
                            classmethod(lambda cls, s, **k: Qdrant(qdrant.base_url, prefix=qdrant.prefix)))
        assert setup_cli.main([]) == 0
        assert "already set up" in capsys.readouterr().out

    def test_cli_reports_unreachable_qdrant(self, monkeypatch, capsys):
        monkeypatch.setattr(setup_cli.Qdrant, "from_settings", classmethod(lambda cls, s, **k: Qdrant("http://127.0.0.1:1")))
        assert setup_cli.main([]) == 1
        err = capsys.readouterr().err
        assert err.startswith("Vector search setup stopped. Vector search (Qdrant) isn't reachable") and "Traceback" not in err


# --------------------------------------------------------------------------- MySQL and Qdrant together

@pytest.fixture
def db() -> Iterator[Settings]:
    with scratch_database() as settings:
        with connection(settings) as conn:
            migrate(conn)
        yield settings


def _add_post(conn, external: str, platform_id: int = 2, language: str = "lt") -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO posts (uuid, platform_id, external_id, language, published_at) VALUES (%s, %s, %s, %s, %s)",
                    (post_uuid("telegram", external).bytes, platform_id, external, language, "2026-11-10 08:30:00.250"))
        return cur.lastrowid


@pytest.mark.integration
class TestWithDatabase:
    def test_stored_vector_carries_the_posts_filter_fields(self, db, qdrant):
        with connection(db) as conn:
            post_id = _add_post(conn, "chan/1")
            point = store_post_vector(conn, qdrant, post_id, vec(e0=1))
            assert point == str(post_uuid("telegram", "chan/1"))
            hit = qdrant.similar_posts(vec(e0=1))[0]
            assert hit.id == point
            assert hit.payload == {"post_id": post_id, "platform": "telegram", "language": "lt", "post_type": "post",
                                   "published_at": "2026-11-10T08:30:00.250Z", "origin": "local"}
            with conn.cursor() as cur:
                cur.execute("SELECT embedding_model_id, embedded_at FROM posts WHERE post_id = %s", (post_id,))
                row = cur.fetchone()
            assert row["embedding_model_id"] == 1 and row["embedded_at"] is not None
            with pytest.raises(LookupError):
                post_payload(conn, 999999)

    def test_deleting_posts_removes_their_vectors(self, db, qdrant):
        with connection(db) as conn:
            keep, gone = _add_post(conn, "chan/1"), _add_post(conn, "chan/2")
            store_post_vector(conn, qdrant, keep, vec(e0=1))
            store_post_vector(conn, qdrant, gone, vec(e1=1))
            assert delete_posts(conn, qdrant, [gone, gone, 424242]) == 1
            assert qdrant.count("posts") == 1
            assert [h.payload["post_id"] for h in qdrant.similar_posts(vec(e1=1))] == [keep]
            with conn.cursor() as cur:
                cur.execute("SELECT post_id FROM posts")
                assert [r["post_id"] for r in cur.fetchall()] == [keep]
            assert delete_posts(conn, qdrant, []) == 0

    def test_qdrant_failure_deletes_nothing(self, db, qdrant):
        with connection(db) as conn:
            post_id = _add_post(conn, "chan/1")
            store_post_vector(conn, qdrant, post_id, vec(e0=1))
            down = Qdrant("http://127.0.0.1:1", prefix=qdrant.prefix)
            with pytest.raises(VectorStoreUnavailable):
                delete_posts(conn, down, [post_id])
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM posts")
                assert cur.fetchone()["n"] == 1  # rolled back
            assert qdrant.count("posts") == 1

    def test_orphan_vectors_are_removed(self, db, qdrant):
        with connection(db) as conn:
            kept = [_add_post(conn, f"chan/{i}") for i in range(3)]
            for i, post_id in enumerate(kept):
                store_post_vector(conn, qdrant, post_id, vec(e0=1, e1=i / 10))
            # vectors whose post was removed behind Qdrant's back
            qdrant.upsert("posts", [Point(pid(5000 + i), vec(e2=1, e3=i / 10), {"post_id": 5000 + i}) for i in range(12)])
            with conn.cursor() as cur:
                cur.execute("DELETE FROM posts WHERE post_id = %s", (kept[0],))
            assert remove_orphans(conn, qdrant, batch=5) == 13
            assert sorted(h.payload["post_id"] for h in qdrant.similar_posts(vec(e0=1))) == kept[1:]
            assert remove_orphans(conn, qdrant) == 0
