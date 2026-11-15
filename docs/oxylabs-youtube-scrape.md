# Oxylabs Web API and YouTube transcript scraping

Instructions for coding agents building `scrapers/youtube/`, which collects the transcripts of recent videos from the YouTube channels in `data/positives/youtube_sources.json`. It uses the same Oxylabs client, conventions and secrets rules as the Telegram and Facebook scrapers (`docs/oxylabs-telegram-scrape.md`, `docs/oxylabs-facebook-scrape.md`). Read this whole file and the Telegram one before writing code.

## Overview

- Per channel: list its newest videos, fetch each video's transcript (the subtitle track YouTube already has), and parse it into the same JSON fields as Telegram and Facebook, plus the video's `title`. A video's transcript is its `content`.
- Everything goes through the Oxylabs Web API's dedicated YouTube endpoints. Nothing calls YouTube directly from this machine.
- Only existing transcripts are collected. No audio or video is downloaded, and nothing is transcribed locally.

## Source of truth for the API

- Read these before writing code, and follow them where this file is less exact:
  - <https://developers.oxylabs.io/api-reference/dedicated-scrapers/media-scrapers.md>: the YouTube endpoints and their parameters
  - <https://developers.oxylabs.io/products/web-api/scrape/dedicated-scrapers.md>: how dedicated scrapers work
  - <https://developers.oxylabs.io/products/web-api/for-agents.md>: auth, errors, retries (already implemented in `scrapers/oxylabs.py`)
- If they don't load, stop and tell the user. Don't reconstruct the API from memory.
- It's the same product and key as the other scrapers: base URL `https://webapi.oxylabs.io`, `Authorization: Bearer` with `OXYLABS_WEB_API_KEY`.
- **Don't use the older Web Scraper API.** It's `realtime.oxylabs.io/v1/queries` with `"source": "youtube_transcript"`, Basic auth and `OXYLABS_USERNAME`/`OXYLABS_PASSWORD`. That's a different product with a different key. `backend/veritas/collectors.py` uses it for "Analyze a link"; the scrapers don't. Most older examples online (and model memory) describe that one.
- `GET /v1/scrapers` lists the endpoints this key can use, and `OPTIONS` on an endpoint returns its parameters. Check the YouTube ones there during the smoke test.

What the reference said on 2026-11-15, for the three endpoints this scraper uses:

- **`POST /v1/scrape/media/youtube/channel`**
  - `query`: the handle (`@name`, the `@` optional). `UC…` channel ids aren't accepted.
  - `limit`: videos to return. Default 20; `0` returns channel details only.
  - `output`: `["json"]` for parsed data. The default is `["html"]`.
  - Returns channel details (name, id, subscriber, video and view counts, country, join date, links) and its latest videos. No pagination is documented.
- **`POST /v1/scrape/media/youtube/subtitles`**
  - `query`: a video id, not a URL.
  - `language_code`: lowercase, e.g. `lt`. An unknown code is a 400. Omit it for all languages.
  - `subtitle_origin`: `uploader_provided` or `auto_generated`. Omit it for both.
  - Returns the video's subtitle tracks with text and timings, grouped by origin and language. A valid language with no track returns `{}`, not an error.
- **`POST /v1/scrape/media/youtube/metadata`**
  - `query`: a video id. `output`: `["json"]` (the default).
  - Returns the title, channel, description, duration, counts, upload date, chapters and available subtitle languages.
  - A deleted, private or restricted video comes back `done` with empty results and a status code: `11202` (unavailable) or `11206` (age-restricted).

The parsed data is in `results[0].json` of the usual response envelope. The reference doesn't show the field names inside it, the largest `limit` the channel endpoint accepts, or the shape of a subtitle track. The smoke test finds them (see Workflow). Write what you find into this file, with the date, the way the other two docs record what they checked.

## Shared rules

The same as the Telegram scraper. In short:

- Python 3.10+, run with `uv run`. No `pip`, `venv` or `requirements.txt`.
- Stdlib only, no new dependencies: responses are JSON, so `json` is enough.
  - Don't add `youtube-transcript-api`, `yt-dlp`, `pytube` or similar. They call YouTube directly from this machine, which is what Oxylabs is for.
- Tests: stdlib `unittest`, with a mocked transport. No real network calls in tests.
- **Secrets:**
  - The key is `OXYLABS_WEB_API_KEY`: the process environment first, then `.env`, read via the existing `load_api_key()`. If it's missing, stop and ask the user.
  - Never print it, log it, or put it in errors, fixtures, saved responses or replies.
  - `.env` is gitignored. `.env.example` only ever holds a placeholder.
- One bad channel never stops the run. On a 401, or a 429 that outlasts the client's retries, the whole run stops; finished channels are still saved.

### Client

- Add one method to `WebApiClient` in `scrapers/oxylabs.py`, e.g. `youtube(endpoint, payload)`, that posts to `/v1/scrape/media/youtube/<endpoint>` through `_post`. That gives it the same auth, 150 s timeout, retries, error mapping and request-id logging as `search` and `scrape`.
- Allow only `channel`, `subtitles` and `metadata`.
- Validate input before sending, so a bad value never costs a request: a video id matches `^[A-Za-z0-9_-]{11}$`, and a handle is a non-empty `@name`.
- Sync mode only. Don't use async, batch, `callback_url` or `storage`, and don't use the `download` endpoint.

## Layout

```text
scrapers/
  oxylabs.py            # + youtube(endpoint, payload)
  youtube/
    __init__.py
    __main__.py         # CLI: python -m scrapers.youtube
    fetch.py            # channel list -> video listing -> subtitles (and metadata when needed)
    parse.py            # subtitle response -> transcript text; listing -> video dicts
tests/
  test_oxylabs.py       # + the YouTube endpoint
  test_youtube_fetch.py
  test_youtube_parse.py
  fixtures/youtube/     # trimmed real responses (a few cues each)
data/
  positives/youtube_sources.json          # input (committed)
  raw/youtube/<key>/channel-001.json      # each listing response, numbered in the order saved (gitignored)
  raw/youtube/<key>/subtitles-<id>.json   # one per video
  raw/youtube/<key>/metadata-<id>.json    # only when fetched
  raw/youtube/<key>/meta.json             # channel id, request ids, sha256s, remembered videos
  parsed/youtube/posts.json               # {"<key>": [videos]}
  parsed/youtube/_report.json             # per-channel run summary
```

## Input

`data/positives/youtube_sources.json`, shaped like the Telegram file (`telegram_sources.json`), with one optional extra field:

```json
{
  "YT": [
    {"name": "Example Channel", "link": "https://www.youtube.com/@ExampleChannel", "language": "lt", "userSize": 12000},
    {"link": "https://www.youtube.com/@AnotherChannel"}
  ]
}
```

| Field      | Required | Use                                                                                                                                         |
| ---------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `link`     | yes      | The channel's `@handle` link                                                                                                                |
| `name`     | no       | `channelName`. Without it, the channel's display name is used                                                                               |
| `language` | no       | The language the channel speaks, as a lowercase ISO 639-1 code (`lt`, `ru`, `en`). Makes transcript requests smaller (see Which transcript) |
| `userSize` | no       | Subscriber count, for people reading the file. The scraper doesn't use it                                                                   |

Rules:

- **The handle** is the `@…` path segment of `link`: `https://www.youtube.com/@ExampleChannel/videos` → `@ExampleChannel`. YouTube handles ignore case.
- **The key** is the handle without `@`, lowercased (`examplechannel`). It's used for the cache folder, the keys in `posts.json` and `_report.json`, and `--channels`.
- A link without a handle (`/channel/UC…`, `/c/<name>`, `/user/<name>`) can't be listed, because the channel endpoint doesn't take channel ids.
  - Mark that channel `unreadable` with the reason "link has no @handle; replace it with the channel's @handle link".
  - Don't guess the handle.
- **A bad `language`:**
  - One that isn't a lowercase code (`^[a-z]{2,3}(-[a-z0-9]+)?$`) is caught before any request. The channel is marked `unreadable` with the reason.
  - If the API rejects the code (a 400 on the first transcript request), stop that channel's transcript requests, mark it `partial` with "language code rejected: fix it in youtube_sources.json", and carry on with the next channel.

## How it works

Per channel, in the order of `youtube_sources.json`, `--parallel` channels at a time (default 3):

1. **List the newest videos** with the `channel` endpoint (`output: ["json"]`). This also gives the channel id (`UC…`) and display name; record both in `meta.json`.
2. **Keep the videos in range.** Then skip:
   - upcoming premieres and streams that are still live (no transcript yet, so a later run picks them up once they're finished)
   - videos whose transcript is already saved
   - videos remembered as unavailable or as having no transcript (see Running it again)
3. **Fetch the transcripts** with `subtitles`, newest first, `--workers` at a time (default 4). A free slot is refilled as soon as a request finishes.
4. **Fetch metadata only when it's needed.**
   - If the listing gives each video's title and absolute publish date, `metadata` isn't called at all.
   - If it lacks either, call `metadata` for each video kept, to get `title` and `dateTime`.
   - If the listing has only relative dates ("3 days ago"), use them only to decide which videos to fetch, with a 2-day margin at the start date, like Facebook's search-snippet rule.
5. **Parse**, filter by date, and cap at N.

### How far back to go

- **By count:** `--videos N` returns the N newest videos that have a transcript.
  - Without `--days` or `--since`, the default comes from `YOUTUBE_VIDEOS_PER_CHANNEL` in the environment or `.env`, else 10.
  - The default is lower than Telegram's 20 because every video costs its own request, and one transcript can be as long as hundreds of posts.
- **By date:** `--days D` or `--since 2026-10-01` (UTC unless an offset is given) returns every video published at or after that moment.
- **Both:** `--days 30 --videos 100` stops at whichever comes first.
- The channel endpoint's `limit` is the only way back in time; each call lists again from the newest video.
  - A count run asks for `limit = max(20, 2 × N)`, because videos without a transcript don't count.
  - A date run starts at `limit = 50`.
  - Either way, if the listing came back full and hasn't reached the count or the start date, ask again with double the limit, up to `--max-videos` (default 200).
- If the smoke test shows that `limit` has a cap, write it here. A channel with more videos in range than the cap is `partial`, with that as the reason.
- **Status:**
  - `ok`: the count or the date was reached, or the listing held all of the channel's videos. A readable channel with nothing in range is `ok` with 0 videos.
  - `partial`: `--max-videos` or the `limit` cap stopped it early, a count run found fewer than N videos with a transcript, or a request failed.
  - `unreadable`: the link has no handle, the `language` is malformed, or the channel endpoint returned no channel.

### Which transcript

- **With a `language` in the source:** request `subtitles` with that `language_code`. That returns at most two tracks, one per origin, so the response stays small.
  - If it returns `{}`, the video may be in another language. Make one more request without `language_code`.
- **Without a `language`:** make one request with neither `language_code` nor `subtitle_origin`, so every track comes back in one response.
- Pick one track:
  1. If there's an auto-generated track, its language is the spoken language. Use the uploader-provided track in that language if there is one (it has punctuation and corrections), else the auto-generated track.
  2. With no auto-generated track, use the uploader-provided track the response marks as the default or original if it marks one, else the first one listed. Record which in `meta.json`.
  3. Never use a machine-translated track. If the response includes YouTube's auto-translations, tell them apart from both origins in the smoke test.
- If omitting `language_code` returns auto-translations into dozens of languages and the responses get huge, change the no-`language` case: fetch `metadata` first for the available languages, then `subtitles` with the right `language_code`. Write down here which way it ended up.
- No usable track (`{}`, or only translated tracks) means the video has no transcript.
- Don't translate anything.

## Raw responses

- Save every response under `data/raw/youtube/<key>/` exactly as received, as UTF-8 JSON:
  - `channel-NNN.json`: each listing, numbered in the order saved. Earlier ones are kept, because a video can drop off a later listing.
  - `subtitles-<video id>.json` and `metadata-<video id>.json`. Video ids are case-sensitive, so file names keep the id as YouTube gives it.
- `meta.json` records:
  - the channel id and display name
  - for each response: its endpoint, request id, sha256, and what it covered (the video ids listed, or the track chosen)
  - `noTranscript`: video id → when it was first seen and whether its one retry has been used
  - `unavailable`: video id → when it was first seen
- A saved file whose sha256 no longer matches `meta.json` is dropped and fetched again.
- `data/raw/` is already gitignored. Don't commit scraped output unless the user asks.

## Running it again

Run the same command again to pick up new videos:

- **A channel's listing is re-checked once it's 6 hours old** (`--recheck-hours`). New videos in it get their transcripts fetched. A run right after another makes almost no requests, and a daily run lists every channel.
- **A saved transcript is never fetched again.** It's re-parsed from disk.
- **No transcript yet:** auto-generated captions can appear some hours after upload, so a video with no transcript gets one retry.
  - The retry happens on the first run at least 24 hours after the video was published.
  - A video that's already older than that when first seen gets no retry.
  - After that, it's remembered as having none and skipped until `--retry-missing`.
  - So a video costs at most one extra request. A channel YouTube can't auto-caption doesn't burn requests on every re-check.
- **Unavailable** (deleted, private, age-restricted, members-only): remembered and skipped until `--retry-missing`.
- An API error isn't a definite answer, so it isn't remembered, and the next run tries again.
- `--update` re-lists every channel now, however recently it was checked.
- `--cached` makes no requests and rebuilds `posts.json` from the saved responses, which is useful after a parser change. It works without an API key.
- `--refresh` throws a channel's saved files and its list in `posts.json` away and starts over. It can't be combined with `--update` or `--cached`.

## Parsing

All videos go into `data/parsed/youtube/posts.json`, newest first, UTF-8 with `ensure_ascii=False`. There's one key per channel, in the order of `youtube_sources.json`.

- **The file grows across runs**, like Facebook's. Each channel keeps the videos from earlier runs, even once they're older than `--days`/`--since`, and each run adds its new ones (matched by `postUrl`).
- A run over some channels (`--channels`) replaces only those entries in both files and keeps the rest.
- An unreadable channel gets an empty array.
- The file is written once, at the end of a run, and atomically.

The fields are Telegram's and Facebook's five, plus `title`:

```json
{
   "examplechannel": [
      {
         "author": "Example Channel",
         "title": "The video's title",
         "content": "the transcript as one block of text ...",
         "dateTime": "2026-11-14T09:30:00Z",
         "channelName": "Example Channel",
         "postUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
      }
   ]
}
```

| Field         | Source                                                                                                                                 |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `author`      | The channel's display name, from the listing (or from `metadata` when that was fetched)                                                |
| `title`       | The video's title as YouTube shows it, from the listing (or from `metadata`). Not translated or cleaned                                |
| `content`     | The chosen transcript track's text (see the rules below)                                                                               |
| `dateTime`    | The video's publish time, as UTC ISO 8601 with a `Z` suffix. If the source gives only a date, use midnight UTC and say so in this file |
| `channelName` | The `name` from `youtube_sources.json`, else the channel's display name                                                                |
| `postUrl`     | `https://www.youtube.com/watch?v=<video id>`, also for Shorts and finished streams                                                     |

Rules for `content`:

- Join the cues in time order with single spaces.
- Turn line breaks inside a cue into spaces, unescape HTML entities, collapse runs of whitespace, and trim.
- Auto-generated tracks often repeat a line across rolling cues. Drop a cue whose text is exactly the previous cue's text.
- Keep sound tags such as `[Music]` as they are. Don't translate the text or clean it in any other way.
- Timings aren't kept in `posts.json`. The raw responses have them.

Other rules:

- Videos with no transcript are left out of `posts.json` and don't count towards `--videos N`. Don't put the title or description in `content` in their place: `content` is what was said.
- Deduplicate by video id.
- **`external_id`:**
  - A video's `external_id` is its video id exactly as YouTube gives it (`dQw4w9WgXcQ`). Video ids are case-sensitive, so unlike Telegram it's never lowercased.
  - The account's `external_id` is the channel id (`UC…`). It stays the same when a channel changes its handle.
  - The YouTube row in `docs/data-architecture.md` is added separately later. Don't add it as part of this work.
- Output exactly these six fields. Don't add others without asking.
- **Size:** an hour of speech is about 9,000 words: roughly 55 KB in Latin script, about twice that in Cyrillic. `posts.json` grows quickly. GitHub warns at 50 MB and rejects files over 100 MB, so don't commit it once it grows.

Also write `data/parsed/youtube/_report.json`, with one entry per channel:

- `handle` (the key) and `channelId`
- `requested` (null for a date-only run) and `since`
- `listed`: videos the largest listing this run returned
- `got`: videos in range with a transcript this run
- `added`: new to `posts.json`; `total`: in `posts.json` now
- `noTranscript`, `unavailable` and `skippedLive`
- `transcriptOrigin`: `{"uploaderProvided": n, "autoGenerated": n}`
- `scrapedThisRun` and `failedThisRun`
- `status` (`ok`, `partial`, `unreadable`) and `reason`
- the Oxylabs request ids

## Commands

```bash
uv run python -m scrapers.youtube                          # 10 newest videos with a transcript per channel (YOUTUBE_VIDEOS_PER_CHANNEL)
uv run python -m scrapers.youtube --videos 30
uv run python -m scrapers.youtube --days 30                # every video from the last 30 days
uv run python -m scrapers.youtube --since 2026-10-01 --videos 100
uv run python -m scrapers.youtube --days 30 --update       # re-list every channel now
uv run python -m scrapers.youtube --cached                 # no requests: rebuild posts.json
uv run python -m scrapers.youtube --channels examplechannel,otherchannel
uv run python -m scrapers.youtube --retry-missing          # retry videos with no transcript or unavailable
uv run python -m scrapers.youtube --refresh                # start the listed channels over
uv run python -m scrapers.youtube --parallel 2 --workers 2 # fewer requests at once (on 429s)
uv run python -m unittest tests.test_oxylabs tests.test_youtube_fetch tests.test_youtube_parse
```

- Any other 400 on one video is a bug in the request. Don't retry it unchanged. Record the reason, count it under `failedThisRun` and carry on.
- **Cost per channel and run:** one or more listing requests, one `subtitles` request per new video, and one `metadata` request per new video only if the listing lacks dates. Measure the real numbers and timings on the first full run and write them here.

## Workflow

1. Read the three Oxylabs pages.
2. **Smoke-test the real API before writing the parser or tests:**
   - Call `GET /v1/scrapers` and confirm the key can use the YouTube `channel`, `subtitles` and `metadata` endpoints.
   - Call `channel` for the first source with `limit: 5`, then call `subtitles` and `metadata` for one of its videos. Show the user all request ids. On a 401, stop and tell the user.
   - From those responses, answer these and write the answers into this file with the date:
     - The listing's field names: the video id, title, publish date (absolute or relative), and whether Shorts and live or upcoming streams are listed and flagged.
     - Whether `limit` has a cap. Try `limit: 100` on one channel.
     - A subtitle track's shape: the origin and language keys, the cue fields (text, start, duration), and whether auto-translations are included.
     - Where the `11202`/`11206` status code sits for an unavailable video.
3. Add the YouTube method to the client, with tests.
4. Write the fetcher and parser. Save trimmed real responses (a few cues each) as fixtures in `tests/fixtures/youtube/`:
   - a listing
   - an uploader-provided track
   - an auto-generated-only video
   - a video with no transcript
5. Add tests and run them:
   - **Client:**
     - the endpoint paths
     - a bad video id or handle sends nothing
     - retries and error mapping, the same as the other endpoints
   - **Fetcher:**
     - handle parsing from each link form, and `unreadable` for `/channel/UC…`
     - `limit` growth stops at N, at the start date or at `--max-videos`
     - saved transcripts aren't fetched again
     - a video with no transcript is retried once, on the first run at least 24 hours after it was published, and then remembered; one first seen after that isn't retried
     - with a `language`, `{}` leads to exactly one more request without `language_code`
     - a malformed `language` makes the channel `unreadable` without a request, and a 400 for it stops that channel's transcript requests
     - unavailable videos are remembered
     - live and upcoming videos are skipped
     - `--cached` makes no requests
     - one bad channel doesn't stop the run
     - a 401 stops the run with finished channels saved
   - **Parser:**
     - track choice: the uploader-provided track in the spoken language, then auto-generated, then the default uploader-provided track
     - cue joining, rolling duplicates and entities
     - `{}` counts as no transcript
     - the six fields, including `title`
6. Run the full scrape with the default N.
7. If you can't run commands in this environment, say so once and give the exact commands instead.

Don't ask whether to proceed; do the work. Stop only for a missing key, a 401, an Oxylabs page that won't load, the key not having the YouTube endpoints, or `uv` not being installed.

## Report back

- The endpoints used, and confirmation that nothing calls YouTube directly.
- Each checklist item from the Oxylabs pages, marked done or not.
- The smoke-test request ids, and what the smoke test found (the field names and the `limit` cap, now written into this file).
- A per-channel summary from `_report.json`: videos, transcripts by origin, `noTranscript`, unreadable channels and why.
- The test output.
- What the user still has to do, and anything left unfinished.

## Limits and next steps

- **Only transcripts YouTube already has.** Auto-generated captions exist only where the channel left them on and YouTube's speech recognition supports the language. Watch how many videos come out `noTranscript`. Covering those would mean downloading audio and transcribing it locally, which isn't planned.
- **Auto-generated transcripts have no punctuation or speaker turns.** Later steps that split text into sentences will see one very long sentence, so they should chunk YouTube text by word count instead.
- Timings are dropped from `posts.json`. A later feature can link a claim to its moment in the video (`&t=<seconds>s`) from the raw responses.
- Descriptions, comments and community posts aren't collected. The description is in the `metadata` response when that was fetched; adding it as a field needs asking first.
- The `subtitles` endpoint also has a batch mode (an array of ids), which could cut overhead on a big backfill.
- The YouTube endpoints document no `location`, so there's no region fallback like Telegram's. A video blocked in the exit region comes out unavailable.
