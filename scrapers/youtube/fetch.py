"""Fetch YouTube channel transcripts through the Oxylabs Web API's YouTube endpoints.

Per channel (docs/oxylabs-youtube-scrape.md):
- List the newest videos with `channel` (the channel's Videos tab). The listing is
  re-checked once it's `recheck_hours` old, and asked for again with a bigger `limit`
  (doubling, up to `max_videos`) when the count or the start date isn't reached yet.
- Per video, newest first: `metadata` (title, upload day, whether a transcript exists),
  then `subtitles` only when there can be a transcript. Nothing saved is fetched again.
- A video with no transcript gets one retry, two days after its upload day (the API
  gives only the day, so that's always at least 24 hours after publishing). After that,
  and for unavailable videos, it's skipped until retry_missing.
- 401 and a 429 that outlasts the client's retries stop the whole run; any other API
  error only affects that video or channel.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence

from scrapers.oxylabs import (AuthenticationError, MediaResult, MissingApiKeyError, RateLimitError,
                              ValidationError, WebApiError)
from scrapers.youtube.parse import (AUTO, UPLOADER, Listing, ListedVideo, Track, VideoMeta,
                                    choose_track,
                                    parse_listing, parse_metadata, parse_subtitles,
                                    relative_min_age, status_reason, to_post, to_utc_iso)

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "positives" / "youtube_sources.json"
RAW_DIR = ROOT / "data" / "raw" / "youtube"
PARSED_DIR = ROOT / "data" / "parsed" / "youtube"
POSTS_NAME = "posts.json"          # {"<key>": [video, ...], ...}
REPORT_NAME = "_report.json"

DEFAULT_VIDEOS = 10
DEFAULT_MAX_VIDEOS = 200           # the largest listing asked for
DEFAULT_RECHECK_HOURS = 6.0
MIN_LISTING = 20                   # a count run lists max(20, 2N) videos
DATE_LISTING = 50                  # a date run starts with 50
DATE_MARGIN = timedelta(days=2)    # slack on relative dates ("5d ago") at the start date
RETRY_AFTER_UPLOAD_DAY = timedelta(days=2)
LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]+)?$")
FATAL = (AuthenticationError, RateLimitError, MissingApiKeyError)

log = logging.getLogger(__name__)


class YouTubeApi(Protocol):
    def youtube(self, endpoint: str, query: str, **params: Any) -> MediaResult: ...


# ---- sources ----------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    key: str                     # handle without @, lowercased: folder, posts.json key, --channels
    link: str
    handle: str | None = None    # "@Name" as the link writes it
    name: str | None = None
    language: str | None = None
    problem: str = ""            # set when the source can't be scraped at all


_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}


def handle_from_link(link: str) -> str | None:
    """https://www.youtube.com/@Name/videos -> @Name. None for links without a handle."""
    link = (link or "").strip()
    if link and "://" not in link:
        link = "https://" + link
    parsed = urllib.parse.urlparse(link)
    if parsed.netloc.lower() not in _HOSTS:
        return None
    parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
    return parts[0] if parts and parts[0].startswith("@") and len(parts[0]) > 1 else None


def load_sources(path: Path = INPUT) -> list[Source]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("YT", []) if isinstance(data, dict) else []
    sources, seen = [], set()
    for entry in entries:
        link = str(entry.get("link") or "").strip()
        name = str(entry.get("name") or "").strip() or None
        language = entry.get("language")
        language = str(language).strip() if language is not None else None
        handle = handle_from_link(link)
        problem = ""
        if handle:
            key = handle[1:].lower()
        else:
            path_part = urllib.parse.urlparse(link if "://" in link else "https://" + link).path
            key = "/".join(p for p in path_part.split("/") if p).lower() or link.lower()
            problem = "link has no @handle; replace it with the channel's @handle link"
        if not problem and language is not None and not LANGUAGE.match(language):
            problem = f"language {language!r} isn't a lowercase language code such as 'lt'"
        if key in seen:
            continue
        seen.add(key)
        sources.append(Source(key, link, handle, name, language or None, problem))
    return sources


# ---- raw response cache -------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def retry_due(mark: dict) -> datetime:
    """When a video with no transcript gets its one retry."""
    published = _parse_iso(mark.get("published"))
    if published is not None:
        day = published.replace(hour=0, minute=0, second=0, microsecond=0)
        return day + RETRY_AFTER_UPLOAD_DAY
    return (_parse_iso(mark.get("firstSeen")) or datetime.now(timezone.utc)) + timedelta(hours=24)


class ChannelCache:
    """data/raw/youtube/<key>/: channel-NNN.json (each listing, numbered in the order saved),
    metadata-<id>.json and subtitles-<id>.json (one each per video), and meta.json with
    the channel id, every file's request id and sha256, and the remembered videos."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.meta_file = directory / "meta.json"
        self._lock = threading.RLock()
        self.meta = self._read_meta()
        self._verify()

    def _read_meta(self) -> dict:
        try:
            meta = json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return self._with_defaults(meta if isinstance(meta, dict) else {})

    @staticmethod
    def _with_defaults(meta: dict) -> dict:
        for key in ("channelId", "channelName"):
            meta.setdefault(key, None)
        meta.setdefault("listings", [])
        for key in ("videos", "noTranscript", "unavailable"):
            meta.setdefault(key, {})
        return meta

    @property
    def videos(self) -> dict:
        return self.meta["videos"]

    @property
    def no_transcript(self) -> dict:
        return self.meta["noTranscript"]

    @property
    def unavailable(self) -> dict:
        return self.meta["unavailable"]

    def _matches(self, entry: dict) -> bool:
        try:
            return _sha256((self.dir / entry["file"]).read_text(encoding="utf-8")) == entry.get("sha256")
        except (OSError, KeyError, UnicodeDecodeError):
            return False

    def _verify(self) -> None:
        """Drop saved files that are missing or don't match their sha256, so they're fetched again."""
        dropped = []
        keep = []
        for entry in self.meta["listings"]:
            (keep if self._matches(entry) else dropped).append(entry)
        self.meta["listings"] = keep
        for video_id, entry in list(self.videos.items()):
            for kind in ("metadata", "subtitles"):
                if kind in entry and not self._matches(entry[kind]):
                    dropped.append(entry.pop(kind))
            if not entry:
                del self.videos[video_id]
        if dropped:
            log.warning("%s: %d saved file(s) missing or not matching meta.json; they'll be fetched "
                        "again", self.dir.name, len(dropped))
            for entry in dropped:
                if entry.get("file"):
                    (self.dir / entry["file"]).unlink(missing_ok=True)
            self.write_meta()

    def read(self, entry: dict) -> Any:
        return json.loads((self.dir / entry["file"]).read_text(encoding="utf-8"))

    def listings_newest_first(self) -> Iterator[tuple[dict, Any]]:
        for entry in reversed(list(self.meta["listings"])):
            yield entry, self.read(entry)

    def newest_listing_time(self) -> datetime | None:
        times = [_parse_iso(e.get("scrapedAt")) for e in self.meta["listings"]]
        return max((t for t in times if t), default=None)

    def _write_file(self, name: str, data: Any) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False)
        (self.dir / name).write_text(text, encoding="utf-8")
        return {"file": name, "sha256": _sha256(text)}

    def save_listing(self, data: Any, request_id: str | None, limit: int, listing: Listing,
                     now: datetime) -> None:
        with self._lock:
            numbers = [int(m.group(1)) for e in self.meta["listings"]
                       if (m := re.fullmatch(r"channel-(\d+)\.json", e["file"]))]
            entry = self._write_file(f"channel-{max(numbers, default=0) + 1:03d}.json", data)
            entry.update({"requestId": request_id, "limit": limit,
                          "videoIds": [v.video_id for v in listing.videos],
                          "scrapedAt": to_utc_iso(now)})
            self.meta["listings"].append(entry)
            if listing.channel_id:
                self.meta["channelId"] = listing.channel_id
            if listing.name:
                self.meta["channelName"] = listing.name
            self.write_meta()

    def save_empty(self, data: Any) -> None:
        """Keep a listing without a channel for inspection. It isn't a cached listing."""
        self._write_file("empty-channel.json", data)

    def save_video(self, kind: str, video_id: str, data: Any, request_id: str | None,
                   now: datetime, **extra: Any) -> None:
        with self._lock:
            entry = self._write_file(f"{kind}-{video_id}.json", data)
            entry.update({"requestId": request_id, "scrapedAt": to_utc_iso(now), **extra})
            self.videos.setdefault(video_id, {})[kind] = entry
            self.write_meta()

    def mark_no_transcript(self, video_id: str, published: str | None, now: datetime, *,
                           retried: bool, reason: str) -> None:
        with self._lock:
            mark = self.no_transcript.get(video_id) or {"firstSeen": to_utc_iso(now)}
            mark["published"] = published or mark.get("published")
            mark["reason"] = reason
            # A video first seen after its retry time gets no retry.
            mark["retried"] = bool(retried or mark.get("retried") or now >= retry_due(mark))
            self.no_transcript[video_id] = mark
            self.unavailable.pop(video_id, None)
            self.write_meta()

    def mark_unavailable(self, video_id: str, reason: str, now: datetime,
                         request_id: str | None) -> None:
        with self._lock:
            first = (self.unavailable.get(video_id) or {}).get("firstSeen") or to_utc_iso(now)
            self.unavailable[video_id] = {"firstSeen": first, "reason": reason, "requestId": request_id}
            self.no_transcript.pop(video_id, None)
            self.write_meta()

    def clear_marks(self, video_id: str) -> None:
        with self._lock:
            had_mark = self.no_transcript.pop(video_id, None) is not None
            had_mark = (self.unavailable.pop(video_id, None) is not None) or had_mark
            if had_mark:
                self.write_meta()

    def clear(self) -> None:
        with self._lock:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.meta = self._with_defaults({})

    def write_meta(self) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.meta_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.meta_file)


# ---- one channel ------------------------------------------------------------

@dataclass
class ChannelResult:
    source: Source
    requested: int | None          # video count asked for; None = no count limit
    since: str | None = None       # start date asked for; None = no date limit
    channel_id: str | None = None
    listed: int = 0                # videos known from the channel's listings
    posts: list[dict] = field(default_factory=list)   # every saved video with a transcript
    selected: list[dict] = field(default_factory=list)  # this run's: in range, at most N, newest first
    got: int = 0                   # len(selected)
    added: int = 0
    total: int = 0
    no_transcript: int = 0
    unavailable: int = 0
    skipped_live: int = 0
    origins: dict = field(default_factory=lambda: {"uploaderProvided": 0, "autoGenerated": 0})
    scraped: int = 0               # requests made this run
    failed: int = 0                # videos whose requests failed this run
    status: str = "ok"             # ok | partial | unreadable
    reason: str = ""
    request_ids: list[str] = field(default_factory=list)
    refreshed: bool = False

    def report(self) -> dict:
        return {
            "handle": self.source.key,
            "channelId": self.channel_id,
            "requested": self.requested,
            "since": self.since,
            "listed": self.listed,
            "got": self.got,
            "added": self.added,
            "total": self.total,
            "noTranscript": self.no_transcript,
            "unavailable": self.unavailable,
            "skippedLive": self.skipped_live,
            "transcriptOrigin": self.origins,
            "scrapedThisRun": self.scraped,
            "failedThisRun": self.failed,
            "status": self.status,
            "reason": self.reason,
            "requestIds": self.request_ids,
        }


class _LanguageRejected(Exception):
    pass


class _Unusable(Exception):
    pass


class _ChannelRun:
    def __init__(self, client: YouTubeApi | None, source: Source, n: int | None, raw_root: Path, *,
                 since: datetime | None, update: bool, refresh: bool, cached: bool,
                 retry_missing: bool, recheck_hours: float, max_videos: int, workers: int,
                 now: datetime):
        self.client = client
        self.source = source
        self.n = n
        self.since = since
        self.since_day = to_utc_iso(since)[:10] if since else None
        self.update = update
        self.cached = cached
        self.retry_missing = retry_missing
        self.recheck = timedelta(hours=recheck_hours)
        self.max_videos = max_videos
        self.workers = max(1, workers)
        self.now = now
        self.result = ChannelResult(source, n, to_utc_iso(since) if since else None, refreshed=refresh)
        self.lock = threading.Lock()
        self.errors: list[str] = []
        self.failed: set[str] = set()
        self.live: set[str] = set()
        self.resolved: set[str] = set()      # handled this run: never queued twice
        self.language_rejected = ""
        self.capped = False
        self.metas: dict[str, VideoMeta] = {}
        self.posts: dict[str, dict] = {}
        self.origins: dict[str, str] = {}
        if source.problem:
            return
        self.cache = ChannelCache(raw_root / source.key)
        if refresh:
            self.cache.clear()
        self._load_listings()
        self._load_videos()

    # -- saved state --

    def _load_listings(self) -> None:
        self.order: list[str] = []
        self.listed: dict[str, tuple[ListedVideo, datetime]] = {}
        self.limit = 0
        self.listing_found = False
        complete = False
        for entry, data in self.cache.listings_newest_first():
            listing = parse_listing(data)
            if not listing.found:
                continue
            self.listing_found = True
            at = _parse_iso(entry.get("scrapedAt")) or self.now
            for video in listing.videos:
                if video.video_id not in self.listed:
                    self.listed[video.video_id] = (video, at)
                    self.order.append(video.video_id)
            limit = int(entry.get("limit") or len(listing.videos))
            self.limit = max(self.limit, limit)
            if len(listing.videos) < limit or (listing.video_count is not None
                                               and len(listing.videos) >= listing.video_count):
                complete = True                  # this listing held every video the channel had
        self.more_listed = not complete

    def _load_videos(self) -> None:
        for video_id, entry in list(self.cache.videos.items()):
            if "metadata" in entry:
                meta = parse_metadata(self.cache.read(entry["metadata"]), video_id)
                if meta is not None:
                    self.metas[video_id] = meta
            if "subtitles" in entry and video_id in self.metas:
                self._absorb(video_id, parse_subtitles(self.cache.read(entry["subtitles"])))

    @property
    def channel_name(self) -> str:
        return self.source.name or self.cache.meta.get("channelName") or self.source.key

    def _absorb(self, video_id: str, tracks: Sequence[Track]) -> bool:
        meta = self.metas[video_id]
        hint = self.source.language or (meta.spoken_languages[0] if meta.spoken_languages else None)
        track = choose_track(tracks, hint)
        if track is None or meta.date_time is None:
            return False
        listed = self.listed.get(video_id)
        self.posts[video_id] = to_post(meta, track, self.channel_name,
                                       listed[0].title if listed else "")
        self.origins[video_id] = track.origin
        return True

    # -- requests --

    def _call(self, endpoint: str, query: str, **params: Any) -> MediaResult:
        assert self.client is not None, "no requests in a cached run"
        res = self.client.youtube(endpoint, query, **{k: v for k, v in params.items() if v is not None})
        with self.lock:
            self.result.scraped += 1
            if res.request_id:
                self.result.request_ids.append(res.request_id)
        return res

    def _listing_due(self) -> bool:
        newest = self.cache.newest_listing_time()
        return newest is None or self.update or self.now - newest >= self.recheck

    def _fetch_listing(self, limit: int) -> bool:
        res = self._call("channel", self.source.handle or "", limit=limit)
        listing = parse_listing(res.data)
        if not listing.found:
            self.cache.save_empty(res.data)
            return False
        self.cache.save_listing(res.data, res.request_id, limit, listing, self.now)
        self._load_listings()
        for video_id in self.posts:              # titles from the newest listing, if metadata lacks one
            if not self.posts[video_id]["title"] and video_id in self.listed:
                self.posts[video_id]["title"] = self.listed[video_id][0].title
        return True

    # -- which videos --

    def _known_day(self, video_id: str) -> str | None:
        meta = self.metas.get(video_id)
        return meta.date_time[:10] if meta and meta.date_time else None

    def _before_start(self, video_id: str) -> bool:
        """Is this video older than the start date? Dates compare by day, since the API gives
        only the upload day. Before metadata, the listing's relative date decides, with slack."""
        day = self._known_day(video_id)
        if day is not None:
            return day < (self.since_day or "")
        video, listed_at = self.listed[video_id]
        age = relative_min_age(video.relative)
        return age is not None and self.since is not None and listed_at - age < self.since - DATE_MARGIN

    def _state(self, video_id: str) -> str:
        if video_id in self.failed:
            return "failed"
        if video_id in self.live:
            return "live"
        if video_id in self.posts:
            return "done"
        fresh = video_id in self.resolved
        if video_id in self.cache.unavailable:
            return "pending" if self.retry_missing and not fresh else "unavailable"
        mark = self.cache.no_transcript.get(video_id)
        if mark is not None:
            if fresh:
                return "none"
            due = self.retry_missing or (not mark.get("retried") and self.now >= retry_due(mark))
            return "pending" if due else "none"
        return "pending"

    def _walk(self) -> tuple[list[str], bool, list[str], Counter]:
        """Go through the listed videos newest first until the count or the start date is
        reached. Returns the videos still to fetch, whether a limit was reached, the videos
        selected so far and a count of each state seen."""
        batch: list[str] = []
        selected: list[str] = []
        counts: Counter = Counter()
        reached = False
        for video_id in self.order:
            if self.n is not None and len(selected) + len(batch) >= self.n:
                reached = True
                break
            if self.since_day and self._before_start(video_id):
                reached = True
                break
            state = self._state(video_id)
            counts[state] += 1
            if state == "done":
                selected.append(video_id)
            elif state == "pending":
                batch.append(video_id)
        if self.cached or self.language_rejected:
            batch = []
        return batch, reached, selected, counts

    def _resolve(self, video_id: str) -> None:
        if self.language_rejected:               # queued before the rejection: don't spend more
            return
        retry = video_id in self.cache.no_transcript or video_id in self.cache.unavailable
        meta = None if retry else self.metas.get(video_id)
        if meta is None or meta.upcoming_or_live:
            res = self._call("metadata", video_id)
            meta = parse_metadata(res.data, video_id)
            if meta is None:
                reason = status_reason(res.data) or "no metadata: deleted, private or restricted"
                self.cache.mark_unavailable(video_id, reason, self.now, res.request_id)
                return
            if meta.upcoming_or_live:            # not remembered: a later run picks it up
                with self.lock:
                    self.live.add(video_id)
                return
            self.cache.save_video("metadata", video_id, res.data, res.request_id, self.now)
            with self.lock:
                self.metas[video_id] = meta
        if meta.date_time is None:
            raise _Unusable("the metadata has no upload date")
        if self.since_day and meta.date_time[:10] < self.since_day:
            return                               # older than the start date: no transcript needed
        if (meta.transcript_available is False and not meta.spoken_languages
                and not meta.subtitle_languages):
            self.cache.mark_no_transcript(video_id, meta.date_time, self.now, retried=retry,
                                          reason="metadata: no transcript")
            return
        hint = self.source.language or (
            meta.spoken_languages[0] if len(meta.spoken_languages) == 1 else None)
        if self.language_rejected:
            return
        try:
            res = self._call("subtitles", video_id, language_code=hint)
        except ValidationError as e:
            if self.source.language:
                raise _LanguageRejected(
                    f"language code rejected: fix it in youtube_sources.json ({e})") from None
            raise
        tracks = parse_subtitles(res.data)
        if not tracks and hint:                  # maybe in another language: ask for all of them
            hint = None
            res = self._call("subtitles", video_id)
            tracks = parse_subtitles(res.data)
        self.cache.save_video("subtitles", video_id, res.data, res.request_id, self.now,
                              languageCode=hint)
        with self.lock:
            usable = self._absorb(video_id, tracks)
        if usable:
            self.cache.clear_marks(video_id)
        else:
            self.cache.mark_no_transcript(video_id, meta.date_time, self.now, retried=retry,
                                          reason="no usable subtitle track")

    def _resolve_all(self, batch: Sequence[str]) -> None:
        failed_before = len(self.failed)
        with ThreadPoolExecutor(max_workers=min(self.workers, len(batch))) as pool:
            futures = {pool.submit(self._resolve, video_id): video_id for video_id in batch}
            try:
                for future in as_completed(futures):
                    video_id = futures[future]
                    self.resolved.add(video_id)
                    try:
                        future.result()
                    except FATAL:
                        raise
                    except _LanguageRejected as e:
                        self.language_rejected = str(e)
                    except (WebApiError, _Unusable) as e:
                        self.failed.add(video_id)
                        self.errors.append(f"{video_id}: {e}")
                        log.warning("%s: %s: %s", self.source.key, video_id, e)
                    except Exception as e:  # a bug or odd response must not end the channel
                        log.exception("%s: %s: unexpected error", self.source.key, video_id)
                        self.failed.add(video_id)
                        self.errors.append(f"{video_id}: unexpected error: {e.__class__.__name__}: {e}")
            except FATAL:
                for f in futures:
                    f.cancel()
                raise
        if len(self.failed) - failed_before == len(batch):
            raise _BatchFailed()

    # -- the run --

    def run(self) -> ChannelResult:
        result = self.result
        if self.source.problem:
            result.status, result.reason = "unreadable", self.source.problem
            return result
        if not self.cached and self._listing_due():
            limit = max(MIN_LISTING, 2 * self.n) if self.n is not None else DATE_LISTING
            try:
                if not self._fetch_listing(min(self.max_videos, limit)):
                    self.errors.append(f"the channel endpoint returned no channel for "
                                       f"{self.source.handle} (wrong handle, or the channel is gone)")
            except FATAL:
                raise
            except WebApiError as e:
                self.errors.append(f"couldn't list the channel's videos: {e}")
        if not self.listing_found:
            result.status = "unreadable"
            result.reason = self.errors[0] if self.errors else "no saved listing yet (run without --cached)"
            return self._finish(final=False)
        try:
            while True:
                batch, reached, _, _ = self._walk()
                if batch:
                    self._resolve_all(batch)
                    continue
                if reached or not self.more_listed or self.cached or self.language_rejected:
                    break
                if self.limit >= self.max_videos:
                    self.capped = True
                    break
                known = len(self.order)
                try:
                    found = self._fetch_listing(min(self.max_videos, self.limit * 2))
                except FATAL:
                    raise
                except WebApiError as e:
                    self.errors.append(f"couldn't list more videos: {e}")
                    break
                if not found or len(self.order) <= known:
                    break
        except _BatchFailed:
            log.warning("%s: every request in a batch failed; stopping this channel", self.source.key)
        return self._finish(final=True)

    def _finish(self, *, final: bool) -> ChannelResult:
        result = self.result
        result.channel_id = self.cache.meta.get("channelId")
        result.listed = len(self.order)
        rank = {video_id: i for i, video_id in enumerate(self.order)}
        posts = sorted(self.posts.items(), key=lambda kv: rank.get(kv[0], len(rank)))
        posts.sort(key=lambda kv: kv[1]["dateTime"] or "", reverse=True)     # stable: ties keep order
        result.posts = [post for _, post in posts]
        result.failed = len(self.failed)
        if not final:
            return result
        _, reached, selected, counts = self._walk()
        result.selected = [self.posts[v] for v in selected]
        result.got = len(selected)
        result.no_transcript = counts["none"]
        result.unavailable = counts["unavailable"]
        result.skipped_live = counts["live"]
        origins = Counter(self.origins[v] for v in selected)
        result.origins = {"uploaderProvided": origins[UPLOADER], "autoGenerated": origins[AUTO]}
        pending = counts["pending"]
        if self.language_rejected:
            result.status, result.reason = "partial", self.language_rejected
        elif self.errors:
            more = f" (and {len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
            result.status, result.reason = "partial", f"{self.errors[0]}{more}"
        elif self.capped and not reached:
            result.status = "partial"
            result.reason = f"stopped at the --max-videos limit ({self.max_videos} videos listed)"
        elif pending and self.cached:
            result.status = "partial"
            result.reason = f"{pending} video(s) not fetched yet (--cached makes no requests)"
        elif self.n is not None and len(selected) < self.n and not (self.since_day and reached):
            result.status = "partial"
            result.reason = (f"only {len(selected)} video(s) with a transcript among the "
                             f"channel's {len(self.order)} listed")
        else:
            result.status = "ok"
            if self.since_day and not selected:
                result.reason = f"no videos with a transcript since {self.since_day}"
        return result


class _BatchFailed(Exception):
    pass


def fetch_channel(client: YouTubeApi | None, source: Source, n: int | None,
                  raw_root: Path = RAW_DIR, *, since: datetime | None = None,
                  update: bool = False, refresh: bool = False, cached: bool = False,
                  retry_missing: bool = False, recheck_hours: float = DEFAULT_RECHECK_HOURS,
                  max_videos: int = DEFAULT_MAX_VIDEOS, workers: int = 4,
                  now: datetime | None = None) -> ChannelResult:
    """Transcripts of the N newest videos, every video since `since`, or both (whichever
    stops first). cached=True makes no requests (client may be None)."""
    if n is None and since is None:
        raise ValueError("give a video count, a start date, or both")
    if not cached and client is None:
        raise ValueError("a client is needed unless cached=True")
    return _ChannelRun(client, source, n, raw_root, since=since, update=update, refresh=refresh,
                       cached=cached, retry_missing=retry_missing, recheck_hours=recheck_hours,
                       max_videos=max_videos, workers=workers,
                       now=now or datetime.now(timezone.utc)).run()


# ---- many channels ----------------------------------------------------------

def run_channels(client: YouTubeApi | None, sources: Sequence[Source], n: int | None, *,
                 since: datetime | None = None, update: bool = False, refresh: bool = False,
                 cached: bool = False, retry_missing: bool = False,
                 recheck_hours: float = DEFAULT_RECHECK_HOURS,
                 max_videos: int = DEFAULT_MAX_VIDEOS, parallel: int = 3, workers: int = 4,
                 raw_root: Path = RAW_DIR, parsed_root: Path = PARSED_DIR,
                 on_result: Callable[[int, ChannelResult], None] | None = None,
                 now: datetime | None = None) -> list[ChannelResult]:
    """Fetch and parse every channel, then save posts.json and _report.json. One bad
    channel never stops the run; 401 / persistent 429 do (finished channels are still saved)."""
    now = now or datetime.now(timezone.utc)
    results: dict[str, ChannelResult] = {}
    order = {s.key: i for i, s in enumerate(sources)}
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            futures = {pool.submit(fetch_channel, client, s, n, raw_root, since=since, update=update,
                                   refresh=refresh, cached=cached, retry_missing=retry_missing,
                                   recheck_hours=recheck_hours, max_videos=max_videos,
                                   workers=workers, now=now): s
                       for s in sources}
            try:
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        result = future.result()
                    except FATAL:
                        raise
                    except Exception as e:  # a bug or odd response must not end the run
                        log.exception("%s: unexpected error", source.key)
                        result = ChannelResult(source, n, to_utc_iso(since) if since else None,
                                               status="unreadable",
                                               reason=f"unexpected error: {e.__class__.__name__}: {e}")
                    results[source.key] = result
                    done += 1
                    if on_result:
                        on_result(done, result)
            except FATAL:
                for f in futures:
                    f.cancel()
                raise
    finally:
        finished = sorted(results.values(), key=lambda r: order[r.source.key])
        write_posts(finished, parsed_root, all_sources=sources)
        write_report(finished, parsed_root, all_sources=sources)
    return finished


def _ordered(keys: Iterable[str], all_sources: Sequence[Source]) -> list[str]:
    """Source-list order first, then anything no longer in the list, alphabetically."""
    rank = {s.key: i for i, s in enumerate(all_sources)}
    return sorted(keys, key=lambda k: (rank.get(k, len(rank)), k))


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)          # atomic: a crash never leaves a half-written file


def write_posts(results: Sequence[ChannelResult], parsed_root: Path = PARSED_DIR,
                all_sources: Sequence[Source] = ()) -> Path:
    """Merge into posts.json ({key: [videos]}). Each channel keeps its videos from earlier
    runs; this run's videos are added, and a video saved again replaces its earlier copy.
    Sets each result's `added` and `total`."""
    path = parsed_root / POSTS_NAME
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}
    for r in results:
        old = [] if r.refreshed else [p for p in existing.get(r.source.key, []) if isinstance(p, dict)]
        known = {p.get("postUrl") for p in old}
        merged = {p["postUrl"]: p for p in r.posts}
        merged.update({p["postUrl"]: p for p in old if p.get("postUrl") not in merged})
        videos = sorted(merged.values(), key=lambda p: p.get("dateTime") or "", reverse=True)
        r.added = sum(p["postUrl"] not in known for p in r.posts)
        r.total = len(videos)
        existing[r.source.key] = videos
    _write_json(path, {k: existing[k] for k in _ordered(existing, all_sources)})
    return path


def write_report(results: Sequence[ChannelResult], parsed_root: Path = PARSED_DIR,
                 all_sources: Sequence[Source] = ()) -> Path:
    """Merge into _report.json so a run over a few channels keeps the others' entries."""
    path = parsed_root / REPORT_NAME
    try:
        existing = {e["handle"]: e for e in json.loads(path.read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError, TypeError):
        existing = {}
    for r in results:
        existing[r.source.key] = r.report()
    _write_json(path, [existing[k] for k in _ordered(existing, all_sources)])
    return path
