# Oxylabs Web API and YouTube transcript scraping

How `scrapers/youtube/` collects the transcripts of recent videos from the YouTube channels in `data/positives/youtube_sources.json`, and why it works the way it does. It uses the same Oxylabs client, conventions and secrets rules as the Telegram and Facebook scrapers (`docs/oxylabs-telegram-scrape.md`, `docs/oxylabs-facebook-scrape.md`). Read this whole file before changing the code.

## Overview

- Per channel: list its newest videos, fetch each video's metadata and transcript (the subtitle track YouTube already has), and parse them into the same JSON fields as Telegram and Facebook, plus the video's `title`. A video's transcript is its `content`.
- Everything goes through the Oxylabs Web API's dedicated YouTube endpoints. Nothing calls YouTube directly from this machine.
- Only existing transcripts are collected. No audio or video is downloaded, and nothing is transcribed locally.

## Source of truth for the API

- Read these before changing the client or the request flow, and follow them where this file is less exact:
  - <https://developers.oxylabs.io/api-reference/dedicated-scrapers/media-scrapers.md>: the YouTube endpoints and their parameters
  - <https://developers.oxylabs.io/products/web-api/scrape/dedicated-scrapers.md>: how dedicated scrapers work
  - <https://developers.oxylabs.io/products/web-api/for-agents.md>: auth, errors, retries (implemented in `scrapers/oxylabs.py`)
- If they don't load, stop and tell the user. Don't reconstruct the API from memory.
- It's the same product and key as the other scrapers: base URL `https://webapi.oxylabs.io`, `Authorization: Bearer` with `OXYLABS_WEB_API_KEY`.
- **Don't use the older Web Scraper API.** It's `realtime.oxylabs.io/v1/queries` with `"source": "youtube_transcript"`, Basic auth and `OXYLABS_USERNAME`/`OXYLABS_PASSWORD`. That's a different product with a different key. `backend/veritas/collectors.py` uses it for "Analyze a link"; the scrapers don't. Most older examples online (and model memory) describe that one.
- `GET /v1/scrapers` lists the endpoints this key can use, and `OPTIONS` on an endpoint returns its parameters. On 2026-11-15 the key had all the YouTube ones, sync and async.

### The three endpoints, as checked on 2026-11-15

The parsed data is in `results[0].json` of the usual response envelope. Checked against real responses for `@VaidasZemaitisLekstutis`.

- **`POST /v1/scrape/media/youtube/channel`**
  - `query`: the handle (`@name`, the `@` optional). `UC…` channel ids aren't accepted.
  - `limit`: videos to return, default 20. `limit: 100` worked in one request (3.6 s); higher wasn't tried.
  - `output`: must be `["json"]`. The default is `["html"]`.
  - Returns `id` (`UC…`), `name`, `video_count`, `subscriber_count` (`"11.6K"`), `country`, `joined_date`, `links`, and `videos[]` with `pos`, `title`, `url` (`https://www.youtube.com/watch?v=<id>`), `upload_date_relative` (`"1d ago"`, `"2w ago"`, `"5mo ago"`) and `view_count`.
  - It reads the channel's **Videos tab**, so Shorts and live streams aren't listed. No pagination: a bigger `limit` lists again from the newest.
- **`POST /v1/scrape/media/youtube/metadata`**
  - `query`: a video id.
  - `results[0].json.results` holds `video_id`, `title`, `uploader`, `uploader_id` (`@…`), `channel_id`, `upload_date` (`"20261113"`), `duration` (seconds), `is_live`, `is_transcript_available`, `generated_subtitle_languages` (`["lt"]`: the spoken language), `description`, `tags`, `categories`, counts, `formats` and `thumbnails`.
  - **`upload_date` is only a day, with no time**, and no exact timestamp field exists. It's the UTC day.
  - A deleted, private or restricted video comes back `done` with no fields and a status code: `11202` (unavailable) or `11206` (age-restricted), per the reference. Not seen yet in a real response; `status_reason()` reads any non-success code under `results[0].metadata.statuses`.
- **`POST /v1/scrape/media/youtube/subtitles`**
  - `query`: a video id. `language_code`: lowercase, e.g. `lt`; an unknown code is a 400. `subtitle_origin`: `uploader_provided` or `auto_generated`. Omitting either returns every track.
  - Returns `{"auto_generated": {"lt": <track>}, "uploader_provided": {"<lang>": <track>}}`. Each track is in YouTube's own **json3** format: `events[]`, each with `tStartMs`, `dDurationMs` and `segs[]` of `{"utf8": "word", "tOffsetMs": …}`. Events with `aAppend: 1` hold only a line break.
  - No auto-translations came back with all languages asked for. A valid language with no track returns `{}`.
  - About 360 KB per 20-minute video for one track.
- `OPTIONS` shows that all three also accept `location`, `device`, and `json.prompt`/`json.schema`, which the reference didn't list. The scraper doesn't use them.

## Shared rules

The same as the Telegram scraper. In short:

- Python 3.10+, run with `uv run`. No `pip`, `venv` or `requirements.txt`.
- Stdlib only, no new dependencies: responses are JSON, so `json` is enough.
  - Don't add `youtube-transcript-api`, `yt-dlp`, `pytube` or similar. They call YouTube directly from this machine, which is what Oxylabs is for.
- Tests: stdlib `unittest`, with a fake client or a mocked transport. No real network calls in tests.
- **Secrets:**
  - The key is `OXYLABS_WEB_API_KEY`: the process environment first, then `.env`, read via `load_api_key()`. If it's missing, the CLI stops and says so.
  - Never print it, log it, or put it in errors, fixtures, saved responses or replies.
  - `.env` is gitignored. `.env.example` only ever holds a placeholder.
- One bad channel never stops the run. On a 401, or a 429 that outlasts the client's retries, the whole run stops; finished channels are still saved.

### Client

- `WebApiClient.youtube(endpoint, query, *, limit, language_code, subtitle_origin, location)` in `scrapers/oxylabs.py` posts to `/v1/scrape/media/youtube/<endpoint>` through `_post`. That gives it the same auth, 150 s timeout, retries, error mapping and request-id logging as `search` and `scrape`. It returns a `MediaResult` (the whole response as `data`, plus `request_id` and `json`).
- Only `channel`, `subtitles` and `metadata` are allowed. `channel` always sends `output: ["json"]`.
- Input is checked before sending, so a bad value never costs a request: a video id matches `^[A-Za-z0-9_-]{11}$`, a handle is a single `@name` token, and `language_code` is lowercase. Each parameter is only accepted by the endpoint it belongs to.
- Sync mode only. No async, batch, `callback_url`, `storage` or `download`.

## Layout

```text
scrapers/
  oxylabs.py            # + youtube(endpoint, query, ...) -> MediaResult
  youtube/
    __init__.py
    __main__.py         # CLI: python -m scrapers.youtube
    fetch.py            # sources -> listing -> metadata -> subtitles; cache, retries, posts.json
    parse.py            # listing, metadata and json3 tracks -> the six fields
tests/
  test_oxylabs.py       # + the YouTube endpoint
  test_youtube_fetch.py # fake client in the real response shapes
  test_youtube_parse.py
  fixtures/youtube/     # real responses from 2026-11-15, trimmed: channel.json, metadata.json, subtitles-auto.json
data/
  positives/youtube_sources.json          # input (committed)
  raw/youtube/<key>/channel-001.json      # each listing response, numbered in the order saved (gitignored)
  raw/youtube/<key>/metadata-<id>.json    # one per video
  raw/youtube/<key>/subtitles-<id>.json   # one per video with a possible transcript
  raw/youtube/<key>/meta.json             # channel id, request ids, sha256s, remembered videos
  parsed/youtube/posts.json               # {"<key>": [videos]}
  parsed/youtube/_report.json             # per-channel run summary
```

## Input

`data/positives/youtube_sources.json`, shaped like the Telegram file (`telegram_sources.json`), with one optional extra field:

```json
{
  "YT": [
    {"name": "Vaidas Žemaitis Lekstutis", "link": "https://www.youtube.com/@VaidasZemaitisLekstutis", "language": "lt", "userSize": 11600},
    {"link": "https://www.youtube.com/@AnotherChannel"}
  ]
}
```

| Field      | Required | Use                                                                                                                           |
| ---------- | -------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `link`     | yes      | The channel's `@handle` link                                                                                                  |
| `name`     | no       | `channelName`. Without it, the channel's display name is used                                                                 |
| `language` | no       | The language the channel speaks, as a lowercase code (`lt`, `ru`, `en`). Picks the transcript language (see Which transcript) |
| `userSize` | no       | Subscriber count, for people reading the file. The scraper doesn't use it                                                     |

Rules:

- **The handle** is the `@…` path segment of `link`: `https://www.youtube.com/@ExampleChannel/videos` → `@ExampleChannel`. `youtube.com/@x` without a scheme and `m.youtube.com` links work too. YouTube handles ignore case.
- **The key** is the handle without `@`, lowercased (`vaidaszemaitislekstutis`). It's used for the cache folder, the keys in `posts.json` and `_report.json`, and `--channels`.
- A link without a handle (`/channel/UC…`, `/c/<name>`, `/user/<name>`) can't be listed, because the channel endpoint doesn't take channel ids. The channel is `unreadable` with the reason "link has no @handle; replace it with the channel's @handle link". The handle isn't guessed.
- **A bad `language`:**
  - One that isn't a lowercase code (`^[a-z]{2,3}(-[a-z0-9]+)?$`) is caught before any request: the channel is `unreadable` with the reason.
  - If the API rejects the code (a 400 on a transcript request), that channel's transcript requests stop, it's `partial` with "language code rejected: fix it in youtube_sources.json", and the run carries on with the next channel.

## How it works

Per channel, in the order of `youtube_sources.json`, `--parallel` channels at a time (default 3):

1. **List the newest videos** with `channel`, unless a saved listing is less than `--recheck-hours` old. This also gives the channel id (`UC…`) and display name, recorded in `meta.json`.
2. **Walk the listed videos newest first**, until the count or the start date is reached. Skip:
   - videos whose transcript is already saved (they count as found)
   - videos remembered as unavailable or as having no transcript (see Running it again)
   - in a date run, videos whose listing date already shows they're older than the start date, with 2 days of slack (`"2w ago"` is at least 14 days old). They aren't requested at all.
3. **Fetch each remaining video**, `--workers` at a time per channel (default 4):
   1. `metadata`, for the title and upload day. The listing has only relative dates, so this is needed for every video.
      - A live stream or upcoming premiere (`is_live`, or `duration` 0) is skipped and not remembered, so a later run picks it up once it's finished.
      - A video older than the start date stops there.
      - `is_transcript_available: false` with no subtitle languages: the video has no transcript, and **no `subtitles` request is made**.
   2. `subtitles`, with a `language_code` (see Which transcript).
4. **Fetch more of the listing if needed.** If the walk ran out of listed videos before the count or the start date, and the listing came back full, list again with double the `limit`, up to `--max-videos` (default 200).
5. **Parse**, filter by date, and cap at N.

If every request in a batch fails, the channel stops there, so an outage doesn't burn through the whole listing.

### How far back to go

- **By count:** `--videos N` returns the N newest videos that have a transcript.
  - Without `--days` or `--since`, the default comes from `YOUTUBE_VIDEOS_PER_CHANNEL` in the environment or `.env`, else 10.
  - The default is lower than Telegram's 20 because every video costs two requests, and one transcript can be as long as hundreds of posts.
  - The first listing asks for `max(20, 2 × N)` videos, because videos without a transcript don't count.
- **By date:** `--days D` or `--since 2026-10-01` returns every video uploaded on or after that day. **Dates compare by day**, because the API gives only the upload day. The first listing asks for 50 videos.
- **Both:** `--days 30 --videos 100` stops at whichever comes first.
- **Status:**
  - `ok`: the count or the date was reached, or the listing held all of the channel's videos. A readable channel with nothing in range is `ok` with 0 videos.
  - `partial`: `--max-videos` stopped it early, a count run found fewer than N videos with a transcript, a request failed, or the language code was rejected.
  - `unreadable`: the link has no handle, the `language` is malformed, the channel endpoint returned no channel, or listing failed with nothing saved.

### Which transcript

- **The request:** `language_code` is the source's `language`, else the spoken language from metadata (`generated_subtitle_languages`) when there's exactly one. That returns at most two tracks, one per origin, so the response stays small.
  - If it returns `{}`, the video may be in another language. One more request is made without `language_code`.
  - With no language from either, the request asks for every track.
- **Picking one track:**
  1. If there's an auto-generated track, its language is the spoken language (the source's `language` if there's an auto track in it). Use the uploader-provided track in that language if there is one (it has corrections), else the auto-generated track.
  2. With no auto-generated track, use the uploader-provided track in the source's `language`, else the first one listed.
  3. Origins other than `uploader_provided` and `auto_generated` are ignored, so a machine translation is never used.
- No usable track (`{}`, or only empty tracks) means the video has no transcript.
- Nothing is translated.

## Raw responses

- Every response is saved under `data/raw/youtube/<key>/` as received, as UTF-8 JSON:
  - `channel-NNN.json`: each listing, numbered in the order saved. Earlier ones are kept, because a video can drop off a later, shorter listing.
  - `metadata-<video id>.json` and `subtitles-<video id>.json`. Video ids are case-sensitive, so file names keep the id as YouTube gives it.
  - `empty-channel.json`: a listing that had no channel, kept for inspection. It isn't a cached listing.
  - Metadata for live videos isn't saved; neither is the response for an unavailable video (its reason and request id go in `meta.json`).
- `meta.json` records:
  - the channel id and display name
  - for each file: its request id, sha256 and save time; for listings, the `limit` and the video ids; for subtitles, the `languageCode` asked for
  - `noTranscript`: video id → when it was first seen, its upload day, why, and whether its one retry has been used
  - `unavailable`: video id → when it was first seen, the reason (the status code) and the request id
- A saved file whose sha256 no longer matches `meta.json`, or that's missing, is dropped and fetched again.
- Size: about 190 KB per video with one track (1.9 MB for the first 10 videos). `data/raw/` is gitignored. Don't commit scraped output unless the user asks.

## Running it again

Run the same command again to pick up new videos:

- **A channel's listing is re-checked once it's 6 hours old** (`--recheck-hours`). New videos in it are fetched. A run right after another makes no requests, and a daily run lists every channel once.
- **Nothing saved is fetched again.** Saved metadata and transcripts are re-parsed from disk, and a bigger `--videos` only fetches the videos still missing.
- **No transcript yet:** auto-generated captions can appear some hours after upload, so a video with no transcript gets one retry.
  - The retry happens on the first run at least two days after its upload day (from 00:00 UTC on the second day after). The API gives only the day, so that's always at least 24 hours after publishing.
  - A video that's already past that when first seen gets no retry.
  - After that, it's remembered as having none and skipped until `--retry-missing`.
  - So a video costs at most one extra round of requests, and a channel YouTube can't auto-caption doesn't burn requests on every re-check.
- **Unavailable** (deleted, private, age-restricted, members-only): remembered and skipped until `--retry-missing`.
- An API error isn't a definite answer, so it isn't remembered, and the next run tries again.
- `--update` re-lists every channel now, however recently it was checked.
- `--cached` makes no requests and rebuilds `posts.json` from the saved responses, which is useful after a parser change. It works without an API key.
- `--refresh` throws a channel's saved files and its list in `posts.json` away and starts over.
- `--update`, `--cached` and `--refresh` can't be combined.

## Parsing

All videos go into `data/parsed/youtube/posts.json`, newest first, UTF-8 with `ensure_ascii=False`. There's one key per channel, in the order of `youtube_sources.json`.

- **The file grows across runs**, like Facebook's. A channel's list is every saved video with a transcript, plus the videos from earlier runs (matched by `postUrl`; a re-parsed video replaces its earlier copy). It keeps them even once they're older than `--days`/`--since`.
- A run over some channels (`--channels`) replaces only those entries in both files and keeps the rest.
- An unreadable channel gets an empty array.
- The file is written once, at the end of a run, and atomically.

The fields are Telegram's and Facebook's five, plus `title`:

```json
{
   "vaidaszemaitislekstutis": [
      {
         "author": "Vaidas Žemaitis Lekstutis",
         "title": "Taigi, kada baigsis karas Ukrainoje?",
         "content": "Sveiki gyvi. Pakalbėsim apie Ukrainos karą. Ir Ukrainos karą, pabaigą, ...",
         "dateTime": "2026-11-13T00:00:00Z",
         "channelName": "Vaidas Žemaitis Lekstutis",
         "postUrl": "https://www.youtube.com/watch?v=2aZ_aDxebwI"
      }
   ]
}
```

| Field         | Source                                                                                                  |
| ------------- | ------------------------------------------------------------------------------------------------------- |
| `author`      | `uploader` from the metadata (the channel's display name), else `channelName`                           |
| `title`       | `title` from the metadata, else the listing's title. Not translated or cleaned                          |
| `content`     | The chosen transcript track's text (see the rules below)                                                |
| `dateTime`    | **Midnight UTC of `upload_date`**: the API gives only the day. If it ever adds a timestamp, that's used |
| `channelName` | The `name` from `youtube_sources.json`, else the channel's display name                                 |
| `postUrl`     | `https://www.youtube.com/watch?v=<video id>`                                                            |

Rules for `content`:

- Each json3 event's `segs[].utf8` are joined into one cue. The words carry their own leading spaces.
- Cues are joined in time order with single spaces. Line breaks inside a cue become spaces, HTML entities are unescaped, runs of whitespace are collapsed, and the text is trimmed. The `aAppend` line-break events drop out this way.
- A cue whose text is exactly the previous cue's text is dropped (rolling captions).
- Sound tags such as `[atsidūsta]` or `[Music]` are kept as they are. Nothing is translated or cleaned in any other way.
- Timings aren't kept in `posts.json`. The raw responses have them.

Other rules:

- Videos with no transcript are left out of `posts.json` and don't count towards `--videos N`. The title or description is never put in `content` in their place: `content` is what was said.
- Deduplicate by video id.
- **`external_id`:**
  - A video's `external_id` is its video id exactly as YouTube gives it (`2aZ_aDxebwI`). Video ids are case-sensitive, so unlike Telegram it's never lowercased.
  - The account's `external_id` is the channel id (`UC…`, in `_report.json` as `channelId`). It stays the same when a channel changes its handle.
  - The YouTube row in `docs/data-architecture.md` is added separately later.
- Output exactly these six fields. Don't add others without asking.
- **Size:** the first 10 videos (8,000 to 27,000 characters each) came to 168 KB. GitHub warns at 50 MB and rejects files over 100 MB, so don't commit `posts.json` once it grows.

`data/parsed/youtube/_report.json` has one entry per channel:

- `handle` (the key) and `channelId`
- `requested` (null for a date-only run) and `since`
- `listed`: videos known from the channel's listings
- `got`: videos in range with a transcript this run (at most N)
- `added`: new to `posts.json`; `total`: in `posts.json` now
- `noTranscript`, `unavailable` and `skippedLive`, among the videos walked this run
- `transcriptOrigin`: `{"uploaderProvided": n, "autoGenerated": n}` for the `got` videos
- `scrapedThisRun`: requests made; `failedThisRun`: videos whose requests failed
- `status` (`ok`, `partial`, `unreadable`) and `reason`
- `requestIds`: this run's Oxylabs request ids

## Commands

```bash
uv run python -m scrapers.youtube                          # 10 newest videos with a transcript per channel (YOUTUBE_VIDEOS_PER_CHANNEL)
uv run python -m scrapers.youtube --videos 30
uv run python -m scrapers.youtube --days 30                # every video from the last 30 days
uv run python -m scrapers.youtube --since 2026-10-01 --videos 100
uv run python -m scrapers.youtube --days 30 --update       # re-list every channel now
uv run python -m scrapers.youtube --cached                 # no requests: rebuild posts.json
uv run python -m scrapers.youtube --channels vaidaszemaitislekstutis
uv run python -m scrapers.youtube --retry-missing          # retry videos with no transcript or unavailable
uv run python -m scrapers.youtube --refresh                # start the listed channels over
uv run python -m scrapers.youtube --parallel 2 --workers 2 # fewer requests at once (on 429s)
uv run python -m unittest tests.test_oxylabs tests.test_youtube_fetch tests.test_youtube_parse
```

- Any 400 other than a rejected source language is a bug in the request. It isn't retried; the video counts under `failedThisRun` and the run carries on.
- **Cost and speed** (first run, 2026-11-15, one channel, 10 videos): 21 requests (1 listing, 10 metadata, 10 subtitles) in 41 seconds with 4 workers.
  - A listing takes about 3 s, metadata 1 to 4 s, subtitles 3 to 20 s.
  - Per channel and run: one listing request when it's due (more only if the listing has to grow), then one `metadata` and usually one `subtitles` request per new video.
  - A re-run within `--recheck-hours` makes no requests.

## Changing it

- If a response shape changes, the parser finds nothing: listings come out `unreadable` ("no channel"), or every video comes out `noTranscript`. Make one real `channel`, `metadata` and `subtitles` call, compare with `tests/fixtures/youtube/`, fix `parse.py`, replace the fixtures with trimmed new responses, then run `--cached` to rebuild `posts.json` without new requests.
- Keep the tests free of network calls; `FakeYouTube` in `tests/test_youtube_fetch.py` serves the real shapes.
- Don't stop to ask for routine work. Stop only for a missing key, a 401, an Oxylabs page that won't load, or `uv` not being installed.

## Limits and next steps

- **Only the Videos tab.** Shorts and live-stream recordings aren't listed by the channel endpoint, so they aren't collected.
- **Only transcripts YouTube already has.** Auto-generated captions exist only where the channel left them on and YouTube's speech recognition supports the language. Lithuanian works: all 10 first videos had auto-generated `lt` tracks. Watch how many videos come out `noTranscript`. Covering those would mean downloading audio and transcribing it locally, which isn't planned.
- **Auto-generated transcripts are rough.** The Lithuanian ones had punctuation, but misheard words ("Sveiki, gėjui" for "Sveiki gyvi") and no speaker turns. Later steps that split text into sentences should cope with long, loosely punctuated text.
- **`dateTime` is the day only** (midnight UTC). Ordering within a day and propagation timing finer than a day aren't possible from this source.
- Timings are dropped from `posts.json`. A later feature can link a claim to its moment in the video (`&t=<seconds>s`) from the raw responses.
- Descriptions, comments and community posts aren't collected. The description is in the saved `metadata` response; adding it as a field needs asking first.
- The `subtitles` and `metadata` endpoints also have a batch mode (`/v1/async/scrape/batch/…`), which could cut overhead on a big backfill.
- All three endpoints accept `location` (per `OPTIONS`), but there's no region fallback like Telegram's yet. A video blocked in the exit region comes out unavailable.
