"""What the Sources screens and the landing page's stories show, derived from the collected posts.

Run after every clustering run ("Update clusters", backend/cluster.py), in three steps:

1. Copies (find_copies). Every post not checked yet is compared with the whole collection
   (EmbeddingGemma vectors in Qdrant). A post by another account that says the same thing is
   recorded in post_relations, from the earlier post to the later one: "copy" (same language,
   cosine >= NEAR_COPY_MIN), "translation" (another language) or "paraphrase". Checked posts are
   marked in Qdrant ("copies_checked"), so each run only compares the new posts.
2. Sources (build_sources): one profile per account, from its posts and those relations:
   posts per day, the share of original posts and of near copies, links to state media, its
   main narrative, its claims found false or misleading, daily activity, the accounts it copies
   from (and how fast), and its recent posts with their verdicts.
3. Stories (build_stories): one trending story per narrative of the latest clustering run,
   told through the post making its most significant claim: the post (with an English
   translation), the claim and its verdict, where it came from (the earlier posts it copies),
   how far it travelled (its copies), and the narrative it belongs to.
   build_story() also tells the story of any collected post on request (the landing page's
   "paste a link"), without any AI call.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from .analysis import COPY_MIN, NEAR_COPY_MIN
from .clustering import LANGUAGE_NAMES, _author, _where
from .links import link_key
from .queries import PLATFORM_NAMES
from .vectors import Qdrant

log = logging.getLogger("veritas.derive")

SIMILAR_LIMIT = 20
RECENT = 5
DAILY_DAYS = 30
MAX_STORIES = 50
# Domains of Russian and Belarusian state media and outlets they run (links to them in a post).
STATE_MEDIA = ("rt.com", "sputniknews", "sputnik.", "ria.ru", "tass.ru", "tass.com", "iz.ru", "1tv.ru", "vesti.ru",
               "rg.ru", "lenta.ru", "ntv.ru", "smotrim.ru", "russian.rt", "belta.by", "sb.by", "baltnews", "rubaltic",
               "news-front", "southfront", "snanews", "ren.tv", "tvzvezda.ru", "kp.ru", "aif.ru", "regnum.ru")
VERDICT_ORDER = {"false": 0, "mostly_false": 0, "misleading": 1, "mixed": 1, "true": 2, "mostly_true": 2}
SHOWN_VERDICT = {"false": "false", "mostly_false": "false", "misleading": "misleading", "mixed": "misleading",
                 "true": "true", "mostly_true": "true"}
RELATION_WORD = {"copy": "copied", "translation": "translated", "paraphrase": "reworded"}
TYPE_NAMES = {"channel": "channel", "group": "group", "page": "page", "user": "profile"}


def _rows(conn, sql: str, args: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def _json(value, default):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _utc(t: datetime | None) -> datetime | None:
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def _in(ids: Iterable[int]) -> tuple[str, tuple]:
    ids = tuple(ids)
    return ", ".join(["%s"] * len(ids)), ids


def _language(code: str | None) -> str:
    return LANGUAGE_NAMES.get(code or "", (code or "").upper() or "Unknown language")


def _short_number(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _span(seconds: float) -> str:
    minutes = max(0, seconds) / 60
    if minutes < 90:
        return f"{max(1, round(minutes))} min"
    if minutes < 48 * 60:
        return f"{round(minutes / 60)} h"
    return f"{round(minutes / 1440)} days"


def _when(t: datetime | None) -> str:
    return t.strftime("%d %b %H:%M").lstrip("0") if t else "Date unknown"


def _verdict(values: Iterable[str | None]) -> str:
    """One verdict for a post or story from its claims' verdicts: the most serious checked one."""
    checked = sorted((v for v in values if v in VERDICT_ORDER), key=lambda v: VERDICT_ORDER[v])
    return SHOWN_VERDICT[checked[0]] if checked else "unverified"


# --------------------------------------------------------------------------- 1. copies

def find_copies(conn, qdrant: Qdrant, progress: Callable[[str], None] | None = None) -> int:
    """Record copies among the posts not checked yet. Returns how many relations were found."""
    say = progress or (lambda message: None)
    todo = [p for p in qdrant.vectors("posts", flt={"must_not": [{"key": "copies_checked", "match": {"value": True}}]})
            if isinstance(p[2].get("post_id"), int)]
    if not todo:
        return 0
    say(f"Looking for copies of {len(todo):,} new posts.")
    found = 0
    for start in range(0, len(todo), 200):
        batch = todo[start:start + 200]
        hits = {p[2]["post_id"]: [h for h in qdrant.similar_posts(p[1], limit=SIMILAR_LIMIT, exclude=[p[0]])
                                  if h.score >= COPY_MIN and isinstance(h.payload.get("post_id"), int)] for p in batch}
        ids = set(hits) | {h.payload["post_id"] for hs in hits.values() for h in hs}
        marks, args = _in(ids)
        rows = {r["post_id"]: r for r in _rows(
            conn, f"SELECT post_id, published_at, language, COALESCE(container_account_id, account_id) AS account"
                  f" FROM posts WHERE post_id IN ({marks}) AND deleted_at IS NULL", args)}
        with conn.cursor() as cur:
            for post_id, similar in hits.items():
                me = rows.get(post_id)
                if me is None:
                    continue
                for h in similar:
                    other = rows.get(h.payload["post_id"])
                    if other is None or (other["account"] is not None and other["account"] == me["account"]):
                        continue   # the same account repeating itself isn't a copy across sources
                    same_language = not me["language"] or not other["language"] or me["language"] == other["language"]
                    kind = "copy" if same_language and h.score >= NEAR_COPY_MIN else "paraphrase" if same_language else "translation"
                    far = datetime.max
                    earlier, later = sorted((me, other), key=lambda r: (r["published_at"] or far, r["post_id"]))
                    cur.execute("INSERT INTO post_relations (source_post_id, target_post_id, relation_type, detected_by, similarity)"
                                " VALUES (%s, %s, %s, 'embedding', %s) ON DUPLICATE KEY UPDATE similarity = VALUES(similarity)",
                                (earlier["post_id"], later["post_id"], kind, round(min(1.0, h.score), 4)))
                    found += cur.rowcount == 1
        conn.commit()
        qdrant.set_payload("posts", [p[0] for p in batch], {"copies_checked": True})
    return found


def _relations(conn) -> list[dict]:
    """Copies between different accounts, with both posts' times and accounts."""
    return _rows(conn, "SELECT r.source_post_id AS src, r.target_post_id AS dst, r.relation_type AS kind,"
                       " s.published_at AS src_at, t.published_at AS dst_at,"
                       " COALESCE(s.container_account_id, s.account_id) AS src_account,"
                       " COALESCE(t.container_account_id, t.account_id) AS dst_account"
                       " FROM post_relations r JOIN posts s ON s.post_id = r.source_post_id JOIN posts t ON t.post_id = r.target_post_id"
                       " WHERE r.relation_type IN ('copy', 'translation', 'paraphrase')")


# --------------------------------------------------------------------------- 2. sources

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_.-]+", "-", (text or "").lower()).strip("-.")
    return s[:80] or "source"


def _latest_run(conn) -> int | None:
    row = _rows(conn, "SELECT run_id FROM clustering_runs WHERE status = 'done' ORDER BY finished_at DESC, run_id DESC LIMIT 1")
    return row[0]["run_id"] if row else None


def _type_label(platform: str, account_type: str | None) -> str:
    if platform == "web":
        return "News site"
    return f"{PLATFORM_NAMES[platform]} {TYPE_NAMES.get(account_type or '', 'account')}"


def build_sources(conn, now: datetime | None = None) -> int:
    """A profile for every account with posts. Returns how many were written."""
    now = now or datetime.now(timezone.utc)
    accounts = {r["account_id"]: r for r in _rows(
        conn, "SELECT a.account_id, a.handle, a.display_name, a.external_id, a.account_type, pl.code AS platform,"
              " (SELECT m.followers FROM account_metrics m WHERE m.account_id = a.account_id AND m.followers IS NOT NULL"
              "  ORDER BY m.captured_at DESC LIMIT 1) AS followers,"
              " (SELECT sp.source_key FROM source_profiles sp WHERE sp.account_id = a.account_id) AS source_key"
              " FROM accounts a JOIN platforms pl ON pl.platform_id = a.platform_id")
        if r["platform"] in PLATFORM_NAMES}
    posts: dict[int, list[dict]] = defaultdict(list)
    for p in _rows(conn, "SELECT post_id, published_at, language, url, COALESCE(container_account_id, account_id) AS account"
                         " FROM posts WHERE deleted_at IS NULL AND is_stub = FALSE"):
        if p["account"] in accounts:
            posts[p["account"]].append(p)
    if not posts:
        return 0

    relations = _relations(conn)
    copied_in: dict[int, list[dict]] = defaultdict(list)          # post -> relations that copy it from elsewhere
    for r in relations:
        if r["src_account"] != r["dst_account"]:
            copied_in[r["dst"]].append(r)
    run_id = _latest_run(conn)
    labels = {r["post_id"]: r["label"] for r in _rows(
        conn, "SELECT pc.post_id, c.label FROM post_clusters pc JOIN clusters c ON c.cluster_id = pc.cluster_id WHERE pc.run_id = %s",
        (run_id,))} if run_id else {}
    verdicts: dict[int, list[str]] = defaultdict(list)
    flagged_claims: dict[int, set[int]] = defaultdict(set)
    for r in _rows(conn, "SELECT pc.post_id, c.claim_id, c.verdict FROM post_claims pc JOIN claims c ON c.claim_id = pc.claim_id"):
        verdicts[r["post_id"]].append(r["verdict"])
    state_media = {r["post_id"] for r in _rows(
        conn, "SELECT post_id, text FROM posts WHERE deleted_at IS NULL AND (" +
              " OR ".join(["text LIKE %s"] * len(STATE_MEDIA)) + ")", tuple(f"%{d}%" for d in STATE_MEDIA))}
    state_media |= {r["post_id"] for r in _rows(
        conn, "SELECT pu.post_id, u.domain FROM post_urls pu JOIN urls u ON u.url_id = pu.url_id")
        if any(d in (r["domain"] or "") for d in STATE_MEDIA)}

    used_keys = {r["source_key"]: r["account_id"] for r in _rows(conn, "SELECT account_id, source_key FROM source_profiles")}
    written = 0
    today = now.date()
    for account_id, mine in posts.items():
        a = accounts[account_id]
        mine.sort(key=lambda p: (_utc(p["published_at"]) or now, p["post_id"]))
        recent30 = [p for p in mine if p["published_at"] and _utc(p["published_at"]) >= now - timedelta(days=30)]
        basis = recent30 or mine
        n = len(basis)
        copies = [p for p in basis if copied_in.get(p["post_id"])]
        near = [p for p in basis if any(r["kind"] == "copy" for r in copied_in.get(p["post_id"], []))]
        languages = Counter(p["language"] for p in mine if p["language"])
        language = _language(languages.most_common(1)[0][0]) if languages else ""
        topics = Counter(labels.get(p["post_id"], "") for p in basis)
        named = [(name, k) for name, k in topics.most_common() if name]
        flags = set()
        for p in mine:
            for claim_verdict in verdicts.get(p["post_id"], []):
                if claim_verdict in ("false", "mostly_false", "misleading", "mixed"):
                    flags.add((p["post_id"], claim_verdict))
        claim_flags = _rows(conn, "SELECT COUNT(DISTINCT c.claim_id) AS n FROM post_claims pc JOIN claims c ON c.claim_id = pc.claim_id"
                                  " JOIN posts p ON p.post_id = pc.post_id WHERE COALESCE(p.container_account_id, p.account_id) = %s"
                                  " AND c.verdict IN ('false', 'mostly_false', 'misleading', 'mixed')", (account_id,))[0]["n"]

        upstream: dict[int, list[float]] = defaultdict(list)
        for p in basis:
            for r in copied_in.get(p["post_id"], []):
                if r["src_at"] and r["dst_at"]:
                    upstream[r["src_account"]].append((r["dst_at"] - r["src_at"]).total_seconds())
                else:
                    upstream[r["src_account"]].append(float("nan"))
        up = []
        for src, delays in sorted(upstream.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:5]:
            s = accounts.get(src)
            if s is None:
                continue
            known = [d for d in delays if d == d]
            up.append({"name": _author({**s, "platform": s["platform"]}), "kind": _type_label(s["platform"], s["account_type"]),
                       "posts": len(delays), "typicalDelay": _span(statistics.median(known)) if known else "unknown"})

        latest = mine[::-1][:RECENT]
        texts = {r["post_id"]: r for r in _rows(conn, "SELECT p.post_id, p.text, (SELECT m.views FROM post_metrics m WHERE m.post_id ="
                                                       " p.post_id ORDER BY m.captured_at DESC LIMIT 1) AS views FROM posts p"
                                                       f" WHERE p.post_id IN ({_in(p['post_id'] for p in latest)[0]})",
                                                 tuple(p["post_id"] for p in latest))}
        days = Counter(_utc(p["published_at"]).date() for p in mine if p["published_at"])
        first = next((p["published_at"] for p in mine if p["published_at"]), None)
        followers = f"{_short_number(a['followers'])} {'subscribers' if a['platform'] == 'telegram' else 'followers'}" \
            if a["followers"] else ""
        report = {
            "typeLabel": _type_label(a["platform"], a["account_type"]),
            "subtitle": " · ".join(x for x in (followers, f"writes in {language}" if language else "",
                                               f"first post {first.strftime('%B %Y')}" if first else "") if x),
            "basedOn": f"Based on {len(mine):,} post{'s' if len(mine) != 1 else ''} collected"
                       + (f" since {first.strftime('%B %Y')}." if first else "."),
            "stats": {"postsPerDay": round(len(recent30) / 30, 1),
                      "originalPct": round(100 * (n - len(copies)) / n, 1),
                      "nearCopyPct": round(100 * len(near) / n, 1),
                      "stateMediaLinkPct": round(100 * sum(p["post_id"] in state_media for p in basis) / n, 1)},
            "daily": [{"date": (today - timedelta(days=i)).isoformat(), "posts": days.get(today - timedelta(days=i), 0)}
                      for i in range(DAILY_DAYS - 1, -1, -1)],
            "topics": [{"name": name, "pct": round(100 * k / n, 1)} for name, k in named[:4]] +
                      ([{"name": "Fits no narrative", "pct": round(100 * topics[""] / n, 1)}] if topics.get("") else []),
            "upstream": up,
            "recent": [{"id": str(p["post_id"]), "url": p["url"] or "", "text": (texts[p["post_id"]]["text"] or "")[:280],
                        "publishedAt": _utc(p["published_at"]).strftime("%Y-%m-%dT%H:%M:%SZ") if p["published_at"] else "",
                        "views": int(texts[p["post_id"]]["views"] or 0), "verdict": _verdict(verdicts.get(p["post_id"], []))}
                       for p in latest],
        }
        key = a["source_key"]
        if not key:
            base = _slug(a["handle"] or a["display_name"] or a["external_id"])
            key, i = base, 2
            while key in used_keys and used_keys[key] != account_id:
                key, i = f"{base}-{i}", i + 1
            used_keys[key] = account_id
        kind = " · ".join(x for x in (TYPE_NAMES.get(a["account_type"] or "", "account").capitalize()
                                      if a["platform"] != "web" else "News site", language, followers) if x)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO source_profiles (account_id, source_key, kind_label, posts_30d, original_pct, near_copy_pct,"
                        " main_topic, open_flags, report) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON DUPLICATE KEY UPDATE kind_label = VALUES(kind_label), posts_30d = VALUES(posts_30d),"
                        " original_pct = VALUES(original_pct), near_copy_pct = VALUES(near_copy_pct), main_topic = VALUES(main_topic),"
                        " open_flags = VALUES(open_flags), report = VALUES(report)",
                        (account_id, key, kind[:255], len(recent30), report["stats"]["originalPct"], report["stats"]["nearCopyPct"],
                         (named[0][0] if named else "")[:100], claim_flags, json.dumps(report, ensure_ascii=False)))
        written += 1
    conn.commit()
    return written


# --------------------------------------------------------------------------- 3. stories

def _post(conn, post_id: int) -> dict | None:
    rows = _rows(conn, "SELECT p.post_id, p.text, p.url, p.language, p.published_at, pl.code AS platform,"
                       " a.account_id, a.handle, a.display_name, a.account_type"
                       " FROM posts p JOIN platforms pl ON pl.platform_id = p.platform_id"
                       " LEFT JOIN accounts a ON a.account_id = COALESCE(p.container_account_id, p.account_id)"
                       " WHERE p.post_id = %s AND p.deleted_at IS NULL", (post_id,))
    return rows[0] if rows else None


def translation_of(conn, post: dict, translate: Callable[[str], tuple[str, str, str]] | None = None) -> str:
    """The post in English: itself when it is English; a stored translation; or a new one
    (translate returns (translation, prompt label, model) and the result is stored)."""
    text = (post["text"] or "")[:4000]
    if (post["language"] or "") == "en" or not text.strip():
        return text
    stored = _rows(conn, "SELECT details FROM post_analyses WHERE post_id = %s AND analyzer = 'translation'"
                         " ORDER BY created_at DESC, analysis_id DESC LIMIT 1", (post["post_id"],))
    if stored:
        return str(_json(stored[0]["details"], {}).get("translation") or "")
    if translate is None:
        return ""
    english, prompt, model = translate(text)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO post_analyses (post_id, analyzer, analyzer_version, model, label, details)"
                    " VALUES (%s, 'translation', %s, %s, NULL, %s)",
                    (post["post_id"], prompt[:50], model[:100], json.dumps({"translation": english}, ensure_ascii=False)))
    conn.commit()
    return english


_MARKDOWN = re.compile(r"(\*\*|__|~~|`|^#+\s*|^\s*[-*>]\s+)", re.MULTILINE)


def plain(text: str) -> str:
    """Text without Markdown marks (posts and translations often carry **bold** or # headings)."""
    return _MARKDOWN.sub("", text or "")


def _first_sentence(text: str, limit: int = 140) -> str:
    text = " ".join(plain(text).split())
    m = re.match(r"(.+?[.!?…])(\s|$)", text)
    s = m.group(1) if m and len(m.group(1)) >= 20 else text
    return s if len(s) <= limit else s[:limit - 1].rsplit(" ", 1)[0] + "…"


def _cluster_of(conn, post_id: int) -> dict | None:
    run_id = _latest_run(conn)
    if run_id is None:
        return None
    rows = _rows(conn, "SELECT c.cluster_id, c.label, c.size, c.change_pct, c.accounts, c.first_seen, c.last_seen, c.narrative_id"
                       " FROM post_clusters pc JOIN clusters c ON c.cluster_id = pc.cluster_id WHERE pc.run_id = %s AND pc.post_id = %s",
                 (run_id, post_id))
    return rows[0] if rows else None


def _claim_for(conn, post_ids: list[int]) -> dict | None:
    """The most significant claim these posts make: checked before unchecked, false before misleading
    before true, then the one most of them make."""
    if not post_ids:
        return None
    marks, args = _in(post_ids)
    rows = _rows(conn, "SELECT c.claim_id, c.text, c.verdict, c.verdict_explanation, c.verdict_at, MIN(pc.post_id) AS post_id,"
                       f" COUNT(*) AS n FROM post_claims pc JOIN claims c ON c.claim_id = pc.claim_id WHERE pc.post_id IN ({marks})"
                       " GROUP BY c.claim_id, c.text, c.verdict, c.verdict_explanation, c.verdict_at", args)
    if not rows:
        return None
    rows.sort(key=lambda r: (r["verdict_at"] is None, VERDICT_ORDER.get(r["verdict"], 3), -r["n"], r["claim_id"]))
    return rows[0]


def _story_claim(conn, claim: dict | None) -> dict:
    if claim is None:
        return {"headline": "We found no checkable claim in it.", "text": "", "verdict": "unverified",
                "explanation": "It may be an opinion, a question or a call to action rather than a statement of fact.",
                "evidence": ""}
    verdict = SHOWN_VERDICT.get(claim["verdict"], "unverified")
    headline = {"false": "Its main claim doesn't hold up.", "misleading": "Its main claim is misleading.",
                "true": "Its main claim checks out."}.get(verdict, "Its main claim hasn't been confirmed yet."
                                                          if claim["verdict_at"] else "Its main claim hasn't been checked yet.")
    sources = _rows(conn, "SELECT DISTINCT publisher FROM claim_evidence WHERE claim_id = %s AND publisher IS NOT NULL"
                          " ORDER BY publisher LIMIT 5", (claim["claim_id"],))
    names = [s["publisher"] for s in sources]
    evidence = (f"Checked against {len(names)} source{'s' if len(names) != 1 else ''}: {', '.join(names)}." if names
                else "No source has been found for it yet.")
    return {"headline": headline, "text": claim["text"], "verdict": verdict,
            "explanation": claim["verdict_explanation"] or "Not fact-checked yet.", "evidence": evidence}


def build_story(conn, post_id: int, *, story_id: str | None = None, cluster: dict | None = None, claim: dict | None = None,
                translate: Callable[[str], tuple[str, str, str]] | None = None, picked: bool = False) -> dict | None:
    """A post's story, as the Story contract (models.Story). None when the post can't be shown."""
    post = _post(conn, post_id)
    if post is None or post["platform"] not in PLATFORM_NAMES:
        return None
    cluster = cluster if cluster is not None else _cluster_of(conn, post_id)
    claim = claim if claim is not None else _claim_for(conn, [post_id])
    text = plain(post["text"] or "")[:1200]
    english = plain(translation_of(conn, post, translate))[:1200]

    # where it came from: the earlier posts it copies; how it travelled: everything copied from them
    rel = _rows(conn, "SELECT source_post_id AS src, target_post_id AS dst, relation_type AS kind FROM post_relations"
                      " WHERE relation_type IN ('copy', 'translation', 'paraphrase') AND (target_post_id = %s OR source_post_id = %s)",
                (post_id, post_id))
    sources = {r["src"]: r["kind"] for r in rel if r["dst"] == post_id}
    roots = set(sources) | {post_id}
    marks, args = _in(roots)
    family = _rows(conn, "SELECT source_post_id AS src, target_post_id AS dst, relation_type AS kind FROM post_relations"
                         f" WHERE relation_type IN ('copy', 'translation', 'paraphrase') AND source_post_id IN ({marks})", args)
    copies = {r["dst"]: r["kind"] for r in family if r["dst"] not in roots}
    members = {pid: _post(conn, pid) for pid in roots | set(copies)}
    members = {k: v for k, v in members.items() if v is not None}
    far = datetime.max
    ordered = sorted(members.values(), key=lambda p: (p["published_at"] or far, p["post_id"]))

    def node(p):
        word = RELATION_WORD.get(sources.get(p["post_id"]) or copies.get(p["post_id"]) or "", "")
        meta = " · ".join(x for x in (_when(p["published_at"]), _language(p["language"]), word) if x)
        kind = ("The post you picked" if picked else "This post") if p["post_id"] == post_id else _where(p)
        return {"meta": meta, "name": _author(p), "kind": kind}

    earlier = [p for p in ordered if p["post_id"] in sources]
    later = [p for p in ordered if p["post_id"] in copies]
    chain = [node(p) for p in earlier[:2]] + [node(post)] + [node(p) for p in later[:2]]
    if earlier and earlier[0]["published_at"] and post["published_at"]:
        gap = _span((post["published_at"] - earlier[0]["published_at"]).total_seconds())
        origin = f"It started with {_author(earlier[0])}, {gap} earlier."
    elif earlier:
        origin = f"It started with {_author(earlier[0])}."
    else:
        origin = "We found no earlier version: this is the first post we have that says it."

    spread = [p for p in ordered if p["post_id"] != ordered[0]["post_id"]] if len(ordered) > 1 else []
    accounts = {p["account_id"] for p in ordered if p["account_id"]}
    langs = Counter(_language(p["language"]) for p in ordered)
    times = [p["published_at"] for p in ordered if p["published_at"]]
    time_label = _span((times[-1] - times[0]).total_seconds()) if len(times) > 1 else "—"
    if spread:
        k = len(langs)
        travel = (f"Within {time_label} it was copied {len(spread)} time{'s' if len(spread) != 1 else ''}, "
                  f"in {'one language' if k == 1 else f'{k} languages'}.")
    else:
        travel = "We haven't found copies of it yet."

    if cluster:
        change = cluster["change_pct"]
        trend_text = ("new this week" if change is None else
                      f"{'up' if change > 0 else 'down' if change < 0 else 'no change'}"
                      f"{f' {abs(float(change)):g}%' if change else ''} on the run before")
        narrative = {"headline": f"Yes. It's one of {cluster['size']:,} posts telling the same story.",
                     "name": cluster["label"] or "Unnamed narrative",
                     "stats": f"{cluster['size']:,} posts from {cluster['accounts']:,} accounts · {trend_text}"}
    else:
        narrative = {"headline": "Not yet: it doesn't fit any narrative we track.", "name": "", "stats": ""}

    day = (post["published_at"] or datetime.now(timezone.utc)).date()
    first = cluster["first_seen"] if cluster and cluster["first_seen"] else min((t.date() for t in times), default=day)
    last = cluster["last_seen"] if cluster and cluster["last_seen"] else max((t.date() for t in times), default=day)
    if first > last:
        first, last = last, first
    status = SHOWN_VERDICT.get(claim["verdict"], "unverified") if claim else "unverified"
    return {
        "id": story_id or f"post-{post_id}",
        "platform": PLATFORM_NAMES[post["platform"]],
        "status": status,
        "firstSeen": first.isoformat(), "lastSeen": last.isoformat(),
        "topic": ((cluster["label"] if cluster else "") or "")[:100],
        "trend": {"posts": cluster["size"] if cluster else len(ordered),
                  "changePct": float(cluster["change_pct"]) if cluster and cluster["change_pct"] is not None else None},
        "headline": f"“{_first_sentence(english or text)}”",
        "sub": f"{PLATFORM_NAMES[post['platform']]} · {_language(post['language'])}",
        "post": {**({"url": post["url"]} if post["url"] else {}), "author": _author(post), "where": _where(post),
                 "text": text, "translation": english},
        "claim": _story_claim(conn, claim),
        "origin": {"headline": origin, "chain": chain},
        "travel": {"headline": travel, "copies": len(spread), "accounts": len(accounts), "timeLabel": time_label,
                   "languages": [{"name": n, "count": c} for n, c in langs.most_common()]},
        "narrative": narrative,
    }


def build_stories(conn, translate: Callable[[str], tuple[str, str, str]] | None = None,
                  progress: Callable[[str], None] | None = None) -> int:
    """One trending story per narrative of the latest run (replacing the earlier ones)."""
    say = progress or (lambda message: None)
    run_id = _latest_run(conn)
    clusters = _rows(conn, "SELECT cluster_id, label, size, change_pct, accounts, first_seen, last_seen, narrative_id, stats"
                           " FROM clusters WHERE run_id = %s ORDER BY size DESC, cluster_id LIMIT %s", (run_id, MAX_STORIES)) \
        if run_id else []
    keys: list[str] = []
    for n, cluster in enumerate(clusters, 1):
        say(f"Writing the story of narrative {n} of {len(clusters)}.")
        members = [r["post_id"] for r in _rows(conn, "SELECT post_id FROM post_clusters WHERE run_id = %s AND cluster_id = %s",
                                               (run_id, cluster["cluster_id"]))]
        claim = _claim_for(conn, members)
        stats = _json(cluster["stats"], {})
        typical = [int(r["id"]) for r in stats.get("typical", [])]
        post_id = claim["post_id"] if claim else (typical[0] if typical else (members[0] if members else None))
        if post_id is None:
            continue
        key = f"{_slug(cluster['label'] or 'narrative')[:48].strip('-.')}-{cluster['narrative_id'] or cluster['cluster_id']}"
        story = build_story(conn, post_id, story_id=key, cluster=cluster, claim=claim, translate=translate)
        if story is None:
            continue
        post = _post(conn, post_id)
        platform = _rows(conn, "SELECT platform_id FROM platforms WHERE code = %s", (post["platform"],))[0]["platform_id"]
        with conn.cursor() as cur:
            cur.execute("INSERT INTO stories (story_key, platform_id, status, topic, first_seen, last_seen, trend_posts,"
                        " trend_change_pct, post_link_key, body) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON DUPLICATE KEY UPDATE platform_id = VALUES(platform_id), status = VALUES(status), topic = VALUES(topic),"
                        " first_seen = VALUES(first_seen), last_seen = VALUES(last_seen), trend_posts = VALUES(trend_posts),"
                        " trend_change_pct = VALUES(trend_change_pct), post_link_key = VALUES(post_link_key), body = VALUES(body)",
                        (key, platform, story["status"], story["topic"], story["firstSeen"], story["lastSeen"],
                         story["trend"]["posts"], story["trend"]["changePct"],
                         link_key(post["url"])[:512] if post["url"] else None,
                         json.dumps({k: v for k, v in story.items() if k in ("headline", "sub", "post", "claim", "origin",
                                                                              "travel", "narrative")}, ensure_ascii=False)))
        keys.append(key)
    with conn.cursor() as cur:
        if keys:
            marks, args = _in(keys)
            cur.execute(f"DELETE FROM stories WHERE story_key NOT IN ({marks})", args)
        else:
            cur.execute("DELETE FROM stories")
    conn.commit()
    return len(keys)


def derive_all(conn, qdrant: Qdrant, translate=None, progress: Callable[[str], None] | None = None) -> dict:
    say = progress or (lambda message: None)
    copies = find_copies(conn, qdrant, say)
    say("Updating the source profiles.")
    sources = build_sources(conn)
    stories = build_stories(conn, translate, say)
    return {"copies": copies, "sources": sources, "stories": stories}


__all__ = ["find_copies", "build_sources", "build_stories", "build_story", "derive_all", "translation_of", "date"]
