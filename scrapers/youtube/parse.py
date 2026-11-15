"""Parse the Oxylabs YouTube responses: channel listings, video metadata and subtitle tracks.

Shapes checked against real responses on 2026-11-15 (docs/oxylabs-youtube-scrape.md):
- channel:   results[0].json = {id, name, video_count, videos: [{pos, title, url,
             upload_date_relative, view_count}], ...}. The Videos tab: no Shorts, no streams.
- metadata:  results[0].json.results = {video_id, title, uploader, channel_id, upload_date
             (YYYYMMDD, UTC), duration, is_live, is_transcript_available,
             generated_subtitle_languages, ...}
- subtitles: results[0].json = {"auto_generated" | "uploader_provided": {"<lang>": track}},
             each track in YouTube's json3 format (events[].segs[].utf8). {} = no tracks.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

UPLOADER = "uploader_provided"
AUTO = "auto_generated"
ORIGINS = (UPLOADER, AUTO)           # anything else (e.g. translations) is ignored

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_UPLOAD_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_RELATIVE = re.compile(r"(\d+)\s*([a-z]+)", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_SUCCESS_CODES = {None, 200, 12000}


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def video_id_from_url(url: str) -> str | None:
    """watch?v=<id>, youtu.be/<id>, /shorts/<id>, /live/<id>, /embed/<id> -> <id>."""
    parsed = urllib.parse.urlparse((url or "").strip())
    candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    if not candidate:
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc.lower().endswith("youtu.be") and parts:
            candidate = parts[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            candidate = parts[1]
    return candidate if _VIDEO_ID.match(candidate or "") else None


def _first(data: Any) -> dict:
    results = data.get("results") if isinstance(data, dict) else None
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    return {}


def _text(value: Any) -> str:
    return _WHITESPACE.sub(" ", html.unescape(str(value))).strip() if value is not None else ""


def status_reason(data: Any) -> str:
    """The non-success status codes Oxylabs attached to a result, e.g. 'video_download 11202'."""
    statuses = (_first(data).get("metadata") or {}).get("statuses") or {}
    parts = []
    for name, status in statuses.items() if isinstance(statuses, dict) else ():
        if isinstance(status, dict) and status.get("code") not in _SUCCESS_CODES:
            parts.append(" ".join(str(x) for x in (name, status.get("code"), status.get("tag")) if x))
    return ", ".join(parts)


# ---- channel listing ----------------------------------------------------------

@dataclass(frozen=True)
class ListedVideo:
    video_id: str
    title: str
    relative: str        # "1d ago", "2w ago", "5mo ago": relative to when the listing was fetched
    position: int


@dataclass(frozen=True)
class Listing:
    channel_id: str | None
    name: str | None
    video_count: int | None
    videos: tuple[ListedVideo, ...]

    @property
    def found(self) -> bool:
        return bool(self.channel_id or self.videos)


def parse_listing(data: Any) -> Listing:
    body = _first(data).get("json")
    if not isinstance(body, dict):
        return Listing(None, None, None, ())
    videos, seen = [], set()
    for i, entry in enumerate(body.get("videos") or []):
        if not isinstance(entry, dict):
            continue
        video_id = video_id_from_url(str(entry.get("url") or ""))
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        position = entry.get("pos") if isinstance(entry.get("pos"), int) else i + 1
        videos.append(ListedVideo(video_id, _text(entry.get("title")),
                                  str(entry.get("upload_date_relative") or ""), position))
    count = body.get("video_count")
    if isinstance(count, str) and count.replace(",", "").isdigit():
        count = int(count.replace(",", ""))
    return Listing(str(body["id"]) if body.get("id") else None, _text(body.get("name")) or None,
                   count if isinstance(count, int) else None, tuple(videos))


_UNIT_SECONDS = (("mo", 28 * 86400),      # checked before "m" (minutes); 28 days is the shortest month
                 ("y", 365 * 86400), ("w", 7 * 86400), ("d", 86400), ("h", 3600),
                 ("mi", 60), ("m", 60), ("s", 1))


def relative_min_age(text: str) -> timedelta | None:
    """The least time that can have passed for a YouTube relative date ('5mo ago' -> 140 days).

    YouTube rounds down, so the real age lies between this and one unit more."""
    match = _RELATIVE.search(text or "")
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    for prefix, seconds in _UNIT_SECONDS:
        if unit.startswith(prefix):
            return timedelta(seconds=value * seconds)
    return None


# ---- video metadata ------------------------------------------------------------

@dataclass(frozen=True)
class VideoMeta:
    video_id: str
    title: str
    author: str
    channel_id: str | None
    date_time: str | None          # midnight UTC of upload_date (the API gives only the day)
    duration: int | None
    is_live: bool
    transcript_available: bool | None
    spoken_languages: tuple[str, ...]      # generated_subtitle_languages
    subtitle_languages: tuple[str, ...]    # uploader-provided languages, if the response lists them

    @property
    def upcoming_or_live(self) -> bool:
        """No finished recording yet, so no transcript yet either."""
        return self.is_live or self.duration == 0


def _languages(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict):
        value = list(value)
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v)


def parse_metadata(data: Any, video_id: str = "") -> VideoMeta | None:
    """None when the video is unavailable (deleted, private, age-restricted, members-only)."""
    body = _first(data).get("json")
    fields = body.get("results") if isinstance(body, dict) else None
    if not isinstance(fields, dict) or not (fields.get("video_id") or fields.get("title")):
        return None
    date_time = None
    for key in ("timestamp", "release_timestamp"):           # exact, if the API ever adds them
        if isinstance(fields.get(key), (int, float)) and fields[key] > 0:
            date_time = to_utc_iso(datetime.fromtimestamp(fields[key], timezone.utc))
            break
    if date_time is None:
        match = _UPLOAD_DATE.match(str(fields.get("upload_date") or ""))
        if match:
            date_time = f"{match.group(1)}-{match.group(2)}-{match.group(3)}T00:00:00Z"
    duration = fields.get("duration")
    available = fields.get("is_transcript_available")
    return VideoMeta(
        video_id=str(fields.get("video_id") or video_id),
        title=_text(fields.get("title")),
        author=_text(fields.get("uploader") or fields.get("channel")),
        channel_id=str(fields["channel_id"]) if fields.get("channel_id") else None,
        date_time=date_time,
        duration=int(duration) if isinstance(duration, (int, float)) else None,
        is_live=fields.get("is_live") is True or fields.get("live_status") in ("is_live", "is_upcoming"),
        transcript_available=available if isinstance(available, bool) else None,
        spoken_languages=_languages(fields.get("generated_subtitle_languages")),
        subtitle_languages=_languages(fields.get("subtitle_languages") or fields.get("subtitles")),
    )


# ---- subtitles --------------------------------------------------------------

@dataclass(frozen=True)
class Track:
    origin: str          # uploader_provided | auto_generated
    language: str
    text: str


def _cues(track: Any) -> list[str]:
    if isinstance(track, dict) and isinstance(track.get("events"), list):     # YouTube json3
        return ["".join(str(seg.get("utf8", "")) for seg in event.get("segs") or []
                        if isinstance(seg, dict))
                for event in track["events"] if isinstance(event, dict)]
    if isinstance(track, list):                                              # [{text}, ...]
        return [str(cue.get("text") or cue.get("utf8") or "") for cue in track if isinstance(cue, dict)]
    return []


def track_text(track: Any) -> str:
    """Cues in time order, joined with single spaces. Line breaks inside a cue become spaces,
    entities are unescaped, and a cue that repeats the previous one exactly is dropped
    (rolling auto-generated captions). Nothing else is cleaned or translated."""
    out: list[str] = []
    for cue in _cues(track):
        cue = _text(cue)
        if cue and (not out or cue != out[-1]):
            out.append(cue)
    return " ".join(out)


def parse_subtitles(data: Any) -> list[Track]:
    """Every usable track: uploader-provided first, then auto-generated. [] for none."""
    body = _first(data).get("json")
    if not isinstance(body, dict):
        return []
    tracks = []
    for origin in ORIGINS:
        by_language = body.get(origin)
        if not isinstance(by_language, dict):
            continue
        for language, track in by_language.items():
            text = track_text(track)
            if text:
                tracks.append(Track(origin, str(language), text))
    return tracks


def _same_language(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.lower().split("-")[0] == b.lower().split("-")[0]


def choose_track(tracks: Sequence[Track], language: str | None = None) -> Track | None:
    """One track per video (docs/oxylabs-youtube-scrape.md, "Which transcript"):

    1. An auto-generated track's language is the spoken one (the source's `language` if
       there's an auto track in it). Prefer the uploader's track in that language, else
       the auto-generated one.
    2. With no auto-generated track: the uploader's track in the source's `language`,
       else the first one listed.
    """
    auto = [t for t in tracks if t.origin == AUTO]
    uploaded = [t for t in tracks if t.origin == UPLOADER]
    if auto:
        spoken = next((t for t in auto if _same_language(t.language, language)), auto[0])
        return next((t for t in uploaded if _same_language(t.language, spoken.language)), spoken)
    if uploaded:
        return next((t for t in uploaded if _same_language(t.language, language)), uploaded[0])
    return None


# ---- output -----------------------------------------------------------------

def to_post(meta: VideoMeta, track: Track, channel_name: str, listed_title: str = "") -> dict:
    """The six output fields, in this order."""
    return {
        "author": meta.author or channel_name,
        "title": meta.title or listed_title,
        "content": track.text,
        "dateTime": meta.date_time,
        "channelName": channel_name,
        "postUrl": watch_url(meta.video_id),
    }
