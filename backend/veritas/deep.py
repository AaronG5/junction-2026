"""In-depth analysis of one post for the browser extension, on request ("Analyze" button).

Three parts, run one after another in a background job (the screen polls it):
  1. patterns  Gemma marks the phrases that use propaganda or manipulation techniques, each with a
               tier: "clear", "likely" or "possible" (prompt patterns.v1). The extension highlights
               them in the post, coloured by tier.
  2. truth     Gemma lists the checkable claims and gives each a verdict from its own knowledge:
               true / false / misleading / unverifiable, with a confidence and an explanation
               (prompt truth.v1). No sources are consulted yet, and the extension says so.
  3. trace     Where the post came from, as a timeline, oldest first. No AI: it is put together from
               what is known about the post:
                 - the documented narrative and its state-media echo (narrative bank, sent by the
                   extension from the fast check it already has),
                 - matching Telegram posts (also from the fast check),
                 - earlier similar posts in our own database (EmbeddingGemma + Qdrant + MySQL),
                 - the original post when this one quotes or shares it,
                 - this post, and how it reached the reader's feed (the group, page or repost).
Every quote Gemma gives is checked against the post (analyzers.find_wording): a pattern whose
phrase isn't in the post is dropped, a claim's quote is cleared.

Jobs live in memory (the newest 2000); one runs at a time, because the local AI answers one
request at a time anyway. The same post asked for again gets the same job.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from .ai import AiError, AiUnavailable, LocalAI, load_prompt
from .analyzers import Short, _Strict, find_wording

log = logging.getLogger("veritas.deep")

STEPS = ("Finding propaganda patterns", "Checking the claims", "Tracing where it came from")
MAX_TEXT = 4000
TELEGRAM_MIN = 0.85      # a Telegram post this similar (cosine) is shown in the trace
SIMILAR_MIN = 0.80       # ... and an earlier post in our database
MAX_JOBS = 2000
FAILED = "The in-depth analysis failed. Please try again."


# --------------------------------------------------------------------------- answer schemas (sent to Gemma)

class PatternAnswer(_Strict):
    quote: Short
    technique: Annotated[str, Field(max_length=60)]
    tier: Literal["clear", "likely", "possible"]
    why: Short


class PatternsAnswer(_Strict):
    patterns: list[PatternAnswer] = Field(max_length=8)
    overall: Short


class ClaimCheck(_Strict):
    claim: Short
    quote: Short
    verdict: Literal["true", "false", "misleading", "unverifiable"]
    confidence: int = Field(ge=0, le=100)
    explanation: Short


class TruthAnswer(_Strict):
    claims: list[ClaimCheck] = Field(max_length=4)


# --------------------------------------------------------------------------- the three parts

def find_patterns(ai: LocalAI, text: str, language: str = "unknown") -> dict:
    answer = ai.ask(load_prompt("patterns"), PatternsAnswer, text=text, language=language)
    order = {"clear": 0, "likely": 1, "possible": 2}
    kept, seen = [], set()
    for p in answer.value.patterns:
        found = find_wording(text, p.quote)
        if found is None or found.lower() in seen:
            continue
        seen.add(found.lower())
        kept.append({"quote": found, "technique": p.technique.strip(), "tier": p.tier, "why": p.why.strip()})
    kept.sort(key=lambda p: order[p["tier"]])
    return {"items": kept, "overall": answer.value.overall.strip(), "model": answer.model, "prompt": answer.prompt}


def check_truth(ai: LocalAI, text: str, published: str | None, language: str = "unknown") -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    answer = ai.ask(load_prompt("truth"), TruthAnswer, text=text, language=language, today=today,
                    published=(published or "at an unknown date")[:40])
    claims = [{"claim": c.claim.strip(), "quote": find_wording(text, c.quote) or "", "verdict": c.verdict,
               "confidence": c.confidence, "explanation": c.explanation.strip()}
              for c in answer.value.claims if c.claim.strip()]
    return {"items": claims, "model": answer.model, "prompt": answer.prompt,
            "basis": "The model's own knowledge. No sources were checked, and it may be wrong or out of date."}


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def _https(url: Any) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def _snip(text: Any, n: int = 200) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


SOURCE_NAMES = {"euvsdisinfo": "EUvsDisinfo", "rt": "RT", "ria": "RIA Novosti", "sputnik": "Sputnik", "tass": "TASS",
                "a4n": "Antibot4Navalny", "overload": "Operation Overload"}


def _platform(item: dict) -> str:
    url = str(item.get("url") or "")
    if item.get("platform") == "facebook" or "facebook.com" in url:
        return "Facebook"
    if "t.me/" in url:
        return "Telegram"
    return "X"


def build_trace(item: dict, context: dict, similar: list[dict]) -> dict:
    """The timeline, oldest first. Nodes without a date (the narrative) come first, as background.
    Node: {kind, title, who, where, time, url, sim, text, note}."""
    nodes: list[dict] = []
    narrative = context.get("narrative") or {}
    if isinstance(narrative, dict) and narrative.get("text"):
        src = SOURCE_NAMES.get(str(narrative.get("source") or "").lower(), narrative.get("source") or "a fact-checking database")
        nodes.append({"kind": "narrative", "title": "Known disinformation narrative", "who": src, "where": "Documented narrative",
                      "time": None, "url": None, "sim": narrative.get("sim"), "text": _snip(narrative["text"]),
                      "note": f"This post matches a narrative documented by {src}."})
    for echo in (context.get("state_media_echo") or [])[:2]:
        if not isinstance(echo, dict):
            continue
        src = SOURCE_NAMES.get(str(echo.get("source") or "").lower(), echo.get("source") or "state media")
        aired = f"aired {echo['n_aired']} times" if echo.get("n_aired") else "aired"
        nodes.append({"kind": "state_media", "title": f"State media: {src}", "who": src, "where": "State media",
                      "time": _iso(_when(echo.get("first"))), "url": None, "sim": echo.get("sim"),
                      "text": _snip(echo.get("text")),
                      "note": f"The same narrative {aired} on {src}" + (f", last on {str(echo['last'])[:10]}." if echo.get("last") else ".")})
    sm = context.get("source_match") or {}
    for tg in (sm.get("telegram") or [])[:3] if isinstance(sm, dict) else []:
        sim = tg.get("sim") if isinstance(tg, dict) else None
        if not isinstance(sim, (int, float)) or sim < TELEGRAM_MIN:
            continue
        channel = "@" + str(tg.get("channel") or "unknown").lstrip("@")
        nodes.append({"kind": "telegram", "title": "Telegram post", "who": channel, "where": "Pro-Kremlin Telegram channel",
                      "time": _iso(_when(tg.get("date"))), "url": _https(tg.get("url")), "sim": round(float(sim), 4),
                      "text": _snip(tg.get("text")),
                      "note": "Same text (or a translation of it)." if sim >= 0.95 else "Very similar text."})
    for s in similar:
        nodes.append({"kind": "similar", "title": s["relation"], "who": s["who"], "where": s["where"], "time": s["time"],
                      "url": _https(s.get("url")), "sim": s["sim"], "text": _snip(s.get("text")),
                      "note": "Collected earlier by Veritas Lens."})
    shared = item.get("shared") or item.get("quoted")
    if isinstance(shared, dict) and (shared.get("text") or shared.get("url")):
        nodes.append({"kind": "original", "title": "Original post (shared or quoted here)",
                      "who": shared.get("author_name") or shared.get("author") or "Unknown author", "where": _platform(shared),
                      "time": _iso(_when(shared.get("time"))), "url": _https(shared.get("url")), "sim": None,
                      "text": _snip(shared.get("text")), "note": "This post passes it on."})
    me_time = _when(item.get("time"))
    where = _platform(item) + (f" group “{item['group']}”" if item.get("group") else "")
    nodes.append({"kind": "this", "title": "This post", "who": item.get("author_name") or item.get("author") or "Unknown author",
                  "where": where, "time": _iso(me_time), "url": _https(item.get("url")), "sim": None, "text": None,
                  "note": _feed_note(item)})

    # Undated background (the narrative) on top; an undated original goes right before this post,
    # which shares it; everything dated in time order.
    # Later appearances (found after this post was published) come after it.
    background = [n for n in nodes if n["time"] is None and n["kind"] not in ("this", "original")]
    this = next(n for n in nodes if n["kind"] == "this")
    with_this = [this] if this["time"] else []
    dated = sorted([n for n in nodes if n["time"] is not None and n["kind"] != "this"] + with_this,
                   key=lambda n: (n["time"], n["kind"] == "this"))
    if not this["time"]:
        dated.append(this)
    undated_original = [n for n in nodes if n["time"] is None and n["kind"] == "original"]
    i = dated.index(this)
    ordered = background + dated[:i] + undated_original + dated[i:]
    for n in dated[i + 1:]:
        n["note"] = (n["note"] + " Appeared after this post.").strip()
    me_time = _when(this["time"])
    earlier = [n for n in dated if n["kind"] != "this" and n["time"] and me_time and _when(n["time"]) < me_time]
    if earlier:
        first = earlier[0]
        days = (me_time - _when(first["time"])).days
        ago = f"{days} days" if days >= 2 else "hours" if days < 1 else "a day"
        summary = (f"The earliest trace found is {first['who']} ({first['where']}), {ago} before this post. "
                   f"{len(earlier)} earlier appearance{'s' if len(earlier) != 1 else ''} found.")
    elif len(ordered) > 1:
        summary = "Related material was found, but nothing dated before this post."
    else:
        summary = "No earlier trace found: in what we know, this post is the first appearance of this text."
    return {"nodes": ordered, "summary": summary}


def _feed_note(item: dict) -> str:
    kind = item.get("kind")
    if kind == "repost":
        return "Reposted into your feed."
    if kind == "quote" or item.get("shared"):
        return "Shares or quotes the original post."
    if item.get("group"):
        return f"Posted in the group “{item['group']}”, which is how it reached your feed."
    return "How it reached your feed: posted by this account."


# --------------------------------------------------------------------------- similar posts in our database

def similar_in_database(settings, ai: LocalAI, text: str, url: str | None) -> list[dict]:
    """Earlier posts in Veritas Lens' own database with the same or similar text. Empty when the
    database or vector store isn't running (the trace then shows what else is known)."""
    from .analysis import _POST_COLUMNS, _author, _where  # same helpers as "Analyze a link"
    from .db import connection
    from .vectors import Qdrant

    try:
        vector = ai.embed_documents([text])[0]
        with Qdrant.from_settings(settings) as q:
            hits = [h for h in q.similar_posts(vector, limit=10) if h.score >= SIMILAR_MIN]
        ids = [h.payload.get("post_id") for h in hits if isinstance(h.payload.get("post_id"), int)]
        if not ids:
            return []
        with connection(settings) as conn, conn.cursor() as cur:
            cur.execute(_POST_COLUMNS + f" WHERE p.post_id IN ({', '.join(['%s'] * len(ids))}) AND p.deleted_at IS NULL",
                        tuple(ids))
            rows = {r["post_id"]: r for r in cur.fetchall()}
    except Exception as exc:  # database, Qdrant or AI down: the trace goes on without it
        log.info("trace: database search skipped (%s)", exc)
        return []
    out = []
    mine = (url or "").rstrip("/")
    for h in hits:
        r = rows.get(h.payload.get("post_id"))
        if not r or (r.get("url") or "").rstrip("/") == mine:
            continue
        sim = round(min(1.0, h.score), 4)
        out.append({"relation": "Near copy" if sim >= 0.95 else "Same content" if sim >= 0.85 else "Similar post",
                    "who": _author(r), "where": _where(r["platform"], r.get("account_type")),
                    "time": _iso(_when(r.get("published_at"))), "url": r.get("url"), "sim": sim, "text": r.get("text")})
        if len(out) == 6:
            break
    return out


# --------------------------------------------------------------------------- jobs

@dataclass
class DeepServices:
    ai: Callable[[], LocalAI]
    similar: Callable[[LocalAI, str, str | None], list[dict]]


class DeepJobs:
    def __init__(self, services: DeepServices, workers: int = 1):
        self.services = services
        self.jobs: OrderedDict[str, dict] = OrderedDict()
        self.by_key: dict[str, str] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deep")

    @staticmethod
    def key(item: dict) -> str:
        text = str(item.get("text") or "")
        return str(item.get("id")) + ":" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def start(self, item: dict, context: dict) -> dict:
        """The job for this post: an existing one (running or done), or a new one."""
        key = self.key(item)
        with self.lock:
            jid = self.by_key.get(key)
            job = self.jobs.get(jid) if jid else None
            if job and job["status"] != "error":
                return self._public(job)
        ai = self.services.ai()
        try:
            ai.check()
        finally:
            ai.close()
        job = {"id": secrets.token_hex(12), "post_id": item.get("id"), "status": "running", "step": 0,
               "steps": list(STEPS), "patterns": None, "truth": None, "trace": None, "error": None}
        with self.lock:
            self.jobs[job["id"]] = job
            self.by_key[key] = job["id"]
            while len(self.jobs) > MAX_JOBS:
                old_id, old = self.jobs.popitem(last=False)
                self.by_key = {k: v for k, v in self.by_key.items() if v != old_id}
        self.pool.submit(self._run, job["id"], item, context)
        return self._public(job)

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self._public(job) if job else None

    def _public(self, job: dict) -> dict:
        return {k: v for k, v in job.items()}

    def _set(self, job_id: str, **values) -> None:
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(values)

    def _run(self, job_id: str, item: dict, context: dict) -> None:
        text = str(item.get("text") or "")[:MAX_TEXT]
        language = str(item.get("lang") or "unknown")
        if language in ("und", "qme", "zxx"):
            language = "unknown"
        ai = self.services.ai()
        try:
            self._set(job_id, step=0)
            self._set(job_id, patterns=find_patterns(ai, text, language), step=1)
            self._set(job_id, truth=check_truth(ai, text, item.get("time"), language), step=2)
            similar = self.services.similar(ai, text, item.get("url"))
            self._set(job_id, trace=build_trace(item, context, similar), step=3, status="done")
        except (AiUnavailable, AiError) as exc:
            self._set(job_id, status="error", error=str(exc))
        except Exception:
            log.exception("in-depth analysis %s failed", job_id)
            self._set(job_id, status="error", error=FAILED)
        finally:
            ai.close()


_jobs: DeepJobs | None = None
_jobs_lock = threading.Lock()


def get_jobs(settings) -> DeepJobs:
    global _jobs
    with _jobs_lock:
        if _jobs is None:
            _jobs = DeepJobs(DeepServices(
                ai=lambda: LocalAI.from_settings(settings),
                similar=lambda ai, text, url: similar_in_database(settings, ai, text, url)))
        return _jobs


def set_jobs(jobs: DeepJobs | None) -> None:
    """Tests swap the job runner."""
    global _jobs
    _jobs = jobs
