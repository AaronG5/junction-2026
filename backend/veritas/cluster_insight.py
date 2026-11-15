"""What the Narratives and Claims screens show about a cluster's representative posts.

For each cluster that a clustering run made or changed, its typical and edge posts get:
  - highlights: Gemma marks the phrases that use propaganda or manipulation techniques,
    each with a tier (clear / likely / possible) and a one-line reason. This is the same
    analysis as the browser extension's in-depth check (deep.find_patterns, prompt patterns.v1),
    so a post reads the same everywhere. The card shows the text with those phrases marked
    and how many there are per tier;
  - claims: Gemma lists the checkable claims (prompt claims.v1), stored and matched to known
    claims exactly as "Analyze a link" does (analysis._store_claims). The claims are linked to
    the cluster, and the claim most of its posts make (at least MAIN_CLAIM_MIN_POSTS) becomes
    the cluster's main claim (its status on the Narratives screen). New claims stay "unverified" until checked.
Both are stored per post in post_analyses, so a post is never analysed twice: an update only
costs Gemma calls for posts that became typical or edge for the first time.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Iterable

from . import analyzers
from .ai import AiError, LocalAI
from .analysis import _language_name, _store_claims
from .deep import MAX_TEXT, find_patterns
from .vectors import Qdrant

log = logging.getLogger("veritas.cluster_insight")

TIERS = ("clear", "likely", "possible")
MAIN_CLAIM_MIN_POSTS = 2    # a claim only one post makes doesn't speak for the cluster
SHOWN_CHARS = MAX_TEXT      # the card shows the text the highlights were looked for in


def _rows(conn, sql: str, args: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def _json(value, default):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def segments(text: str, patterns: Iterable[dict]) -> list[dict]:
    """The text in pieces; a highlighted piece carries its technique, tier and reason.
    Overlapping phrases: the first one (clearest, as find_patterns orders them) wins."""
    marks: list[tuple[int, int, dict]] = []
    for p in patterns:
        quote = p.get("quote") or ""
        start = text.find(quote) if quote else -1
        if start < 0:
            continue
        end = start + len(quote)
        if all(end <= s or start >= e for s, e, _ in marks):
            marks.append((start, end, p))
    marks.sort(key=lambda m: m[0])
    out, pos = [], 0
    for start, end, p in marks:
        if start > pos:
            out.append({"text": text[pos:start]})
        out.append({"text": text[start:end], "technique": p["technique"], "tier": p["tier"], "why": p["why"]})
        pos = end
    if pos < len(text) or not out:
        out.append({"text": text[pos:]})
    return out


def counts(patterns: Iterable[dict]) -> dict:
    tiers = [p.get("tier") for p in patterns]
    return {tier: tiers.count(tier) for tier in TIERS}


def _stored(conn, post_id: int, analyzer: str) -> dict | None:
    row = _rows(conn, "SELECT details FROM post_analyses WHERE post_id = %s AND analyzer = %s"
                      " ORDER BY created_at DESC, analysis_id DESC LIMIT 1", (post_id, analyzer))
    return _json(row[0]["details"], {}) if row else None


def _store(conn, post_id: int, analyzer: str, version: str, model: str, label: str | None, details: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO post_analyses (post_id, analyzer, analyzer_version, model, label, details)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (post_id, analyzer, version[:50], model[:100], label, json.dumps(details, ensure_ascii=False)))
    conn.commit()


def post_patterns(conn, ai: LocalAI, post: dict) -> list[dict]:
    """The post's highlights: stored ones, or found now (and stored)."""
    done = _stored(conn, post["post_id"], "patterns")
    if done is not None:
        return done.get("items", [])
    text = (post["text"] or "")[:MAX_TEXT]
    if not text.strip():
        return []
    found = find_patterns(ai, text, _language_name(post["language"]))
    _store(conn, post["post_id"], "patterns", found["prompt"], found["model"], None,
           {"items": found["items"], "overall": found["overall"]})
    return found["items"]


def post_claims(conn, ai: LocalAI, qdrant: Qdrant, post: dict) -> None:
    """The post's claims into the claims table, unless they were extracted before."""
    if _stored(conn, post["post_id"], "claims") is not None:
        return
    text = (post["text"] or "")[:MAX_TEXT]
    if not text.strip():
        return
    answer = analyzers.extract_claims(ai, text, _language_name(post["language"]))
    _store_claims(conn, ai, qdrant, post, answer)
    _store(conn, post["post_id"], "claims", answer.prompt, answer.model, None,
           {"claims": [c.model_dump() for c in answer.value.claims]})


def unread_clusters(conn, run_id: int) -> list[int]:
    """Clusters of the run with a typical or edge post that hasn't been read yet
    (made before this existed, or the reading was stopped)."""
    out = []
    for row in _rows(conn, "SELECT cluster_id, stats FROM clusters WHERE run_id = %s", (run_id,)):
        stats = _json(row["stats"], {})
        if any("highlights" not in ref for ref in stats.get("typical", []) + stats.get("edge", [])):
            out.append(row["cluster_id"])
    return out


def enrich_clusters(conn, ai: LocalAI, qdrant: Qdrant, cluster_ids: Iterable[int],
                    progress: Callable[[str], None] | None = None) -> dict:
    """Highlights and claims for the typical and edge posts of these clusters.
    A post Gemma can't read (AiError) is skipped; the AI not running stops the pass (AiUnavailable)."""
    say = progress or (lambda message: None)
    ids = sorted(set(cluster_ids))
    done = skipped = 0
    for n, cluster_id in enumerate(ids, 1):
        row = _rows(conn, "SELECT run_id, stats FROM clusters WHERE cluster_id = %s", (cluster_id,))
        if not row:
            continue
        stats = _json(row[0]["stats"], {})
        refs = stats.get("typical", []) + stats.get("edge", [])
        say(f"Reading the typical posts of cluster {n} of {len(ids)}.")
        for ref in refs:
            post_rows = _rows(conn, "SELECT post_id, text, language FROM posts WHERE post_id = %s", (int(ref["id"]),))
            if not post_rows:
                continue
            post = post_rows[0]
            try:
                patterns = post_patterns(conn, ai, post)
                post_claims(conn, ai, qdrant, post)
            except AiError as exc:
                log.warning("Post %s: %s", post["post_id"], exc)
                skipped += 1
                continue
            ref["segments"] = segments((post["text"] or "")[:SHOWN_CHARS], patterns)
            ref["highlights"] = counts(patterns)
            done += 1

        members = "SELECT post_id FROM post_clusters WHERE run_id = %s AND cluster_id = %s"
        main = _rows(conn, f"SELECT claim_id, COUNT(*) AS n FROM post_claims WHERE post_id IN ({members})"
                           f" GROUP BY claim_id HAVING n >= {MAIN_CLAIM_MIN_POSTS} ORDER BY n DESC, claim_id LIMIT 1",
                     (row[0]["run_id"], cluster_id))
        with conn.cursor() as cur:
            cur.execute(f"UPDATE claims SET cluster_id = %s WHERE claim_id IN (SELECT claim_id FROM post_claims"
                        f" WHERE post_id IN ({members}))", (cluster_id, row[0]["run_id"], cluster_id))
            cur.execute("UPDATE clusters SET stats = %s, main_claim_id = %s WHERE cluster_id = %s",
                        (json.dumps(stats, ensure_ascii=False), main[0]["claim_id"] if main else None, cluster_id))
        conn.commit()
    return {"posts": done, "skipped": skipped, "clusters": len(ids)}
