"""Analyze a link (docs/analysis-cycle.md): the steps, the stored results, the report.

Steps (models.ANALYZE_STEPS, what the screen shows while it waits):
  0 Reading the post          find it in the database, or collect it (collectors.py)
  1 Making a text fingerprint  EmbeddingGemma vector, stored in Qdrant
  2 Finding similar posts      nearest posts in every language, how they relate, nearest cluster
  3 Checking the claims        Gemma extracts claims; each is matched to a known claim or stored as new
  4 Gemma reads the post       tone, intent, techniques; translation; categories
Then the report (models.PostReport) is stored with the job, so the same post is
answered at once next time, without asking the AI again.

The work runs in a background thread (JobRunner); the screen polls GET /api/analyze/{id}.
Speed isn't the priority yet: each step is plain and sequential.
"""
from __future__ import annotations

import json
import logging
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pymysql

from . import analyzers
from .ai import AiError, AiUnavailable, LocalAI
from .collectors import NOT_SET_UP, CollectedPost, CollectError, Collector
from .config import Settings
from .content import content_hash, store_post_vector
from .db import DatabaseUnavailable, connect
from .ids import NAMESPACE, account_uuid, post_uuid
from .links import ParsedLink, link_key, parse_link
from .models import AnalysisJob, ClusterCard, PostReport
from .queries import PLATFORM_NAMES, VERDICTS
from .vectors import Point, Qdrant, VectorStoreError, VectorStoreUnavailable

log = logging.getLogger("veritas.analysis")

# How similar (cosine, 0-1) two posts must be to count as...
COPY_MIN = 0.85          # ...the same content (copy, translation, close paraphrase)
SIMILAR_MIN = 0.70       # ...worth listing as similar
NEAR_COPY_MIN = 0.95     # ...a near copy (same language)
CLAIM_MATCH_MIN = 0.90   # ...the same claim
CLUSTER_MIN = 0.75       # ...belonging to a cluster
CATEGORY_MIN = 0.55      # ...a candidate category
CATEGORY_SURE = 0.80     # ...a category without asking Gemma
JOB_TIMEOUT = timedelta(minutes=20)
# How a similar post is labelled on screen -> post_relations.relation_type (also shown in the chain).
RELATION_TYPES = {"Near copy": "copy", "Translation": "translation", "Paraphrase": "paraphrase"}
DONE_STEP = 5

LANGUAGES = {"lt": "Lithuanian", "ru": "Russian", "en": "English", "pl": "Polish", "lv": "Latvian", "et": "Estonian",
             "uk": "Ukrainian", "be": "Belarusian", "de": "German", "fr": "French"}
LANGUAGE_CODES = {name.lower(): code for code, name in LANGUAGES.items()}
ACCOUNT_KINDS = {"channel": "channel", "group": "group", "page": "page", "website": "", "user": "profile"}

NOT_A_LINK = "That isn't a Telegram or Facebook link."
ACCOUNT_LINK = "That's a link to a channel, group or page. Paste a link to one of its posts."
UNKNOWN_POST_LINK = "That link doesn't say which post it is (for example a share link). Open the post and copy its own link."
NO_JOB = "That analysis isn't available."
NO_TEXT = "That post has no text to analyze."
INTERRUPTED = "The analysis stopped before it finished (the backend was restarted or took too long). Try again."
SERVER_ERROR = "Something went wrong during the analysis. Please try again."


class AnalysisRefused(Exception):
    """The request can't be analyzed. status = the HTTP answer; the message is shown as is."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass
class Services:
    """What a job needs. Tests swap in fakes."""
    settings: Settings
    ai: Callable[[], LocalAI]
    qdrant: Callable[[], Qdrant]
    collector: Collector
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------- small helpers

def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _rows(conn, sql: str, args: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def _one(conn, sql: str, args: tuple = ()) -> dict | None:
    rows = _rows(conn, sql, args)
    return rows[0] if rows else None


def _language_name(code: str | None) -> str:
    return LANGUAGES.get((code or "").lower(), (code or "Unknown").upper() if code else "Unknown")


def _where(platform_code: str, account_type: str | None) -> str:
    platform = PLATFORM_NAMES.get(platform_code, platform_code.title())
    if platform_code == "web":
        return "News site"
    kind = ACCOUNT_KINDS.get(account_type or "", "")
    return f"{platform} {kind}".strip()


def _author(row: dict) -> str:
    if row.get("platform") == "telegram" and row.get("handle"):
        return "@" + row["handle"]
    return row.get("display_name") or row.get("handle") or row.get("account_external_id") or "Unknown author"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _duration(delta: timedelta) -> str:
    minutes = int(round(delta.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    return f"{round(hours / 24)} days"


# --------------------------------------------------------------------------- jobs (database)

def _job_json(row: dict) -> dict:
    status = {"queued": "queued", "scraping": "running", "analyzing": "running", "done": "done", "failed": "failed"}[row["status"]]
    report = json.loads(row["result"]) if status == "done" and row["result"] else None
    return AnalysisJob(id=row["job_key"], status=status, step=row["step"], report=report,
                       error=row["error"] if status == "failed" else None,
                       error_status=row["error_status"] if status == "failed" else None).to_json()


_JOB_COLUMNS = "SELECT request_id, job_key, status, step, result, error, error_status, created_at, started_at FROM analysis_requests"


def get_job(conn, job_key: str, now: datetime | None = None) -> dict:
    row = _one(conn, _JOB_COLUMNS + " WHERE job_key = %s", (job_key,))
    if row is None or row["job_key"] != job_key:
        raise AnalysisRefused(NO_JOB, 404)
    now = now or datetime.now(timezone.utc)
    if row["status"] in ("queued", "scraping", "analyzing") and _utc(row["started_at"] or row["created_at"]) < now - JOB_TIMEOUT:
        _fail(conn, row["request_id"], INTERRUPTED, 500)
        row = _one(conn, _JOB_COLUMNS + " WHERE request_id = %s", (row["request_id"],))
    return _job_json(row)


def _fail(conn, request_id: int, message: str, status: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE analysis_requests SET status = 'failed', error = %s, error_status = %s,"
                    " finished_at = CURRENT_TIMESTAMP(3) WHERE request_id = %s", (message, status, request_id))
    conn.commit()


def _set(conn, request_id: int, **values: Any) -> None:
    cols = ", ".join(f"{k} = %s" for k in values)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE analysis_requests SET {cols} WHERE request_id = %s", (*values.values(), request_id))
    conn.commit()


def _find_post(conn, link: ParsedLink) -> dict | None:
    return _one(conn, "SELECT p.post_id FROM posts p JOIN platforms pl ON pl.platform_id = p.platform_id"
                      " WHERE pl.code = %s AND p.external_id = %s AND p.deleted_at IS NULL AND p.is_stub = FALSE",
                (link.platform, link.external_id))


def start(conn, services: Services, runner: "JobRunner", url: str) -> tuple[dict, bool]:
    """Check the request, then start (or reuse) a job. Returns (job json, is_new).
    Raises AnalysisRefused for answers the screen shows right away (400/404/503)."""
    link = parse_link(url)
    if link is None:
        raise AnalysisRefused(NOT_A_LINK, 400)
    if link.kind == "account":
        raise AnalysisRefused(ACCOUNT_LINK, 400)
    if link.kind != "post":
        raise AnalysisRefused(UNKNOWN_POST_LINK, 400)
    key = link_key(url)
    post = _find_post(conn, link)
    # The same post analyzed before (or being analyzed now): answer with that job, no second AI call.
    if post is not None:
        done = _one(conn, _JOB_COLUMNS + " WHERE post_id = %s AND status = 'done' ORDER BY request_id DESC LIMIT 1",
                    (post["post_id"],))
        if done:
            return _job_json(done), False
    busy = _one(conn, _JOB_COLUMNS + " WHERE link_key = %s AND status IN ('queued', 'scraping', 'analyzing')"
                      " ORDER BY request_id DESC LIMIT 1", (key,))
    if busy and _utc(busy["started_at"] or busy["created_at"]) >= services.now() - JOB_TIMEOUT:
        return _job_json(busy), False
    if post is None and not services.collector.available():
        raise AnalysisRefused(NOT_SET_UP, 404)
    ai = services.ai()
    try:
        ai.check()
    finally:
        ai.close()
    job_key = secrets.token_hex(16)
    platform = _one(conn, "SELECT platform_id FROM platforms WHERE code = %s", (link.platform,))
    with conn.cursor() as cur:
        cur.execute("INSERT INTO analysis_requests (job_key, input_url, link_key, target_type, platform_id, post_id, status, step)"
                    " VALUES (%s, %s, %s, 'post', %s, %s, 'queued', 0)",
                    (job_key, url.strip()[:2048], key, platform["platform_id"] if platform else None,
                     post["post_id"] if post else None))
    conn.commit()
    runner.submit(job_key, link, url.strip())
    return get_job(conn, job_key), True


# --------------------------------------------------------------------------- running a job

class JobRunner:
    """Runs analyses in background threads, so a slow one never blocks other requests."""

    def __init__(self, services: Services, workers: int = 1):
        self.services = services
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="analysis")

    def submit(self, job_key: str, link: ParsedLink, url: str):
        return self._pool.submit(run_job, self.services, job_key, link, url)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def run_job(services: Services, job_key: str, link: ParsedLink, url: str) -> None:
    try:
        conn = connect(services.settings)
    except DatabaseUnavailable:
        log.error("Analysis %s: database unavailable", job_key)
        return
    try:
        row = _one(conn, "SELECT request_id FROM analysis_requests WHERE job_key = %s", (job_key,))
        if row is None:
            return
        request_id = row["request_id"]
        try:
            report, post_id = _analyze(conn, services, request_id, link, url)
            _set(conn, request_id, status="done", step=DONE_STEP, result=json.dumps(report, ensure_ascii=False),
                 post_id=post_id, finished_at=services.now().replace(tzinfo=None))
        except BaseException as exc:  # every failure ends the job with a plain message
            conn.rollback()
            message, status = _explain(exc)
            if status >= 500 and not isinstance(exc, (AiUnavailable, DatabaseUnavailable, VectorStoreUnavailable)):
                log.exception("Analysis %s failed", job_key)
            _fail(conn, request_id, message, status)
            if not isinstance(exc, Exception):
                raise
    finally:
        conn.close()


def _explain(exc: BaseException) -> tuple[str, int]:
    if isinstance(exc, AnalysisRefused):
        return str(exc), exc.status
    if isinstance(exc, CollectError):
        return str(exc), exc.status
    if isinstance(exc, (AiUnavailable, DatabaseUnavailable, VectorStoreUnavailable)):
        return str(exc), 503
    if isinstance(exc, (AiError, VectorStoreError)):
        return str(exc), 502
    return SERVER_ERROR, 500


def _analyze(conn, services: Services, request_id: int, link: ParsedLink, url: str) -> tuple[dict, int]:
    _set(conn, request_id, status="scraping", step=0, started_at=services.now().replace(tzinfo=None))
    post = _read_post(conn, services, link)
    if not (post["text"] or "").strip():
        raise AnalysisRefused(NO_TEXT, 422)
    _set(conn, request_id, status="analyzing", step=1, post_id=post["post_id"])

    ai, qdrant = services.ai(), services.qdrant()
    try:
        vector = ai.embed_documents([post["text"]], [post["title"]])[0]
        point_id = store_post_vector(conn, qdrant, post["post_id"], vector)

        _set(conn, request_id, step=2)
        similar = _similar(conn, qdrant, post, vector, point_id)
        cluster = _cluster(conn, qdrant, vector)

        _set(conn, request_id, step=3)
        language = _language_name(post["language"])
        claims_answer = analyzers.extract_claims(ai, post["text"], language)
        claims = _store_claims(conn, ai, qdrant, post, claims_answer)

        _set(conn, request_id, step=4)
        reading = analyzers.read_post(ai, post["text"], language)
        translation = analyzers.translate(ai, post["text"])
        categories = _categories(conn, ai, qdrant, post, vector, language)
        _store_analyses(conn, post["post_id"], claims_answer, reading, translation)
        if not post["language"]:
            code = LANGUAGE_CODES.get(translation.value.source_language.strip().lower())
            if code:
                post["language"] = code
                with conn.cursor() as cur:
                    cur.execute("UPDATE posts SET language = %s WHERE post_id = %s", (code, post["post_id"]))
                conn.commit()
    finally:
        ai.close()
        qdrant.close()

    report = _report(url, services.now(), post, similar, cluster, claims, reading, translation, categories)
    return PostReport.model_validate(report).to_json(), post["post_id"]


# --------------------------------------------------------------------------- step 0: the post

_POST_COLUMNS = ("SELECT p.post_id, p.uuid, p.external_id, p.url, p.title, p.text, p.language, p.published_at,"
                 " pl.code AS platform, a.account_id, a.handle, a.display_name, a.account_type,"
                 " a.external_id AS account_external_id, sp.source_key"
                 " FROM posts p JOIN platforms pl ON pl.platform_id = p.platform_id"
                 " LEFT JOIN accounts a ON a.account_id = COALESCE(p.container_account_id, p.account_id)"
                 " LEFT JOIN source_profiles sp ON sp.account_id = a.account_id")


def _load_post(conn, post_id: int) -> dict:
    return _one(conn, _POST_COLUMNS + " WHERE p.post_id = %s", (post_id,))


def _read_post(conn, services: Services, link: ParsedLink) -> dict:
    found = _find_post(conn, link)
    if found:
        return _load_post(conn, found["post_id"])
    if not services.collector.available():
        raise AnalysisRefused(NOT_SET_UP, 404)
    collected = services.collector.collect(link)
    return _load_post(conn, save_collected(conn, collected))


def save_collected(conn, c: CollectedPost) -> int:
    """Store a collected post (and its account). Returns the post id. Runs again safely."""
    platform = _one(conn, "SELECT platform_id FROM platforms WHERE code = %s", (c.platform,))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (uuid, platform_id, external_id, handle, display_name, account_type, last_scraped_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP(3))"
            " ON DUPLICATE KEY UPDATE display_name = VALUES(display_name), last_scraped_at = VALUES(last_scraped_at),"
            " account_id = LAST_INSERT_ID(account_id)",
            (account_uuid(c.platform, c.account).bytes, platform["platform_id"], c.account,
             c.account if c.platform == "telegram" else None, c.account_name, c.account_type))
        account_id = cur.lastrowid
        if c.followers is not None:
            cur.execute("INSERT IGNORE INTO account_metrics (account_id, captured_at, followers) VALUES (%s, CURRENT_TIMESTAMP(3), %s)",
                        (account_id, c.followers))
        published = c.published_at.astimezone(timezone.utc).replace(tzinfo=None) if c.published_at else None
        cur.execute(
            "INSERT INTO posts (uuid, platform_id, account_id, container_account_id, external_id, url, title, text, byline,"
            " language, content_hash, published_at, scraped_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP(3))"
            " ON DUPLICATE KEY UPDATE text = VALUES(text), title = VALUES(title), content_hash = VALUES(content_hash),"
            " language = COALESCE(VALUES(language), language), scraped_at = VALUES(scraped_at),"
            " is_stub = FALSE, post_id = LAST_INSERT_ID(post_id)",
            (post_uuid(c.platform, c.external_id).bytes, platform["platform_id"], account_id, account_id, c.external_id,
             c.url, c.title, c.text, c.byline, c.language, content_hash(c.text), published))
        post_id = cur.lastrowid
        if c.views is not None:
            cur.execute("INSERT IGNORE INTO post_metrics (post_id, captured_at, views) VALUES (%s, CURRENT_TIMESTAMP(3), %s)",
                        (post_id, c.views))
    conn.commit()
    return post_id


# --------------------------------------------------------------------------- step 2: similar posts, cluster

@dataclass
class Related:
    row: dict
    score: float
    relation: str = "Similar"


def _similar(conn, qdrant: Qdrant, post: dict, vector, point_id: str) -> list[Related]:
    hits = [h for h in qdrant.similar_posts(vector, limit=25, exclude=[point_id]) if h.score >= SIMILAR_MIN]
    ids = [h.payload.get("post_id") for h in hits if isinstance(h.payload.get("post_id"), int)]
    rows = {}
    if ids:
        marks = ", ".join(["%s"] * len(ids))
        rows = {r["post_id"]: r for r in _rows(conn, _POST_COLUMNS + f" WHERE p.post_id IN ({marks}) AND p.deleted_at IS NULL", tuple(ids))}
    related = [Related(rows[h.payload["post_id"]], round(min(1.0, h.score), 4)) for h in hits if h.payload.get("post_id") in rows]
    mine = post["language"]
    for r in related:
        same_language = not mine or not r.row["language"] or r.row["language"] == mine
        if r.score >= NEAR_COPY_MIN and same_language:
            r.relation = "Near copy"
        elif r.score >= COPY_MIN and not same_language:
            r.relation = "Translation"
        elif r.score >= COPY_MIN:
            r.relation = "Paraphrase"
    copies = [r for r in related if r.score >= COPY_MIN]
    with conn.cursor() as cur:
        for r in copies:
            earlier, later = _order(r.row, post)
            kind = RELATION_TYPES.get(r.relation, "paraphrase")
            cur.execute("INSERT INTO post_relations (source_post_id, target_post_id, relation_type, detected_by, similarity)"
                        " VALUES (%s, %s, %s, 'embedding', %s) ON DUPLICATE KEY UPDATE similarity = VALUES(similarity)",
                        (earlier["post_id"], later["post_id"], kind, r.score))
    conn.commit()
    return related


def _order(a: dict, b: dict) -> tuple[dict, dict]:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return (a, b) if (_utc(a["published_at"]) or far, a["post_id"]) <= (_utc(b["published_at"]) or far, b["post_id"]) else (b, a)


def _cluster(conn, qdrant: Qdrant, vector) -> dict | None:
    run = _one(conn, "SELECT run_id FROM clustering_runs WHERE status = 'done' ORDER BY finished_at DESC, run_id DESC LIMIT 1")
    if run is None:
        return None
    hit = qdrant.nearest_cluster(vector, min_score=CLUSTER_MIN, run_id=run["run_id"])
    if hit is None or not isinstance(hit.payload.get("cluster_id"), int):
        return None
    return _one(conn, "SELECT cluster_id, label, size, change_pct, accounts, stats FROM clusters WHERE cluster_id = %s",
                (hit.payload["cluster_id"],))


# --------------------------------------------------------------------------- step 3: claims

def _claim_uuid(text: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, "claim:" + " ".join(text.lower().split()))


def _store_claims(conn, ai: LocalAI, qdrant: Qdrant, post: dict, answer) -> list[dict]:
    texts = [c.claim for c in answer.value.claims]
    if not texts:
        return []
    vectors = ai.embed_documents(texts)
    claim_ids: list[int] = []
    for text, vector in zip(texts, vectors):
        hits = qdrant.search("claims", vector, limit=1, min_score=CLAIM_MATCH_MIN)
        known = hits[0].payload.get("claim_id") if hits else None
        row = _one(conn, "SELECT claim_id FROM claims WHERE claim_id = %s", (known,)) if isinstance(known, int) else None
        if row is None:
            cid = _claim_uuid(text)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO claims (uuid, text, language, verdict) VALUES (%s, %s, 'en', 'unverified')"
                            " ON DUPLICATE KEY UPDATE claim_id = LAST_INSERT_ID(claim_id)", (cid.bytes, text))
                claim_id = cur.lastrowid
            conn.commit()
            qdrant.upsert("claims", [Point(str(cid), vector, {"claim_id": claim_id, "language": "en", "verdict": "unverified"})])
        else:
            claim_id = row["claim_id"]
        if claim_id not in claim_ids:
            claim_ids.append(claim_id)
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO post_claims (post_id, claim_id, extracted_by) VALUES (%s, %s, %s)",
                        (post["post_id"], claim_id, f"{answer.prompt} {answer.model}"[:100]))
        _refresh_claim(conn, claim_id)
    conn.commit()
    marks = ", ".join(["%s"] * len(claim_ids))
    rows = {r["claim_id"]: r for r in _rows(
        conn, f"SELECT c.claim_id, c.text, c.verdict, c.verdict_explanation, c.post_count, c.languages,"
              f" (SELECT COUNT(*) FROM claim_evidence e WHERE e.claim_id = c.claim_id AND e.stance = 'refutes') AS refutes,"
              f" (SELECT COUNT(*) FROM claim_evidence e WHERE e.claim_id = c.claim_id AND e.stance = 'supports') AS supports,"
              f" (SELECT COUNT(*) FROM claim_evidence e WHERE e.claim_id = c.claim_id) AS evidence"
              f" FROM claims c WHERE c.claim_id IN ({marks})", tuple(claim_ids))}
    return [rows[i] for i in claim_ids]


def _refresh_claim(conn, claim_id: int) -> None:
    """Recompute a claim's summary (where and when it was seen) from its posts."""
    rows = _rows(conn, "SELECT p.published_at, p.scraped_at, p.language, pl.code FROM post_claims pc"
                       " JOIN posts p ON p.post_id = pc.post_id JOIN platforms pl ON pl.platform_id = p.platform_id"
                       " WHERE pc.claim_id = %s", (claim_id,))
    days = sorted(d.date() for d in ((r["published_at"] or r["scraped_at"]) for r in rows) if d)
    platforms: list[str] = []
    languages: list[str] = []
    for r in rows:
        name = PLATFORM_NAMES.get(r["code"])
        if name and name not in platforms:
            platforms.append(name)
        if r["language"] and r["language"] not in languages:
            languages.append(r["language"])
    with conn.cursor() as cur:
        cur.execute("UPDATE claims SET post_count = %s, platforms = %s, languages = %s,"
                    " first_seen = COALESCE(%s, first_seen), last_seen = COALESCE(%s, last_seen) WHERE claim_id = %s",
                    (len(rows), json.dumps(platforms), json.dumps(languages),
                     days[0] if days else None, days[-1] if days else None, claim_id))


# --------------------------------------------------------------------------- step 4: categories, stored analyses

def _categories(conn, ai: LocalAI, qdrant: Qdrant, post: dict, vector, language: str) -> list[dict]:
    out = []
    for hit in qdrant.search("categories", vector, limit=3, min_score=CATEGORY_MIN):
        category = _one(conn, "SELECT category_id, name, description FROM categories WHERE category_id = %s AND is_active",
                        (hit.payload.get("category_id"),))
        if category is None:
            continue
        score, method, model = round(min(1.0, hit.score), 4), "Matched by similarity only", None
        if hit.score < CATEGORY_SURE:
            answer = analyzers.confirm_category(ai, post["text"], category["name"], category["description"] or "", language)
            if not answer.value.matches:
                continue
            method, model = "Matched by similarity, confirmed by Gemma", answer.model
        out.append({"name": category["name"], "score": score, "method": method})
        with conn.cursor() as cur:
            cur.execute("INSERT INTO post_categories (post_id, category_id, method, score, model) VALUES (%s, %s, %s, %s, %s)"
                        " ON DUPLICATE KEY UPDATE score = VALUES(score), model = VALUES(model)",
                        (post["post_id"], category["category_id"], "llm" if model else "embedding", score, model))
    conn.commit()
    return out


def _store_analyses(conn, post_id: int, claims, reading, translation) -> None:
    rows = [
        ("claims", claims, None, None, {"claims": [c.model_dump() for c in claims.value.claims]}),
        ("reading", reading, reading.value.tone[:100], reading.value.manipulation / 10, reading.value.model_dump()),
        ("translation", translation, translation.value.source_language[:100], None, translation.value.model_dump()),
    ]
    with conn.cursor() as cur:
        for analyzer, answer, label, score, details in rows:
            cur.execute("INSERT INTO post_analyses (post_id, analyzer, analyzer_version, model, label, score, details)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (post_id, analyzer, answer.prompt, answer.model, label, score, json.dumps(details, ensure_ascii=False)))
    conn.commit()


# --------------------------------------------------------------------------- the report

def _segments(text: str, techniques) -> list[dict]:
    """The post's text in pieces, with each technique's flagged wording marked."""
    marks = []
    for t in techniques:
        start = text.find(t.wording)
        if start < 0:
            continue
        end = start + len(t.wording)
        if all(end <= s or start >= e for s, e, _ in marks):  # no overlaps
            marks.append((start, end, t.name))
    marks.sort()
    out, pos = [], 0
    for start, end, name in marks:
        if start > pos:
            out.append({"text": text[pos:start]})
        out.append({"text": text[start:end], "flag": name})
        pos = end
    if pos < len(text) or not out:
        out.append({"text": text[pos:]})
    return out


def _verdict(claims: list[dict]) -> dict:
    verdicts = [VERDICTS.get(c["verdict"], "unverified") for c in claims]
    if not verdicts:
        return {"verdict": "unverified", "summary": "No checkable claims found"}
    counts = {v: verdicts.count(v) for v in ("false", "misleading", "true", "unverified")}
    overall = next(v for v in ("false", "misleading") if counts[v]) if counts["false"] or counts["misleading"] else (
        "true" if counts["true"] == len(verdicts) else "unverified")
    parts = []
    if counts["false"]:
        parts.append(_plural(counts["false"], "false claim"))
    if counts["misleading"]:
        parts.append(f"{counts['misleading']} misleading")
    if counts["true"]:
        parts.append(f"{counts['true']} true")
    if counts["unverified"]:
        parts.append(f"{counts['unverified']} not checked yet")
    return {"verdict": overall, "summary": ", ".join(parts)}


def _claim_summary(c: dict) -> dict:
    if c["refutes"]:
        evidence = f"{_plural(c['refutes'], 'source contradicts', 'sources contradict')} it"
    elif c["supports"]:
        evidence = f"{_plural(c['supports'], 'source supports', 'sources support')} it"
    elif c["evidence"]:
        evidence = f"{_plural(c['evidence'], 'source')} found"
    else:
        evidence = "Not checked against sources yet"
    languages = json.loads(c["languages"]) if isinstance(c["languages"], str) else (c["languages"] or [])
    return {"id": c["claim_id"], "text": c["text"], "verdict": VERDICTS.get(c["verdict"], "unverified"),
            "explanation": c["verdict_explanation"] or "No verdict yet: this claim hasn't been checked.",
            "evidenceSummary": evidence,
            "seenSummary": f"Seen in {_plural(c['post_count'], 'post')}, {_plural(len(languages) or 1, 'language')}"}


def _report(url: str, now: datetime, post: dict, related: list[Related], cluster: dict | None, claims: list[dict],
            reading, translation, categories: list[dict]) -> dict:
    copies = [r for r in related if r.score >= COPY_MIN]
    me = {"row": post, "relation": None}
    timeline = sorted([me] + [{"row": r.row, "relation": r.relation} for r in copies],
                      key=lambda n: (_utc(n["row"]["published_at"]) or datetime.max.replace(tzinfo=timezone.utc), n["row"]["post_id"]))
    first = timeline[0]["row"]
    if first is post:
        earliest = {"name": _author(post), "note": "This post is the earliest copy found so far"}
    else:
        gap = (_utc(post["published_at"]) - _utc(first["published_at"])) if post["published_at"] and first["published_at"] else None
        before = f"{_duration(gap)} before this post · " if gap else ""
        earliest = {"name": _author(first), "note": f"{before}{_where(first['platform'], first['account_type'])}"}
    # the chain: this post, and up to 4 others closest to it in time
    mine = next(i for i, n in enumerate(timeline) if n["row"] is post)
    lo = max(0, min(mine - 2, len(timeline) - 5))
    shown = timeline[lo:lo + 5]
    chain = []
    for i, n in enumerate(shown):
        row = n["row"]
        t = _utc(row["published_at"])
        node = {"name": _author(row), "kind": "This post" if row is post else _where(row["platform"], row["account_type"]),
                "language": (row["language"] or "??").upper(), "time": t.strftime("%H:%M") if t else "--:--"}
        if i > 0 or lo > 0:
            if row is post:  # how this post relates to the one before it
                before = timeline[lo + i - 1]["row"]
                differ = before["language"] and post["language"] and before["language"] != post["language"]
                node["relation"] = "translation" if differ else "copy"
            else:
                node["relation"] = RELATION_TYPES.get(n["relation"], "paraphrase")
        if row is post:
            node["isThisPost"] = True
        chain.append(node)
    times = [t for t in (_utc(n["row"]["published_at"]) for n in timeline) if t]
    hours = round((max(times) - min(times)).total_seconds() / 3600, 1) if len(times) > 1 else 0
    languages = {n["row"]["language"] for n in timeline if n["row"]["language"]}

    cluster_card = None
    if cluster:
        stats = json.loads(cluster["stats"]) if isinstance(cluster["stats"], str) else (cluster["stats"] or {})
        change = cluster["change_pct"]
        trend = "new this week" if change is None else f"{'up' if change >= 0 else 'down'} {abs(float(change)):g}% on the week before"
        cluster_card = ClusterCard(id=cluster["cluster_id"], name=cluster["label"] or "Unnamed narrative",
                                   summary=f"{cluster['size']:,} posts · {trend}",
                                   languages=stats.get("languages", [])).to_json()

    r = reading.value
    return {
        "url": url, "analyzedAt": _iso(now),
        "answeredFrom": f"this computer ({_plural(len(related), 'related post')})" if related else "this computer",
        "post": {"author": _author(post), "sourceId": post["source_key"], "where": _where(post["platform"], post["account_type"]),
                 "original": _segments(post["text"], r.techniques),
                 "originalLanguage": _language_name(post["language"]) if post["language"] else translation.value.source_language,
                 "translation": [{"text": translation.value.translation}]},
        "verdict": _verdict(claims),
        "manipulation": r.manipulation,
        "spread": {"copies": len(copies), "languages": len(languages), "hours": hours},
        "earliest": earliest,
        "chain": chain,
        "moreCopies": max(0, len(copies) - (len(chain) - 1)),
        "claims": [_claim_summary(c) for c in claims],
        "similar": [{"language": (s.row["language"] or "??").upper(), "author": _author(s.row),
                     "where": _where(s.row["platform"], s.row["account_type"]), "text": (s.row["text"] or "")[:280],
                     "relation": "Earliest source" if s.row is first else s.relation, "similarity": s.score}
                    for s in related[:5]],
        "categories": categories,
        "reading": {"tone": r.tone, "intent": r.intent, "techniques": [{"name": t.name, "detail": t.detail} for t in r.techniques],
                    "model": f"{reading.model} · prompt {reading.prompt} · ran on this computer"},
        "cluster": cluster_card,
    }
