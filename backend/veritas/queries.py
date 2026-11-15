"""What the screens read: lists filtered, sorted and counted, and single reports.

The database holds summaries written by the analysis jobs (migration 0004). This
module only selects them. Filtering uses veritas/filters.py, the same rules as the
frontend (shared/cases/filters.json), done in Python so both sides agree exactly
(e.g. claim search must be accent-sensitive, which MySQL's collation isn't).
Speed isn't the priority yet: lists are read whole, then filtered.

Orders (the frontend's demo sorts the same way; ties by id, compared as plain text):
  narratives   largest cluster first
  claims       most recently seen first
  sources      most near copies first
  stories      most posts first
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pymysql

from .filters import DateRange, matches_platform, matches_query, matches_status, overlaps
from .links import link_key
from .models import (
    ChannelReport, Claim, ClaimsResponse, Cluster, MapPoint, NarrativesResponse, Run, Source,
    SourcesResponse, StatusCounts, Story, TrendingResponse,
)

# Most items a list returns (counts and totals still cover everything).
MAX_CLUSTERS = 200
MAX_POINTS = 20_000
MAX_CLAIMS = 500
MAX_SOURCES = 1_000
MAX_STORIES = 50

# Platform codes (platforms.code) shown in the API, and back.
PLATFORM_NAMES = {"telegram": "Telegram", "facebook": "Facebook", "web": "Website"}
PLATFORM_CODES = {name: code for code, name in PLATFORM_NAMES.items()}

# The database's finer verdicts, as the four the screens show.
VERDICTS = {
    "true": "true", "mostly_true": "true",
    "false": "false", "mostly_false": "false",
    "misleading": "misleading", "mixed": "misleading",
    "unverified": "unverified", "unverifiable": "unverified",
}
# Evidence stance in the database -> on screen.
STANCES = {"supports": "supports", "refutes": "contradicts", "context": "context", "mixed": "neither", "unrelated": "neither"}

NO_SOURCE_REPORT = "There is no report for this source yet."
NO_STORY = "That story isn't available."
NOT_A_LINK = "That isn't a Telegram or Facebook link."
NOT_COLLECTED = "We haven't collected that post yet. Analyze it to see where it came from."


class NotFound(Exception):
    """Nothing to show. The message is shown to users as is."""


@dataclass(frozen=True)
class ListFilters:
    platform: str = "all"            # "all" or a platform name
    range: DateRange = DateRange()   # empty sides = no limit
    status: str = "all"              # "all" or a verdict


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _day(value: date | datetime | None, fallback: str) -> str:
    if value is None:
        return fallback
    return (value.date() if isinstance(value, datetime) else value).isoformat()


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _num(value: Decimal | int | float | None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _counts(verdicts: list[str]) -> StatusCounts:
    counts = {"all": len(verdicts), "true": 0, "false": 0, "misleading": 0, "unverified": 0}
    for verdict in verdicts:
        counts[verdict] += 1
    return StatusCounts(**counts)


def _rows(conn: pymysql.connections.Connection, sql: str, args: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


# --------------------------------------------------------------------------- narratives

def narratives(conn, f: ListFilters) -> NarrativesResponse:
    runs = _rows(conn, "SELECT run_id, finished_at, started_at, post_count, cluster_count, unclustered_count"
                       " FROM clustering_runs WHERE status = 'done' ORDER BY finished_at DESC, run_id DESC LIMIT 1")
    if not runs:
        empty = _counts([])
        return NarrativesResponse(run=Run(date=_today(), posts=0, clusters=0, unclustered=0),
                                  clusters=[], total=0, counts=empty, points=[])
    run_row = runs[0]
    run_day = _day(run_row["finished_at"] or run_row["started_at"], _today())
    run = Run(date=run_day, posts=run_row["post_count"] or 0, clusters=run_row["cluster_count"] or 0,
              unclustered=run_row["unclustered_count"] or 0)

    rows = _rows(conn, "SELECT c.cluster_id, c.label, c.size, c.change_pct, c.accounts, c.first_seen, c.last_seen,"
                       " c.stats, c.created_at, cl.verdict AS main_verdict"
                       " FROM clusters c LEFT JOIN claims cl ON cl.claim_id = c.main_claim_id"
                       " WHERE c.run_id = %s", (run_row["run_id"],))
    cards: list[Cluster] = []
    for row in rows:
        stats = _json(row["stats"], {})
        created = _day(row["created_at"], run_day)
        cards.append(Cluster(
            id=row["cluster_id"], name=row["label"] or "Unnamed narrative", size=row["size"],
            change_pct=_num(row["change_pct"]), accounts=row["accounts"],
            status=VERDICTS.get(row["main_verdict"] or "unverified", "unverified"),
            platforms=stats.get("platforms", []),
            first_seen=_day(row["first_seen"], created), last_seen=_day(row["last_seen"], created),
            languages=stats.get("languages", []), typical=stats.get("typical", []), edge=stats.get("edge", []),
        ))
    cards.sort(key=lambda c: (-c.size, c.id))
    base = [c for c in cards if matches_platform(c.platforms, f.platform) and overlaps(c.first_seen, c.last_seen, f.range)]
    visible = [c for c in base if matches_status(c.status, f.status)][:MAX_CLUSTERS]

    # Points: posts that fit no cluster first, then each visible cluster's, in list order.
    point_rows = _rows(conn, "SELECT cluster_id, map_x, map_y FROM post_clusters"
                             " WHERE run_id = %s AND map_x IS NOT NULL AND map_y IS NOT NULL"
                             " ORDER BY rank_in_cluster IS NULL, rank_in_cluster, post_id", (run_row["run_id"],))
    by_cluster: dict[int | None, list[MapPoint]] = {}
    for p in point_rows:
        by_cluster.setdefault(p["cluster_id"], []).append(
            MapPoint(x=_num(p["map_x"]), y=_num(p["map_y"]), cluster_id=p["cluster_id"]))
    points = list(by_cluster.get(None, []))
    for card in visible:
        points.extend(by_cluster.get(card.id, []))

    return NarrativesResponse(run=run, clusters=visible, total=len(cards),
                              counts=_counts([c.status for c in base]), points=points[:MAX_POINTS])


# --------------------------------------------------------------------------- claims

def claims(conn, f: ListFilters, query: str = "") -> ClaimsResponse:
    rows = _rows(conn, "SELECT claim_id, text, verdict, platforms, languages, first_seen, last_seen, post_count,"
                       " verdict_explanation, verdict_method, verdict_confidence, verdict_at, created_at, cluster_id"
                       " FROM claims")
    items = []
    for row in rows:
        created = _day(row["created_at"], _today())
        items.append({
            "row": row,
            "verdict": VERDICTS.get(row["verdict"], "unverified"),
            "platforms": _json(row["platforms"], []),
            "first_seen": _day(row["first_seen"], created),
            "last_seen": _day(row["last_seen"], created),
        })
    items.sort(key=lambda it: it["row"]["claim_id"])
    items.sort(key=lambda it: it["last_seen"], reverse=True)  # stable: newest first, then by id
    base = [it for it in items
            if matches_query(it["row"]["text"], query)
            and matches_platform(it["platforms"], f.platform)
            and overlaps(it["first_seen"], it["last_seen"], f.range)]
    visible = [it for it in base if matches_status(it["verdict"], f.status)][:MAX_CLAIMS]

    evidence: dict[int, list[dict]] = {}
    ids = [it["row"]["claim_id"] for it in visible]
    if ids:
        marks = ", ".join(["%s"] * len(ids))
        for ev in _rows(conn, f"SELECT claim_id, publisher, title, url, stance, excerpt FROM claim_evidence"
                              f" WHERE claim_id IN ({marks}) ORDER BY evidence_id", tuple(ids)):
            evidence.setdefault(ev["claim_id"], []).append({
                "source": ev["publisher"] or ev["title"] or ev["url"] or "Unknown source",
                "stance": STANCES.get(ev["stance"], "neither"),
                "note": ev["excerpt"] or "",
                "url": ev["url"],
            })

    out = []
    for it in visible:
        row = it["row"]
        out.append(Claim(
            id=row["claim_id"], text=row["text"], verdict=it["verdict"], platforms=it["platforms"],
            first_seen=it["first_seen"], last_seen=it["last_seen"], posts=row["post_count"],
            languages=_json(row["languages"], []), explanation=row["verdict_explanation"] or "",
            evidence=evidence.get(row["claim_id"], []), checked_by=row["verdict_method"] or "",
            confidence=_num(row["verdict_confidence"]) or 0, checked_at=_day(row["verdict_at"], it["first_seen"]),
            cluster_id=row["cluster_id"],
        ))
    return ClaimsResponse(claims=out, total=len(items), counts=_counts([it["verdict"] for it in base]))


# --------------------------------------------------------------------------- sources

def _source_rows(conn, where: str = "", args: tuple = ()) -> list[dict]:
    return _rows(conn, "SELECT sp.source_key, sp.kind_label, sp.posts_30d, sp.original_pct, sp.near_copy_pct,"
                       " sp.main_topic, sp.open_flags, sp.report, a.display_name, a.handle, p.code AS platform_code"
                       " FROM source_profiles sp JOIN accounts a ON a.account_id = sp.account_id"
                       " JOIN platforms p ON p.platform_id = a.platform_id"
                       " WHERE a.deleted_at IS NULL" + where, args)


def _source_name(row: dict) -> str:
    return row["display_name"] or row["handle"] or row["source_key"]


def sources(conn, platform: str = "all") -> SourcesResponse:
    out = []
    for row in _source_rows(conn):
        name = PLATFORM_NAMES.get(row["platform_code"])
        if name is None or (platform != "all" and name != platform):
            continue  # platforms the screens don't show yet
        out.append(Source(
            id=row["source_key"], name=_source_name(row), kind=row["kind_label"], platform=name,
            posts_30d=row["posts_30d"], original_pct=_num(row["original_pct"]), near_copy_pct=_num(row["near_copy_pct"]),
            main_topic=row["main_topic"], open_flags=row["open_flags"],
        ))
    out.sort(key=lambda s: (-s.near_copy_pct, s.id))
    return SourcesResponse(sources=out[:MAX_SOURCES])


def source_report(conn, source_key: str) -> ChannelReport:
    rows = _source_rows(conn, " AND sp.source_key = %s", (source_key,))
    # The key column's collation ignores case; links are exact.
    rows = [r for r in rows if r["source_key"] == source_key]
    if not rows or rows[0]["report"] is None or rows[0]["platform_code"] not in PLATFORM_NAMES:
        raise NotFound(NO_SOURCE_REPORT)
    row = rows[0]
    report = _json(row["report"], {})
    return ChannelReport.model_validate({**report, "id": row["source_key"], "name": _source_name(row),
                                         "platform": PLATFORM_NAMES[row["platform_code"]]})


# --------------------------------------------------------------------------- stories

_STORY_COLUMNS = ("SELECT s.story_key, s.status, s.topic, s.first_seen, s.last_seen, s.trend_posts, s.trend_change_pct,"
                  " s.body, p.code AS platform_code FROM stories s JOIN platforms p ON p.platform_id = s.platform_id")


def _story(row: dict) -> Story:
    body = _json(row["body"], {})
    return Story.model_validate({
        **body,
        "id": row["story_key"], "platform": PLATFORM_NAMES[row["platform_code"]],
        "status": row["status"], "topic": row["topic"],
        "firstSeen": _day(row["first_seen"], ""), "lastSeen": _day(row["last_seen"], ""),
        "trend": {"posts": row["trend_posts"], "changePct": _num(row["trend_change_pct"])},
    })


def trending(conn, f: ListFilters) -> TrendingResponse:
    stories = [_story(r) for r in _rows(conn, _STORY_COLUMNS) if r["platform_code"] in PLATFORM_NAMES]
    stories.sort(key=lambda s: (-s.trend.posts, s.id))
    keep = [s for s in stories if matches_platform([s.platform], f.platform) and matches_status(s.status, f.status)
            and overlaps(s.first_seen, s.last_seen, f.range)]
    return TrendingResponse(stories=keep[:MAX_STORIES])


_POST_STORY = re.compile(r"^post-([1-9][0-9]{0,18})$")


def story(conn, story_key: str) -> Story:
    rows = [r for r in _rows(conn, _STORY_COLUMNS + " WHERE s.story_key = %s", (story_key,))
            if r["story_key"] == story_key and r["platform_code"] in PLATFORM_NAMES]
    if rows:
        return _story(rows[0])
    m = _POST_STORY.match(story_key)
    if m:   # a collected post that isn't a trending story: told on request (veritas/derive.py)
        from .derive import build_story
        built = build_story(conn, int(m.group(1)), picked=True)
        if built is not None:
            return Story.model_validate(built)
    raise NotFound(NO_STORY)


def trace(conn, url: str) -> Story:
    """The story whose post is this link (written any way linkKey() treats as the same)."""
    key = link_key(url)
    rows = [r for r in _rows(conn, _STORY_COLUMNS.replace("s.body,", "s.body, s.post_link_key,")
                             + " WHERE s.post_link_key = %s", (key,))
            if r["post_link_key"] == key and r["platform_code"] in PLATFORM_NAMES]
    if rows:
        rows.sort(key=lambda r: (-r["trend_posts"], r["story_key"]))
        return _story(rows[0])
    # Not a trending story's post, but maybe collected: tell its own story.
    from .analysis import _find_post
    from .derive import build_story
    from .links import parse_link
    link = parse_link(url)
    found = _find_post(conn, link) if link is not None and link.kind == "post" else None
    if found is None and link is not None:   # stored under another id format (e.g. Facebook reels): the link itself
        bare = url.strip()
        found = next(iter(_rows(conn, "SELECT post_id FROM posts WHERE url IN (%s, %s, %s) AND deleted_at IS NULL"
                                      " ORDER BY post_id LIMIT 1", (bare, bare.rstrip("/"), bare.rstrip("/") + "/"))), None)
    built = build_story(conn, found["post_id"], picked=True) if found else None
    if built is None:
        raise NotFound(NOT_COLLECTED)
    return Story.model_validate(built)
