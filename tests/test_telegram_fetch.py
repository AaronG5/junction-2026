"""Telegram fetcher: pagination, RU fallback, caching and run-level error handling.

A fake client stands in for Oxylabs and serves t.me/s pages for fake channels.
"""
import argparse
import json
import logging
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrapers.oxylabs import AuthenticationError, ScrapeResult, WebApiError
from scrapers.telegram.__main__ import parse_since
from scrapers.telegram.fetch import (Channel, PageCache, fetch_channel, load_channels,
                                     preview_url, run_channels, username_from_link)

logging.getLogger("scrapers").addHandler(logging.NullHandler())

PER_PAGE = 20
START = datetime(2026, 11, 1, tzinfo=timezone.utc)
LANDING = ("<html><head><title>Telegram: View @chan</title></head><body>"
           "If you have Telegram, you can view posts right away. Preview channel</body></html>")


def posted(i):
    """Fake post i was published i hours after START, so higher ids are newer."""
    return START + timedelta(hours=i)


def channel_page(username, ids, texts=None):
    texts = texts or {}
    messages = "".join(f"""
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="{username}/{i}">
 <div class="tgme_widget_message_author"><a class="tgme_widget_message_owner_name"><span>{username}</span></a></div>
 <div class="tgme_widget_message_text js-message_text">{texts.get(i, f"post {i}")}</div>
 <a class="tgme_widget_message_date"><time datetime="{posted(i).isoformat()}"></time></a>
</div></div>""" for i in ids)
    return f"<html><body>{messages}</body></html>"


class FakeTelegram:
    """Channel `u` has posts 1..totals[u]. Regions in blocked[u] only get the landing
    page. errors maps a URL to the exception scraping it raises. texts overrides
    a post's text (to simulate edits). All of these can be changed between runs."""

    def __init__(self, totals, blocked=None, errors=None):
        self.totals = totals
        self.blocked = blocked or {}
        self.errors = errors or {}
        self.texts = {}
        self.calls = []

    def scrape(self, url, output=("html",), *, location=None):
        self.calls.append((url, location))
        if url in self.errors:
            raise self.errors[url]
        parsed = urllib.parse.urlparse(url)
        username = parsed.path.split("/")[2]
        before = int(urllib.parse.parse_qs(parsed.query).get("before", ["0"])[0]) or None
        if (location or "default") in self.blocked.get(username, ()):
            html = LANDING
        else:
            ids = [i for i in range(1, self.totals[username] + 1) if before is None or i < before]
            html = channel_page(username, ids[-PER_PAGE:], self.texts)
        return ScrapeResult({"html": html}, None, f"req-{len(self.calls)}", {})


def chan(username="chan"):
    return Channel(username, f"{username} name", f"https://t.me/{username}")


def ids_of(result):
    return [int(p["postUrl"].rsplit("/", 1)[1]) for p in result.posts]


class FetchTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw = Path(tmp.name) / "raw"
        self.parsed = Path(tmp.name) / "parsed"


class RegionFallbackTests(FetchTestCase):
    def test_no_fallback_when_page_one_has_posts(self):
        client = FakeTelegram({"chan": 50})
        result = fetch_channel(client, chan(), 20, self.raw)
        self.assertEqual(client.calls, [("https://t.me/s/chan", None)])
        self.assertEqual((result.status, result.region, len(result.posts)), ("ok", "default", 20))

    def test_ru_fallback_when_page_one_is_empty_and_ru_sticks(self):
        client = FakeTelegram({"chan": 50}, blocked={"chan": {"default"}})
        result = fetch_channel(client, chan(), 30, self.raw)
        self.assertEqual(client.calls, [
            ("https://t.me/s/chan", None),
            ("https://t.me/s/chan", "RU"),
            ("https://t.me/s/chan?before=31", "RU"),      # later pages keep RU
        ])
        self.assertEqual((result.status, result.region, len(result.posts)), ("ok", "RU", 30))
        self.assertEqual(result.request_ids, ["req-1", "req-2", "req-3"])

    def test_unreadable_when_ru_is_empty_too(self):
        client = FakeTelegram({"chan": 50}, blocked={"chan": {"default", "RU"}})
        result = fetch_channel(client, chan(), 20, self.raw)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual((result.status, result.region, result.posts), ("unreadable", "RU", []))
        self.assertIn("RU", result.reason)
        # Empty pages are kept for inspection but not cached: the next run tries again.
        self.assertTrue((self.raw / "chan" / "empty-default.html").is_file())
        self.assertFalse((self.raw / "chan" / "page-001.html").exists())
        fetch_channel(client, chan(), 20, self.raw)
        self.assertEqual(len(client.calls), 4)

    def test_api_error_does_not_trigger_the_region_fallback(self):
        client = FakeTelegram({"chan": 50}, errors={"https://t.me/s/chan": WebApiError("500", status=500)})
        result = fetch_channel(client, chan(), 20, self.raw)
        self.assertEqual(client.calls, [("https://t.me/s/chan", None)])
        self.assertEqual(result.status, "unreadable")
        self.assertIn("API error", result.reason)


class PaginationTests(FetchTestCase):
    def test_stops_once_n_posts_are_collected(self):
        client = FakeTelegram({"chan": 100})
        result = fetch_channel(client, chan(), 45, self.raw)
        self.assertEqual([u for u, _ in client.calls], [
            "https://t.me/s/chan", "https://t.me/s/chan?before=81", "https://t.me/s/chan?before=61"])
        self.assertEqual(ids_of(result), list(range(100, 55, -1)))   # the 45 newest, newest first
        self.assertEqual(result.status, "ok")

    def test_stops_at_the_channels_first_message(self):
        client = FakeTelegram({"chan": 30})
        result = fetch_channel(client, chan(), 100, self.raw)
        self.assertEqual(len(client.calls), 2)          # 30..11, then 10..1 reaches message 1
        self.assertEqual((result.status, len(result.posts)), ("partial", 30))
        self.assertIn("only has 30 posts", result.reason)

    def test_stops_when_nothing_older_exists(self):
        client = FakeTelegram({"chan": 30})
        # Messages 1..10 were deleted: the third page is empty, which also means "no older posts".
        original = client.scrape

        def without_oldest(url, output=("html",), *, location=None):
            page = original(url, output, location=location)
            if "before=11" in url:
                page.content["html"] = "<html><body></body></html>"
            return page
        client.scrape = without_oldest
        result = fetch_channel(client, chan(), 100, self.raw)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual((result.status, len(result.posts)), ("partial", 20))
        again = fetch_channel(client, chan(), 100, self.raw)    # remembered: no new request
        self.assertEqual((len(client.calls), len(again.posts)), (2, 20))

    def test_error_on_a_later_page_keeps_what_was_collected(self):
        client = FakeTelegram({"chan": 100},
                              errors={"https://t.me/s/chan?before=81": WebApiError("503", status=503)})
        result = fetch_channel(client, chan(), 40, self.raw)
        self.assertEqual((result.status, len(result.posts)), ("partial", 20))
        self.assertIn("stopped early", result.reason)


class BlankPostTests(FetchTestCase):
    def test_posts_without_text_are_left_out_and_dont_count_towards_n(self):
        client = FakeTelegram({"chan": 50})
        client.texts = {50: "", 49: "   ", 47: ""}          # media-only posts
        result = fetch_channel(client, chan(), 20, self.raw)
        self.assertEqual(len(result.posts), 20)
        self.assertTrue(all(p["content"].strip() for p in result.posts))
        self.assertNotIn(50, ids_of(result))
        self.assertEqual(ids_of(result)[:3], [48, 46, 45])
        self.assertEqual(result.report()["blankExcluded"], 3)
        self.assertEqual(len(client.calls), 2)        # 3 blanks on page 1, so page 2 was needed

    def test_date_run_leaves_them_out_too(self):
        client = FakeTelegram({"chan": 30})
        client.texts = {30: ""}
        result = fetch_channel(client, chan(), None, self.raw, since=posted(25))
        self.assertEqual(ids_of(result), [29, 28, 27, 26, 25])
        self.assertEqual(result.blank, 1)


class DateRangeTests(FetchTestCase):
    def test_collects_every_post_since_the_date_and_stops_one_page_past_it(self):
        client = FakeTelegram({"chan": 100})
        result = fetch_channel(client, chan(), None, self.raw, since=posted(55))
        self.assertEqual([u for u, _ in client.calls], [
            "https://t.me/s/chan",                  # 100..81
            "https://t.me/s/chan?before=81",        # 80..61
            "https://t.me/s/chan?before=61",        # 60..41: holds 54, older than the date
        ])
        self.assertEqual(ids_of(result), list(range(100, 54, -1)))  # 55 is exactly on the date: kept
        self.assertEqual((result.status, result.requested, result.reason), ("ok", None, ""))
        self.assertEqual(result.since, "2026-11-03T07:00:00Z")
        self.assertEqual(result.report()["since"], "2026-11-03T07:00:00Z")

    def test_count_and_date_stop_at_whichever_comes_first(self):
        client = FakeTelegram({"chan": 100})
        by_count = fetch_channel(client, chan("chan"), 10, self.raw, since=posted(55))
        self.assertEqual((len(client.calls), len(by_count.posts), by_count.status), (1, 10, "ok"))

        client = FakeTelegram({"other": 100})
        by_date = fetch_channel(client, chan("other"), 500, self.raw, since=posted(90))
        self.assertEqual((len(client.calls), len(by_date.posts), by_date.status), (1, 11, "ok"))

    def test_channel_younger_than_the_date_is_complete(self):
        client = FakeTelegram({"chan": 30})
        result = fetch_channel(client, chan(), None, self.raw, since=START - timedelta(days=1))
        self.assertEqual(len(client.calls), 2)          # 30..11, then 10..1 reaches message 1
        self.assertEqual((result.status, len(result.posts)), ("ok", 30))

    def test_quiet_channel_with_no_posts_in_range_is_ok_not_unreadable(self):
        client = FakeTelegram({"chan": 100})
        result = fetch_channel(client, chan(), None, self.raw, since=posted(101))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual((result.status, result.posts), ("ok", []))
        self.assertIn("no posts since", result.reason)

    def test_page_limit_gives_partial(self):
        client = FakeTelegram({"chan": 100})
        result = fetch_channel(client, chan(), None, self.raw, since=posted(1), max_pages=2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual((result.status, len(result.posts)), ("partial", 40))
        self.assertIn("2-page limit", result.reason)

    def test_error_mid_range_gives_partial(self):
        client = FakeTelegram({"chan": 100},
                              errors={"https://t.me/s/chan?before=81": WebApiError("503", status=503)})
        result = fetch_channel(client, chan(), None, self.raw, since=posted(50))
        self.assertEqual((result.status, len(result.posts)), ("partial", 20))
        self.assertIn("stopped early", result.reason)

    def test_date_run_reuses_cached_pages_and_fetches_only_older_ones(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)                     # page 1 cached
        result = fetch_channel(client, chan(), None, self.raw, since=posted(55))
        self.assertEqual([u for u, _ in client.calls[1:]],
                         ["https://t.me/s/chan?before=81", "https://t.me/s/chan?before=61"])
        self.assertEqual(len(result.posts), 46)

    def test_needs_a_count_or_a_date(self):
        with self.assertRaises(ValueError):
            fetch_channel(FakeTelegram({"chan": 1}), chan(), None, self.raw)

    def test_parse_since(self):
        self.assertEqual(parse_since("2026-10-01"), datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(parse_since("2026-10-01T12:00+03:00"),
                         datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))
        for bad in ("last month", "2999-01-01"):
            with self.assertRaises(argparse.ArgumentTypeError):
                parse_since(bad)


class UpdateTests(FetchTestCase):
    def test_without_update_saved_pages_are_reused_as_they_are(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)
        client.totals["chan"] = 130
        result = fetch_channel(client, chan(), 20, self.raw)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(ids_of(result)[0], 100)

    def test_update_fetches_new_posts_and_joins_them_to_saved_pages(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), None, self.raw, since=posted(85))       # saves 81..100
        client.totals["chan"] = 130                                           # 30 new posts
        result = fetch_channel(client, chan(), None, self.raw, since=posted(85), update=True)
        self.assertEqual([u for u, _ in client.calls[1:]], [
            "https://t.me/s/chan",              # 111..130
            "https://t.me/s/chan?before=111",   # 91..110 joins the saved 81..100
        ])
        self.assertEqual(ids_of(result), list(range(130, 84, -1)))
        self.assertEqual(result.status, "ok")

    def test_update_with_nothing_new_costs_one_request(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)
        result = fetch_channel(client, chan(), 20, self.raw, update=True)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(ids_of(result), list(range(100, 80, -1)))
        self.assertEqual(PageCache(self.raw / "chan").page_count, 1)          # no duplicate saved
        self.assertEqual(result.request_ids, ["req-1", "req-2"])

    def test_update_keeps_the_channels_region(self):
        client = FakeTelegram({"chan": 100}, blocked={"chan": {"default"}})
        fetch_channel(client, chan(), 20, self.raw)                           # default, then RU
        fetch_channel(client, chan(), 20, self.raw, update=True)
        self.assertEqual(client.calls[2:], [("https://t.me/s/chan", "RU")])

    def test_a_gap_left_by_the_page_limit_is_filled_later(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)                           # saves 81..100
        client.totals["chan"] = 200
        first = fetch_channel(client, chan(), None, self.raw, since=posted(50),
                              update=True, max_pages=2)                       # 181..200, 161..180
        self.assertEqual(first.status, "partial")
        self.assertIn("limit", first.reason)

        second = fetch_channel(client, chan(), None, self.raw, since=posted(50))
        self.assertEqual(ids_of(second), list(range(200, 49, -1)))           # no holes
        self.assertEqual(second.status, "ok")
        urls = [u for u, _ in client.calls]
        self.assertNotIn("https://t.me/s/chan?before=101", urls)             # 81..100 was saved
        self.assertEqual(len(urls), len(set(urls)) + 1)                       # only page 1 twice

    def test_update_keeps_saved_posts_when_page_one_is_now_empty(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)
        client.blocked["chan"] = {"default", "RU"}
        result = fetch_channel(client, chan(), 20, self.raw, update=True)
        self.assertEqual(len(client.calls), 3)                                # default, then RU
        self.assertEqual((result.status, len(result.posts)), ("partial", 20))
        self.assertIn("couldn't update", result.reason)

    def test_update_keeps_saved_posts_on_an_api_error(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)
        client.errors["https://t.me/s/chan"] = WebApiError("503", status=503)
        result = fetch_channel(client, chan(), 20, self.raw, update=True)
        self.assertEqual((result.status, len(result.posts)), ("partial", 20))
        self.assertIn("couldn't update", result.reason)

    def test_update_picks_up_edited_posts(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)
        client.texts[100] = "post 100, edited"
        result = fetch_channel(client, chan(), 20, self.raw, update=True)
        self.assertEqual(result.posts[0]["content"], "post 100, edited")
        self.assertEqual(PageCache(self.raw / "chan").page_count, 2)          # the edit is saved
        again = fetch_channel(client, chan(), 20, self.raw)
        self.assertEqual(again.posts[0]["content"], "post 100, edited")

    def test_update_without_saved_pages_is_a_normal_first_run(self):
        client = FakeTelegram({"chan": 100})
        result = fetch_channel(client, chan(), 20, self.raw, update=True)
        self.assertEqual((len(client.calls), result.status, len(result.posts)), (1, "ok", 20))

    def test_caches_saved_before_id_ranges_still_load(self):
        folder = self.raw / "chan"
        folder.mkdir(parents=True)
        (folder / "page-001.html").write_text(channel_page("chan", range(5, 21)), encoding="utf-8")
        (folder / "meta.json").write_text(json.dumps({
            "region": "default", "exhausted": True,
            "pages": [{"file": "page-001.html", "url": "https://t.me/s/chan", "requestId": "old"}],
        }), encoding="utf-8")
        client = FakeTelegram({"chan": 20})
        result = fetch_channel(client, chan(), 50, self.raw)
        self.assertEqual(client.calls, [])                  # "exhausted" still means no older posts
        self.assertEqual((result.status, len(result.posts), result.request_ids),
                         ("partial", 16, ["old"]))
        meta = PageCache(folder).meta
        self.assertEqual(meta["emptyBefore"], 5)
        self.assertNotIn("exhausted", json.loads((folder / "meta.json").read_text(encoding="utf-8")))


class CacheTests(FetchTestCase):
    def test_saved_pages_are_reparsed_and_only_missing_pages_fetched(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)
        self.assertTrue((self.raw / "chan" / "page-001.html").is_file())

        again = fetch_channel(client, chan(), 20, self.raw)
        self.assertEqual(len(client.calls), 1)          # nothing scraped
        self.assertEqual(len(again.posts), 20)
        self.assertEqual(again.request_ids, ["req-1"])

        more = fetch_channel(client, chan(), 40, self.raw)
        self.assertEqual(client.calls[1:], [("https://t.me/s/chan?before=81", None)])
        self.assertEqual(len(more.posts), 40)

    def test_cached_region_is_reused_for_later_pages(self):
        client = FakeTelegram({"chan": 100}, blocked={"chan": {"default"}})
        fetch_channel(client, chan(), 20, self.raw)
        fetch_channel(client, chan(), 40, self.raw)
        self.assertEqual(client.calls[-1], ("https://t.me/s/chan?before=81", "RU"))

    def test_a_page_overwritten_by_another_crawl_is_dropped_and_fetched_again(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 40, self.raw)                           # 81..100, 61..80
        folder = self.raw / "chan"
        # What two crawls at once did: another process wrote a different page under this name.
        (folder / "page-002.html").write_text((folder / "page-001.html").read_text(encoding="utf-8"),
                                              encoding="utf-8")
        result = fetch_channel(client, chan(), 40, self.raw)
        self.assertEqual(client.calls[-1], ("https://t.me/s/chan?before=81", None))
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(ids_of(result), list(range(100, 60, -1)))
        self.assertEqual([p["file"] for p in PageCache(folder).meta["pages"]],
                         ["page-001.html", "page-002.html"])

    def test_pages_without_hashes_are_checked_by_id_range(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 40, self.raw)
        folder = self.raw / "chan"
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        for page in meta["pages"]:
            del page["sha256"]                                                # saved before hashes
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (folder / "page-002.html").write_text((folder / "page-001.html").read_text(encoding="utf-8"),
                                              encoding="utf-8")
        result = fetch_channel(client, chan(), 40, self.raw)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(ids_of(result), list(range(100, 60, -1)))

    def test_refresh_scrapes_again(self):
        client = FakeTelegram({"chan": 100})
        fetch_channel(client, chan(), 20, self.raw)
        fetch_channel(client, chan(), 20, self.raw, refresh=True)
        self.assertEqual(len(client.calls), 2)


class RunTests(FetchTestCase):
    def test_an_unreadable_channel_does_not_stop_the_run(self):
        client = FakeTelegram({"bad": 10, "good": 10}, blocked={"bad": {"default", "RU"}})
        results = run_channels(client, [chan("bad"), chan("good")], 5,
                               raw_root=self.raw, parsed_root=self.parsed)
        self.assertEqual([(r.channel.username, r.status) for r in results],
                         [("bad", "unreadable"), ("good", "ok")])
        report = json.loads((self.parsed / "_report.json").read_text(encoding="utf-8"))
        self.assertEqual([e["username"] for e in report], ["bad", "good"])
        self.assertEqual(set(report[1]), {"username", "requested", "since", "got", "blankExcluded",
                                          "region", "status", "reason", "requestIds"})
        posts = self.read_posts()
        self.assertEqual(list(posts), ["bad", "good"])
        self.assertEqual(posts["bad"], [])
        self.assertEqual([p["postUrl"] for p in posts["good"]],
                         [f"https://t.me/good/{i}" for i in range(10, 5, -1)])
        self.assertEqual(sorted(p.name for p in self.parsed.iterdir()), ["_report.json", "posts.json"])

    def read_posts(self):
        return json.loads((self.parsed / "posts.json").read_text(encoding="utf-8"))

    def test_unexpected_error_in_one_channel_does_not_stop_the_run(self):
        client = FakeTelegram({"good": 10}, errors={"https://t.me/s/bad": KeyError("boom")})
        results = run_channels(client, [chan("bad"), chan("good")], 5,
                               raw_root=self.raw, parsed_root=self.parsed)
        self.assertEqual([r.status for r in results], ["unreadable", "ok"])
        self.assertIn("unexpected error", results[0].reason)

    def test_401_stops_the_run_and_keeps_the_report(self):
        client = FakeTelegram({"first": 10, "third": 10},
                              errors={"https://t.me/s/second": AuthenticationError("401", status=401)})
        with self.assertRaises(AuthenticationError):
            run_channels(client, [chan("first"), chan("second"), chan("third")], 5,
                         raw_root=self.raw, parsed_root=self.parsed, workers=1)
        report = json.loads((self.parsed / "_report.json").read_text(encoding="utf-8"))
        self.assertIn("first", [e["username"] for e in report])
        self.assertNotIn("second", [e["username"] for e in report])
        self.assertEqual(len(self.read_posts()["first"]), 5)              # finished work is saved

    def test_outputs_keep_channels_not_in_this_run(self):
        client = FakeTelegram({"a": 10, "b": 10})
        run_channels(client, [chan("a"), chan("b")], 5, raw_root=self.raw, parsed_root=self.parsed)
        run_channels(client, [chan("b")], 3, raw_root=self.raw, parsed_root=self.parsed)
        report = {e["username"]: e for e in
                  json.loads((self.parsed / "_report.json").read_text(encoding="utf-8"))}
        self.assertEqual((report["a"]["got"], report["b"]["got"]), (5, 3))
        posts = self.read_posts()
        self.assertEqual((len(posts["a"]), len(posts["b"])), (5, 3))

    def test_posts_file_follows_the_channel_list_order(self):
        client = FakeTelegram({"a": 3, "b": 3, "c": 3})
        run_channels(client, [chan("c"), chan("a"), chan("b")], 2, raw_root=self.raw,
                     parsed_root=self.parsed, workers=3)
        self.assertEqual(list(self.read_posts()), ["c", "a", "b"])


class ChannelListTests(unittest.TestCase):
    def test_username_from_link(self):
        self.assertEqual(username_from_link("https://t.me/cop_lt"), "cop_lt")
        self.assertEqual(username_from_link("https://t.me/s/cop_lt"), "cop_lt")
        self.assertEqual(username_from_link("https://t.me/cop_lt/123"), "cop_lt")
        with self.assertRaises(ValueError):
            username_from_link("https://t.me/")

    def test_preview_url(self):
        self.assertEqual(preview_url("infalt"), "https://t.me/s/infalt")
        self.assertEqual(preview_url("infalt", 20149), "https://t.me/s/infalt?before=20149")

    def test_load_channels_dedupes_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.json"
            path.write_text(json.dumps({"TG": [
                {"name": "A", "link": "https://t.me/Alpha", "userSize": 1},
                {"name": "A again", "link": "https://t.me/alpha", "userSize": 1},
                {"name": "B", "link": "https://t.me/beta", "userSize": 2},
            ]}), encoding="utf-8")
            channels = load_channels(path)
        self.assertEqual([(c.username, c.name) for c in channels], [("Alpha", "A"), ("beta", "B")])

    def test_real_channel_list_loads(self):
        channels = load_channels()
        self.assertGreater(len(channels), 0)
        self.assertTrue(all(c.username and "/" not in c.username for c in channels))


if __name__ == "__main__":
    unittest.main()
