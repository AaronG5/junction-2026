"""In-depth analysis for the browser extension (veritas/deep.py, POST/GET /api/ext/deep).

- Patterns: phrases not in the post are dropped, duplicates too; clearest tier first.
- Truth check: verdicts as given, a quote not in the post is cleared, the basis is stated.
- Trace: oldest first, background (undated narrative) on top, weak Telegram matches left out,
  only https links, the original of a shared post, this post with how it reached the feed.
- API: start, poll, the same post reuses its job, empty text 400, AI down 503, unknown job 404.
Plain tests: a fake AI, no database (the database search is swapped out).
"""
from __future__ import annotations

import time

import pytest

from conftest import make_client
from test_b6_analyze import FakeAI
from veritas import deep
from veritas.deep import DeepJobs, DeepServices, build_trace, check_truth, find_patterns

TEXT = ("Kyiv regime is nothing but an American puppet. Everyone knows the media lies about it. "
        "Ukraine received 60 billion dollars from the US in 2024.")

PATTERNS = {"patterns": [
    {"quote": "nothing but an American puppet", "technique": "Name-calling", "tier": "likely", "why": "Labels a government."},
    {"quote": "Everyone knows the media lies", "technique": "Media distrust", "tier": "clear", "why": "Dismisses all media."},
    {"quote": "secret NATO plan", "technique": "Conspiracy framing", "tier": "clear", "why": "Not in the post."},
    {"quote": "everyone KNOWS the   media lies", "technique": "Bandwagon", "tier": "possible", "why": "Duplicate phrase."},
], "overall": "Uses labels and distrust of media to make readers reject Ukraine."}

TRUTH = {"claims": [
    {"claim": "Ukraine received 60 billion dollars from the US in 2024.", "quote": "Ukraine received 60 billion dollars",
     "verdict": "misleading", "confidence": 60, "explanation": "Aid was approved, but much of it went to US industry."},
    {"claim": "Kyiv is controlled by the US.", "quote": "words that are not there", "verdict": "false", "confidence": 85,
     "explanation": "Ukraine is a sovereign state."},
]}


@pytest.fixture
def ai():
    fake = FakeAI()
    fake.answers["patterns"] = PATTERNS
    fake.answers["truth"] = TRUTH
    return fake


def test_patterns_are_checked_against_the_post(ai):
    out = find_patterns(ai.client(), TEXT, "English")
    assert [p["quote"] for p in out["items"]] == ["Everyone knows the media lies", "nothing but an American puppet"]
    assert [p["tier"] for p in out["items"]] == ["clear", "likely"]
    assert out["overall"].startswith("Uses labels") and out["prompt"] == "patterns.v1"


def test_truth_check_clears_quotes_not_in_the_post(ai):
    out = check_truth(ai.client(), TEXT, "2026-11-14T10:00:00Z", "English")
    assert [c["verdict"] for c in out["items"]] == ["misleading", "false"]
    assert out["items"][0]["quote"] == "Ukraine received 60 billion dollars" and out["items"][1]["quote"] == ""
    assert "No sources were checked" in out["basis"]


ITEM = {"id": "fb1", "url": "https://www.facebook.com/groups/g1/posts/1/", "author": "a.b", "author_name": "Ann B",
        "text": TEXT, "time": "2026-11-14T10:00:00.000Z", "kind": "post", "platform": "facebook", "group": "Group One"}
CONTEXT = {
    "narrative": {"text": "The Kyiv regime is an American puppet", "source": "euvsdisinfo", "sim": 0.93},
    "state_media_echo": [{"text": "Kiev is a puppet of Washington", "source": "rt", "n_aired": 253, "first": "2024-01-07", "last": "2024-10-02"}],
    "source_match": {"strong": True, "telegram": [
        {"channel": "two_majors", "url": "https://t.me/two_majors/1", "date": "2026-11-13T18:49:44Z", "sim": 0.97, "text": "Kyiv puppet"},
        {"channel": "weak_one", "url": "https://t.me/weak_one/2", "date": "2026-11-12T00:00:00Z", "sim": 0.6, "text": "other"},
        {"channel": "bad_link", "url": "javascript:alert(1)", "date": "2026-11-13T00:00:00Z", "sim": 0.9, "text": "x"}]},
}


def test_trace_is_a_timeline_oldest_first():
    similar = [{"relation": "Same content", "who": "@kanal", "where": "Telegram channel", "time": "2026-11-14T08:00:00Z",
                "url": "https://t.me/kanal/5", "sim": 0.9, "text": "Kyiv puppet again"}]
    shared = {**ITEM, "shared": {"author_name": "Orig Inal", "url": "https://www.facebook.com/x/posts/9", "time": "2026-11-14T09:00:00Z", "text": "orig"}}
    trace = build_trace(shared, CONTEXT, similar)
    kinds = [n["kind"] for n in trace["nodes"]]
    assert kinds == ["narrative", "state_media", "telegram", "telegram", "similar", "original", "this"]
    assert [n["who"] for n in trace["nodes"] if n["kind"] == "telegram"] == ["@bad_link", "@two_majors"]
    assert next(n for n in trace["nodes"] if n["who"] == "@bad_link")["url"] is None   # only https links
    assert trace["nodes"][0]["time"] is None and trace["nodes"][-1]["where"] == "Facebook group “Group One”"
    assert trace["summary"].startswith("The earliest trace found is RT (State media)")
    assert "5 earlier appearances" in trace["summary"]


def test_trace_with_nothing_known():
    trace = build_trace({**ITEM, "group": None}, {}, [])
    assert [n["kind"] for n in trace["nodes"]] == ["this"]
    assert trace["summary"].startswith("No earlier trace found")
    assert trace["nodes"][0]["note"] == "How it reached your feed: posted by this account."


# --------------------------------------------------------------------------- the API

@pytest.fixture
def client(settings, ai):
    jobs = DeepJobs(DeepServices(ai=ai.client, similar=lambda _ai, text, url: []))
    deep.set_jobs(jobs)
    yield make_client(settings), ai
    deep.set_jobs(None)


def wait(c, job_id):
    for _ in range(100):
        job = c.get(f"/api/ext/deep/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("the job didn't finish")


def test_api_runs_all_three_parts(client):
    c, ai = client
    res = c.post("/api/ext/deep", json={"item": ITEM, "context": CONTEXT,
                                        "shared": {"author_name": "Orig", "url": "https://www.facebook.com/x/posts/9", "text": "o"}})
    assert res.status_code == 200 and res.json()["status"] == "running"
    job = wait(c, res.json()["id"])
    assert job["status"] == "done" and job["step"] == 3 and job["error"] is None
    assert len(job["patterns"]["items"]) == 2 and len(job["truth"]["items"]) == 2
    assert [n["kind"] for n in job["trace"]["nodes"]][-2:] == ["original", "this"]
    assert ai.calls.count("patterns") == 1 and ai.calls.count("truth") == 1


def test_api_same_post_reuses_its_job(client):
    c, ai = client
    first = c.post("/api/ext/deep", json={"item": ITEM}).json()
    wait(c, first["id"])
    again = c.post("/api/ext/deep", json={"item": ITEM}).json()
    assert again["id"] == first["id"] and again["status"] == "done"
    assert ai.calls.count("patterns") == 1


def test_api_refusals(client):
    c, ai = client
    assert c.post("/api/ext/deep", json={"item": {**ITEM, "text": "  "}}).status_code == 400
    assert c.get("/api/ext/deep/nope").status_code == 404
    ai.up = False
    res = c.post("/api/ext/deep", json={"item": {**ITEM, "id": "fb2"}})
    assert res.status_code == 503 and "local AI" in res.json()["detail"]


def test_api_ai_failing_mid_job_ends_with_a_plain_error(client):
    c, ai = client
    del ai.answers["truth"]   # the fake AI raises KeyError -> 500 from the AI -> AiUnavailable
    job = wait(c, c.post("/api/ext/deep", json={"item": {**ITEM, "id": "fb3"}}).json()["id"])
    assert job["status"] == "error" and job["error"] and job["patterns"] is not None
