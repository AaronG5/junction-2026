"""B5: the local AI client and the analyzers.

Unit tests talk to a fake AI server (httpx.MockTransport). Live tests (marker
`llm`) use the llama-swap on this computer and are skipped when it isn't running.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import httpx
import pytest

from veritas.ai import (
    DOCUMENT_PROMPT, EMBEDDING_DIMENSIONS, PROMPTS_DIR, QUERY_PROMPT, AiError, AiUnavailable, LocalAI, Prompt,
    grammar_schema, load_prompt,
)
from veritas.analyzers import (
    ClaimsAnswer, ReadingAnswer, confirm_category, extract_claims, find_wording, read_post, translate,
)
from veritas.config import ROOT, Settings

POST = "„NATO tankai vėl provokuoja prie Baltarusijos sienos. Kodėl apie tai tyli mūsų žiniasklaida?“"


class FakeAI:
    """Answers chat requests from a queue of contents; records every request."""

    def __init__(self, *contents, models=None):
        self.contents = list(contents)
        self.requests: list[dict] = []
        self.models = models if models is not None else [{"id": "chat", "meta": {"llamaswap": {"modelID": "gemma-test"}}}]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": self.models})
        body = json.loads(request.content)
        self.requests.append({"path": path, **body})
        if path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [
                {"index": i, "embedding": [float(i + 1)] * EMBEDDING_DIMENSIONS} for i in reversed(range(len(body["input"])))]})
        content = self.contents.pop(0)
        if isinstance(content, httpx.Response):
            return content
        if not isinstance(content, str):
            content = json.dumps(content)
        return httpx.Response(200, json={"model": "chat", "choices": [{"message": {"role": "assistant", "content": content}}]})

    def client(self) -> LocalAI:
        return LocalAI("http://ai.test/v1", transport=httpx.MockTransport(self))


# --------------------------------------------------------------------------- prompts

PROMPT_FILES = sorted(PROMPTS_DIR.glob("*.md"))


@pytest.mark.parametrize("path", PROMPT_FILES, ids=lambda p: p.name)
def test_prompt_files_load_and_use_only_known_placeholders(path: Path):
    name, version = re.match(r"^([a-z_]+)\.(v[0-9]+)\.md$", path.name).groups()
    prompt = load_prompt(name, version)
    assert prompt.label == f"{name}.{version}" and prompt.system and prompt.user
    allowed = {"text", "language", "category", "description", "target",
               "published", "source",                       # claims_strict: dates resolved from the post's date
               "claim", "first_seen", "today", "sources"}   # fact checks (factcheck, verdict)
    assert prompt.placeholders() <= allowed
    assert prompt.placeholders() & {"text", "claim"}         # every prompt reads a post or a claim


def test_the_four_analyzer_prompts_exist():
    assert {load_prompt(n).name for n in ("claims", "reading", "category", "translate")} == {"claims", "reading", "category", "translate"}


def test_prompt_fill_keeps_braces_in_the_post():
    prompt = Prompt("t", "v1", "System about {language}.", "Post: {text}")
    system, user = prompt.fill(text="Look: {language} and {text} stay}", language="Lithuanian")
    assert system == "System about Lithuanian."
    assert user == "Post: Look: {language} and {text} stay}"


def test_prompt_fill_needs_every_value():
    with pytest.raises(ValueError, match="needs a value for: language"):
        Prompt("t", "v1", "{language}", "{text}").fill(text="x")


def test_newest_prompt_version_is_used(tmp_path):
    for v in (1, 2, 10):
        (tmp_path / f"demo.v{v}.md").write_text(f"# demo\n\n## system\nsys {v}\n\n## user\n{{text}}\n", encoding="utf-8")
    (tmp_path / "demo.vx.md").write_text("ignored", encoding="utf-8")
    assert load_prompt("demo", folder=tmp_path).version == "v10"
    assert load_prompt("demo", "v2", folder=tmp_path).system == "sys 2"
    with pytest.raises(ValueError, match="no version v3"):
        load_prompt("demo", "v3", folder=tmp_path)
    with pytest.raises(ValueError, match="no prompt called"):
        load_prompt("nothing", folder=tmp_path)


def test_prompt_without_a_section_is_refused(tmp_path):
    (tmp_path / "bad.v1.md").write_text("## system\nonly system\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no '## user' section"):
        load_prompt("bad", folder=tmp_path)


# --------------------------------------------------------------------------- chat requests and answers

def test_request_asks_for_json_without_thinking():
    fake = FakeAI({"claims": []})
    answer = extract_claims(fake.client(), POST, "Lithuanian")
    req = fake.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["model"] == "chat" and req["temperature"] == 0
    assert req["chat_template_kwargs"] == {"enable_thinking": False}
    assert req["response_format"]["type"] == "json_schema"
    sent = req["response_format"]["json_schema"]["schema"]
    assert sent == grammar_schema(ClaimsAnswer.model_json_schema())
    assert "maxLength" not in json.dumps(sent) and sent["properties"]["claims"]["maxItems"] == 5
    assert POST in req["messages"][1]["content"] and "Lithuanian" in req["messages"][1]["content"]
    assert answer.prompt == "claims.v1" and answer.model == "gemma-test"  # the real model behind "chat"


def test_model_name_falls_back_to_the_alias():
    fake = FakeAI({"claims": []}, models=[])
    assert extract_claims(fake.client(), POST).model == "chat"


def test_invalid_json_is_retried_once():
    fake = FakeAI("not json {", {"claims": [{"claim": "NATO tanks provoke at the border.", "quote": "tankai vėl provokuoja"}]})
    answer = extract_claims(fake.client(), POST)
    assert len(fake.requests) == 2
    assert answer.value.claims[0].quote == "tankai vėl provokuoja"


@pytest.mark.parametrize("bad", [
    "not json", "", "[]", {"claims": "x"}, {"claim": []}, {"claims": [{"claim": "x"}]},
    {"claims": [{"claim": "x", "quote": "y", "extra": 1}]}, {"claims": [{"claim": "x", "quote": "y"}] * 6},
])
def test_two_unusable_answers_are_a_clear_error(bad):
    fake = FakeAI(bad, bad)
    with pytest.raises(AiError, match="claims.v1 couldn't be read, twice in a row"):
        extract_claims(fake.client(), POST)
    assert len(fake.requests) == 2


def test_missing_choices_count_as_unusable():
    fake = FakeAI(httpx.Response(200, json={"choices": []}), {"claims": []})
    assert extract_claims(fake.client(), POST).value.claims == []


@pytest.mark.parametrize("response, error, message", [
    (httpx.Response(503, text="loading"), AiUnavailable, "isn't ready"),
    (httpx.Response(502, text="bad gateway"), AiUnavailable, "isn't ready"),
    (httpx.Response(400, json={"error": "bad"}), AiError, "refused the request"),
    (httpx.Response(200, text="<html>"), AiError, "isn't JSON"),
    (httpx.Response(200, json=[1, 2]), AiError, "unexpected shape"),
])
def test_server_problems_are_plain_errors(response, error, message):
    with pytest.raises(error, match=message):
        extract_claims(FakeAI(response).client(), POST)


def test_not_running_and_too_slow_are_plain_errors():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AiUnavailable, match="isn't running. Start it with the launcher."):
        extract_claims(LocalAI("http://ai.test/v1", transport=httpx.MockTransport(refuse)), POST)
    with pytest.raises(AiUnavailable, match="took too long"):
        extract_claims(LocalAI("http://ai.test/v1", transport=httpx.MockTransport(slow)), POST)


# --------------------------------------------------------------------------- analyzers

def test_flagged_wording_must_be_in_the_post():
    fake = FakeAI({"tone": "Alarmist", "intent": "Distrust NATO", "manipulation": 7, "techniques": [
        {"name": "Loaded language", "wording": "PROVOKUOJA", "detail": "Frames an exercise as aggression."},
        {"name": "Media distrust", "wording": "Kodėl apie tai\n tyli", "detail": "Suggests a cover-up."},
        {"name": "No source", "wording": "according to anonymous sources", "detail": "Invented wording."},
        {"name": "Empty", "wording": "   ", "detail": "Nothing quoted."},
    ]})
    reading = read_post(fake.client(), POST).value
    assert [(t.name, t.wording) for t in reading.techniques] == [
        ("Loaded language", "provokuoja"),          # the post's own spelling
        ("Media distrust", "Kodėl apie tai tyli"),  # runs of spaces matched
    ]
    assert reading.tone == "Alarmist" and reading.manipulation == 7


def test_claim_quotes_not_in_the_post_are_cleared():
    fake = FakeAI({"claims": [
        {"claim": "NATO tanks provoke at the Belarus border.", "quote": "NATO tankai vėl provokuoja"},
        {"claim": "Media are silent.", "quote": "the media is silent"},
        {"claim": "   ", "quote": ""},
    ]})
    claims = extract_claims(fake.client(), POST).value.claims
    assert [(c.claim, c.quote) for c in claims] == [
        ("NATO tanks provoke at the Belarus border.", "NATO tankai vėl provokuoja"),
        ("Media are silent.", ""),
    ]


@pytest.mark.parametrize("wording, found", [
    ("provokuoja", "provokuoja"), ("NATO TANKAI", "NATO tankai"), ("tankai   vėl", "tankai vėl"),
    ("tankai vel", None), ("(", None), ("", None), (".*", None), ("sienos. Kodėl", "sienos. Kodėl"),
])
def test_find_wording(wording, found):
    assert find_wording(POST, wording) == found


def test_category_and_translation():
    fake = FakeAI({"matches": True, "confidence": 0.8, "reason": "About NATO."},
                  {"source_language": "Lithuanian", "translation": "NATO tanks are provoking again."})
    ai = fake.client()
    cat = confirm_category(ai, POST, "Anti-NATO", "Posts portraying NATO as an aggressor")
    assert cat.value.matches and cat.prompt == "category.v1"
    assert "Anti-NATO" in fake.requests[0]["messages"][1]["content"]
    tr = translate(ai, POST)
    assert tr.value.source_language == "Lithuanian" and tr.prompt == "translate.v1"
    assert "English" in fake.requests[1]["messages"][1]["content"]


@pytest.mark.parametrize("answer", [{"matches": True, "confidence": 1.5, "reason": "x"},
                                    {"matches": "yes", "confidence": 0.5, "reason": "x"},
                                    {"matches": 1, "confidence": 0.5, "reason": "x"},
                                    {"matches": True, "confidence": "0.5", "reason": "x"}])
def test_out_of_range_category_answers_are_unusable(answer):
    with pytest.raises(AiError):
        confirm_category(FakeAI(answer, answer).client(), POST, "c", "d")


@pytest.mark.parametrize("score", [11, -1, "7", 7.5])
def test_reading_score_must_be_a_whole_number_0_to_10(score):
    bad = {"tone": "x", "intent": "y", "techniques": [], "manipulation": score}
    with pytest.raises(AiError):
        read_post(FakeAI(bad, bad).client(), POST)


def test_long_answers_are_still_limited_after_reading():
    long = {"source_language": "Lithuanian", "translation": "x" * 20_001}
    with pytest.raises(AiError):
        translate(FakeAI(long, long).client(), POST)


# --------------------------------------------------------------------------- embeddings

def test_embedding_prompts_match_the_database_seed():
    init = (ROOT / "docker" / "mysql" / "init.sql").read_text(encoding="utf-8")
    assert f"'{DOCUMENT_PROMPT}', '{QUERY_PROMPT}'" in init


def test_documents_and_queries_use_their_own_prompts_and_keep_order():
    fake = FakeAI()
    ai = fake.client()
    vectors = ai.embed_documents(["first {text}", "second"], titles=["A title", None])
    assert fake.requests[0]["input"] == ["title: A title | text: first {text}", "title: none | text: second"]
    assert fake.requests[0]["model"] == "embedding"
    assert [v[0] for v in vectors] == [1.0, 2.0]  # answered out of order, returned in order
    ai.embed_query("NATO tanks")
    assert fake.requests[1]["input"] == ["task: search result | query: NATO tanks"]
    assert ai.embed_documents([]) == []
    with pytest.raises(ValueError):
        ai.embed_documents(["a"], titles=[])


@pytest.mark.parametrize("data, message", [
    ({"data": [{"index": 0, "embedding": [0.1] * 384}]}, "384 numbers instead of 768"),
    ({"data": []}, "0 embeddings for 1 texts"),
    ({"data": [{"index": 0, "embedding": [float("nan")] * 768}]}, "invalid vector"),
    ({"nothing": 1}, "unexpected shape"),
])
def test_bad_embeddings_are_refused(data, message):
    ai = LocalAI("http://ai.test/v1", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=json.dumps(data, allow_nan=True))))
    with pytest.raises(AiError, match=message):
        ai.embed_query("x")


# --------------------------------------------------------------------------- live (llama-swap on this computer)

@pytest.fixture(scope="module")
def live() -> LocalAI:
    ai = LocalAI.from_settings(Settings.load(), timeout=300)
    try:
        ai.embed_query("ping")
    except (AiUnavailable, AiError) as exc:
        pytest.skip(f"local AI not available: {exc}")
    return ai


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


@pytest.mark.llm
def test_live_embedding_has_768_numbers(live):
    assert len(live.embed_query("Ar NATO provokuoja prie sienos?")) == 768


@pytest.mark.llm
def test_live_translation_is_closer_than_an_unrelated_sentence(live):
    lt, en, other = live.embed_documents([
        "NATO tankai vėl provokuoja prie Baltarusijos sienos.",
        "NATO tanks are provoking at the Belarusian border again.",
        "Šiandien kepiau obuolių pyragą su cinamonu.",
    ])
    assert _cos(lt, en) > _cos(lt, other) + 0.1


@pytest.mark.llm
def test_live_analyzers_answer_with_valid_json(live):
    text = POST
    articles = ROOT / "articles.json"  # sample data, not in git; used when present
    if articles.is_file():
        data = json.loads(articles.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("articles", [])
        full = [a for a in items if isinstance(a, dict) and len(str(a.get("text") or a.get("content") or "")) > 400]
        if full:
            text = str(full[0].get("text") or full[0].get("content"))[:3000]
    claims = extract_claims(live, text, "Lithuanian")
    assert claims.prompt == "claims.v1" and claims.model != "chat"
    assert isinstance(claims.value, ClaimsAnswer)
    reading = read_post(live, POST, "Lithuanian")
    assert isinstance(reading.value, ReadingAnswer) and 0 <= reading.value.manipulation <= 10
    for t in reading.value.techniques:
        assert t.wording in POST
    tr = translate(live, POST)
    assert "NATO" in tr.value.translation


def test_an_answer_cut_off_while_thinking_is_retried_with_a_bigger_budget():
    cut = httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "Thinking..."}}]})
    fake = FakeAI(cut, {"claims": []})
    assert extract_claims(fake.client(), POST).value.claims == []
    assert [r["max_tokens"] for r in fake.requests] == [1024, 4096]


def test_the_bigger_budget_has_a_ceiling():
    cut = httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": ""}}]})
    fake = FakeAI(cut, cut)
    with pytest.raises(AiError):
        translate(fake.client(), POST)  # starts at 4096
    assert [r["max_tokens"] for r in fake.requests] == [4096, 8192]


def test_the_retry_is_a_different_request_and_the_reason_is_logged(caplog):
    """At temperature 0 an identical retry gives the identical unusable answer: the retry samples."""
    fake = FakeAI({"claims": "not a list"}, {"claims": []})
    with caplog.at_level("WARNING", logger="veritas.ai"):
        extract_claims(fake.client(), POST)
    assert [r["temperature"] for r in fake.requests] == [0, 0.4]
    assert "Unusable answer to claims.v1 (attempt 1" in caplog.text and "claims: Input should be a valid list" in caplog.text
