"""Telegram fetcher: pagination, RU fallback, caching and run-level error handling.

A fake client stands in for Oxylabs and serves t.me/s pages for fake channels.
"""
import json
import logging
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from scrapers.oxylabs import AuthenticationError, ScrapeResult, WebApiError
from scrapers.telegram.fetch import (Channel, fetch_channel, load_channels, preview_url,
                                     run_channels, username_from_link)

logging.getLogger("scrapers").addHandler(logging.NullHandler())

PER_PAGE = 20
LANDING = ("<html><head><title>Telegram: View @chan</title></head><body>"
           "If you have Telegram, you can view posts right away. Preview channel</body></html>")


def channel_page(username, ids):
    messages = "".join(f"""
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="{username}/{i}">
 <div class="tgme_widget_message_author"><a class="tgme_widget_message_owner_name"><span>{username}</span></a></div>
 <div class="tgme_widget_message_text js-message_text">post {i}</div>
 <a class="tgme_widget_message_date"><time datetime="2026-11-14T09:00:00+00:00"></time></a>
</div></div>""" for i in ids)
    return f"<html><body>{messages}</body></html>"


class FakeTelegram:
    """Channel `u` has posts 1..totals[u]. Regions in blocked[u] only get the landing
    page. errors maps a URL to the exception scraping it raises."""

    def __init__(self, totals, blocked=None, errors=None):
        self.totals = totals
        self.blocked = blocked or {}
        self.errors = errors or {}
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
            html = channel_page(username, ids[-PER_PAGE:])
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

    def test_stops_when_a_page_adds_no_new_posts(self):
        client = FakeTelegram({"chan": 30})
        result = fetch_channel(client, chan(), 100, self.raw)
        self.assertEqual(len(client.calls), 3)          # 30..11, 10..1, then nothing new
        self.assertEqual((result.status, len(result.posts)), ("partial", 30))
        self.assertIn("only has 30 posts", result.reason)

    def test_error_on_a_later_page_keeps_what_was_collected(self):
        client = FakeTelegram({"chan": 100},
                              errors={"https://t.me/s/chan?before=81": WebApiError("503", status=503)})
        result = fetch_channel(client, chan(), 40, self.raw)
        self.assertEqual((result.status, len(result.posts)), ("partial", 20))
        self.assertIn("stopped early", result.reason)


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
        self.assertEqual(set(report[1]), {"username", "requested", "got", "region",
                                          "status", "reason", "requestIds"})
        posts = json.loads((self.parsed / "good.json").read_text(encoding="utf-8"))
        self.assertEqual(len(posts), 5)
        self.assertEqual(json.loads((self.parsed / "bad.json").read_text(encoding="utf-8")), [])

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

    def test_report_keeps_entries_for_channels_not_in_this_run(self):
        client = FakeTelegram({"a": 10, "b": 10})
        run_channels(client, [chan("a"), chan("b")], 5, raw_root=self.raw, parsed_root=self.parsed)
        run_channels(client, [chan("b")], 3, raw_root=self.raw, parsed_root=self.parsed)
        report = {e["username"]: e for e in
                  json.loads((self.parsed / "_report.json").read_text(encoding="utf-8"))}
        self.assertEqual((report["a"]["got"], report["b"]["got"]), (5, 3))


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
