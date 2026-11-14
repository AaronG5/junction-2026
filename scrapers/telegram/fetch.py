"""Fetch Telegram channel previews (t.me/s/<username>) through the Oxylabs Web API.

Per channel (docs/oxylabs-telegram-scrape.md):
- Page 1 is https://t.me/s/<username>. Older pages are ?before=<message id>.
  A page fetched with ?before=B holds every message from its lowest id up to
  B-1, so saved pages cover known ranges of message ids.
- How far back to go: a post count (N newest posts), a start date (every post
  since then), or both (whichever is reached first). Paging always continues
  from the bottom of the newest unbroken run of saved pages, so a gap left by
  an earlier run is filled before going further back. It stops at the
  channel's first message, or after max_pages requests in one run.
- update=True fetches page 1 again (and more pages until they join the saved
  ones), so posts published since the last run are picked up. Without it,
  saved pages are reused as they are.
- If page 1 has no messages, retry it once with location RU. If RU works, the
  channel's later pages use RU too. If RU is empty as well, the channel is
  unreadable (or, when updating, keeps its saved posts and says so).
- 401 and a 429 that outlasts the client's retries stop the whole run; any
  other API error only affects that channel.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from scrapers.oxylabs import (AuthenticationError, MissingApiKeyError, RateLimitError,
                              ScrapeResult, WebApiError)
from scrapers.telegram.parse import ParsedPage, message_id, parse_page, to_utc_iso

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "positives" / "telegram_sources.json"
RAW_DIR = ROOT / "data" / "raw" / "telegram"
PARSED_DIR = ROOT / "data" / "parsed" / "telegram"
POSTS_NAME = "posts.json"          # {"<username>": [post, ...], ...}
REPORT_NAME = "_report.json"

DEFAULT_POSTS = 20
DEFAULT_MAX_PAGES = 500           # requests per channel per run, about 10,000 posts
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


def _before_of(url: str) -> int | None:
    value = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("before", [""])[0]
    return int(value) if value.isdigit() else None


# ---- which message ids are covered -------------------------------------------

class _Coverage:
    """Merged ranges of message ids whose messages have all been fetched."""

    def __init__(self) -> None:
        self._ranges: list[tuple[int, int]] = []     # newest range first

    def add(self, lo: int, hi: int) -> None:
        if hi < lo:
            return
        keep = []
        for r_lo, r_hi in self._ranges:
            if r_lo <= hi + 1 and lo <= r_hi + 1:        # overlapping or adjacent
                lo, hi = min(lo, r_lo), max(hi, r_hi)
            else:
                keep.append((r_lo, r_hi))
        keep.append((lo, hi))
        self._ranges = sorted(keep, key=lambda r: r[1], reverse=True)

    @property
    def top(self) -> tuple[int, int] | None:
        """The newest unbroken range."""
        return self._ranges[0] if self._ranges else None

    @property
    def complete(self) -> bool:
        """The newest range reaches the channel's first message."""
        return self.top is not None and self.top[0] <= 1

    @property
    def lowest(self) -> int | None:
        return min(r[0] for r in self._ranges) if self._ranges else None

    def contains(self, lo: int, hi: int) -> bool:
        return any(r_lo <= lo and hi <= r_hi for r_lo, r_hi in self._ranges)


def _sha256(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def _page_matches(entry: dict, html: str, ids: Sequence[int]) -> bool:
    """Is this file still the page meta.json says it is? Uses the content hash, or
    for pages saved before hashes were recorded, the message-id range."""
    if entry.get("sha256"):
        return entry["sha256"] == _sha256(html)
    if entry.get("minId") is not None:
        return bool(ids) and min(ids) == entry["minId"] and max(ids) == entry["maxId"]
    return True                  # oldest format: nothing recorded to check against


def _page_range(before: int | None, ids: Sequence[int]) -> tuple[int, int] | None:
    """Ids a page covers. ?before=B with no messages means nothing below B exists."""
    if not ids:
        return (1, before - 1) if before else None
    lo = min(ids)
    if before is None:
        return lo, max(ids)
    return (lo, before - 1) if lo < before else None   # None: the page didn't go older


# ---- raw page cache ---------------------------------------------------------

class PageCache:
    """data/raw/telegram/<username>/: page-NNN.html (numbered in the order saved)
    plus meta.json with the region and each page's url, request id and id range."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.meta_file = directory / "meta.json"
        try:
            self.meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.meta = {}
        self.meta.setdefault("region", "default")
        self.meta.setdefault("emptyBefore", None)
        self.meta.setdefault("pages", [])
        # Only keep entries whose file still exists; older caches didn't store `before`.
        self.meta["pages"] = [p for p in self.meta["pages"] if (self.dir / p["file"]).is_file()]
        for page in self.meta["pages"]:
            page.setdefault("before", _before_of(page.get("url", "")))

    @property
    def region(self) -> str:
        return self.meta["region"]

    @property
    def page_count(self) -> int:
        return len(self.meta["pages"])

    @property
    def request_ids(self) -> list[str]:
        return [p["requestId"] for p in self.meta["pages"] if p.get("requestId")]

    def pages(self) -> Iterable[tuple[dict, str]]:
        for entry in self.meta["pages"]:
            yield entry, (self.dir / entry["file"]).read_text(encoding="utf-8")

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.meta = {"region": "default", "emptyBefore": None, "pages": []}

    def save_page(self, html: str, url: str, request_id: str | None, region: str,
                  before: int | None, ids: Sequence[int]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        numbers = [int(m.group(1)) for p in self.meta["pages"]
                   if (m := re.fullmatch(r"page-(\d+)\.html", p["file"]))]
        name = f"page-{max(numbers, default=0) + 1:03d}.html"
        (self.dir / name).write_text(html, encoding="utf-8")
        self.meta["region"] = region
        self.meta["pages"].append({
            "file": name, "url": url, "requestId": request_id, "before": before,
            "minId": min(ids) if ids else None, "maxId": max(ids) if ids else None,
            "sha256": _sha256(html),
            "scrapedAt": to_utc_iso(datetime.now(timezone.utc)),
        })
        self.write_meta()

    def drop(self, entries: Sequence[dict]) -> None:
        """Forget pages (and delete their files), e.g. ones whose content is wrong."""
        files = {e["file"] for e in entries}
        for name in files:
            (self.dir / name).unlink(missing_ok=True)
        self.meta["pages"] = [p for p in self.meta["pages"] if p["file"] not in files]
        self.write_meta()

    def mark_empty_before(self, before: int) -> None:
        """No messages exist below `before` (the channel's first post was reached)."""
        self.meta["emptyBefore"] = max(before, self.meta.get("emptyBefore") or 0)
        self.write_meta()

    def save_empty(self, html: str, region: str) -> None:
        """Keep an empty page 1 for inspection. It is not a cached page."""
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"empty-{region}.html").write_text(html, encoding="utf-8")

    def write_meta(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        meta = {k: v for k, v in self.meta.items() if k != "exhausted"}   # pre-coverage key
        tmp = self.meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(self.meta_file)


# ---- one channel ------------------------------------------------------------

@dataclass
class ChannelResult:
    channel: Channel
    requested: int | None         # post count asked for; None = no count limit
    since: str | None = None      # start date asked for (dateTime format); None = no date limit
    posts: list[dict] = field(default_factory=list)
    region: str = "default"
    status: str = "ok"            # ok | partial | unreadable
    reason: str = ""
    blank: int = 0                # posts in range left out because they have no text
    request_ids: list[str] = field(default_factory=list)

    def report(self) -> dict:
        return {
            "username": self.channel.username,
            "requested": self.requested,
            "since": self.since,
            "got": len(self.posts),
            "blankExcluded": self.blank,
            "region": self.region,
            "status": self.status,
            "reason": self.reason,
            "requestIds": self.request_ids,
        }


def fetch_channel(client: Scraper, channel: Channel, n: int | None, raw_root: Path = RAW_DIR,
                  *, since: datetime | None = None, update: bool = False, refresh: bool = False,
                  max_pages: int | None = DEFAULT_MAX_PAGES) -> ChannelResult:
    """Fetch the N newest posts, every post since `since`, or both (whichever stops first)."""
    if n is None and since is None:
        raise ValueError("give a post count, a start date, or both")
    since_iso = to_utc_iso(since) if since is not None else None
    result = ChannelResult(channel, n, since_iso)
    cache = PageCache(raw_root / channel.username)
    if refresh:
        cache.clear()

    posts: dict[str, dict] = {}       # by postUrl; later scrapes replace earlier ones (edits)
    coverage = _Coverage()

    def absorb(parsed: ParsedPage, before: int | None) -> tuple[int, int] | None:
        for post in parsed.posts:
            posts[post["postUrl"]] = post
        span = _page_range(before, parsed.message_ids)
        if span:
            coverage.add(*span)
        return span

    broken = []
    for entry, html in cache.pages():
        parsed = parse_page(html, channel.name)
        if _page_matches(entry, html, parsed.message_ids):
            absorb(parsed, entry.get("before"))
        else:
            broken.append(entry)
    if broken:
        log.warning("%s: %d saved page(s) don't match meta.json (overwritten?); dropping them "
                    "so they're fetched again", channel.username, len(broken))
        cache.drop(broken)
    if cache.meta.get("exhausted") and not cache.meta["emptyBefore"] and coverage.lowest:
        cache.meta["emptyBefore"] = coverage.lowest     # migrate caches from before id ranges
        cache.write_meta()
    if cache.meta["emptyBefore"]:
        coverage.add(1, cache.meta["emptyBefore"] - 1)

    region = cache.region
    request_ids = list(cache.request_ids)    # saved pages first, then this run's calls
    had_saved_posts = coverage.top is not None
    fetched = 0
    error = ""
    update_problem = ""
    page_one_done = False
    capped = False

    def scrape(before: int | None, region: str) -> tuple[str, ScrapeResult, ParsedPage]:
        nonlocal fetched
        url = preview_url(channel.username, before)
        page = client.scrape(url, output=["html"],
                             location=None if region == "default" else region)
        fetched += 1
        if page.request_id:
            request_ids.append(page.request_id)
        return url, page, parse_page(page.text("html"), channel.name)

    def save(url: str, page: ScrapeResult, parsed: ParsedPage, before: int | None) -> None:
        cache.save_page(page.text("html"), url, page.request_id, region, before, parsed.message_ids)
        absorb(parsed, before)

    def in_range(post: dict) -> bool:
        return since_iso is None or (post["dateTime"] is not None and post["dateTime"] >= since_iso)

    def has_text(post: dict) -> bool:
        """Media-only posts (and ones the preview can't show) are left out of the output."""
        return bool(post["content"].strip())

    def top_posts() -> list[dict]:
        top = coverage.top
        if top is None:
            return []
        return [p for p in posts.values() if top[0] <= (message_id(p["postUrl"]) or 0) <= top[1]]

    def enough() -> bool:
        newest = top_posts()
        if n is not None and sum(in_range(p) and has_text(p) for p in newest) >= n:
            return True
        return since_iso is not None and any(
            p["dateTime"] is not None and p["dateTime"] < since_iso for p in newest)

    try:
        if update or not had_saved_posts:
            tried = [region]
            url, page, parsed = scrape(None, region)
            if not parsed.has_messages and region != FALLBACK_REGION:
                cache.save_empty(page.text("html"), region)
                log.info("%s: no posts with the %s region, retrying with %s",
                         channel.username, region, FALLBACK_REGION)
                tried.append(FALLBACK_REGION)
                url, page, parsed = scrape(None, FALLBACK_REGION)
            if parsed.has_messages:
                span = _page_range(None, parsed.message_ids)
                unchanged = (had_saved_posts and span is not None and coverage.contains(*span)
                             and all(posts.get(p["postUrl"]) == p for p in parsed.posts))
                if region != tried[-1]:
                    region = cache.meta["region"] = tried[-1]
                    cache.write_meta()
                if unchanged:
                    log.info("%s: nothing new since the last run", channel.username)
                else:
                    save(url, page, parsed, None)
            else:
                cache.save_empty(page.text("html"), tried[-1])
                regions = " or ".join(tried)
                if not had_saved_posts:
                    result.region = tried[-1]
                    result.request_ids = request_ids
                    result.status = "unreadable"
                    result.reason = (f"no posts on page 1 with region {regions} "
                                     "(web preview off, channel gone, or blocked)")
                    return result
                update_problem = (f"couldn't update: page 1 had no posts with region {regions}; "
                                  "kept the saved posts")
        page_one_done = True

        while coverage.top and not enough() and not coverage.complete:
            if max_pages is not None and fetched >= max_pages:
                capped = True
                break
            before = coverage.top[0]
            url, page, parsed = scrape(before, region)
            if not parsed.has_messages:
                cache.mark_empty_before(before)
                coverage.add(1, before - 1)
                break
            if _page_range(before, parsed.message_ids) is None:
                log.warning("%s: ?before=%d returned no older messages", channel.username, before)
                break
            save(url, page, parsed, before)
            if fetched % 25 == 0:
                log.info("%s: %d pages this run, back to %s", channel.username, fetched,
                         min((p["dateTime"] for p in posts.values() if p["dateTime"]), default="?"))
    except FATAL:
        raise
    except WebApiError as e:
        error = str(e)
        log.warning("%s: %s", channel.username, error)
        if update and had_saved_posts and not page_one_done:
            update_problem = f"couldn't update: {error}; kept the saved posts"

    result.region = region
    result.request_ids = request_ids
    ordered = sorted((p for p in posts.values() if in_range(p)),
                     key=lambda p: message_id(p["postUrl"]) or 0, reverse=True)
    result.blank = sum(not has_text(p) for p in ordered)
    ordered = [p for p in ordered if has_text(p)]
    result.posts = ordered if n is None else ordered[:n]

    if coverage.top is None:
        result.status = "unreadable"
        result.reason = f"API error: {error}" if error else "no posts found"
    elif update_problem:
        result.status = "partial"
        result.reason = update_problem
    elif enough() or (since_iso is not None and coverage.complete):
        result.status = "ok"
        if since_iso is not None and not result.posts:
            result.reason = f"no posts since {since_iso}"
    elif error:
        result.status, result.reason = "partial", f"stopped early: {error}"
    elif capped:
        result.status, result.reason = "partial", f"stopped at the {max_pages}-page limit for one run"
    elif coverage.complete:
        result.status = "partial"
        result.reason = f"the channel preview only has {len(result.posts)} posts"
    else:
        result.status, result.reason = "partial", "stopped: a page didn't go further back"
    return result


# ---- many channels ----------------------------------------------------------

def run_channels(client: Scraper, channels: Sequence[Channel], n: int | None, *,
                 since: datetime | None = None, update: bool = False,
                 raw_root: Path = RAW_DIR, parsed_root: Path = PARSED_DIR,
                 refresh: bool = False, max_pages: int | None = DEFAULT_MAX_PAGES,
                 workers: int = 1,
                 on_result: Callable[[int, ChannelResult], None] | None = None
                 ) -> list[ChannelResult]:
    """Fetch and parse every channel, then save posts.json and _report.json. One bad
    channel never stops the run; 401 / persistent 429 do (finished channels are
    still saved)."""
    results: dict[str, ChannelResult] = {}
    order = {c.username: i for i, c in enumerate(channels)}
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch_channel, client, c, n, raw_root, since=since,
                                   update=update, refresh=refresh, max_pages=max_pages): c
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
                        result = ChannelResult(
                            channel, n, to_utc_iso(since) if since else None, status="unreadable",
                            reason=f"unexpected error: {e.__class__.__name__}: {e}")
                    results[channel.username] = result
                    done += 1
                    if on_result:
                        on_result(done, result)
            except FATAL:
                for f in futures:
                    f.cancel()
                raise
    finally:
        finished = sorted(results.values(), key=lambda r: order[r.channel.username])
        write_posts(finished, parsed_root, all_channels=channels)
        write_report(finished, parsed_root, all_channels=channels)
    return finished


def _ordered(usernames: Iterable[str], all_channels: Sequence[Channel]) -> list[str]:
    """Channel-list order first, then anything no longer in the list, alphabetically."""
    rank = {c.username: i for i, c in enumerate(all_channels)}
    return sorted(usernames, key=lambda u: (rank.get(u, len(rank)), u))


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)          # atomic: a crash never leaves a half-written file


def write_posts(results: Sequence[ChannelResult], parsed_root: Path = PARSED_DIR,
                all_channels: Sequence[Channel] = ()) -> Path:
    """Merge into posts.json ({username: [posts]}) so a run over a few channels keeps
    the others' arrays."""
    path = parsed_root / POSTS_NAME
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}
    for r in results:
        existing[r.channel.username] = r.posts
    _write_json(path, {u: existing[u] for u in _ordered(existing, all_channels)})
    return path


def write_report(results: Sequence[ChannelResult], parsed_root: Path = PARSED_DIR,
                 all_channels: Sequence[Channel] = ()) -> Path:
    """Merge into _report.json so a run over a few channels keeps the others' entries."""
    path = parsed_root / REPORT_NAME
    try:
        existing = {e["username"]: e for e in json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError, TypeError):
        existing = {}
    for r in results:
        existing[r.channel.username] = r.report()
    _write_json(path, [existing[u] for u in _ordered(existing, all_channels)])
    return path
