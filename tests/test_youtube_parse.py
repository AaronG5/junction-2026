"""YouTube parser: listings, metadata and subtitle tracks.

The fixtures in tests/fixtures/youtube/ are real Oxylabs responses from 2026-11-15, trimmed
(a few cues, no formats or description). Other shapes are built inline.
"""
import json
import unittest
from datetime import timedelta
from pathlib import Path

from scrapers.youtube.parse import (Track, choose_track, parse_listing, parse_metadata,
                                    parse_subtitles, relative_min_age, status_reason, to_post,
                                    track_text, video_id_from_url)

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def subtitles(body):
    return {"state": "done", "results": [{"json": body, "metadata": {}}]}


def json3(*cues):
    return {"wireMagic": "pb3", "events": [{"tStartMs": i * 1000, "segs": [{"utf8": c}]}
                                           for i, c in enumerate(cues)]}


class ListingTests(unittest.TestCase):
    def test_real_listing(self):
        listing = parse_listing(fixture("channel.json"))
        self.assertTrue(listing.found)
        self.assertEqual(listing.channel_id, "UCeoRphKlraUAXobWli_pvQQ")
        self.assertEqual(listing.name, "Vaidas Žemaitis Lekstutis")
        self.assertEqual(listing.video_count, 682)
        self.assertEqual([v.video_id for v in listing.videos],
                         ["2aZ_aDxebwI", "oMmfc2ed0ac", "mhcV-nvCzIQ", "uvH18mgHGWM", "A-w2ljy36nI"])
        first = listing.videos[0]
        self.assertEqual((first.title, first.relative, first.position),
                         ("Taigi, kada baigsis karas Ukrainoje?", "1d ago", 1))

    def test_no_channel(self):
        self.assertFalse(parse_listing({"state": "done", "results": []}).found)
        self.assertFalse(parse_listing(subtitles({})).found)

    def test_relative_dates_give_the_least_possible_age(self):
        day = timedelta(days=1)
        cases = {"1d ago": day, "2w ago": 14 * day, "4w ago": 28 * day, "5mo ago": 140 * day,
                 "1y ago": 365 * day, "3h ago": timedelta(hours=3), "10m ago": timedelta(minutes=10),
                 "2 months ago": 56 * day, "Streamed 3 days ago": 3 * day}
        for text, age in cases.items():
            self.assertEqual(relative_min_age(text), age, text)
        self.assertIsNone(relative_min_age(""))
        self.assertIsNone(relative_min_age("yesterday"))

    def test_video_ids_from_links(self):
        for url in ("https://www.youtube.com/watch?v=2aZ_aDxebwI", "https://youtu.be/2aZ_aDxebwI",
                    "https://www.youtube.com/shorts/2aZ_aDxebwI", "https://m.youtube.com/live/2aZ_aDxebwI",
                    "https://www.youtube.com/watch?v=2aZ_aDxebwI&t=30s"):
            self.assertEqual(video_id_from_url(url), "2aZ_aDxebwI", url)
        self.assertIsNone(video_id_from_url("https://www.youtube.com/@VaidasZemaitisLekstutis"))
        self.assertIsNone(video_id_from_url("https://www.youtube.com/watch?v=short"))


class MetadataTests(unittest.TestCase):
    def test_real_metadata(self):
        meta = parse_metadata(fixture("metadata.json"))
        self.assertEqual(meta.video_id, "2aZ_aDxebwI")
        self.assertEqual(meta.title, "Taigi, kada baigsis karas Ukrainoje?")
        self.assertEqual(meta.author, "Vaidas Žemaitis Lekstutis")
        self.assertEqual(meta.channel_id, "UCeoRphKlraUAXobWli_pvQQ")
        self.assertEqual(meta.date_time, "2026-11-13T00:00:00Z")      # the API gives only the day
        self.assertEqual(meta.duration, 1220)
        self.assertFalse(meta.upcoming_or_live)
        self.assertIs(meta.transcript_available, True)
        self.assertEqual(meta.spoken_languages, ("lt",))

    def test_unavailable_video(self):
        data = {"state": "done", "results": [{"metadata": {"statuses": {
            "http_request": {"code": 200}, "video_download": {"code": 11202, "tag": "UNAVAILABLE"}}}}]}
        self.assertIsNone(parse_metadata(data))
        self.assertEqual(status_reason(data), "video_download 11202 UNAVAILABLE")
        self.assertIsNone(parse_metadata({"state": "done", "results": []}))

    def test_live_and_upcoming(self):
        base = fixture("metadata.json")
        base["results"][0]["json"]["results"]["is_live"] = True
        self.assertTrue(parse_metadata(base).upcoming_or_live)
        base["results"][0]["json"]["results"].update(is_live=False, duration=0)
        self.assertTrue(parse_metadata(base).upcoming_or_live)


class SubtitleTests(unittest.TestCase):
    def test_real_auto_generated_track(self):
        tracks = parse_subtitles(fixture("subtitles-auto.json"))
        self.assertEqual([(t.origin, t.language) for t in tracks], [("auto_generated", "lt")])
        text = tracks[0].text
        self.assertTrue(text.startswith("Sveiki gyvi. Pakalbėsim apie Ukrainos karą. Ir Ukrainos "
                                        "karą, pabaigą, kurių laukia bemaž pusė pasaulio. Nes tai"), text)
        self.assertNotIn("\n", text)
        self.assertNotIn("  ", text)

    def test_cue_joining(self):
        track = json3("First line\nsecond line", "\n", "rolling", "rolling", "Tom &amp; Jerry",
                      "  [Music]  ", "")
        self.assertEqual(track_text(track),
                         "First line second line rolling Tom & Jerry [Music]")
        self.assertEqual(track_text([{"text": "a"}, {"text": "b"}]), "a b")
        self.assertEqual(track_text("not a track"), "")

    def test_empty_response_means_no_transcript(self):
        self.assertEqual(parse_subtitles(subtitles({})), [])
        self.assertEqual(parse_subtitles({"state": "done", "results": []}), [])
        self.assertEqual(parse_subtitles(subtitles({"auto_generated": {"lt": json3("", "\n")}})), [])

    def test_unknown_origins_are_ignored(self):
        body = {"translated": {"en": json3("machine translation")},
                "auto_generated": {"lt": json3("originalas")}}
        self.assertEqual([t.text for t in parse_subtitles(subtitles(body))], ["originalas"])


class TrackChoiceTests(unittest.TestCase):
    up_lt, up_en = Track("uploader_provided", "lt", "u-lt"), Track("uploader_provided", "en", "u-en")
    auto_lt, auto_ru = Track("auto_generated", "lt", "a-lt"), Track("auto_generated", "ru", "a-ru")

    def test_uploader_track_in_the_spoken_language_wins(self):
        self.assertEqual(choose_track([self.up_en, self.up_lt, self.auto_lt]), self.up_lt)

    def test_auto_generated_beats_an_uploader_track_in_another_language(self):
        self.assertEqual(choose_track([self.up_en, self.auto_lt]), self.auto_lt)

    def test_source_language_picks_among_auto_tracks(self):
        self.assertEqual(choose_track([self.auto_ru, self.auto_lt], "lt"), self.auto_lt)
        self.assertEqual(choose_track([self.auto_ru, self.auto_lt]), self.auto_ru)

    def test_without_auto_tracks(self):
        self.assertEqual(choose_track([self.up_en, self.up_lt], "lt"), self.up_lt)
        self.assertEqual(choose_track([self.up_en, self.up_lt]), self.up_en)
        self.assertEqual(choose_track([Track("uploader_provided", "lt-LT", "x")], "lt").text, "x")
        self.assertIsNone(choose_track([]))


class PostTests(unittest.TestCase):
    def test_the_six_fields(self):
        meta = parse_metadata(fixture("metadata.json"))
        track = parse_subtitles(fixture("subtitles-auto.json"))[0]
        post = to_post(meta, track, "Vaidas Žemaitis Lekstutis")
        self.assertEqual(list(post), ["author", "title", "content", "dateTime", "channelName", "postUrl"])
        self.assertEqual(post["title"], "Taigi, kada baigsis karas Ukrainoje?")
        self.assertEqual(post["dateTime"], "2026-11-13T00:00:00Z")
        self.assertEqual(post["postUrl"], "https://www.youtube.com/watch?v=2aZ_aDxebwI")
        self.assertEqual(post["content"], track.text)


if __name__ == "__main__":
    unittest.main()
