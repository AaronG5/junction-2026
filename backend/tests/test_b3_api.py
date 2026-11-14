"""B3: the read-only API answers exactly like the frontend's demo.

Integration tests: one throwaway database (veritas_test_<random>) gets the schema
and the demo data (shared/demo/demo-data.json), then the API is called like the
frontend calls it. Skipped when MySQL isn't running.
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import uuid
from typing import Iterator

import pytest

import demo_data as demo_cli
from conftest import FIXTURES, SHARED, make_client, scratch_database
from veritas import queries
from veritas.config import Settings
from veritas.db import connection
from veritas.db import demo
from veritas.db.migrations import migrate
from veritas.filters import DateRange, matches_platform, matches_query, matches_status, overlaps, shift_days
from veritas.links import link_key

pytestmark = pytest.mark.integration

MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
SEED = json.loads((SHARED / "demo" / "demo-data.json").read_text(encoding="utf-8"))
TODAY = SEED["today"]
LINK_KEYS = json.loads((SHARED / "cases" / "links.json").read_text(encoding="utf-8"))["keys"]

# Fixtures answered by later phases. Listed here so none is forgotten (see test below).
LATER = {"/api/system": "B7"}


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> Iterator[Settings]:
    """A database with the schema and the demo data, shared by the read-only tests."""
    with scratch_database() as settings:
        with connection(settings) as conn:
            migrate(conn)
            demo.load(conn)
        yield dataclasses.replace(settings, frontend_dist=tmp_path_factory.mktemp("dist"))


@pytest.fixture(scope="module")
def api(demo_db):
    return make_client(demo_db)


@pytest.fixture
def empty_db(tmp_path) -> Iterator[Settings]:
    with scratch_database() as settings:
        with connection(settings) as conn:
            migrate(conn)
        yield dataclasses.replace(settings, frontend_dist=tmp_path)


def _call(client, entry: dict):
    if entry["method"] in ("POST", "PUT"):
        return client.request(entry["method"], entry["path"], params=entry["query"], json=entry.get("body"))
    return client.get(entry["path"], params=entry["query"])


# --------------------------------------------------------------------------- parity: the key test

EXACT = [e for e in MANIFEST["fixtures"] if e["replay"] == "exact" and e["path"] not in LATER]


@pytest.mark.parametrize("entry", EXACT, ids=lambda e: e["file"])
def test_same_answer_as_the_frontend_demo(api, entry):
    res = _call(api, entry)
    expected = json.loads((FIXTURES / entry["file"]).read_text(encoding="utf-8"))
    assert res.status_code == entry["status"], res.text
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == expected  # parsed equality: key order ignored, 38 == 38.0


def test_only_later_phases_are_skipped():
    skipped = {e["path"] for e in MANIFEST["fixtures"] if e["replay"] == "exact" and e["path"] in LATER}
    assert skipped <= set(LATER)
    assert len(EXACT) >= 17


# --------------------------------------------------------------------------- every filter combination

RANGES = {
    "none": {},
    "7d": {"from": shift_days(TODAY, -7), "to": TODAY},
    "30d": {"from": shift_days(TODAY, -30), "to": TODAY},
    "90d": {"from": shift_days(TODAY, -90), "to": TODAY},
    "from-only": {"from": shift_days(TODAY, -3)},
    "to-only": {"to": shift_days(TODAY, -15)},
    "past-window": {"from": shift_days(TODAY, -70), "to": shift_days(TODAY, -40)},
    "reversed": {"from": TODAY, "to": shift_days(TODAY, -30)},
    "single-day": {"from": shift_days(TODAY, -4), "to": shift_days(TODAY, -4)},
}
PLATFORMS = ["all", "Telegram", "Facebook"]
STATUSES = ["all", "true", "false", "misleading", "unverified"]
COMBOS = list(itertools.product(PLATFORMS, STATUSES, RANGES))


def _params(platform, status, range_name, **extra):
    p = dict(RANGES[range_name], **extra)
    if platform != "all":
        p["platform"] = platform
    if status != "all":
        p["status"] = status
    return p


def _range(range_name) -> DateRange:
    r = RANGES[range_name]
    f, t = r.get("from", ""), r.get("to", "")
    return DateRange(f, t, bool(f and t and f > t))


def _expect(items, platform_of, status_of, platform, status, range_name, extra=lambda _: True):
    """Reference answer from the demo data: ids in list order, and status counts."""
    r = _range(range_name)
    base = [i for i in items if matches_platform(platform_of(i), platform)
            and overlaps(i["firstSeen"], i["lastSeen"], r) and extra(i)]
    counts = {"all": len(base), "true": 0, "false": 0, "misleading": 0, "unverified": 0}
    for i in base:
        counts[status_of(i)] += 1
    return [i["id"] for i in base if matches_status(status_of(i), status)], counts


@pytest.mark.parametrize("platform, status, range_name", COMBOS)
def test_narratives_filters(api, platform, status, range_name):
    body = api.get("/api/narratives", params=_params(platform, status, range_name)).json()
    ids, counts = _expect(SEED["clusters"], lambda c: c["platforms"], lambda c: c["status"], platform, status, range_name)
    assert [c["id"] for c in body["clusters"]] == ids
    assert body["counts"] == counts  # counted before the status filter
    assert body["total"] == len(SEED["clusters"])
    # points: posts that fit no cluster first, then each listed cluster's, in list order
    expected_points = [p for p in SEED["points"] if p["clusterId"] is None]
    for cid in ids:
        expected_points += [p for p in SEED["points"] if p["clusterId"] == cid]
    assert body["points"] == expected_points


@pytest.mark.parametrize("platform, status, range_name", COMBOS)
@pytest.mark.parametrize("q", ["", "nato", "  ELECTRICITY ", "ą", "zzz"])
def test_claims_filters(api, platform, status, range_name, q):
    params = _params(platform, status, range_name, **({"q": q} if q else {}))
    body = api.get("/api/claims", params=params).json()
    ids, counts = _expect(SEED["claims"], lambda c: c["platforms"], lambda c: c["verdict"], platform, status, range_name,
                          extra=lambda c: matches_query(c["text"], q))
    assert [c["id"] for c in body["claims"]] == ids
    assert body["counts"] == counts
    assert body["total"] == len(SEED["claims"])


@pytest.mark.parametrize("platform, status, range_name", COMBOS)
def test_trending_filters(api, platform, status, range_name):
    body = api.get("/api/trending", params=_params(platform, status, range_name)).json()
    ids, _ = _expect(SEED["stories"], lambda s: [s["platform"]], lambda s: s["status"], platform, status, range_name)
    assert [s["id"] for s in body["stories"]] == ids


@pytest.mark.parametrize("platform", ["all", "Telegram", "Facebook", "Website"])
def test_sources_filter(api, platform):
    params = {} if platform == "all" else {"platform": platform}
    body = api.get("/api/sources", params=params).json()
    assert [s["id"] for s in body["sources"]] == [
        s["id"] for s in SEED["sources"] if platform == "all" or s["platform"] == platform]


def test_lists_are_ordered():
    assert [c["size"] for c in SEED["clusters"]] == sorted((c["size"] for c in SEED["clusters"]), reverse=True)
    assert [c["lastSeen"] for c in SEED["claims"]] == sorted((c["lastSeen"] for c in SEED["claims"]), reverse=True)
    assert [s["nearCopyPct"] for s in SEED["sources"]] == sorted((s["nearCopyPct"] for s in SEED["sources"]), reverse=True)
    assert [s["trend"]["posts"] for s in SEED["stories"]] == sorted((s["trend"]["posts"] for s in SEED["stories"]), reverse=True)


def test_all_values_are_accepted_like_missing_ones(api):
    for path in ("/api/narratives", "/api/claims", "/api/trending"):
        assert api.get(path, params={"platform": "all", "status": "all"}).json() == api.get(path).json()
    assert api.get("/api/sources", params={"platform": "all"}).json() == api.get("/api/sources").json()


# --------------------------------------------------------------------------- single items and errors

@pytest.mark.parametrize("url, key", LINK_KEYS)
def test_trace_finds_a_story_by_any_spelling_of_its_link(api, url, key):
    stored = {link_key(s["post"]["url"]): s["id"] for s in SEED["stories"] if s["post"].get("url")}
    res = api.get("/api/trace", params={"url": url})
    if key in stored:
        assert res.status_code == 200 and res.json()["id"] == stored[key]
    elif url.strip() and res.status_code != 400:
        assert res.status_code == 404
        assert res.json() == {"detail": queries.NOT_COLLECTED}


@pytest.mark.parametrize("url", ["https://T.ME/kanalas_b/4521/", "t.me/kanalas_b/4521?single", "http://telegram.me/kanalas_b/4521"])
def test_trace_variants_of_a_stored_link(api, url):
    res = api.get("/api/trace", params={"url": url})
    assert res.status_code == 200 and res.json()["id"] == "nato-border"


def test_trace_unknown_post(api):
    res = api.get("/api/trace", params={"url": "https://t.me/someone/42"})
    assert res.status_code == 404 and res.json() == {"detail": queries.NOT_COLLECTED}


@pytest.mark.parametrize("path, message", [
    ("/api/stories/NATO-BORDER", queries.NO_STORY),  # ids are exact, even though MySQL compares ignoring case
    ("/api/stories/nato-border%20", queries.NO_STORY),
    ("/api/sources/KANALAS_B", queries.NO_SOURCE_REPORT),
    ("/api/sources/kanal_a", queries.NO_SOURCE_REPORT),  # a source without a report yet
    ("/api/sources/x'%20OR%20'1'='1", queries.NO_SOURCE_REPORT),
    ("/api/stories/'%3B%20DROP%20TABLE%20stories%3B%20--", queries.NO_STORY),
])
def test_unknown_items_are_404_with_the_frontends_message(api, path, message):
    res = api.get(path)
    assert res.status_code == 404 and res.json() == {"detail": message}


@pytest.mark.parametrize("path, params, mentions", [
    ("/api/narratives", {"status": "maybe"}, "status"),
    ("/api/narratives", {"from": "yesterday"}, "from"),
    ("/api/narratives", {"from": "2026-02-30"}, "from"),
    ("/api/narratives", {"to": "2026-1-5"}, "to"),
    ("/api/narratives", {"platform": "Website"}, "platform"),  # no websites in narratives
    ("/api/narratives", {"platform": "telegram"}, "platform"),
    ("/api/claims", {"q": "x" * 201}, "q"),
    ("/api/trending", {"status": "False"}, "status"),
    ("/api/sources", {"platform": "Twitter"}, "platform"),
    ("/api/trace", {}, "url"),
    ("/api/trace", {"url": "https://t.me/" + "a" * 2048}, "url"),
])
def test_bad_parameters_are_422_in_plain_words(api, path, params, mentions):
    res = api.get(path, params=params)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail.startswith("The request isn't valid.") and mentions in detail


def test_invalid_link_is_400(api):
    res = api.get("/api/trace", params={"url": "hello"})
    assert res.status_code == 400 and res.json() == {"detail": queries.NOT_A_LINK}


def test_search_is_never_sql(api, demo_db):
    for q in ("'; DROP TABLE claims; --", "%", "_", "\\", "' OR 1=1 --"):
        res = api.get("/api/claims", params={"q": q})
        assert res.status_code == 200 and res.json()["claims"] == []
    with connection(demo_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM claims")
        assert cur.fetchone()["n"] == len(SEED["claims"])


def test_lists_are_capped_but_counts_are_not(api, monkeypatch):
    monkeypatch.setattr(queries, "MAX_CLAIMS", 2)
    monkeypatch.setattr(queries, "MAX_CLUSTERS", 1)
    monkeypatch.setattr(queries, "MAX_STORIES", 1)
    monkeypatch.setattr(queries, "MAX_SOURCES", 3)
    claims = api.get("/api/claims").json()
    assert len(claims["claims"]) == 2 and claims["counts"]["all"] == len(SEED["claims"])
    narratives = api.get("/api/narratives").json()
    assert len(narratives["clusters"]) == 1 and narratives["counts"]["all"] == len(SEED["clusters"])
    assert len(api.get("/api/trending").json()["stories"]) == 1
    assert len(api.get("/api/sources").json()["sources"]) == 3


# --------------------------------------------------------------------------- empty database, database down

def test_empty_database_gives_empty_lists(empty_db):
    client = make_client(empty_db)
    nar = client.get("/api/narratives").json()
    assert nar["clusters"] == [] and nar["points"] == [] and nar["total"] == 0
    assert nar["counts"] == {"all": 0, "true": 0, "false": 0, "misleading": 0, "unverified": 0}
    assert nar["run"]["posts"] == 0
    claims = client.get("/api/claims", params={"q": "nato"}).json()
    assert claims == {"claims": [], "total": 0, "counts": nar["counts"]}
    assert client.get("/api/sources").json() == {"sources": []}
    assert client.get("/api/trending").json() == {"stories": []}
    assert client.get("/api/stories/nato-border").status_code == 404
    assert client.get("/api/sources/kanalas_b").status_code == 404
    assert client.get("/api/trace", params={"url": "https://t.me/kanalas_b/4521"}).status_code == 404


def test_database_down_is_503_in_plain_words(tmp_path):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    client = make_client(Settings(mysql_port=port, mysql_password="pw-never-shown-77", frontend_dist=tmp_path))
    for path in ("/api/narratives", "/api/claims", "/api/sources", "/api/trending", "/api/stories/x", "/api/sources/x"):
        res = client.get(path)
        assert res.status_code == 503, path
        assert "isn't reachable" in res.json()["detail"] and "pw-never-shown-77" not in res.text
    # a typo in the link is answered without the database
    assert client.get("/api/trace", params={"url": "hello"}).status_code == 400
    assert client.get("/api/trace", params={"url": "https://t.me/x/1"}).status_code == 503


# --------------------------------------------------------------------------- the database's own values

def test_finer_verdicts_and_evidence_links(empty_db):
    with connection(empty_db) as conn, conn.cursor() as cur:
        for i, verdict in enumerate(["mostly_false", "mixed", "mostly_true", "unverifiable"], 1):
            cur.execute("INSERT INTO claims (claim_id, uuid, text, verdict, first_seen, last_seen, platforms)"
                        " VALUES (%s, %s, %s, %s, '2026-11-01', '2026-11-02', '[\"Telegram\"]')",
                        (i, uuid.uuid4().bytes, f"claim {verdict}", verdict))
        cur.execute("INSERT INTO claim_evidence (claim_id, url, publisher, stance, excerpt) VALUES"
                    " (1, 'https://example.org/a', 'Example', 'refutes', 'no'),"
                    " (1, NULL, NULL, 'mixed', NULL)")
    body = make_client(empty_db).get("/api/claims").json()
    assert {c["text"]: c["verdict"] for c in body["claims"]} == {
        "claim mostly_false": "false", "claim mixed": "misleading", "claim mostly_true": "true", "claim unverifiable": "unverified"}
    first = next(c for c in body["claims"] if c["id"] == 1)
    assert first["evidence"] == [
        {"source": "Example", "stance": "contradicts", "note": "no", "url": "https://example.org/a"},
        {"source": "Unknown source", "stance": "neither", "note": ""},  # no url key at all
    ]
    assert first["checkedBy"] == "" and first["confidence"] == 0


def test_sources_on_other_platforms_are_not_listed(empty_db):
    with connection(empty_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (account_id, uuid, platform_id, external_id, display_name) VALUES"
                    " (1, %s, 3, 'someone', 'Someone on X')", (uuid.uuid4().bytes,))
        cur.execute("INSERT INTO source_profiles (account_id, source_key, kind_label, report) VALUES (1, 'someone', 'Account', '{}')")
    client = make_client(empty_db)
    assert client.get("/api/sources").json() == {"sources": []}
    assert client.get("/api/sources/someone").status_code == 404


# --------------------------------------------------------------------------- demo loader

def test_demo_loader_refuses_a_database_with_real_data(empty_db):
    with connection(empty_db) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO claims (uuid, text) VALUES (%s, 'a real claim')", (uuid.uuid4().bytes,))
        with pytest.raises(demo.DemoDataError, match="already holds real data"):
            demo.load(conn)
        assert demo.remove(conn) is False  # not demo data: untouched
        with conn.cursor() as cur:
            cur.execute("SELECT text FROM claims")
            assert [r["text"] for r in cur.fetchall()] == ["a real claim"]
            cur.execute("SELECT COUNT(*) AS n FROM settings")
            assert cur.fetchone()["n"] == 0


def test_demo_loader_reload_replaces_and_remove_empties(empty_db):
    def count(cur, table):
        cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
        return cur.fetchone()["n"]

    with connection(empty_db) as conn:
        first = demo.load(conn)
        assert demo.load(conn) == first  # loading again replaces, no duplicates
        with conn.cursor() as cur:
            assert count(cur, "claims") == len(SEED["claims"])
            assert count(cur, "posts") == len(SEED["points"])
            assert count(cur, "clustering_runs") == 1
        assert demo.is_loaded(conn)
        assert demo.remove(conn) is True
        with conn.cursor() as cur:
            for table in demo.CONTENT_TABLES:
                assert count(cur, table) == 0, table
            assert count(cur, "platforms") == 8  # reference data stays
        assert not demo.is_loaded(conn)


def test_demo_loader_rolls_back_on_bad_data(empty_db):
    broken = json.loads(json.dumps(SEED))
    broken["stories"][0]["platform"] = "Myspace"
    with connection(empty_db) as conn:
        with pytest.raises(KeyError):
            demo.load(conn, broken)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM claims")
            assert cur.fetchone()["n"] == 0  # nothing half-loaded
        assert not demo.is_loaded(conn)


def test_demo_dates_can_be_moved_to_today(empty_db):
    later = shift_days(TODAY, 10)
    with connection(empty_db) as conn:
        demo.load(conn, today=later)
    client = make_client(empty_db)
    story = client.get("/api/stories/nato-border").json()
    original = next(s for s in SEED["stories"] if s["id"] == "nato-border")
    assert story["firstSeen"] == shift_days(original["firstSeen"], 10)
    report = client.get("/api/sources/kanalas_b").json()
    assert report["daily"][-1]["date"] == later
    assert report["recent"][0]["publishedAt"].startswith(shift_days(SEED["channelReports"]["kanalas_b"]["recent"][0]["publishedAt"][:10], 10))
    # the same relative question gives the same answer as on the export day
    moved = client.get("/api/narratives", params={"from": shift_days(later, -7), "to": later}).json()
    assert [c["id"] for c in moved["clusters"]] == [c["id"] for c in json.loads((FIXTURES / "narratives-7d.json").read_text(encoding="utf-8"))["clusters"]]


def test_shift_dates_leaves_other_text_alone():
    value = {"a": "2026-11-14", "b": "2026-11-14T11:00:00Z", "c": "note 2026-11-14", "d": 2026, "e": ["2026-12-31"]}
    assert demo.shift_dates(value, 1) == {"a": "2026-11-15", "b": "2026-11-15T11:00:00Z", "c": "note 2026-11-14", "d": 2026, "e": ["2027-01-01"]}


def test_demo_cli(empty_db, monkeypatch, capsys):
    monkeypatch.setattr(demo_cli.Settings, "load", classmethod(lambda cls, *a, **k: empty_db))
    assert demo_cli.main(["--as-exported"]) == 0
    assert "Demo data loaded: 7 claims" in capsys.readouterr().out
    assert demo_cli.main(["--remove"]) == 0
    assert "Demo data removed." in capsys.readouterr().out
    assert demo_cli.main(["--remove"]) == 0
    assert "no demo data" in capsys.readouterr().out


def test_demo_cli_needs_the_schema_first(monkeypatch, capsys):
    with scratch_database() as bare:  # no migrations
        monkeypatch.setattr(demo_cli.Settings, "load", classmethod(lambda cls, *a, **k: bare))
        assert demo_cli.main([]) == 1
        assert "backend/migrate.py" in capsys.readouterr().err
