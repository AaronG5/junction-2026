"""YouTube fetcher: listings, per-video requests, caching, retries and run-level errors.

A fake client stands in for Oxylabs and serves channel listings, metadata and subtitles
in the shapes the real API returned on 2026-11-15 (see tests/fixtures/youtube/).
"""
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrapers.oxylabs import AuthenticationError, MediaResult, ValidationError, WebApiError
from scrapers.youtube.fetch import (Source, fetch_channel, handle_from_link, load_sources,
                                    run_channels)

logging.getLogger("scrapers").addHandler(logging.NullHandler())

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def vid(i):
    return f"vid{i:08d}"          # 11 characters, like a real video id


class Video:
    """One fake video. transcript: the spoken language of its auto-generated track, or None.
    uploader: {language: text} of uploader-provided tracks."""

    def __init__(self, i, published, transcript="lt", uploader=None, live=False, unavailable=False):
        self.id = vid(i)
        self.published = published
        self.transcript = transcript
        self.uploader = uploader or {}
        self.live = live
        self.unavailable = unavailable


def relative(age):
    days = age.total_seconds() / 86400
    if days < 1:
        return f"{max(1, int(days * 24))}h ago"
    if days < 7:
        return f"{int(days)}d ago"
    if days < 30:
        return f"{int(days // 7)}w ago"
    if days < 365:
        return f"{int(days // 30)}mo ago"
    return f"{int(days // 365)}y ago"


def json3(text):
    return {"wireMagic": "pb3", "events": [{"tStartMs": 0, "segs": [{"utf8": text}]}]}


def done(body, request_id):
    return MediaResult({"state": "done", "results": [{"json": body, "metadata": {}}],
                        "metadata": {"request_id": request_id}}, request_id)


class FakeYouTube:
    """channels maps a lowercased handle to its videos, newest first. errors maps
    (endpoint, query) to the exception that call raises. Everything can change between runs."""

    def __init__(self, channels, errors=None):
        self.channels = channels
        self.errors = errors or {}
        self.clock = NOW
        self.calls = []

    def video(self, video_id):
        return next(v for videos in self.channels.values() for v in videos if v.id == video_id)

    def youtube(self, endpoint, query, **params):
        self.calls.append((endpoint, query, params))
        request_id = f"req-{len(self.calls)}"
        if (endpoint, query) in self.errors:
            raise self.errors[(endpoint, query)]
        if endpoint == "channel":
            handle = query.lstrip("@").lower()
            if handle not in self.channels:
                return MediaResult({"state": "done", "results": []}, request_id)
            videos = self.channels[handle]
            listed = videos[:params.get("limit", 20)]
            return done({"id": f"UC{handle}", "name": handle.title(), "video_count": len(videos),
                         "videos": [{"pos": i + 1, "title": f"listed {v.id}",
                                     "url": f"https://www.youtube.com/watch?v={v.id}",
                                     "upload_date_relative": relative(self.clock - v.published),
                                     "view_count": "100"} for i, v in enumerate(listed)]}, request_id)
        video = self.video(query)
        if endpoint == "metadata":
            if video.unavailable:
                return MediaResult({"state": "done", "results": [{"metadata": {"statuses": {
                    "video_download": {"code": 11202, "tag": "UNAVAILABLE"}}}}]}, request_id)
            return done({"results": {
                "video_id": video.id, "title": f"title {video.id}", "uploader": "Uploader",
                "channel_id": "UCchan", "upload_date": video.published.strftime("%Y%m%d"),
                "duration": 0 if video.live else 600, "is_live": video.live,
                "is_transcript_available": bool(video.transcript or video.uploader),
                "generated_subtitle_languages": [video.transcript] if video.transcript else []}},
                request_id)
        language = params.get("language_code")
        body = {}
        if video.transcript and language in (None, video.transcript):
            body["auto_generated"] = {video.transcript: json3(f"auto {video.id}")}
        for lang, text in video.uploader.items():
            if language in (None, lang):
                body.setdefault("uploader_provided", {})[lang] = json3(text)
        return done(body, request_id)

    def count(self, endpoint, query=None):
        return sum(1 for e, q, _ in self.calls if e == endpoint and (query is None or q == query))


def daily(count, start=0, **kwargs):
    """`count` videos, one a day, the newest published 6 hours before NOW."""
    return [Video(start + i, NOW - timedelta(hours=6) - timedelta(days=i), **kwargs)
            for i in range(count)]


def source(key="chan", language=None):
    return Source(key, f"https://www.youtube.com/@{key}", f"@{key}", f"{key} name", language)


def ids(posts):
    return [p["postUrl"].rsplit("=", 1)[1] for p in posts]


class FetchTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw = Path(tmp.name) / "raw"
        self.out = Path(tmp.name) / "parsed"

    def fetch(self, client, n=None, src=None, now=NOW, **kwargs):
        return fetch_channel(client, src or source(), n, self.raw, now=now, **kwargs)

    def run_all(self, client, sources, n=None, now=NOW, **kwargs):
        return run_channels(client, sources, n, raw_root=self.raw, parsed_root=self.out,
                            now=now, **kwargs)

    def posts_file(self):
        return json.loads((self.out / "posts.json").read_text(encoding="utf-8"))


class SourceTests(FetchTestCase):
    def test_handles_from_link_forms(self):
        for link in ("https://www.youtube.com/@Name", "https://www.youtube.com/@Name/videos",
                     "youtube.com/@Name", "https://m.youtube.com/@Name?si=x"):
            self.assertEqual(handle_from_link(link), "@Name", link)
        for link in ("https://www.youtube.com/channel/UCabc", "https://www.youtube.com/c/Name",
                     "https://www.youtube.com/user/Name", "https://example.com/@Name"):
            self.assertIsNone(handle_from_link(link), link)

    def test_load_sources_marks_unusable_entries(self):
        path = self.raw.parent / "sources.json"
        path.write_text(json.dumps({"YT": [
            {"name": "One", "link": "https://www.youtube.com/@ChanOne", "language": "lt", "userSize": 5},
            {"link": "https://www.youtube.com/@chanone"},                       # duplicate key
            {"link": "https://www.youtube.com/channel/UCabcdef"},
            {"link": "https://www.youtube.com/@ChanTwo", "language": "LT"},
        ]}), encoding="utf-8")
        sources = load_sources(path)
        self.assertEqual([s.key for s in sources], ["chanone", "channel/ucabcdef", "chantwo"])
        self.assertEqual((sources[0].handle, sources[0].name, sources[0].language, sources[0].problem),
                         ("@ChanOne", "One", "lt", ""))
        self.assertIn("no @handle", sources[1].problem)
        self.assertIn("language", sources[2].problem)

        client = FakeYouTube({})
        results = self.run_all(client, sources[1:], n=5)
        self.assertEqual([r.status for r in results], ["unreadable", "unreadable"])
        self.assertEqual(client.calls, [])                  # no request is spent on them
        self.assertEqual(self.posts_file(), {"channel/ucabcdef": [], "chantwo": []})


class CountRunTests(FetchTestCase):
    def test_newest_n_videos(self):
        client = FakeYouTube({"chan": daily(30)})
        result = self.fetch(client, n=5)
        self.assertEqual(result.status, "ok", result.reason)
        self.assertEqual(client.calls[0], ("channel", "@chan", {"limit": 20}))
        self.assertEqual(ids(result.selected), [vid(i) for i in range(5)])
        self.assertEqual((client.count("metadata"), client.count("subtitles")), (5, 5))
        self.assertEqual(result.got, 5)
        self.assertEqual(result.scraped, 11)
        self.assertEqual(result.channel_id, "UCchan")
        self.assertEqual(result.origins, {"uploaderProvided": 0, "autoGenerated": 5})
        post = result.selected[0]
        self.assertEqual(list(post), ["author", "title", "content", "dateTime", "channelName", "postUrl"])
        self.assertEqual(post["title"], f"title {vid(0)}")
        self.assertEqual(post["content"], f"auto {vid(0)}")
        self.assertEqual(post["dateTime"], (NOW - timedelta(hours=6)).strftime("%Y-%m-%dT00:00:00Z"))
        self.assertEqual(post["channelName"], "chan name")

    def test_videos_without_transcript_dont_count_and_skip_subtitles(self):
        videos = daily(10)
        videos[0].transcript = videos[1].transcript = None
        client = FakeYouTube({"chan": videos})
        result = self.fetch(client, n=3)
        self.assertEqual(ids(result.selected), [vid(2), vid(3), vid(4)])
        self.assertEqual(client.count("metadata"), 5)
        self.assertEqual(client.count("subtitles"), 3)       # none for the two without a transcript
        self.assertEqual(result.no_transcript, 2)

    def test_listing_grows_until_the_count_is_reached(self):
        videos = daily(60, transcript=None)
        for v in videos[50:]:
            v.transcript = "lt"
        client = FakeYouTube({"chan": videos})
        result = self.fetch(client, n=5)
        self.assertEqual([p["limit"] for e, _, p in client.calls if e == "channel"], [20, 40, 80])
        self.assertEqual(ids(result.selected), [vid(i) for i in range(50, 55)])
        self.assertEqual(result.status, "ok", result.reason)

    def test_max_videos_caps_the_listing(self):
        client = FakeYouTube({"chan": daily(60, transcript=None)})
        result = self.fetch(client, n=5, max_videos=30)
        self.assertEqual([p["limit"] for e, _, p in client.calls if e == "channel"], [20, 30])
        self.assertEqual(result.status, "partial")
        self.assertIn("--max-videos", result.reason)

    def test_a_short_channel_is_partial(self):
        client = FakeYouTube({"chan": daily(3)})
        result = self.fetch(client, n=5)
        self.assertEqual(result.status, "partial")
        self.assertIn("only 3", result.reason)
        self.assertEqual(client.count("channel"), 1)          # the listing held every video


class DateRunTests(FetchTestCase):
    def test_every_video_since_the_start_day(self):
        videos = [Video(i, NOW - timedelta(days=2 * i + 0.5)) for i in range(30)]
        client = FakeYouTube({"chan": videos})
        since = NOW - timedelta(days=10)
        result = self.fetch(client, since=since)
        self.assertEqual(result.status, "ok", result.reason)
        self.assertEqual(client.calls[0][2], {"limit": 50})
        expected = [v.id for v in videos if v.published.date() >= since.date()]
        self.assertEqual(ids(result.selected), expected)
        self.assertEqual(result.got, len(expected))
        # Videos the listing's relative date already shows as older aren't requested.
        self.assertLessEqual(client.count("metadata"), len(expected) + 3)

    def test_count_and_date_together(self):
        client = FakeYouTube({"chan": daily(30)})
        result = self.fetch(client, n=3, since=NOW - timedelta(days=10))
        self.assertEqual(ids(result.selected), [vid(0), vid(1), vid(2)])
        result = self.fetch(client, n=20, since=NOW - timedelta(days=4))
        self.assertEqual(result.status, "ok", result.reason)
        self.assertEqual(len(result.selected), 5)     # 0..4 days ago, by upload day
        self.assertEqual(len(result.posts), 5)        # nothing older was fetched


class ReRunTests(FetchTestCase):
    def test_saved_transcripts_are_not_fetched_again(self):
        videos = daily(10)
        client = FakeYouTube({"chan": videos})
        self.run_all(client, [source()], n=3)
        self.assertEqual(len(client.calls), 7)

        client.calls.clear()
        results = self.run_all(client, [source()], n=3, now=NOW + timedelta(hours=1))
        self.assertEqual(client.calls, [])                    # the listing isn't due yet
        self.assertEqual(results[0].got, 3)

        results = self.run_all(client, [source()], n=3, now=NOW + timedelta(hours=7))
        self.assertEqual([c[0] for c in client.calls], ["channel"])   # re-listed; nothing new

        client.calls.clear()
        videos.insert(0, Video(99, NOW + timedelta(hours=8)))
        results = self.run_all(client, [source()], n=3, now=NOW + timedelta(hours=14))
        self.assertEqual([c[0] for c in client.calls], ["channel", "metadata", "subtitles"])
        self.assertEqual((results[0].added, results[0].total), (1, 4))   # posts.json grows
        self.assertEqual(ids(self.posts_file()["chan"]), [vid(99), vid(0), vid(1), vid(2)])

    def test_update_re_lists_now(self):
        client = FakeYouTube({"chan": daily(5)})
        self.fetch(client, n=2)
        client.calls.clear()
        self.fetch(client, n=2, now=NOW + timedelta(minutes=5), update=True)
        self.assertEqual([c[0] for c in client.calls], ["channel"])

    def test_a_bigger_count_only_fetches_the_missing_videos(self):
        client = FakeYouTube({"chan": daily(30)})
        self.fetch(client, n=3)
        client.calls.clear()
        result = self.fetch(client, n=5, now=NOW + timedelta(hours=1))
        self.assertEqual(client.count("channel"), 0)
        self.assertEqual({q for e, q, _ in client.calls}, {vid(3), vid(4)})
        self.assertEqual(len(result.selected), 5)

    def test_no_transcript_gets_one_retry_two_days_after_the_upload_day(self):
        fresh = Video(0, NOW - timedelta(hours=2), transcript=None)       # uploaded 2026-11-15
        client = FakeYouTube({"chan": [fresh] + daily(5, start=1)})
        self.fetch(client, n=1)
        self.assertEqual(client.count("metadata", fresh.id), 1)

        later_today = NOW + timedelta(hours=7)
        self.fetch(client, n=1, now=later_today, update=True)
        self.assertEqual(client.count("metadata", fresh.id), 1)          # not due yet

        fresh.transcript = "lt"                                          # captions appeared
        due = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)
        result = self.fetch(client, n=1, now=due, update=True)
        self.assertEqual(client.count("metadata", fresh.id), 2)
        self.assertEqual(ids(result.selected)[0], fresh.id)

    def test_a_retry_is_used_once_and_old_videos_get_none(self):
        fresh = Video(0, NOW - timedelta(hours=2), transcript=None)
        old = Video(1, NOW - timedelta(days=5), transcript=None)
        client = FakeYouTube({"chan": [fresh, old] + daily(5, start=2)})
        self.fetch(client, n=1)
        due = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)
        self.fetch(client, n=1, now=due, update=True)                    # the retry: still none
        self.fetch(client, n=1, now=due + timedelta(days=3), update=True)
        self.assertEqual(client.count("metadata", fresh.id), 2)
        self.assertEqual(client.count("metadata", old.id), 1)            # first seen too late to retry

        result = self.fetch(client, n=1, now=due + timedelta(days=4), retry_missing=True)
        self.assertEqual(client.count("metadata", fresh.id), 3)
        self.assertEqual(client.count("metadata", old.id), 2)
        self.assertEqual(result.no_transcript, 2)

    def test_unavailable_videos_are_remembered(self):
        videos = daily(5)
        videos[0].unavailable = True
        client = FakeYouTube({"chan": videos})
        result = self.fetch(client, n=2)
        self.assertEqual(ids(result.selected), [vid(1), vid(2)])
        self.assertEqual(result.unavailable, 1)
        self.fetch(client, n=2, now=NOW + timedelta(hours=7))
        self.assertEqual(client.count("metadata", vid(0)), 1)
        meta = json.loads((self.raw / "chan" / "meta.json").read_text(encoding="utf-8"))
        self.assertIn("11202", meta["unavailable"][vid(0)]["reason"])

        videos[0].unavailable = False
        result = self.fetch(client, n=2, now=NOW + timedelta(hours=8), retry_missing=True)
        self.assertEqual(ids(result.selected), [vid(0), vid(1)])

    def test_live_videos_are_skipped_and_not_remembered(self):
        videos = daily(5)
        videos[0].live = True
        client = FakeYouTube({"chan": videos})
        result = self.fetch(client, n=2)
        self.assertEqual(ids(result.selected), [vid(1), vid(2)])
        self.assertEqual(result.skipped_live, 1)
        self.assertEqual(client.count("subtitles", vid(0)), 0)

        videos[0].live = False
        result = self.fetch(client, n=2, now=NOW + timedelta(hours=1))
        self.assertEqual(ids(result.selected)[:2], [vid(0), vid(1)])
        self.assertEqual(client.count("metadata", vid(0)), 2)

    def test_cached_makes_no_requests_and_rebuilds_posts(self):
        client = FakeYouTube({"chan": daily(5)})
        self.run_all(client, [source()], n=3)
        (self.out / "posts.json").unlink()
        results = self.run_all(None, [source(), source("never")], n=3, cached=True)
        self.assertEqual(len(self.posts_file()["chan"]), 3)
        self.assertEqual(results[0].status, "ok", results[0].reason)
        self.assertEqual(results[1].status, "unreadable")
        results = self.run_all(None, [source()], n=4, cached=True)
        self.assertEqual(results[0].status, "partial")
        self.assertIn("not fetched yet", results[0].reason)

    def test_refresh_starts_over(self):
        client = FakeYouTube({"chan": daily(5)})
        self.run_all(client, [source()], n=3)
        results = self.run_all(client, [source()], n=1, refresh=True, now=NOW + timedelta(hours=1))
        self.assertEqual(results[0].total, 1)
        self.assertEqual(len(self.posts_file()["chan"]), 1)

    def test_a_changed_saved_file_is_fetched_again(self):
        client = FakeYouTube({"chan": daily(5)})
        self.fetch(client, n=2)
        (self.raw / "chan" / f"subtitles-{vid(1)}.json").write_text("{}", encoding="utf-8")
        client.calls.clear()
        result = self.fetch(client, n=2, now=NOW + timedelta(hours=1))
        self.assertEqual(client.calls, [("subtitles", vid(1), {"language_code": "lt"})])
        self.assertEqual(len(result.selected), 2)


class LanguageTests(FetchTestCase):
    def test_spoken_language_from_metadata_narrows_the_request(self):
        client = FakeYouTube({"chan": daily(2)})
        self.fetch(client, n=1)
        self.assertEqual(client.calls[-1], ("subtitles", vid(0), {"language_code": "lt"}))

    def test_source_language_with_no_track_asks_for_all_languages(self):
        client = FakeYouTube({"chan": daily(2, transcript="ru")})
        result = self.fetch(client, n=1, src=source(language="lt"))
        subtitle_calls = [p for e, _, p in client.calls if e == "subtitles"]
        self.assertEqual(subtitle_calls, [{"language_code": "lt"}, {}])
        self.assertEqual(result.selected[0]["content"], f"auto {vid(0)}")

    def test_uploader_track_in_the_spoken_language_is_preferred(self):
        client = FakeYouTube({"chan": [Video(0, NOW - timedelta(hours=6),
                                             uploader={"lt": "uploaded lt", "en": "uploaded en"})]})
        result = self.fetch(client, n=1)
        self.assertEqual(result.selected[0]["content"], "uploaded lt")
        self.assertEqual(result.origins, {"uploaderProvided": 1, "autoGenerated": 0})

    def test_a_rejected_language_stops_the_channel(self):
        videos = daily(5)
        errors = {("subtitles", v.id): ValidationError("400 VALIDATION_ERROR", status=400)
                  for v in videos}
        client = FakeYouTube({"chan": videos}, errors)
        result = self.fetch(client, n=3, src=source(language="xx"), workers=1)
        self.assertEqual(result.status, "partial")
        self.assertIn("language code rejected", result.reason)
        self.assertEqual(client.count("subtitles"), 1)


class RunTests(FetchTestCase):
    def test_one_bad_channel_does_not_stop_the_run(self):
        client = FakeYouTube({"good": daily(3)},
                             {("channel", "@bad"): WebApiError("503 UNAVAILABLE", status=503)})
        results = self.run_all(client, [source("bad"), source("good"), source("gone")], n=2)
        self.assertEqual([r.status for r in results], ["unreadable", "ok", "unreadable"])
        self.assertIn("couldn't list", results[0].reason)
        self.assertIn("no channel", results[2].reason)
        self.assertEqual(list(self.posts_file()), ["bad", "good", "gone"])
        report = json.loads((self.out / "_report.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(report[1]), sorted([
            "handle", "channelId", "requested", "since", "listed", "got", "added", "total",
            "noTranscript", "unavailable", "skippedLive", "transcriptOrigin", "scrapedThisRun",
            "failedThisRun", "status", "reason", "requestIds"]))

    def test_a_failed_video_makes_the_channel_partial(self):
        client = FakeYouTube({"chan": daily(5)},
                             {("metadata", vid(1)): WebApiError("500 FAILED", status=500)})
        result = self.fetch(client, n=3)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.failed, 1)
        self.assertEqual(ids(result.selected), [vid(0), vid(2), vid(3)])

    def test_401_stops_the_run_and_finished_channels_are_saved(self):
        client = FakeYouTube({"first": daily(3), "second": daily(3)},
                             {("channel", "@second"): AuthenticationError("401", status=401)})
        with self.assertRaises(AuthenticationError):
            self.run_all(client, [source("first"), source("second")], n=2, parallel=1)
        self.assertEqual(len(self.posts_file()["first"]), 2)
        self.assertNotIn("second", self.posts_file())


if __name__ == "__main__":
    unittest.main()
