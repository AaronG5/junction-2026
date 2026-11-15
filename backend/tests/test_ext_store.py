"""Posts the extension sends are kept in MySQL + Qdrant (veritas/ext_store.py).

- Items -> records: X status links, Facebook group/page posts, reels; unusable items skipped.
- Stored like the importer stores data: rows, accounts and vectors; the same text again changes
  nothing; a changed text is updated and embedded again.
- /api/ext/batch and /api/ext/save queue the posts; saving is off with EXT_SAVE_POSTS=off.
Integration tests use a throwaway database and Qdrant collections (the `world` fixture).
"""
from __future__ import annotations

import pytest

from conftest import make_client
from test_b6_analyze import world  # noqa: F401  (throwaway database + Qdrant + fake AI)
from veritas import ext_store
from veritas.ext_store import PostStore, to_collected
from veritas.vectors import Qdrant

X_ITEM = {"id": "1890412233445566771", "url": "https://x.com/EU_Insider/status/1890412233445566771", "author": "EU_Insider",
          "author_name": "EU Insider", "text": "The Nvidia scandal is a CIA operation.", "lang": "en",
          "time": "2026-11-14T17:48:00.000Z", "kind": "post"}
FB_GROUP = {"id": "fb3674462936037307", "url": "https://www.facebook.com/groups/Kaimynai/posts/3674462936037307/",
            "author": "jonas.j", "author_name": "Jonas J", "text": "Nuomojamas butas", "time": "2026-11-14T20:36:12.000Z",
            "platform": "facebook", "group": "Kaimynų grupė"}
FB_REEL = {"id": "fb1455920952739663", "url": "https://www.facebook.com/reel/1455920952739663/", "author": "LRT.LT",
           "author_name": "LRT", "text": "Atlikus tyrimo veiksmus", "platform": "facebook"}


def test_items_become_records():
    x = to_collected(X_ITEM)
    assert (x.platform, x.external_id, x.account, x.account_name, x.language) == ("x", "1890412233445566771", "eu_insider", "EU Insider", "en")
    g = to_collected(FB_GROUP)
    assert (g.platform, g.external_id, g.account, g.account_name, g.account_type, g.byline) == \
        ("facebook", "kaimynai_3674462936037307", "kaimynai", "Kaimynų grupė", "group", "Jonas J")
    assert g.url == "https://www.facebook.com/groups/kaimynai/posts/3674462936037307"
    r = to_collected(FB_REEL)
    assert (r.external_id, r.account) == ("1455920952739663", "lrt.lt")


@pytest.mark.parametrize("item", [
    {**X_ITEM, "text": "  "},
    {**X_ITEM, "url": None},                                   # a quoted post without a permalink
    {**X_ITEM, "url": "http://x.com/a/status/12345"},          # not https
    {"id": "1", "url": "https://example.com/post", "text": "hello there"},
])
def test_unusable_items_are_skipped(item):
    assert to_collected(item) is None


def store_for(w) -> PostStore:
    return PostStore(w.settings, w.ai.client, lambda: Qdrant(w.qdrant.base_url, prefix=w.qdrant.prefix), start=False)


@pytest.mark.integration
class TestStored:
    def test_posts_are_stored_with_vectors(self, world):
        s = store_for(world)
        s.run_once([to_collected(X_ITEM), to_collected(FB_GROUP), to_collected(FB_REEL)])
        rows = world.rows("SELECT pl.code, p.external_id, p.byline, p.language, p.embedded_at IS NOT NULL AS v"
                          " FROM posts p JOIN platforms pl ON pl.platform_id = p.platform_id ORDER BY p.post_id")
        assert [(r["code"], r["external_id"], r["v"]) for r in rows] == [
            ("x", "1890412233445566771", 1), ("facebook", "kaimynai_3674462936037307", 1), ("facebook", "1455920952739663", 1)]
        assert s.stored == 3 and s.embedded == 3
        accounts = world.rows("SELECT external_id, display_name, account_type FROM accounts ORDER BY account_id")
        assert {"external_id": "kaimynai", "display_name": "Kaimynų grupė", "account_type": "group"} in accounts

    def test_the_same_post_again_changes_nothing_and_a_new_text_is_updated(self, world):
        s = store_for(world)
        s.run_once([to_collected(X_ITEM)])
        calls = len(world.ai.calls)
        s.run_once([to_collected(X_ITEM)])
        assert s.stored == 1 and len(world.ai.calls) == calls        # no second write, no second vector
        s.run_once([to_collected({**X_ITEM, "text": X_ITEM["text"] + " Edited."})])
        rows = world.rows("SELECT text FROM posts")
        assert len(rows) == 1 and rows[0]["text"].endswith("Edited.") and s.stored == 2

    def test_ai_down_keeps_the_rows(self, world):
        world.ai.up = False
        s = store_for(world)
        s.run_once([to_collected(X_ITEM)])
        assert world.rows("SELECT embedded_at FROM posts") == [{"embedded_at": None}] and s.embedded == 0


def test_batch_and_save_queue_the_posts(settings, monkeypatch):
    monkeypatch.setenv("EXT_SAVE_POSTS", "on")
    s = PostStore(settings, None, None, start=False)
    ext_store.set_store(s)
    try:
        c = make_client(settings)
        res = c.post("/api/ext/save", json={"client": "t", "items": [X_ITEM, FB_GROUP, {**X_ITEM, "id": "2", "text": ""}]})
        assert res.status_code == 200 and res.json() == {"queued": 2} and s.queue.qsize() == 2
        monkeypatch.setenv("EXT_SAVE_POSTS", "off")
        assert c.post("/api/ext/save", json={"client": "t", "items": [X_ITEM]}).json() == {"queued": 0}
    finally:
        ext_store.set_store(None)
