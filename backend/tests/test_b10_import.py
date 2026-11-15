"""B10: loading the collected data (data/parsed/) into MySQL and Qdrant.

- The files become the same CollectedPost records the live collectors make: ids follow
  docs/data-architecture.md, dates are UTC, posts without text are left out.
- The committed files all load.
- With a throwaway database, throwaway Qdrant collections and a fake AI: rows, metrics,
  hashes and vectors are stored; a second run changes nothing; a changed text is embedded again;
  a stopped embedding run continues.
- The launcher runs the import only when asked.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import scratch_database
from test_b6_analyze import FakeAI
from veritas import importer
from veritas.ai import EMBED_MAX_CHARS, LocalAI
from veritas.config import ROOT, Settings
from veritas.content import content_hash
from veritas.db import connection
from veritas.db.migrations import migrate
from veritas.ids import post_uuid
from veritas.importer import (
    DataFileError, article_post, canonical_url, load_posts, records, telegram_post, telegram_posts, web_external_id,
)
from veritas.vectors import Qdrant, VectorStoreUnavailable

sys.path.insert(0, str(ROOT))  # the launcher package (local_ai/) lives at the repository root

import import_data  # noqa: E402  (backend/import_data.py; backend/ is on the path for the tests)
from local_ai import __main__ as launcher  # noqa: E402
from local_ai import backend, ui  # noqa: E402

TG_POST = {"author": "INFA.LT", "content": "Pirma eilutė.\nAntra eilutė.", "dateTime": "2026-11-14T09:30:00Z",
           "channelName": "INFA.LT", "postUrl": "https://t.me/InfaLT/1234"}
ARTICLE = {"url": "https://www.respublika.lt/lt/naujienos/estija-uzdraude/", "headline": "Estija uždraudė",
           "teaser": "Trumpai.", "published": "2026-11-14T16:42:00+02:00", "author": "Živilė A., Elta",
           "text": "Estijos kultūros ministerija uždraudė.", "images": [], "links": [], "has_body": True}


# --------------------------------------------------------------------------- Telegram records

def test_telegram_post_follows_the_id_conventions():
    post = telegram_post(TG_POST, {"infalt": 575})
    assert post.platform == "telegram"
    assert post.external_id == "infalt/1234"          # the path, lowercased
    assert post.url == "https://t.me/InfaLT/1234"      # the link keeps Telegram's spelling
    assert post.account == "infalt" and post.account_name == "INFA.LT" and post.account_type == "channel"
    assert post.text == "Pirma eilutė.\nAntra eilutė."
    assert post.published_at == datetime(2026, 11, 14, 9, 30, tzinfo=timezone.utc)
    assert post.byline is None                          # the channel's own name isn't a signature
    assert post.followers == 575
    assert post.language is None                        # Gemma finds the language later


def test_telegram_signature_becomes_the_byline():
    post = telegram_post({**TG_POST, "author": "J. Rašytojas"})
    assert post.byline == "J. Rašytojas"
    assert post.followers is None


@pytest.mark.parametrize("raw", [
    {**TG_POST, "content": ""},
    {**TG_POST, "content": "   \n"},
    {**TG_POST, "postUrl": "https://t.me/infalt"},
    {**TG_POST, "postUrl": ""},
])
def test_telegram_posts_without_text_or_post_link_are_left_out(raw):
    assert telegram_post(raw) is None


def test_telegram_file_is_read_in_channel_order_with_subscribers(tmp_path: Path):
    posts = tmp_path / "posts.json"
    posts.write_text(json.dumps({
        "cop_lt": [{**TG_POST, "postUrl": "https://t.me/cop_lt/2", "channelName": "Revizor"},
                   {**TG_POST, "postUrl": "https://t.me/cop_lt/1", "content": ""}],
        "unreadable": [],
        "infalt": [TG_POST],
    }, ensure_ascii=False), encoding="utf-8")
    sources = tmp_path / "telegram_sources.json"
    sources.write_text(json.dumps({"TG": [{"name": "Revizor", "link": "https://t.me/Cop_LT", "userSize": 575},
                                          {"name": "x", "link": "https://t.me/infalt", "userSize": "many"}]}), encoding="utf-8")
    got = list(telegram_posts(posts, sources))
    assert [p.external_id for p in got] == ["cop_lt/2", "infalt/1234"]
    assert got[0].followers == 575 and got[0].account_name == "Revizor"
    assert got[1].followers is None  # a count that isn't a number is ignored


def test_telegram_file_must_be_an_object(tmp_path: Path):
    bad = tmp_path / "posts.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(DataFileError):
        list(telegram_posts(bad, tmp_path / "missing.json"))


# --------------------------------------------------------------------------- article records

@pytest.mark.parametrize("url", [
    "https://www.respublika.lt/lt/naujienos/estija-uzdraude/",
    "https://www.respublika.lt/lt/naujienos/estija-uzdraude",
    "http://WWW.Respublika.lt/lt/naujienos/estija-uzdraude/?utm_source=x#top",
    "  https://www.respublika.lt/lt/naujienos/estija-uzdraude/ ",
])
def test_canonical_url_gives_one_spelling(url):
    assert canonical_url(url) == "https://www.respublika.lt/lt/naujienos/estija-uzdraude"
    assert web_external_id(url) == hashlib.sha256(b"https://www.respublika.lt/lt/naujienos/estija-uzdraude").hexdigest()


def test_article_post_is_a_web_post_of_the_site():
    post = article_post(ARTICLE, "respublika.lt")
    assert post.platform == "web" and post.account == "respublika.lt" and post.account_type == "website"
    assert post.external_id == web_external_id(ARTICLE["url"]) and len(post.external_id) == 64
    assert post.title == "Estija uždraudė" and post.byline == "Živilė A., Elta"
    assert post.published_at == datetime(2026, 11, 14, 14, 42, tzinfo=timezone.utc)  # +02:00 -> UTC
    assert post.language == "lt"
    assert post.followers is None


def test_unknown_site_language_is_left_open():
    assert article_post(ARTICLE, "example.org").language is None


@pytest.mark.parametrize("raw", [{**ARTICLE, "text": ""}, {**ARTICLE, "url": "not a link"}, {**ARTICLE, "text": None}])
def test_articles_without_text_or_link_are_left_out(raw):
    assert article_post(raw, "respublika.lt") is None


def test_article_file_needs_site_and_articles(tmp_path: Path):
    bad = tmp_path / "articles.json"
    bad.write_text(json.dumps({"articles": [ARTICLE]}), encoding="utf-8")
    with pytest.raises(DataFileError):
        list(importer.article_posts(bad))


def test_unknown_source_and_missing_file_are_plain_errors(tmp_path: Path):
    with pytest.raises(DataFileError, match="Unknown source"):
        list(records("facebook"))
    with pytest.raises(DataFileError, match="no data file"):
        list(records("respublika", {"respublika": tmp_path / "nothing.json"}))


# --------------------------------------------------------------------------- the committed files

@pytest.mark.parametrize("name", sorted(importer.SOURCES))
def test_committed_files_load(name):
    seen = set()
    count = 0
    for post in records(name):
        count += 1
        assert post.text and post.external_id and post.account
        assert post.platform == ("telegram" if name == "telegram" else "web")
        key = post_uuid(post.platform, post.external_id)
        assert key not in seen, f"duplicate post {post.url}"
        seen.add(key)
    assert count >= 100


def test_committed_telegram_channels_have_subscriber_counts():
    subscribers = importer.telegram_subscribers()
    assert len(subscribers) >= 50 and all(isinstance(n, int) for n in subscribers.values())
    first = next(records("telegram"))
    assert first.followers == subscribers[first.account]


# --------------------------------------------------------------------------- hashes and embedding size

def test_content_hash_ignores_whitespace_differences():
    assert content_hash("NATO  tankai\nprovokuoja") == content_hash("NATO tankai provokuoja ")
    assert content_hash("a") != content_hash("b")
    assert content_hash("") is None and content_hash(None) is None and content_hash("  \n") is None
    assert len(content_hash("x")) == 32


def test_embedding_input_is_cut_to_the_opening():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.extend(body["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.0] * 768} for i in range(len(body["input"]))]})

    ai = LocalAI("http://ai.test/v1", transport=httpx.MockTransport(handler))
    ai.embed_documents(["ž" * (EMBED_MAX_CHARS + 500)], ["T" * 1000])
    assert seen[0].startswith("title: " + "T" * 300 + " | text: ")
    assert seen[0].endswith("ž" * EMBED_MAX_CHARS) and seen[0].count("ž") == EMBED_MAX_CHARS


# --------------------------------------------------------------------------- the launcher

def test_launcher_imports_only_with_the_flag(monkeypatch):
    from local_ai import __main__ as launcher
    from local_ai import backend

    calls = []
    monkeypatch.setattr(backend, "import_data", lambda: calls.append(1) or "Done: 5 posts loaded, 5 vectors made.")
    ctx = launcher.Context(args=SimpleNamespace(demo_data=False), state={})
    launcher.step_import(ctx)                         # older argument sets have no import_data at all
    assert calls == []
    launcher.step_import(launcher.Context(args=SimpleNamespace(import_data=True), state={}))
    assert calls == [1]
    titles = [t for t, _ in launcher.STEPS]
    assert titles.index("Setting up vector search") < titles.index("Loading the collected data") < titles.index("Starting the dashboard")


def test_launcher_import_reports_the_last_line_or_fails_plainly(monkeypatch):
    from local_ai import backend, ui

    monkeypatch.setattr(backend, "run_tool", lambda script, *a, **kw: (0, "Loading telegram...\nDone: 3 posts loaded, 3 vectors made."))
    assert backend.import_data() == "Done: 3 posts loaded, 3 vectors made."
    monkeypatch.setattr(backend, "run_tool", lambda script, *a, **kw: (1, "Import stopped. The database isn't reachable at x."))
    with pytest.raises(ui.StepFailed, match="isn't reachable"):
        backend.import_data()


def test_cli_refuses_contradicting_options(capsys):
    import import_data

    assert import_data.main(["--no-embed", "--embed-only"]) == 2
    assert import_data.main(["--limit", "0"]) == 2
    assert "--no-embed and --embed-only" in capsys.readouterr().err


# --------------------------------------------------------------------------- with real databases

@dataclasses.dataclass
class World:
    settings: Settings
    qdrant: Qdrant
    ai: FakeAI
    files: dict[str, Path]

    def conn(self):
        return connection(self.settings)

    def q(self) -> Qdrant:
        return Qdrant(self.qdrant.base_url, prefix=self.qdrant.prefix)


@pytest.fixture
def world(tmp_path: Path):
    q = Qdrant.from_settings(Settings.load(), prefix=f"vi_{secrets.token_hex(4)}_")
    try:
        q.aliases()
    except VectorStoreUnavailable as exc:
        q.close()
        pytest.skip(f"Qdrant not available: {exc}")
    tg = tmp_path / "posts.json"
    tg.write_text(json.dumps({"infalt": [TG_POST, {**TG_POST, "postUrl": "https://t.me/infalt/1235", "content": "Kitas tekstas."},
                                         {**TG_POST, "postUrl": "https://t.me/infalt/1236", "content": ""}]}, ensure_ascii=False),
                  encoding="utf-8")
    art = tmp_path / "articles.json"
    art.write_text(json.dumps({"site": "respublika.lt", "articles": [ARTICLE, {**ARTICLE, "url": "https://www.respublika.lt/kitas/",
                                                                              "text": "Kitas straipsnis."}]}, ensure_ascii=False),
                   encoding="utf-8")
    sources = tmp_path / "telegram_sources.json"
    sources.write_text(json.dumps({"TG": [{"name": "INFA.LT", "link": "https://t.me/infalt", "userSize": 8900}]}), encoding="utf-8")
    with scratch_database() as db:
        settings = dataclasses.replace(db, qdrant_port=Settings.load().qdrant_port)
        with connection(settings) as conn:
            migrate(conn)
        try:
            q.setup()
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(importer, "TELEGRAM_SOURCES", sources)  # read at call time, so the test's channel list is used
                yield World(settings, q, FakeAI(), {"telegram": tg, "respublika": art})
        finally:
            q.drop_all()
            q.close()


def _rows(w: World, sql: str, args: tuple = ()) -> list[dict]:
    with w.conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


@pytest.mark.integration
class TestImport:
    def test_rows_metrics_hashes_and_vectors_are_stored(self, world: World):
        with world.conn() as conn, world.q() as q:
            result = importer.run_import(conn, ["telegram", "respublika"], qdrant=q, ai=world.ai.client(), sources=world.files)
        counts = result["sources"]
        assert (counts["telegram"].new, counts["respublika"].new) == (2, 2)
        assert result["embedded"] == 4 and result["pending"] == 0

        posts = _rows(world, "SELECT p.external_id, p.url, p.title, p.byline, p.language, p.content_hash, p.published_at,"
                             " p.embedded_at, p.embedding_model_id, pl.code, a.display_name, a.account_type"
                             " FROM posts p JOIN platforms pl ON pl.platform_id = p.platform_id"
                             " JOIN accounts a ON a.account_id = p.account_id ORDER BY p.post_id")
        assert [p["code"] for p in posts] == ["telegram", "telegram", "web", "web"]
        tg, art = posts[0], posts[2]
        assert tg["external_id"] == "infalt/1234" and tg["display_name"] == "INFA.LT" and tg["account_type"] == "channel"
        assert tg["published_at"] == datetime(2026, 11, 14, 9, 30) and tg["language"] is None
        assert art["title"] == "Estija uždraudė" and art["byline"] == "Živilė A., Elta" and art["language"] == "lt"
        assert art["account_type"] == "website" and art["published_at"] == datetime(2026, 11, 14, 14, 42)
        assert all(p["content_hash"] is not None and p["embedded_at"] is not None and p["embedding_model_id"] == 1 for p in posts)

        metrics = _rows(world, "SELECT a.external_id, m.followers FROM account_metrics m JOIN accounts a ON a.account_id = m.account_id")
        assert [(m["external_id"], m["followers"]) for m in metrics] == [("infalt", 8900)]
        with world.q() as q:
            assert q.count("posts") == 4

    def test_second_run_changes_nothing_and_changed_text_is_embedded_again(self, world: World):
        with world.conn() as conn, world.q() as q:
            importer.run_import(conn, ["telegram"], qdrant=q, ai=world.ai.client(), sources=world.files)
            again = importer.run_import(conn, ["telegram"], qdrant=q, ai=world.ai.client(), sources=world.files)
        assert again["sources"]["telegram"].unchanged == 2 and again["embedded"] == 0
        assert world.ai.calls.count("embed") == 1

        data = json.loads(world.files["telegram"].read_text(encoding="utf-8"))
        data["infalt"][0]["content"] = "Pataisytas tekstas."
        world.files["telegram"].write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with world.conn() as conn, world.q() as q:
            third = importer.run_import(conn, ["telegram"], qdrant=q, ai=world.ai.client(), sources=world.files)
        assert (third["sources"]["telegram"].updated, third["sources"]["telegram"].unchanged) == (1, 1)
        assert third["embedded"] == 1
        row = _rows(world, "SELECT text, content_hash FROM posts WHERE external_id = 'infalt/1234'")[0]
        assert row["text"] == "Pataisytas tekstas." and row["content_hash"] == content_hash("Pataisytas tekstas.")
        with world.q() as q:
            assert q.count("posts") == 2  # the vector was replaced, not added

    def test_rows_only_then_vectors_in_steps(self, world: World):
        with world.conn() as conn:
            rows_only = importer.run_import(conn, ["telegram", "respublika"], embed=False, sources=world.files)
            assert rows_only["embedded"] == 0 and rows_only["pending"] == 4
            assert world.ai.calls == []
            with world.q() as q:
                assert q.count("posts") == 0
                first = importer.embed_pending(conn, q, world.ai.client(), batch=1, limit=1)
                assert first == 1 and importer.count_pending(conn) == 3
                rest = importer.embed_pending(conn, q, world.ai.client(), batch=2)
                assert rest == 3 and importer.count_pending(conn) == 0
                assert q.count("posts") == 4
        assert world.ai.calls.count("embed") == 3  # 1 + 2 + 1 requests

    def test_limit_takes_the_first_posts_of_each_source(self, world: World):
        with world.conn() as conn:
            result = importer.run_import(conn, ["telegram", "respublika"], embed=False, limit=1, sources=world.files)
        assert all(c.new == 1 for c in result["sources"].values())
        assert [r["external_id"] for r in _rows(world, "SELECT external_id FROM posts ORDER BY post_id")][0] == "infalt/1234"

    def test_load_posts_counts_accounts(self, world: World):
        with world.conn() as conn:
            counts = load_posts(conn, list(records("telegram", world.files)) + list(records("respublika", world.files)))
        assert counts.total == 4 and counts.accounts == {("telegram", "infalt"), ("web", "respublika.lt")}
        assert "4 posts from 2 accounts" in counts.summary()
