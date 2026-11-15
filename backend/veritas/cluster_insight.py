"""What the Narratives and Claims screens show about a cluster's representative posts.

For each cluster that a clustering run made or changed, its typical and edge posts get:
  - highlights: Gemma marks the phrases that use propaganda or manipulation techniques,
    each with a tier (clear / likely / possible) and a one-line reason. This is the same
    analysis as the browser extension's in-depth check (deep.find_patterns, prompt patterns.v1),
    so a post reads the same everywhere. The card shows the text with those phrases marked
    and how many there are per tier;
  - claims: the claims worth checking, stored and matched to known claims exactly as
    "Analyze a link" does (analysis._store_claims). With Gemini set up (veritas/factcheck.py)
    Gemini writes them (prompt claims_strict.v1: self-contained, dated, trivial ones dropped) and
    fact-checks each new one with Google Search; posts whose claims came from the local AI before
    get them again from Gemini. Without Gemini, the local AI writes them (claims.v1) and they
    stay unverified. The claims are linked to
    the cluster, and the claim most of its posts make (at least MAIN_CLAIM_MIN_POSTS) becomes
    the cluster's main claim (its status on the Narratives screen). New claims stay "unverified" until checked.
Both are stored per post in post_analyses, so a post is never analysed twice (except to replace
local-AI claims with Gemini's): an update only costs calls for posts that became typical or edge
for the first time, and for claims not checked yet.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Iterable

from . import analyzers
from . import factcheck
from .ai import AiError, LocalAI
from .analysis import _language_name, _refresh_claim, _store_claims
from .deep import MAX_TEXT, find_patterns
from .gemini import Gemini, GeminiError
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


def _stored_row(conn, post_id: int, analyzer: str) -> dict | None:
    row = _rows(conn, "SELECT details, analyzer_version FROM post_analyses WHERE post_id = %s AND analyzer = %s"
                      " ORDER BY created_at DESC, analysis_id DESC LIMIT 1", (post_id, analyzer))
    return row[0] if row else None


def _stored(conn, post_id: int, analyzer: str) -> dict | None:
    row = _stored_row(conn, post_id, analyzer)
    return _json(row["details"], {}) if row else None


GEMINI_CLAIMS = f"{factcheck.CLAIMS_PROMPT}.v1"   # the analyzer_version of claims Gemini wrote


def _drop_claims(conn, qdrant: Qdrant, post_id: int) -> None:
    """Forget a post's claims (to write them again). A claim no other post makes is deleted."""
    ids = [r["claim_id"] for r in _rows(conn, "SELECT claim_id FROM post_claims WHERE post_id = %s", (post_id,))]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM post_claims WHERE post_id = %s", (post_id,))
    for claim_id in ids:
        if _rows(conn, "SELECT 1 FROM post_claims WHERE claim_id = %s LIMIT 1", (claim_id,)):
            _refresh_claim(conn, claim_id)
            continue
        row = _rows(conn, "SELECT uuid FROM claims WHERE claim_id = %s", (claim_id,))
        with conn.cursor() as cur:
            cur.execute("UPDATE clusters SET main_claim_id = NULL WHERE main_claim_id = %s", (claim_id,))
            cur.execute("DELETE FROM claims WHERE claim_id = %s", (claim_id,))
        if row:
            qdrant.delete("claims", [row[0]["uuid"]])
    conn.commit()


def needs_claims(conn, post_id: int, gemini: bool) -> bool:
    row = _stored_row(conn, post_id, "claims")
    if row is None:
        return True
    return gemini and row["analyzer_version"] != GEMINI_CLAIMS


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


def post_claims(conn, ai: LocalAI, qdrant: Qdrant, post: dict, gemini: Gemini | None = None, source: str = "") -> None:
    """The post's claims into the claims table, unless they were extracted before (by Gemini,
    when Gemini is set up: local-AI claims are then replaced)."""
    if not needs_claims(conn, post["post_id"], gemini is not None):
        return
    text = (post["text"] or "")[:MAX_TEXT]
    if not text.strip():
        return
    if gemini is not None:
        published = post["published_at"].date().isoformat() if post.get("published_at") else ""
        answer = factcheck.extract_claims(gemini, text, language=_language_name(post["language"]), published=published,
                                          source=source)
        _drop_claims(conn, qdrant, post["post_id"])
    else:
        answer = analyzers.extract_claims(ai, text, _language_name(post["language"]))
    _store_claims(conn, ai, qdrant, post, answer)
    _store(conn, post["post_id"], "claims", answer.prompt, answer.model, None,
           {"claims": [c.model_dump() for c in answer.value.claims]})


def _claims_of(conn, post_ids: list[int]) -> list[int]:
    if not post_ids:
        return []
    marks = ", ".join(["%s"] * len(post_ids))
    return [r["claim_id"] for r in _rows(conn, f"SELECT DISTINCT claim_id FROM post_claims WHERE post_id IN ({marks})",
                                         tuple(post_ids))]


def unread_clusters(conn, run_id: int, gemini: bool = False) -> list[int]:
    """Clusters of the run with work left on their typical or edge posts: not read yet (made
    before this existed, or the reading was stopped), and with Gemini: claims still from the
    local AI, or not fact-checked yet."""
    out = []
    for row in _rows(conn, "SELECT cluster_id, stats FROM clusters WHERE run_id = %s", (run_id,)):
        refs = (lambda st: st.get("typical", []) + st.get("edge", []))(_json(row["stats"], {}))
        post_ids = [int(r["id"]) for r in refs]
        if any("highlights" not in ref for ref in refs) or (gemini and (
                any(needs_claims(conn, pid, True) for pid in post_ids) or factcheck.unchecked(conn, _claims_of(conn, post_ids)))):
            out.append(row["cluster_id"])
    return out


def enrich_clusters(conn, ai: LocalAI, qdrant: Qdrant, cluster_ids: Iterable[int],
                    progress: Callable[[str], None] | None = None, gemini: Gemini | None = None) -> dict:
    """Highlights and claims for the typical and edge posts of these clusters, and with Gemini,
    fact checks of their claims. A post the AI can't read (AiError, GeminiError) is skipped; an
    AI that isn't running stops the pass (AiUnavailable, GeminiUnavailable)."""
    say = progress or (lambda message: None)
    ids = sorted(set(cluster_ids))
    done = skipped = 0
    verdicts: dict[str, int] = {}
    for n, cluster_id in enumerate(ids, 1):
        row = _rows(conn, "SELECT run_id, stats FROM clusters WHERE cluster_id = %s", (cluster_id,))
        if not row:
            continue
        stats = _json(row[0]["stats"], {})
        refs = stats.get("typical", []) + stats.get("edge", [])
        say(f"Reading the typical posts of cluster {n} of {len(ids)}.")
        for ref in refs:
            post_rows = _rows(conn, "SELECT post_id, text, language, published_at FROM posts WHERE post_id = %s",
                              (int(ref["id"]),))
            if not post_rows:
                continue
            post = post_rows[0]
            try:
                patterns = post_patterns(conn, ai, post)
                post_claims(conn, ai, qdrant, post, gemini, source=f"{ref.get('author', '')} · {ref.get('where', '')}")
            except (AiError, GeminiError) as exc:
                log.warning("Post %s: %s", post["post_id"], exc)
                skipped += 1
                continue
            ref["segments"] = segments((post["text"] or "")[:SHOWN_CHARS], patterns)
            ref["highlights"] = counts(patterns)
            done += 1

        if gemini is not None:
            for verdict, n in factcheck.check_claims(conn, gemini, qdrant, _claims_of(conn, [int(r["id"]) for r in refs]),
                                                     progress=say).items():
                verdicts[verdict] = verdicts.get(verdict, 0) + n

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
    return {"posts": done, "skipped": skipped, "clusters": len(ids), "checked": verdicts}
