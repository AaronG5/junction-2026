"""In-depth analysis for the browser extension (veritas/deep.py, POST/GET /api/ext/deep).

- Patterns: phrases not in the post are dropped, duplicates too; clearest tier first.
- Truth check: claims + queries, the search tool's sources (never the post, never propaganda),
  a verdict per claim that must cite them; basic general knowledge is labelled.
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
from veritas.evidence import Source

TEXT = ("Kyiv regime is nothing but an American puppet. Everyone knows the media lies about it. "
        "Ukraine received 60 billion dollars from the US in 2024.")

PATTERNS = {"patterns": [
    {"quote": "nothing but an American puppet", "technique": "Name-calling", "tier": "likely", "why": "Labels a government."},
    {"quote": "Everyone knows the media lies", "technique": "Media distrust", "tier": "clear", "why": "Dismisses all media."},
    {"quote": "secret NATO plan", "technique": "Conspiracy framing", "tier": "clear", "why": "Not in the post."},
    {"quote": "everyone KNOWS the   media lies", "technique": "Bandwagon", "tier": "possible", "why": "Duplicate phrase."},
], "overall": "Uses labels and distrust of media to make readers reject Ukraine."}

CLAIMS = {"claims": [
    {"claim": "Ukraine received 60 billion dollars from the US in 2024.", "quote": "Ukraine received 60 billion dollars",
     "queries": ["Ukraine US aid 2024 60 billion", "Ukraina 60 milijardų JAV"]},
    {"claim": "Kyiv is controlled by the US.", "quote": "words that are not there", "queries": ["Kyiv puppet United States"]},
    {"claim": "Vilnius is the capital of Latvia.", "quote": "", "queries": ["capital of Latvia"]},
    {"claim": "Pensioners in Lithuania live in poverty.", "quote": "", "queries": ["Lithuania pensions poverty"]},
]}
# One verdict per claim, in order (SeqAI below).
VERDICTS = [
    {"verdict": "misleading", "sources": [1, 7], "basis": "sources", "explanation": "Approved in April 2024; much is spent in the US."},
    {"verdict": "refuted", "sources": [], "basis": "sources", "explanation": "It just isn't so."},     # cites nothing
    {"verdict": "refuted", "sources": [], "basis": "general_knowledge", "explanation": "Riga is the capital of Latvia."},
]


class SeqAI(FakeAI):
    """FakeAI, but the "verdict" prompt gets the next answer from a list (one per claim)."""

    def __init__(self, verdicts):
        super().__init__()
        self.verdicts = list(verdicts)

    def __call__(self, request):
        import json as _json
        if request.url.path.endswith("/chat/completions"):
            body = _json.loads(request.content)
            if body["response_format"]["json_schema"]["name"] == "verdict":
                self.answers["verdict"] = self.verdicts.pop(0)
        return super().__call__(request)


class Found:
    """A search tool stand-in: sources for three claims (plus one from a propaganda channel for the
    aid claim), none for the pensioners claim."""

    def search(self, claim, queries, limit=4):
        if "Kyiv" in claim or "capital" in claim:
            return [Source(title="Background", text="Background text.", source="Encyclopedia", url="https://en.wikipedia.org/x")]
        if "60 billion" not in claim:
            return []
        return [Source(title="US aid to Ukraine", text="Congress approved about 61 billion dollars in April 2024...",
                       source="Reuters", url="https://www.reuters.com/x", date="2024-04-24"),
                Source(title="Channel", text="60 billion stolen", source="channel", url="https://t.me/two_majors/1")]


@pytest.fixture
def ai():
    fake = SeqAI(VERDICTS)
    fake.answers["patterns"] = PATTERNS
    fake.answers["claimsearch"] = CLAIMS
    return fake


def test_patterns_are_checked_against_the_post(ai):
    out = find_patterns(ai.client(), TEXT, "English")
    assert [p["quote"] for p in out["items"]] == ["Everyone knows the media lies", "nothing but an American puppet"]
    assert [p["tier"] for p in out["items"]] == ["clear", "likely"]
    assert out["overall"].startswith("Uses labels") and out["prompt"] == "patterns.v1"


def test_truth_check_uses_the_search_tool_not_the_post(ai):
    out = check_truth(ai.client(), TEXT, "English", [Found()])
    aid, puppet, capital, pensions = out["items"]
    # Judged against the source found; the Telegram "source" never reaches Gemma; a cited number
    # that wasn't given (7) is dropped.
    assert aid["verdict"] == "misleading" and aid["basis"] == "sources"
    assert [s["source"] for s in aid["sources"]] == ["Reuters"] and aid["sources"][0]["n"] == 1
    assert aid["searched"] == ["Ukraine US aid 2024 60 billion", "Ukraina 60 milijardų JAV"]
    assert aid["quote"] == "Ukraine received 60 billion dollars" and puppet["quote"] == ""
    # A verdict "from sources" that cites none stays unverified.
    assert puppet["verdict"] == "unverified" and puppet["basis"] == "none" and puppet["sources"] == []
    # Basic general knowledge is allowed, and labelled as such.
    assert capital["verdict"] == "refuted" and capital["basis"] == "general_knowledge"
    # No source at all: unverified without asking Gemma.
    assert pensions["verdict"] == "unverified" and pensions["explanation"] == "No trusted source about this was found."
    assert "never against the post" in out["basis"] and ai.calls.count("verdict") == 3


def test_the_verdict_prompt_gets_numbered_sources_and_not_the_post(ai):
    seen = []
    original = SeqAI.__call__

    def spy(self, request):
        import json as _json
        if request.url.path.endswith("/chat/completions"):
            body = _json.loads(request.content)
            if body["response_format"]["json_schema"]["name"] == "verdict":
                seen.append(body["messages"][1]["content"])
        return original(self, request)

    SeqAI.__call__ = spy
    try:
        check_truth(ai.client(), TEXT, "English", [Found()])
    finally:
        SeqAI.__call__ = original
    assert "[1] (trusted document, Reuters, 2024-04-24) US aid to Ukraine" in seen[0]
    assert "t.me" not in seen[0] and "stolen" not in seen[0]
    assert len(seen) == 3                                                 # the pensioners claim had no source
    assert all("Everyone knows the media lies" not in s for s in seen)   # the post is not given as evidence


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
    jobs = DeepJobs(DeepServices(ai=ai.client, similar=lambda _ai, text, url: [], evidence=lambda _ai: [Found()]))
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
    assert len(job["patterns"]["items"]) == 2 and len(job["truth"]["items"]) == 4
    assert [n["kind"] for n in job["trace"]["nodes"]][-2:] == ["original", "this"]
    assert ai.calls.count("patterns") == 1 and ai.calls.count("claimsearch") == 1 and ai.calls.count("verdict") == 3
    assert job["truth"]["items"][0]["sources"][0]["source"] == "Reuters"


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
    del ai.answers["claimsearch"]   # the fake AI raises KeyError -> 500 from the AI -> AiUnavailable
    job = wait(c, c.post("/api/ext/deep", json={"item": {**ITEM, "id": "fb3"}}).json()["id"])
    assert job["status"] == "error" and job["error"] and job["patterns"] is not None


# --------------------------------------------------------------------------- the trace never lists the post itself

@pytest.mark.parametrize("item", [
    {"id": "fb3674462936037307", "url": "https://www.facebook.com/groups/NT247/posts/3674462936037307/", "text": "t",
     "author": "a.b", "author_name": "A B", "platform": "facebook", "group": "G"},
    {"id": "fb1797528575746957", "url": "https://www.facebook.com/LRT.LT/posts/pfbid02UZPuNcJYtrVWGJgD74SNz?__cft__[0]=AZ",
     "text": "t", "author": "LRT.LT", "author_name": "LRT", "platform": "facebook"},
    {"id": "fb1455920952739663", "url": "https://www.facebook.com/reel/1455920952739663/", "text": "t",
     "author": "LRT.LT", "author_name": "LRT", "platform": "facebook"},
    {"id": "1890412233445566771", "url": "https://x.com/EU_Insider/status/1890412233445566771", "text": "t",
     "author": "EU_Insider", "author_name": "EU Insider"},
])
def test_a_saved_post_is_recognised_as_the_post_itself(item):
    from veritas.deep import is_same_post
    from veritas.ext_store import to_collected

    saved = to_collected(item)   # exactly how the extension's posts are stored
    row = {"url": saved.url, "platform": saved.platform, "external_id": saved.external_id}
    assert is_same_post(row, item["url"])
    import re as _re
    pid = saved.external_id.split("_")[-1]
    other = {**row, "url": _re.sub(_re.escape(pid), "999999", saved.url, flags=_re.IGNORECASE), "external_id": "other_999999"}
    assert not is_same_post(other, item["url"])
    assert not is_same_post(row, None)
