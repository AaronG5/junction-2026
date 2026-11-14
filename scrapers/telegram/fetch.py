"""Fetch Telegram channel previews (t.me/s/<username>) through the Oxylabs Web API.

Per channel (docs/oxylabs-telegram-scrape.md):
- Page 1 is https://t.me/s/<username>. Older pages are ?before=<lowest message
  id seen so far>. Stop at N posts, or when a page adds no new messages.
- If page 1 has no messages, retry it once with location RU. If RU works, the
  rest of that channel's pages use RU too. If RU is empty as well, the channel
  is unreadable and the run moves on.
- Raw pages are saved as data/raw/telegram/<username>/page-001.html (newest
  first) and re-parsed on later runs instead of scraped again, unless refresh.
  A later run with a bigger N only fetches the missing older pages.
- 401 and a 429 that outlasts the client's retries stop the whole run; any
  other API error only affects that channel.
"""
from __future__ import annotations

import json
import logging
import shutil
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from scrapers.oxylabs import (AuthenticationError, MissingApiKeyError, RateLimitError,
                              ScrapeResult, WebApiError)
from scrapers.telegram.parse import ParsedPage, message_id, parse_page

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "positives" / "telegram_channels.json"
RAW_DIR = ROOT / "data" / "raw" / "telegram"
PARSED_DIR = ROOT / "data" / "parsed" / "telegram"
REPORT_NAME = "_report.json"

DEFAULT_POSTS = 20
FALLBACK_REGION = "RU"
FATAL = (AuthenticationError, RateLimitError, MissingApiKeyError)

log = logging.getLogger(__name__)


class Scraper(Protocol):
    def scrape(self, url: str, output: Sequence[str] = ..., *,
               location: str | None = ...) -> ScrapeResult: ...


# ---- channels ---------------------------------------------------------------

@dataclass(frozen=True)
class Channel:
    username: str
    name: str
    link: str


def username_from_link(link: str) -> str:
    """https://t.me/cop_lt -> cop_lt (also accepts t.me/s/<name> and post links)."""
    parts = [p for p in urllib.parse.urlparse(link.strip()).path.split("/") if p]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if not parts:
        raise ValueError(f"no channel username in {link!r}")
    return parts[0]


def load_channels(path: Path = INPUT) -> list[Channel]:
    data = json.loads(path.read_text(encoding="utf-8"))
    channels, seen = [], set()
    for entry in data.get("TG", []):
        username = username_from_link(entry["link"])
        if username.lower() in seen:
            continue
        seen.add(username.lower())
        channels.append(Channel(username, entry.get("name") or username, entry["link"]))
    return channels


def preview_url(username: str, before: int | None = None) -> str:
    url = f"https://t.me/s/{username}"
    return f"{url}?before={before}" if before else url


# ---- raw page cache ---------------------------------------------------------

class PageCache:
    """data/raw/telegram/<username>/: page-NNN.html plus meta.json."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.meta_file = directory / "meta.json"
        try:
            self.meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.meta = {}
        self.meta.setdefault("region", "default")
        self.meta.setdefault("exhausted", False)
        self.meta.setdefault("pages", [])
        # Only keep entries whose file still exists.
        self.meta["pages"] = [p for p in self.meta["pages"] if (self.dir / p["file"]).is_file()]

    @property
    def region(self) -> str:
        return self.meta["region"]

    @property
    def exhausted(self) -> bool:
        return self.meta["exhausted"]

    @property
    def request_ids(self) -> list[str]:
        return [p["requestId"] for p in self.meta["pages"] if p.get("requestId")]

    def pages(self) -> Iterable[str]:
        for entry in self.meta["pages"]:
            yield (self.dir / entry["file"]).read_text(encoding="utf-8")

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.meta = {"region": "default", "exhausted": False, "pages": []}

    def save_page(self, html: str, url: str, request_id: str | None, region: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        name = f"page-{len(self.meta['pages']) + 1:03d}.html"
        (self.dir / name).write_text(html, encoding="utf-8")
        self.meta["region"] = region
        self.meta["pages"].append({"file": name, "url": url, "requestId": request_id})
        self._write_meta()

    def save_empty(self, html: str, region: str) -> None:
        """Keep an empty page 1 for inspection. It is not a cached page, so the
        next run tries the channel again."""
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"empty-{region}.html").write_text(html, encoding="utf-8")

    def mark_exhausted(self) -> None:
        self.meta["exhausted"] = True
        self._write_meta()

    def _write_meta(self) -> None:
        tmp = self.meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
        tmp.replace(self.meta_file)


# ---- one channel ------------------------------------------------------------

@dataclass
class ChannelResult:
    channel: Channel
    requested: int
    posts: list[dict] = field(default_factory=list)
    region: str = "default"
    status: str = "ok"            # ok | partial | unreadable
    reason: str = ""
    request_ids: list[str] = field(default_factory=list)

    def report(self) -> dict:
        return {
            "username": self.channel.username,
            "requested": self.requested,
            "got": len(self.posts),
            "region": self.region,
            "status": self.status,
            "reason": self.reason,
            "requestIds": self.request_ids,
        }


class _Collected:
    """Posts from all pages of one channel, deduplicated by postUrl."""

    def __init__(self) -> None:
        self.posts: dict[str, dict] = {}
        self.ids: set[int] = set()

    def add(self, page: ParsedPage) -> int:
        new_ids = set(page.message_ids) - self.ids
        self.ids |= new_ids
        for post in page.posts:
            self.posts.setdefault(post["postUrl"], post)
        return len(new_ids)

    @property
    def lowest_id(self) -> int | None:
        return min(self.ids) if self.ids else None

    def newest(self, n: int) -> list[dict]:
        ordered = sorted(self.posts.values(),
                         key=lambda p: message_id(p["postUrl"]) or 0, reverse=True)
        return ordered[:n]


def fetch_channel(client: Scraper, channel: Channel, n: int, raw_root: Path = RAW_DIR,
                  *, refresh: bool = False) -> ChannelResult:
    result = ChannelResult(channel, n)
    cache = PageCache(raw_root / channel.username)
    if refresh:
        cache.clear()
    collected = _Collected()
    for html in cache.pages():
        collected.add(parse_page(html, channel.name))
    region = cache.region
    request_ids = list(cache.request_ids)    # cached pages first, then this run's calls
    error = ""

    def scrape(url: str, region: str) -> tuple[ScrapeResult, ParsedPage]:
        location = None if region == "default" else region
        page = client.scrape(url, output=["html"], location=location)
        if page.request_id:
            request_ids.append(page.request_id)
        return page, parse_page(page.text("html"), channel.name)

    try:
        if not cache.meta["pages"]:
            url = preview_url(channel.username)
            page, parsed = scrape(url, "default")
            if not parsed.has_messages:
                cache.save_empty(page.text("html"), "default")
                log.info("%s: no posts with the default region, retrying with %s",
                         channel.username, FALLBACK_REGION)
                region = FALLBACK_REGION
                page, parsed = scrape(url, region)
            if not parsed.has_messages:
                cache.save_empty(page.text("html"), region)
                result.region = region
                result.request_ids = request_ids
                result.status = "unreadable"
                result.reason = (f"no posts on page 1 with the default region or {FALLBACK_REGION} "
                                 "(web preview off, channel gone, or blocked)")
                return result
            collected.add(parsed)
            cache.save_page(page.text("html"), url, page.request_id, region)

        while len(collected.posts) < n and not cache.exhausted:
            url = preview_url(channel.username, collected.lowest_id)
            page, parsed = scrape(url, region)
            if collected.add(parsed) == 0:
                cache.mark_exhausted()
                break
            cache.save_page(page.text("html"), url, page.request_id, region)
    except FATAL:
        raise
    except WebApiError as e:
        error = str(e)
        log.warning("%s: %s", channel.username, error)

    result.region = region
    result.request_ids = request_ids
    result.posts = collected.newest(n)
    if not result.posts:
        result.status = "unreadable"
        result.reason = f"API error: {error}" if error else "no posts found"
    elif len(result.posts) < n:
        result.status = "partial"
        result.reason = (f"stopped early: {error}" if error else
                         f"the channel preview only has {len(result.posts)} posts")
    return result


# ---- many channels ----------------------------------------------------------

def run_channels(client: Scraper, channels: Sequence[Channel], n: int, *,
                 raw_root: Path = RAW_DIR, parsed_root: Path = PARSED_DIR,
                 refresh: bool = False, workers: int = 1,
                 on_result: Callable[[int, ChannelResult], None] | None = None
                 ) -> list[ChannelResult]:
    """Fetch, parse and save every channel. One bad channel never stops the run;
    401 / persistent 429 do (the report is still written for finished channels)."""
    results: dict[str, ChannelResult] = {}
    order = {c.username: i for i, c in enumerate(channels)}
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch_channel, client, c, n, raw_root, refresh=refresh): c
                       for c in channels}
            try:
                for future in as_completed(futures):
                    channel = futures[future]
                    try:
                        result = future.result()
                    except FATAL:
                        raise
                    except Exception as e:  # a bug or odd page must not end the run
                        log.exception("%s: unexpected error", channel.username)
                        result = ChannelResult(channel, n, status="unreadable",
                                               reason=f"unexpected error: {e.__class__.__name__}: {e}")
                    results[channel.username] = result
                    write_posts(result, parsed_root)
                    done += 1
                    if on_result:
                        on_result(done, result)
            except FATAL:
                for f in futures:
                    f.cancel()
                raise
    finally:
        finished = sorted(results.values(), key=lambda r: order[r.channel.username])
        write_report(finished, parsed_root, all_channels=channels)
    return finished


def write_posts(result: ChannelResult, parsed_root: Path = PARSED_DIR) -> Path:
    parsed_root.mkdir(parents=True, exist_ok=True)
    path = parsed_root / f"{result.channel.username}.json"
    path.write_text(json.dumps(result.posts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_report(results: Sequence[ChannelResult], parsed_root: Path = PARSED_DIR,
                 all_channels: Sequence[Channel] = ()) -> Path:
    """Merge into _report.json so a run over a few channels keeps the others' entries."""
    parsed_root.mkdir(parents=True, exist_ok=True)
    path = parsed_root / REPORT_NAME
    try:
        existing = {e["username"]: e for e in json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError, TypeError):
        existing = {}
    for r in results:
        existing[r.channel.username] = r.report()
    rank = {c.username: i for i, c in enumerate(all_channels)}
    entries = sorted(existing.values(), key=lambda e: (rank.get(e["username"], len(rank)), e["username"]))
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
