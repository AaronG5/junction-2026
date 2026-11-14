"""B6: Analyze a link.

- Link parsing, and the scraper adapters against saved responses (no live calls).
- The whole pipeline with a fake AI, a throwaway database and throwaway Qdrant
  collections: every step runs, results are stored, the report fits the contract.
- Each failure gives its own answer.
Live scraper tests (marker `scrape`, paid) run only with -m scrape and keys in .env.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from conftest import make_client, scratch_database
from veritas import analysis
from veritas.ai import LocalAI
from veritas.analysis import JobRunner, Services, run_job
from veritas.collectors import (
    NOT_SET_UP, CollectedPost, CollectError, ContextDev, NoCollector, Oxylabs, collector_for, parse_article,
    parse_facebook_page, parse_telegram_widget, telegram_channel_id,
)
from veritas.config import Settings
from veritas.db import connection
from veritas.db.migrations import migrate
from veritas.ids import post_uuid
from veritas.links import parse_link
from veritas.models import AnalysisJob, PostReport
from veritas.vectors import Point, Qdrant, VectorStoreUnavailable

SAMPLES = Path(__file__).parent / "samples"
DIM = 768


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- link parsing

@pytest.mark.parametrize("url, platform, kind, account, external_id", [
    ("t.me/kanalas_b/4521", "telegram", "post", "kanalas_b", "kanalas_b/4521"),
    ("https://t.me/Kanalas_B/4521?single", "telegram", "post", "kanalas_b", "kanalas_b/4521"),
    ("https://t.me/s/kanalas_b/4521", "telegram", "post", "kanalas_b", "kanalas_b/4521"),
    ("http://telegram.me/kanalas_b/4521/", "telegram", "post", "kanalas_b", "kanalas_b/4521"),
    ("https://t.me/forum_group/12/345", "telegram", "post", "forum_group", "forum_group/345"),
    ("https://t.me/c/1234567890/4521", "telegram", "post", "-1001234567890", "-1001234567890/4521"),
    ("https://t.me/kanalas_b", "telegram", "account", "kanalas_b", None),
    ("https://t.me/+AbCdEf123", "telegram", "other", None, None),
    ("https://t.me/joinchat/AbCdEf", "telegram", "other", None, None),
    ("https://t.me/kanalas_b/abc", "telegram", "other", None, None),
    ("https://www.facebook.com/tiesabecenzuros/posts/1188", "facebook", "post", "tiesabecenzuros", "tiesabecenzuros_1188"),
    ("https://m.facebook.com/TiesaBeCenzuros/posts/1188?__tn__=R", "facebook", "post", "tiesabecenzuros", "tiesabecenzuros_1188"),
    ("fb.com/tiesabecenzuros/videos/77", "facebook", "post", "tiesabecenzuros", "tiesabecenzuros_77"),
    ("https://www.facebook.com/permalink.php?story_fbid=123&id=456", "facebook", "post", "456", "456_123"),
    ("https://www.facebook.com/story.php?id=456&story_fbid=123&ref=x", "facebook", "post", "456", "456_123"),
    ("https://www.facebook.com/groups/baltijosgrupe/posts/999", "facebook", "post", "baltijosgrupe", "baltijosgrupe_999"),
    ("https://www.facebook.com/groups/baltijosgrupe/permalink/999/", "facebook", "post", "baltijosgrupe", "baltijosgrupe_999"),
    ("https://www.facebook.com/groups/baltijosgrupe", "facebook", "account", "baltijosgrupe", None),
    ("https://www.facebook.com/tiesabecenzuros", "facebook", "account", "tiesabecenzuros", None),
    ("https://www.facebook.com/profile.php?id=100012345", "facebook", "account", "100012345", None),
    ("https://www.facebook.com/share/p/1AbCdEf/", "facebook", "other", None, None),
    ("https://www.facebook.com/photo/?fbid=1", "facebook", "other", None, None),
    ("https://www.facebook.com/permalink.php?story_fbid=123", "facebook", "other", None, None),
])
def test_parse_link(url, platform, kind, account, external_id):
    link = parse_link(url)
    assert (link.platform, link.kind, link.account, link.external_id) == (platform, kind, account, external_id)
    assert link.url.startswith("https://")


@pytest.mark.parametrize("url", ["hello", "https://example.com/a", "", "https://x.com/someone/status/1"])
def test_unsupported_links_parse_to_none(url):
    assert parse_link(url) is None


def test_the_same_post_written_differently_gets_one_id():
    ids = {parse_link(u).external_id for u in [
        "https://t.me/kanalas_b/4521", "t.me/kanalas_b/4521/", "HTTPS://T.ME/KANALAS_B/4521?single",
        "https://telegram.me/s/kanalas_b/4521#c", " t.me/kanalas_b/4521 "]}
    assert ids == {"kanalas_b/4521"}


# --------------------------------------------------------------------------- scraper adapters (saved responses)

def test_telegram_widget_real_sample():
    post = parse_telegram_widget(sample("telegram_widget_306.html"), parse_link("https://t.me/telegram/306"))
    assert post.text.startswith("Quick Actions for Phone Numbers. Tapping a phone number")
    assert "\n\nMay Features\n" in post.text  # <br/> kept as line breaks, nothing cut off
    assert post.account == "telegram" and post.account_name == "Telegram News" and post.account_type == "channel"
    assert post.published_at == datetime(2024, 6, 10, 13, 12, 54, tzinfo=timezone.utc)
    assert post.views == 2_920_000 and post.external_id == "telegram/306"
    assert telegram_channel_id(sample("telegram_widget_306.html")) == -1005640892


def test_telegram_widget_missing_post():
    with pytest.raises(CollectError, match="doesn't exist or isn't public") as info:
        parse_telegram_widget(sample("telegram_widget_missing.html"), parse_link("t.me/telegram/999999999"))
    assert info.value.status == 404


def test_telegram_widget_for_another_post_is_refused():
    with pytest.raises(CollectError, match="different post"):
        parse_telegram_widget(sample("telegram_widget_306.html"), parse_link("t.me/telegram/305"))


def test_facebook_page_sample():
    post = parse_facebook_page(sample("facebook_post_constructed.html"), parse_link("https://www.facebook.com/tiesabecenzuros/posts/1188"))
    assert post.text == "Pabėgėliai gauna didesnes išmokas nei mūsų pensininkai. Kas čia vyksta? \U0001F621"
    assert post.account_name == "Tiesa be cenzūros" and post.external_id == "tiesabecenzuros_1188"
    assert post.published_at == datetime(2026, 11, 12, 14, 30, tzinfo=timezone.utc)


def test_facebook_login_wall():
    with pytest.raises(CollectError, match="only shows this post to logged-in users") as info:
        parse_facebook_page(sample("facebook_login_constructed.html"), parse_link("https://www.facebook.com/a/posts/1"))
    assert info.value.status == 404


def test_article_sample():
    post = parse_article(sample("article_constructed.html"), "https://www.respublika.lt/lt/naujienos/1")
    assert post.title == "Energetikai: elektros kainos kitą ketvirtį keisis nežymiai"
    assert post.byline == "Jonas Jonaitis" and post.account == "respublika.lt" and post.account_name == "Respublika"
    assert "keisis vos keliais procentais,\no sankcijos" in post.text
    assert "Pagrindinis" not in post.text and "© 2026" not in post.text  # only the article
    assert post.text.endswith("Daugiau informacijos – tarybos svetainėje.")


def _oxylabs(handler) -> Oxylabs:
    return Oxylabs("user", "secret-pw", transport=httpx.MockTransport(handler))


def test_oxylabs_request_and_answer():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [{"content": sample("telegram_widget_306.html"), "status_code": 200}]})

    post = _oxylabs(handler).collect(parse_link("https://t.me/telegram/306"))
    assert post.views == 2_920_000
    assert seen["body"] == {"source": "universal", "url": "https://t.me/telegram/306?embed=1&mode=tme"}
    assert seen["auth"].startswith("Basic ")


@pytest.mark.parametrize("response, status, message", [
    (httpx.Response(401, json={}), 503, "refused the login"),
    (httpx.Response(429, json={}), 429, "too many requests"),
    (httpx.Response(500, text="oops"), 502, "error 500"),
    (httpx.Response(200, text="not json"), 502, "unexpected shape"),
    (httpx.Response(200, json={"results": []}), 502, "unexpected shape"),
    (httpx.Response(200, json={"results": [{"content": "  ", "status_code": 200}]}), 502, "empty page"),
    (httpx.Response(200, json={"results": [{"content": "<html/>", "status_code": 404}]}), 404, "doesn't exist any more"),
])
def test_oxylabs_failures_each_have_their_own_message(response, status, message):
    with pytest.raises(CollectError, match=message) as info:
        _oxylabs(lambda r: response).collect(parse_link("https://t.me/telegram/306"))
    assert info.value.status == status
    assert "secret-pw" not in str(info.value)


def test_oxylabs_network_problems():
    def refuse(request):
        raise httpx.ConnectError("x", request=request)

    def slow(request):
        raise httpx.ReadTimeout("x", request=request)

    with pytest.raises(CollectError, match="can't be reached"):
        _oxylabs(refuse).collect(parse_link("https://t.me/telegram/306"))
    with pytest.raises(CollectError, match="took too long"):
        _oxylabs(slow).collect(parse_link("https://t.me/telegram/306"))


def test_private_telegram_channels_are_not_fetched():
    called = []
    with pytest.raises(CollectError, match="private Telegram channels") as info:
        _oxylabs(lambda r: called.append(r) or httpx.Response(200)).collect(parse_link("https://t.me/c/123456/7"))
    assert info.value.status == 404 and called == []


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"html": sample("article_constructed.html")}),
    httpx.Response(200, text=sample("article_constructed.html"), headers={"content-type": "text/html"}),
])
def test_context_dev_json_or_html(response):
    seen = {}

    def handler(request):
        seen["auth"], seen["url"] = request.headers["authorization"], str(request.url)
        return response

    post = ContextDev("key-123", transport=httpx.MockTransport(handler)).collect_article("https://www.respublika.lt/a")
    assert post.byline == "Jonas Jonaitis"
    assert seen["auth"] == "Bearer key-123" and "url=https%3A%2F%2Fwww.respublika.lt%2Fa" in seen["url"]


def test_no_keys_means_no_collector():
    assert isinstance(collector_for(Settings()), NoCollector)
    assert isinstance(collector_for(Settings(oxylabs_username="u", oxylabs_password="p")), Oxylabs)
    assert "secret" not in repr(Settings(oxylabs_password="secret", context_dev_api_key="secret"))


@pytest.mark.scrape
def test_live_oxylabs_telegram_post():
    settings = Settings.load()
    if not settings.oxylabs_username:
        pytest.skip("OXYLABS_USERNAME / OXYLABS_PASSWORD not in .env")
    post = collector_for(settings).collect(parse_link("https://t.me/telegram/306"))
    assert post.text.startswith("Quick Actions")


@pytest.mark.scrape
def test_live_context_dev_article():
    settings = Settings.load()
    if not settings.context_dev_api_key:
        pytest.skip("CONTEXT_DEV_API_KEY not in .env")
    post = ContextDev(settings.context_dev_api_key).collect_article("https://www.lrt.lt/")
    assert post.text


# --------------------------------------------------------------------------- a fake AI

def vec(**parts: float) -> list[float]:
    v = [0.0] * DIM
    for key, value in parts.items():
        v[int(key[1:])] = value
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def hashed(text: str) -> list[float]:
    """A stable, essentially random direction for any other text (dims 100+)."""
    v = [0.0] * DIM
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    for i in range(16):
        v[100 + (digest[i] * 3 + i) % 600] = 1.0 + digest[16 + i] / 255
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


POST_TEXT = "„NATO tankai vėl provokuoja prie Baltarusijos sienos. Kodėl apie tai tyli mūsų žiniasklaida?“"


class FakeAI:
    """llama-swap stand-in: embeddings by keyword, chat answers by prompt name."""

    def __init__(self):
        self.vectors: dict[str, list[float]] = {}   # text fragment -> vector
        self.answers: dict[str, object] = {
            "claims": {"claims": [{"claim": "NATO tanks are provoking at the Belarus border.", "quote": "NATO tankai vėl provokuoja"},
                                  {"claim": "Lithuanian media are silent about it.", "quote": "tyli mūsų žiniasklaida"}]},
            "reading": {"tone": "Alarmist", "intent": "Make readers distrust NATO.", "manipulation": 7, "techniques": [
                {"name": "Loaded language", "wording": "provokuoja", "detail": "Frames an exercise as aggression."},
                {"name": "Media distrust", "wording": "Kodėl apie tai tyli mūsų žiniasklaida?", "detail": "Suggests a cover-up."},
                {"name": "Invented", "wording": "not in the post", "detail": "x"}]},
            "translate": {"source_language": "Lithuanian", "translation": "NATO tanks are provoking at the Belarus border again."},
            "category": {"matches": True, "confidence": 0.8, "reason": "About NATO."},
        }
        self.calls: list[str] = []
        self.fail: Exception | None = None
        self.gate: threading.Event | None = None
        self.up = True

    def vector(self, text: str) -> list[float]:
        for fragment, v in self.vectors.items():
            if fragment in text:
                return v
        return hashed(text)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "chat", "meta": {"llamaswap": {"modelID": "gemma-test"}}}]})
        if self.gate is not None:
            self.gate.wait(30)
        if self.fail is not None:
            raise self.fail
        body = json.loads(request.content)
        if path.endswith("/embeddings"):
            self.calls.append("embed")
            return httpx.Response(200, json={"data": [{"index": i, "embedding": self.vector(t)} for i, t in enumerate(body["input"])]})
        name = body["response_format"]["json_schema"]["name"]
        self.calls.append(name)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(self.answers[name], ensure_ascii=False)}}]})

    def client(self) -> LocalAI:
        return LocalAI("http://ai.test/v1", transport=httpx.MockTransport(self))


class FakeCollector:
    def __init__(self, post: CollectedPost | None = None, error: CollectError | None = None):
        self.post, self.error, self.calls = post, error, 0

    def available(self) -> bool:
        return True

    def collect(self, link):
        self.calls += 1
        if self.error:
            raise self.error
        return self.post


class InlineRunner(JobRunner):
    """Runs the job right away, in the request's thread (deterministic tests)."""

    def submit(self, job_key, link, url):
        run_job(self.services, job_key, link, url)


# --------------------------------------------------------------------------- the world: database + Qdrant + fakes

@dataclasses.dataclass
class World:
    settings: Settings
    qdrant: Qdrant
    ai: FakeAI
    services: Services
    client: object

    def conn(self):
        return connection(self.settings)

    def rows(self, sql, args=()):
        with self.conn() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())


NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


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
            ai = FakeAI()
            services = Services(settings=settings, ai=ai.client, qdrant=lambda: Qdrant(q.base_url, prefix=q.prefix),
                                collector=NoCollector(), now=lambda: NOW)
            from veritas.main import create_app
            app = create_app(settings)
            app.state.analysis = InlineRunner(services)
            yield World(settings, q, ai, services, make_client(settings, app))
        finally:
            q.drop_all()
            q.close()


def add_account(w: World, platform_id: int, external: str, name: str, handle: str | None = None, kind: str = "channel") -> int:
    with w.conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO accounts (uuid, platform_id, external_id, handle, display_name, account_type) VALUES (%s,%s,%s,%s,%s,%s)",
                    (uuid.uuid4().bytes, platform_id, external, handle, name, kind))
        return cur.lastrowid


def add_post(w: World, account_id: int, external: str, text: str, language: str, published: str, platform: str = "telegram",
             vector: list[float] | None = None) -> int:
    platform_id = {"telegram": 2, "facebook": 4, "web": 1}[platform]
    with w.conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO posts (uuid, platform_id, account_id, container_account_id, external_id, text, language, published_at)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (post_uuid(platform, external).bytes, platform_id, account_id, account_id, external, text, language, published))
            post_id = cur.lastrowid
        if vector is not None:
            from veritas.content import store_post_vector
            store_post_vector(conn, Qdrant(w.qdrant.base_url, prefix=w.qdrant.prefix), post_id, vector)
    return post_id


def analyze(w: World, url: str):
    return w.client.post("/api/analyze", json={"url": url})


def the_post(w: World) -> int:
    kb = add_account(w, 2, "kanalas_b", "Kanalas B", handle="kanalas_b")
    w.ai.vectors["NATO tankai vėl provokuoja"] = vec(e0=1)
    return add_post(w, kb, "kanalas_b/4521", POST_TEXT, "lt", "2026-11-03 11:00:00")


# --------------------------------------------------------------------------- the whole pipeline

@pytest.mark.integration
class TestPipeline:
    def test_every_step_runs_results_are_stored_and_the_report_fits(self, world):
        w = world
        post_id = the_post(w)
        rt = add_account(w, 1, "rt.com", "rt.com", kind="website")
        ka = add_account(w, 2, "kanal_a", "Kanal A", handle="kanal_a")
        tb = add_account(w, 4, "tiesabecenzuros", "Tiesa be cenzūros", kind="page")
        earliest = add_post(w, rt, "rt-1", "NATO armour stages provocations near Belarus", "en", "2026-11-03 08:00:00", "web", vec(e0=1, e1=0.3))
        add_post(w, ka, "kanal_a/881", "Танки НАТО снова провоцируют у белорусской границы", "ru", "2026-11-03 09:30:00", "telegram", vec(e0=1, e1=0.2))
        add_post(w, tb, "tiesabecenzuros_1", "NATO tankai vėl provokuoja prie sienos!", "lt", "2026-11-03 12:40:00", "facebook", vec(e0=1, e1=0.05))
        add_post(w, ka, "kanal_a/9", "Šiandien kepiau obuolių pyragą", "lt", "2026-11-03 13:00:00", "telegram", vec(e5=1))

        res = analyze(w, "https://t.me/kanalas_b/4521")
        assert res.status_code == 202, res.text
        job = res.json()
        AnalysisJob.model_validate(job)
        polled = w.client.get(f"/api/analyze/{job['id']}").json()
        assert polled["status"] == "done" and polled["step"] == 5 and polled["error"] is None
        report = polled["report"]
        PostReport.model_validate(report)

        assert report["url"] == "https://t.me/kanalas_b/4521" and report["analyzedAt"] == "2026-11-14T12:00:00.000Z"
        assert report["post"]["author"] == "@kanalas_b" and report["post"]["where"] == "Telegram channel"
        assert report["post"]["originalLanguage"] == "Lithuanian"
        flagged = [s for s in report["post"]["original"] if "flag" in s]
        assert [(s["text"], s["flag"]) for s in flagged] == [
            ("provokuoja", "Loaded language"), ("Kodėl apie tai tyli mūsų žiniasklaida?", "Media distrust")]
        assert "".join(s["text"] for s in report["post"]["original"]) == POST_TEXT
        assert [t["name"] for t in report["reading"]["techniques"]] == ["Loaded language", "Media distrust"]  # invented one dropped
        assert report["reading"]["model"] == "gemma-test · prompt reading.v1 · ran on this computer"
        assert report["manipulation"] == 7
        assert report["verdict"] == {"verdict": "unverified", "summary": "2 not checked yet"}
        assert [c["text"] for c in report["claims"]] == ["NATO tanks are provoking at the Belarus border.", "Lithuanian media are silent about it."]
        assert report["claims"][0]["seenSummary"] == "Seen in 1 post, 1 language"

        # similar posts: nearest first; the unrelated one isn't listed
        assert [s["author"] for s in report["similar"]] == ["Tiesa be cenzūros", "@kanal_a", "rt.com"]
        assert [s["relation"] for s in report["similar"]] == ["Near copy", "Translation", "Earliest source"]
        assert report["spread"] == {"copies": 3, "languages": 3, "hours": 4.7}
        assert report["earliest"] == {"name": "rt.com", "note": "3 h before this post · News site"}
        assert [(n["name"], n["time"], n.get("relation"), n.get("isThisPost")) for n in report["chain"]] == [
            ("rt.com", "08:00", None, None), ("@kanal_a", "09:30", "translation", None),
            ("@kanalas_b", "11:00", "translation", True), ("Tiesa be cenzūros", "12:40", "copy", None)]
        assert report["moreCopies"] == 0
        assert report["cluster"] is None and report["categories"] == []

        # stored
        assert w.rows("SELECT status, post_id, link_key FROM analysis_requests") == [
            {"status": "done", "post_id": post_id, "link_key": "t.me/kanalas_b/4521"}]
        assert sorted(r["analyzer"] for r in w.rows("SELECT analyzer FROM post_analyses WHERE post_id = %s", (post_id,))) == [
            "claims", "reading", "translation"]
        assert {r["analyzer_version"] for r in w.rows("SELECT analyzer_version FROM post_analyses")} == {"claims.v1", "reading.v1", "translate.v1"}
        assert len(w.rows("SELECT * FROM post_claims WHERE post_id = %s", (post_id,))) == 2
        rel = w.rows("SELECT source_post_id, target_post_id, relation_type FROM post_relations ORDER BY relation_type, source_post_id")
        assert {"source_post_id": earliest, "target_post_id": post_id, "relation_type": "translation"} in rel
        assert len(rel) == 3
        assert w.rows("SELECT embedding_model_id FROM posts WHERE post_id = %s", (post_id,)) == [{"embedding_model_id": 1}]
        claim = w.rows("SELECT post_count, platforms, languages, first_seen FROM claims ORDER BY claim_id LIMIT 1")[0]
        assert claim["post_count"] == 1 and json.loads(claim["platforms"]) == ["Telegram"] and json.loads(claim["languages"]) == ["lt"]
        assert str(claim["first_seen"]) == "2026-11-03"
        assert w.ai.calls.count("claims") == 1

    def test_same_post_again_is_answered_at_once_without_the_ai(self, world):
        w = world
        the_post(w)
        first = analyze(w, "https://t.me/kanalas_b/4521").json()
        calls = list(w.ai.calls)
        again = analyze(w, "HTTPS://T.me/Kanalas_B/4521/?single")  # the same post, written differently
        assert again.status_code == 200
        assert again.json()["id"] == first["id"] and again.json()["status"] == "done"
        assert w.ai.calls == calls  # no second AI call
        assert len(w.rows("SELECT * FROM analysis_requests")) == 1

    def test_a_claim_seen_again_is_matched_not_duplicated(self, world):
        w = world
        the_post(w)
        w.ai.vectors["NATO tanks are provoking"] = vec(e10=1)
        w.ai.vectors["media are silent"] = vec(e11=1)
        analyze(w, "https://t.me/kanalas_b/4521")
        other = add_account(w, 2, "kanal_c", "Kanal C", handle="kanal_c")
        add_post(w, other, "kanal_c/5", "Танки НАТО провоцируют у границы", "ru", "2026-11-05 10:00:00")
        w.ai.answers["claims"] = {"claims": [{"claim": "NATO tanks are provoking at the border with Belarus.", "quote": "Танки НАТО"}]}
        w.ai.vectors["NATO tanks are provoking at the border with Belarus"] = vec(e10=1, e12=0.1)
        report = w.client.get(f"/api/analyze/{analyze(w, 'https://t.me/kanal_c/5').json()['id']}").json()["report"]
        assert len(w.rows("SELECT claim_id FROM claims")) == 2
        assert report["claims"][0]["text"] == "NATO tanks are provoking at the Belarus border."  # the known claim
        assert report["claims"][0]["seenSummary"] == "Seen in 2 posts, 2 languages"
        row = w.rows("SELECT post_count, last_seen, platforms FROM claims WHERE text LIKE 'NATO%%'")[0]
        assert row["post_count"] == 2 and str(row["last_seen"]) == "2026-11-05"

    def test_cluster_and_category(self, world):
        w = world
        post_id = the_post(w)
        with w.conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO clustering_runs (embedding_model_id, algorithm, status, finished_at) VALUES (1, 't', 'done', NOW())")
            run = cur.lastrowid
            cur.execute("INSERT INTO clusters (cluster_id, run_id, label, size, change_pct, stats) VALUES (12, %s, 'NATO provocations', 1284, 38,"
                        " '{\"languages\": [{\"code\": \"lt\", \"name\": \"Lithuanian\", \"share\": 60}]}')", (run,))
            cur.execute("INSERT INTO categories (category_id, code, name, description) VALUES (1, 'anti-nato', 'Anti-NATO', 'NATO as aggressor'),"
                        " (2, 'prices', 'Energy prices', 'Prices'), (3, 'media', 'Media distrust', 'Media hide things')")
        w.qdrant.upsert("clusters", [Point(str(uuid.uuid4()), vec(e0=1, e1=0.2), {"cluster_id": 12, "run_id": run})])
        w.qdrant.upsert("categories", [
            Point(str(uuid.uuid4()), vec(e0=1, e1=0.1), {"category_id": 1}),      # sure: no Gemma needed
            Point(str(uuid.uuid4()), vec(e0=0.7, e3=1), {"category_id": 3}),      # close call: Gemma confirms
            Point(str(uuid.uuid4()), vec(e9=1), {"category_id": 2}),              # not similar
        ])
        report = w.client.get(f"/api/analyze/{analyze(w, 'https://t.me/kanalas_b/4521').json()['id']}").json()["report"]
        assert report["cluster"] == {"id": 12, "name": "NATO provocations", "summary": "1,284 posts · up 38% on the week before",
                                     "languages": [{"code": "lt", "name": "Lithuanian", "share": 60}]}
        assert [(c["name"], c["method"]) for c in report["categories"]] == [
            ("Anti-NATO", "Matched by similarity only"), ("Media distrust", "Matched by similarity, confirmed by Gemma")]
        assert w.ai.calls.count("category") == 1
        assert {r["category_id"] for r in w.rows("SELECT category_id FROM post_categories WHERE post_id = %s", (post_id,))} == {1, 3}

    def test_gemma_can_reject_a_close_category(self, world):
        w = world
        the_post(w)
        with w.conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO categories (category_id, code, name) VALUES (3, 'media', 'Media distrust')")
        w.qdrant.upsert("categories", [Point(str(uuid.uuid4()), vec(e0=0.7, e3=1), {"category_id": 3})])
        w.ai.answers["category"] = {"matches": False, "confidence": 0.9, "reason": "Not about media."}
        report = w.client.get(f"/api/analyze/{analyze(w, 'https://t.me/kanalas_b/4521').json()['id']}").json()["report"]
        assert report["categories"] == []

    def test_a_post_not_in_the_database_is_collected(self, world):
        w = world
        w.services.collector = FakeCollector(CollectedPost(
            platform="telegram", external_id="telegram/306", url="https://t.me/telegram/306", text="Quick Actions for Phone Numbers.",
            account="telegram", account_name="Telegram News", account_type="channel",
            published_at=datetime(2024, 6, 10, 13, 12, 54, tzinfo=timezone.utc), views=2_920_000))
        w.ai.answers["translate"] = {"source_language": "English", "translation": "Quick Actions for Phone Numbers."}
        job = analyze(w, "https://t.me/telegram/306").json()
        report = w.client.get(f"/api/analyze/{job['id']}").json()["report"]
        assert report["post"]["author"] == "@telegram" and report["post"]["originalLanguage"] == "English"
        post = w.rows("SELECT external_id, language, published_at FROM posts")[0]
        assert post["external_id"] == "telegram/306" and post["language"] == "en"
        assert w.rows("SELECT views FROM post_metrics") == [{"views": 2_920_000}]
        assert w.rows("SELECT display_name FROM accounts") == [{"display_name": "Telegram News"}]

    def test_report_for_a_post_with_no_copies_and_no_claims(self, world):
        w = world
        the_post(w)
        w.ai.answers["claims"] = {"claims": []}
        w.ai.answers["reading"] = {"tone": "Calm", "intent": "Inform.", "manipulation": 0, "techniques": []}
        report = w.client.get(f"/api/analyze/{analyze(w, 't.me/kanalas_b/4521').json()['id']}").json()["report"]
        assert report["verdict"] == {"verdict": "unverified", "summary": "No checkable claims found"}
        assert report["earliest"] == {"name": "@kanalas_b", "note": "This post is the earliest copy found so far"}
        assert report["chain"] == [{"name": "@kanalas_b", "kind": "This post", "language": "LT", "time": "11:00", "isThisPost": True}]
        assert report["spread"] == {"copies": 0, "languages": 1, "hours": 0}
        assert report["post"]["original"] == [{"text": POST_TEXT}]
        assert report["answeredFrom"] == "this computer"


# --------------------------------------------------------------------------- each failure has its own answer

@pytest.mark.integration
class TestFailures:
    @pytest.mark.parametrize("url, status, message", [
        ("hello", 400, analysis.NOT_A_LINK),
        ("https://t.me/kanalas_b", 400, analysis.ACCOUNT_LINK),
        ("https://www.facebook.com/groups/baltijosgrupe", 400, analysis.ACCOUNT_LINK),
        ("https://www.facebook.com/share/p/1AbCdEf/", 400, analysis.UNKNOWN_POST_LINK),
        ("https://t.me/someone/42", 404, NOT_SET_UP),
    ])
    def test_answered_at_once(self, world, url, status, message):
        res = analyze(world, url)
        assert res.status_code == status and res.json() == {"detail": message}
        assert world.ai.calls == []

    def test_ai_down_is_503_before_any_job(self, world):
        the_post(world)
        world.ai.up = False
        res = analyze(world, "https://t.me/kanalas_b/4521")
        assert res.status_code == 503 and res.json() == {"detail": "The local AI isn't running. Start it with the launcher."}
        assert world.rows("SELECT * FROM analysis_requests") == []

    def test_database_down_is_503(self, tmp_path):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        client = make_client(Settings(mysql_port=port, frontend_dist=tmp_path))
        res = client.post("/api/analyze", json={"url": "https://t.me/kanalas_b/4521"})
        assert res.status_code == 503 and "isn't reachable" in res.json()["detail"]
        assert client.post("/api/analyze", json={"url": "hello"}).status_code == 400  # checked before the database
        assert client.get("/api/analyze/abc").status_code == 503

    @pytest.mark.parametrize("error, status, message", [
        (httpx.ConnectError("refused"), 503, "The local AI isn't running. Start it with the launcher."),
        (httpx.ReadTimeout("slow"), 503, "The local AI took too long to answer."),
    ])
    def test_ai_failing_during_the_job(self, world, error, status, message):
        the_post(world)
        world.ai.fail = error
        job = analyze(world, "https://t.me/kanalas_b/4521").json()
        polled = world.client.get(f"/api/analyze/{job['id']}").json()
        assert polled["status"] == "failed" and polled["errorStatus"] == status and polled["error"].startswith(message)
        assert polled["report"] is None and polled["step"] == 1

    def test_unusable_ai_answers_fail_the_job_plainly(self, world):
        the_post(world)
        world.ai.answers["reading"] = {"nonsense": True}
        job = analyze(world, "https://t.me/kanalas_b/4521").json()
        polled = world.client.get(f"/api/analyze/{job['id']}").json()
        assert polled["status"] == "failed" and polled["errorStatus"] == 502
        assert "reading.v1 couldn't be read" in polled["error"]
        # a failed analysis isn't reused: trying again starts a new job
        world.ai.answers["reading"] = {"tone": "x", "intent": "y", "techniques": [], "manipulation": 1}
        again = analyze(world, "https://t.me/kanalas_b/4521")
        assert again.status_code == 202 and again.json()["id"] != job["id"]

    def test_collector_errors_fail_the_job_with_their_status(self, world):
        world.services.collector = FakeCollector(error=CollectError("That post doesn't exist any more, or isn't public.", 404))
        job = analyze(world, "https://t.me/someone/42").json()
        polled = world.client.get(f"/api/analyze/{job['id']}").json()
        assert (polled["status"], polled["errorStatus"], polled["error"]) == ("failed", 404, "That post doesn't exist any more, or isn't public.")

    def test_a_post_without_text(self, world):
        kb = add_account(world, 2, "kanalas_b", "Kanalas B", handle="kanalas_b")
        add_post(world, kb, "kanalas_b/1", "   ", "lt", "2026-11-03 11:00:00")
        polled = world.client.get(f"/api/analyze/{analyze(world, 't.me/kanalas_b/1').json()['id']}").json()
        assert (polled["status"], polled["errorStatus"], polled["error"]) == ("failed", 422, analysis.NO_TEXT)

    def test_unknown_job(self, world):
        res = world.client.get("/api/analyze/" + "0" * 32)
        assert res.status_code == 404 and res.json() == {"detail": analysis.NO_JOB}
        assert world.client.get("/api/analyze/x' OR '1'='1").status_code == 404

    def test_a_job_that_never_finished_is_reported_as_interrupted(self, world):
        with world.conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO analysis_requests (job_key, input_url, link_key, status, step, started_at)"
                        " VALUES ('a1', 'https://t.me/x/1', 't.me/x/1', 'analyzing', 2, %s)",
                        ((NOW - timedelta(hours=1)).replace(tzinfo=None),))
        polled = world.client.get("/api/analyze/a1").json()
        assert polled["status"] == "failed" and polled["error"] == analysis.INTERRUPTED

    def test_bad_request_bodies_are_422(self, world):
        assert world.client.post("/api/analyze", json={}).status_code == 422
        assert world.client.post("/api/analyze", json={"url": 5}).status_code == 422
        assert world.client.post("/api/analyze", json={"url": "https://t.me/" + "a" * 2048}).status_code == 422
        assert world.client.post("/api/analyze", content=b"{not json", headers={"content-type": "application/json"}).status_code == 422


@pytest.mark.integration
def test_a_slow_analysis_does_not_block_other_requests(world):
    w = world
    the_post(w)
    w.ai.gate = threading.Event()
    app_runner = JobRunner(w.services)  # real background thread
    w.client.app.state.analysis = app_runner
    try:
        started = time.monotonic()
        res = analyze(w, "https://t.me/kanalas_b/4521")
        assert res.status_code == 202 and res.json()["status"] == "queued"
        assert w.client.get("/api/narratives").status_code == 200
        assert w.client.get(f"/api/analyze/{res.json()['id']}").json()["status"] in ("queued", "running")
        assert time.monotonic() - started < 10
        busy = analyze(w, "https://t.me/kanalas_b/4521")  # asking again while it runs: the same job
        assert busy.json()["id"] == res.json()["id"]
        w.ai.gate.set()
        for _ in range(100):
            if w.client.get(f"/api/analyze/{res.json()['id']}").json()["status"] == "done":
                break
            time.sleep(0.1)
        assert w.client.get(f"/api/analyze/{res.json()['id']}").json()["status"] == "done"
    finally:
        w.ai.gate.set()
        app_runner.shutdown()


@pytest.mark.llm
@pytest.mark.integration
def test_live_pipeline_with_the_real_ai(world):
    """The whole analysis with the llama-swap on this computer (skipped when it isn't running)."""
    real = LocalAI.from_settings(Settings.load(), timeout=300)
    try:
        real.check()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"local AI not available: {exc}")
    w = world
    w.services.ai = lambda: LocalAI.from_settings(Settings.load(), timeout=300)
    the_post(w)
    job = analyze(w, "https://t.me/kanalas_b/4521").json()
    polled = w.client.get(f"/api/analyze/{job['id']}").json()
    assert polled["status"] == "done", polled["error"]
    report = PostReport.model_validate(polled["report"])
    assert report.post.original_language == "Lithuanian"
    assert "".join(s.text for s in report.post.original) == POST_TEXT
    assert "NATO" in report.post.translation[0].text
    assert report.reading.model.startswith("gemma")
    for segment in report.post.original:
        if segment.flag:
            assert segment.text in POST_TEXT
