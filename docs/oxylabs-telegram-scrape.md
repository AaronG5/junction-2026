# Oxylabs Web API and Telegram scraping

Instructions for coding agents working on data sourcing in this repo. That covers the Oxylabs Web API client, the Telegram channel scraper, and the parser that turns scraped HTML into JSON. Read the whole file before writing code.

## Overview

- The project's own code calls the Oxylabs Web API at runtime. Write a client in this repo. Don't install the Oxylabs MCP server or agent skills in its place.
- Main job: scrape the public web preview of each channel in `data/positives/telegram_channels.json`, save the raw HTML, and parse the posts into JSON.
- The client also exposes web search and page scraping for later features: claim checking and propagation chains (see `docs/data-architecture.md`).

## Source of truth for the API

- Before writing the client, read <https://developers.oxylabs.io/products/web-api/for-agents.md> and follow it. It has the base URL, auth, both endpoint schemas, reference code, the error and retry rules, and a checklist.
- If the page doesn't load, stop and tell the user. Don't reconstruct the API from memory.
- Parameter names in this file (`run_js`, `location`) are shorthand. Use the exact names and value formats the page gives.

## Stack and conventions

- Python 3.10+, same as `local_ai/`.
- Environment and dependencies: [uv](https://docs.astral.sh/uv/).
  - If there's no `pyproject.toml` yet, create one with `uv init --bare` and set `requires-python = ">=3.10"`.
  - Add dependencies with `uv add`. Commit `pyproject.toml` and `uv.lock`.
  - Run everything through `uv run`. Don't use `pip`, `python -m venv` or a `requirements.txt`.
  - uv keeps its environment in `.venv/`, which is already gitignored.
  - If `uv` isn't installed, stop and ask the user to install it.
- HTTP: stdlib `urllib.request`, matching `local_ai/services.py` and `local_ai/download.py`. Don't add `requests` or `httpx`.
- HTML parsing: `beautifulsoup4` (`uv add beautifulsoup4`). It is the only third-party dependency.
- Tests: stdlib `unittest`. No real network calls in tests.
- The client takes an injectable transport, meaning a callable that sends one request and returns status, headers and body, so tests can mock it.

## Secrets

- The key is `OXYLABS_WEB_API_KEY`. Read the process environment first, then `.env` via `local_ai.state.read_env()`.
- If it's in neither, stop and ask the user.
- Never print the key. It must not appear in code, logs, exception messages, test fixtures, saved HTML or replies to the user. Redact auth headers whenever you log a request.
- `.env` is gitignored. `.env.example` only ever holds a placeholder.

## Layout

```text
scrapers/
  __init__.py
  oxylabs.py            # Web API client: search + scrape, auth, retries, error mapping
  telegram/
    __init__.py
    __main__.py         # CLI: python -m scrapers.telegram
    fetch.py            # channel list -> t.me/s pages, pagination, RU fallback
    parse.py            # HTML -> post dicts
tests/
  test_oxylabs.py
  test_telegram_fetch.py
  test_telegram_parse.py
  fixtures/telegram/<username>.html
data/
  positives/telegram_channels.json      # input (committed)
  raw/telegram/<username>/page-001.html # scraped HTML (gitignored)
  raw/telegram/<username>/meta.json     # region, request ids, whether the channel's end was reached
  parsed/telegram/<username>.json       # parsed posts
  parsed/telegram/_report.json          # per-channel run summary
pyproject.toml                          # uv project: dependencies, Python version
uv.lock                                 # committed
```

## General web scrapes (not Telegram)

- If a scrape comes back empty or skeletal, retry it once with `run_js: true`.
- If it's still empty and the site is on a country TLD (`.lt`, `.es`, `.co.uk`), retry once more with `run_js` and `location` set to that country. If that also fails, report the page as unreadable.

## Telegram scraping

### Input

- `data/positives/telegram_channels.json` has the shape `{"TG": [{"name", "link", "userSize"}]}`.
- The username is the last path segment of `link`: `https://t.me/cop_lt` → `cop_lt`.
- Scrape the web preview at `https://t.me/s/<username>`. `https://t.me/<username>` is a landing page with no posts.

### Posts per channel

- Set the count with `--posts N` on the CLI. The default comes from `TELEGRAM_POSTS_PER_CHANNEL` in the environment or `.env`, else 20.
- One preview page holds about 20 posts. For more, request `https://t.me/s/<username>?before=<lowest message id seen so far>`, going from newest to oldest. Stop when N posts are collected or when a page adds no new posts.
- Return the N newest posts. If the channel has fewer than N, return what exists and mark the channel `partial`.

### Region fallback

1. Request page 1 with default settings.
2. If it has no posts (no `div.tgme_widget_message[data-post]`), retry that channel once with `location` set to Russia (RU).
3. If RU works, use RU for the rest of that channel's pages.
4. If RU also returns no posts, mark the channel `unreadable`, record the reason and move on. One bad channel never stops the run.

Notes:

- Empty pages usually mean one of three things: the owner turned off the web preview, the channel was deleted or renamed, or Telegram blocks the channel in the request's region (for example EU-sanctioned media). The RU retry addresses the last one.
- `t.me/s` is rendered server-side, so `run_js` shouldn't be needed.
- A region block shows up as the channel's landing page (`Telegram: View @<username>`) instead of the preview. `sputniklive` needed RU on 2026-11-14.
- An empty page 1 is saved as `empty-<region>.html` for inspection but isn't cached, so the next run tries that channel again.
- HTTP errors (401, 429, 5xx, timeouts) follow the Oxylabs page's retry rules, not this fallback. On a 401, or a 429 that outlasts the retries (rate limit or spent quota), stop the whole run and tell the user.

### Raw HTML

- Save each page exactly as received, as `data/raw/telegram/<username>/page-001.html`, where page 1 is the newest.
- If raw pages for a channel already exist, re-parse them instead of scraping again (this saves credits). `--refresh` forces a new scrape.
- Add `data/raw/` to `.gitignore`. Don't commit scraped output unless the user asks.

## Parsing

Each channel's output is `data/parsed/telegram/<username>.json`: a JSON array, newest first, written as UTF-8 with `ensure_ascii=False`.

```json
[
   {
      "author": "INFA.LT",
      "content": "First line of the post.\nSecond line.",
      "dateTime": "2026-11-14T09:30:00Z",
      "channelName": "INFA.LT",
      "postUrl": "https://t.me/infalt/1234"
   }
]
```

| Field         | Source                                                                                                                                          |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `author`      | The post signature (`.tgme_widget_message_from_author`) if there is one, otherwise the channel display name (`.tgme_widget_message_owner_name`) |
| `content`     | The post's own text block (`.tgme_widget_message_text`)                                                                                         |
| `dateTime`    | The `datetime` attribute of `.tgme_widget_message_date time`, converted to UTC, ISO 8601 with a `Z` suffix                                      |
| `channelName` | The `name` field from `telegram_channels.json`                                                                                                  |
| `postUrl`     | The post's `data-post` attribute (`<username>/<message id>`) prefixed with `https://t.me/`, e.g. `https://t.me/infalt/1234`                     |

Rules:

- These selectors were checked against real pages on 2026-11-14. None of the 51 channels signs its posts, so the signature selector is still unverified. If the markup changes, update this file.
- Take `content` from the post's own text block, not from a reply quote inside it (`.tgme_widget_message_reply`). The quote uses the same `.tgme_widget_message_text` class, so filter by ancestry, not by class.
- Posts the web preview can't show ("Please open Telegram to view this post") have no text block, so they come out as media-only.
- Turn `<br>` into `\n`. Keep link text but drop the URLs. Keep emoji, unescape HTML entities and trim surrounding whitespace. Don't translate the text or clean it in any other way.
- Keep media-only posts, with `"content": ""`.
- Skip service messages such as "channel created" or "message pinned" (`.service_message`).
- For forwarded posts, `author` and `postUrl` both refer to the channel that posted, not the original source.
- Deduplicate by `postUrl` across pages.
- Later steps build the post's `external_id` from `postUrl`: its path, lowercased (`https://t.me/InfodefenseLIT/1234` → `infodefenselit/1234`). See `docs/data-architecture.md`. Keep `postUrl` itself in the case Telegram gives.
- Output exactly these five fields. Don't add others without asking.

Also write `data/parsed/telegram/_report.json`, with one entry per channel:

- `username`
- `requested` and `got` (post counts)
- `region`: `default` or `RU`
- `status`: `ok`, `partial` or `unreadable`
- `reason`
- the Oxylabs request ids

## Commands

```bash
uv sync                                                             # create .venv, install from uv.lock
uv run python -m scrapers.telegram                                  # all channels, default N
uv run python -m scrapers.telegram --posts 50                       # 50 posts per channel
uv run python -m scrapers.telegram --posts 10 --channels cop_lt,infalt
uv run python -m scrapers.telegram --refresh                        # ignore saved raw HTML
uv run python -m scrapers.telegram --workers 1                      # one channel at a time (default 4)
uv run python -m unittest discover -s tests
```

## Workflow

1. Read the Oxylabs page and its checklist.
2. Write `scrapers/oxylabs.py` as the page specifies.
3. Before writing any tests, smoke-test the real API: run one search, scrape one URL from its results, and scrape one Telegram channel (`t.me/s/<username>`). Show the user all three request ids. On a 401, stop and tell the user.
4. Write the Telegram fetcher and parser. Save one real page, trimmed, as `tests/fixtures/telegram/<username>.html` for the parser test.
5. Add tests and run them:
   - Client: retry and error mapping (401, 429, 5xx, timeout) against a mocked transport.
   - Fetcher:
      - the RU fallback runs only when page 1 has no posts
      - RU stays on for that channel's later pages
      - pagination stops at N, or when no new posts arrive
      - an unreadable channel doesn't stop the run
   - Parser: the fixture yields the expected five fields. Cover signed, forwarded, media-only and service posts.
6. Run the full scrape with the default N.
7. If you can't run commands in this environment, say so once and give the exact commands instead.

Don't ask whether to proceed; do the work. Stop only for a missing key, a 401, an Oxylabs page that won't load, or `uv` not being installed.

## Report back

- The path taken (a client in the project, not the MCP server).
- Each checklist item from the Oxylabs page, marked done or not.
- The smoke-test request ids.
- A per-channel summary from `_report.json`: post counts, which channels needed RU, and which are unreadable and why.
- The test output.
- What the user still has to do, and anything left unfinished.
