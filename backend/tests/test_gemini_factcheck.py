"""Gemini (veritas/gemini.py), its claims and fact checks (veritas/factcheck.py), and how the
cluster reading uses them (veritas/cluster_insight.py). A fake Gemini: no network, no key."""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime

import httpx
import pytest

from conftest import scratch_database
from veritas import app_settings, factcheck
from veritas.ai import load_prompt
from veritas.analyzers import ExtractedClaim
from veritas.db import connection
from veritas.db.migrations import migrate
from veritas.factcheck import (
    NO_SOURCES, UNSURE, FactCheckAnswer, StrictClaims, check_claim, check_claims, extract_claims, gemini_for,
)
from veritas.gemini import Gemini, GeminiError, GeminiUnavailable, inline_schema
from veritas.models import AppSettings

KEY = "AQ.test-key-never-shown"


def reply(payload, *, sources=(), status=200, thought=None):
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": json.dumps(payload)}]
    candidate = {"content": {"parts": parts}}
    if sources:
        candidate["groundingMetadata"] = {
            "webSearchQueries": ["q"],
            "groundingChunks": [{"web": {"uri": url, "title": title}} for title, url in sources],
            "groundingSupports": [{"segment": {"text": "It says so."}, "groundingChunkIndices": list(range(len(sources)))}],
        }
    return httpx.Response(status, json={"candidates": [candidate], "modelVersion": "gemini-test"})


class FakeGemini:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        answer = self.answers.pop(0)
        return answer() if callable(answer) else answer

    def client(self):
        return Gemini(KEY, "gemini-test", transport=httpx.MockTransport(self), waits=(0, 0))


# --------------------------------------------------------------------------- the client

def test_schemas_are_sent_without_refs():
    schema = inline_schema(StrictClaims.model_json_schema())
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and '"title"' not in text
    assert schema["properties"]["claims"]["items"]["properties"]["checkworthiness"]["enum"] == ["high", "medium", "low"]


def test_an_answer_with_search_sources():
    fake = FakeGemini(reply({"verdict": "true", "confidence": 0.9, "explanation": "Yes."}, thought="hidden reasoning",
                            sources=[("lrt.lt", "https://lrt.lt/a"), ("reuters.com", "https://reuters.com/b")]))
    answer = fake.client().ask(load_prompt("factcheck"), FactCheckAnswer, search=True, claim="C", first_seen="2026-11-01",
                               today="2026-11-15")
    assert answer.value.verdict == "true" and answer.model == "gemini-test" and answer.prompt == "factcheck.v1"
    assert [(s.title, s.url, s.backs) for s in answer.sources] == [("lrt.lt", "https://lrt.lt/a", ("It says so.",)),
                                                                  ("reuters.com", "https://reuters.com/b", ("It says so.",))]
    body = fake.requests[0]
    assert body["tools"] == [{"google_search": {}}]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "Claim: C" in body["contents"][0]["parts"][0]["text"]
    assert fake.headers[0]["x-goog-api-key"] == KEY                       # the key only travels in the header


def test_no_search_tool_unless_asked():
    fake = FakeGemini(reply({"claims": []}))
    fake.client().ask(load_prompt("claims_strict"), StrictClaims, text="t", language="Lithuanian", published="2026-11-13",
                      source="x")
    assert "tools" not in fake.requests[0]


def test_rate_limits_and_server_errors_are_retried():
    fake = FakeGemini(httpx.Response(429, json={}), httpx.Response(503, json={}), reply({"claims": []}))
    answer = fake.client().ask(load_prompt("claims_strict"), StrictClaims, text="t", language="x", published="", source="")
    assert answer.value.claims == [] and len(fake.requests) == 3


def test_errors_are_plain_and_never_show_the_key():
    for status, kind, words in [(403, GeminiUnavailable, "refused the API key"), (404, GeminiUnavailable, "no model called"),
                                (400, GeminiError, "refused the request")]:
        fake = FakeGemini(httpx.Response(status, json={"error": {"message": KEY}}))
        with pytest.raises(kind, match=words) as info:
            fake.client().ask(load_prompt("claims_strict"), StrictClaims, text="t", language="x", published="", source="")
        assert KEY not in str(info.value)
    fake = FakeGemini(*[httpx.Response(429, json={})] * 3)
    with pytest.raises(GeminiUnavailable, match="usage limit"):
        fake.client().ask(load_prompt("claims_strict"), StrictClaims, text="t", language="x", published="", source="")
    assert KEY not in repr(fake.client())
    with pytest.raises(GeminiUnavailable, match="GEMINI_API_KEY"):
        Gemini("")


def test_an_unusable_answer_is_retried_once():
    bad = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})
    fake = FakeGemini(bad, reply({"claims": []}))
    assert fake.client().ask(load_prompt("claims_strict"), StrictClaims, text="t", language="x", published="", source="").value.claims == []
    fake = FakeGemini(bad, bad)
    with pytest.raises(GeminiError, match="couldn't be read, twice"):
        fake.client().ask(load_prompt("claims_strict"), StrictClaims, text="t", language="x", published="", source="")


# --------------------------------------------------------------------------- claims

def test_at_most_five_claims_are_kept():
    many = [{"claim": f"Claim {i}.", "quote": "x", "checkworthiness": "high", "topic": "t"} for i in range(6)]
    answer = extract_claims(FakeGemini(reply({"claims": many})).client(), "x", language="x", published="", source="")
    assert [c.claim for c in answer.value.claims] == [f"Claim {i}." for i in range(5)]


def test_claims_keep_the_worthwhile_ones_and_check_quotes():
    text = "Kitais metais Ukrainai trūks 70 mlrd. eurų. Ačiū visiems!"
    fake = FakeGemini(reply({"claims": [
        {"claim": "Ukraine will have a  shortfall of 70 billion euros in 2027.", "quote": "Ukrainai trūks 70 mlrd. eurų",
         "checkworthiness": "high", "topic": "Ukraine funding"},
        {"claim": "Ukraine will have a shortfall of 70 billion euros in 2027.", "quote": "x", "checkworthiness": "high",
         "topic": "dup"},
        {"claim": "The author thanks everyone.", "quote": "Ačiū visiems!", "checkworthiness": "low", "topic": "thanks"},
        {"claim": "Something medium.", "quote": "not in the post", "checkworthiness": "medium", "topic": "t"},
    ]}))
    answer = extract_claims(fake.client(), text, language="Lithuanian", published="2026-11-13", source="@chan · Telegram")
    assert answer.value.claims == [
        ExtractedClaim(claim="Ukraine will have a shortfall of 70 billion euros in 2027.", quote="Ukrainai trūks 70 mlrd. eurų"),
        ExtractedClaim(claim="Something medium.", quote=""),                       # made-up quote cleared
    ]
    assert answer.prompt == "claims_strict.v1" and answer.model == "gemini-test"
    sent = fake.requests[0]["contents"][0]["parts"][0]["text"]
    assert "Published: 2026-11-13" in sent and "Where: @chan · Telegram" in sent   # dates can be resolved


# --------------------------------------------------------------------------- fact checks, with a database

@pytest.fixture
def db(tmp_path):
    with scratch_database() as settings:
        settings = dataclasses.replace(settings, frontend_dist=tmp_path)
        with connection(settings) as conn:
            migrate(conn)
            yield settings, conn


def add_claim(conn, text="Ukraine will have a shortfall of 70 billion euros in 2027.") -> int:
    import uuid
    with conn.cursor() as cur:
        cur.execute("INSERT INTO claims (uuid, text, language, first_seen) VALUES (%s, %s, 'en', '2026-11-13')",
                    (uuid.uuid4().bytes, text))
        claim_id = cur.lastrowid
    conn.commit()
    return claim_id


def claim_row(conn, claim_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM claims WHERE claim_id = %s", (claim_id,))
        claim = cur.fetchone()
        cur.execute("SELECT url, publisher, stance, excerpt, method FROM claim_evidence WHERE claim_id = %s ORDER BY evidence_id",
                    (claim_id,))
        return claim, list(cur.fetchall())


@pytest.mark.integration
class TestFactChecks:
    def test_a_verdict_with_its_sources(self, db):
        settings, conn = db
        claim_id = add_claim(conn)
        fake = FakeGemini(reply({"verdict": "misleading", "confidence": 0.8, "explanation": "The EU said 2026-27."},
                                sources=[("lrt.lt", "https://lrt.lt/a")]))
        assert check_claim(conn, fake.client(), None, claim_id, today="2026-11-15") == "misleading"
        claim, evidence = claim_row(conn, claim_id)
        assert (claim["verdict"], float(claim["verdict_confidence"]), claim["verdict_explanation"]) == \
            ("misleading", 0.8, "The EU said 2026-27.")
        assert claim["verdict_method"] == "gemini-test + Google Search" and claim["verdict_at"]
        assert evidence == [{"url": "https://lrt.lt/a", "publisher": "lrt.lt", "stance": "context", "excerpt": "It says so.",
                             "method": "gemini+google_search"}]
        sent = fake.requests[0]["contents"][0]["parts"][0]["text"]
        assert "First seen: 2026-11-13" in sent and "Today: 2026-11-15" in sent

    def test_no_sources_or_low_confidence_means_unverified(self, db):
        settings, conn = db
        a, b = add_claim(conn, "A"), add_claim(conn, "B")
        fake = FakeGemini(reply({"verdict": "false", "confidence": 0.9, "explanation": "I think not."}),
                          reply({"verdict": "true", "confidence": 0.3, "explanation": "Maybe."}, sources=[("x.com", "https://x.com")]))
        assert check_claim(conn, fake.client(), None, a) == "unverified"
        assert check_claim(conn, fake.client(), None, b) == "unverified"
        assert claim_row(conn, a)[0]["verdict_explanation"].startswith(NO_SOURCES)
        assert claim_row(conn, b)[0]["verdict_explanation"].startswith(UNSURE)

    def test_each_claim_is_checked_once(self, db):
        settings, conn = db
        a, b = add_claim(conn, "A"), add_claim(conn, "B")
        fake = FakeGemini(*[reply({"verdict": "false", "confidence": 0.9, "explanation": "No."},
                                  sources=[("lrt.lt", "https://lrt.lt/a")])] * 2)
        assert check_claims(conn, fake.client(), None, [a, b, a]) == {"false": 2}
        assert check_claims(conn, fake.client(), None, [a, b]) == {} and len(fake.requests) == 2
        assert [e["stance"] for e in claim_row(conn, a)[1]] == ["refutes"]

    def test_never_in_local_mode_or_without_a_key(self, db):
        settings, conn = db
        assert gemini_for(conn, dataclasses.replace(settings, gemini_api_key="")) is None
        keyed = dataclasses.replace(settings, gemini_api_key=KEY, gemini_model="gemini-test")
        g = gemini_for(conn, keyed)
        assert isinstance(g, Gemini) and g.model == "gemini-test"
        app_settings.save(conn, AppSettings(data_mode="local", share_analyses=False))
        assert gemini_for(conn, keyed) is None                               # "No requests leave this computer."


def test_resolve_keeps_ordinary_links():
    assert FakeGemini().client().resolve("https://lrt.lt/a") == "https://lrt.lt/a"


def test_dates_in_the_claims_prompt():
    assert {"language", "published", "source", "text"} == load_prompt("claims_strict").placeholders()
    assert {"claim", "first_seen", "today"} == load_prompt("factcheck").placeholders()
    assert isinstance(date.today(), date) and isinstance(datetime.now(), datetime)
