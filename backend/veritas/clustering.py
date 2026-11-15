"""Clustering (B9): group posts telling the same story into narratives.

A batch job, run by hand or on a schedule:  uv run python backend/cluster.py --days 7
Needs the optional packages:                uv sync --extra clustering

1. Take the posts published in a window (their EmbeddingGemma vectors are in Qdrant).
2. UMAP squeezes the 768 numbers to 5 (for grouping) and to 2 (for the map), and
   HDBSCAN finds the groups; posts that fit no group are "noise" (unclustered).
   UMAP tends to pull lone posts into the nearest group, so membership is then
   checked in the original 768 numbers: a post that isn't similar enough to its
   group's centre (MEMBER_MIN) fits no group after all.
   Both use a fixed random seed, so the same input always gives the same result.
3. In each group, posts closest to the centre are "typical", the farthest are "edge".
4. Gemma names each group from its 5 most typical posts.
5. Each group is linked to last run's narrative when their centres are close enough,
   so a story keeps its narrative (and its week-on-week change) across runs.
6. Everything the Narratives screen shows is stored: the run, the clusters (with their
   summary numbers), each post's place on the map, and the centres in Qdrant.
Too few posts gives "no clusters" instead of nonsense groups.

Updates that add new posts without recomputing the rest: veritas/clustering_update.py.
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .ai import LocalAI, load_prompt
from .queries import PLATFORM_NAMES
from .vectors import Point, Qdrant

log = logging.getLogger("veritas.clustering")

MIN_POSTS = 20            # fewer posts than this: no clustering
MAX_MIN_CLUSTER = 50      # HDBSCAN's smallest group is n/40, but never more than this (long histories)
SAME_NARRATIVE = 0.85     # cosine similarity of two centres to count as the same story
MEMBER_MIN = 0.5          # a post less similar than this to its group's centre fits no group
SEED = 42
TYPICAL = 2               # typical posts shown per cluster
EDGE = 1                  # edge posts shown per cluster
NAMING_POSTS = 5          # typical posts Gemma reads to name a cluster
EDGE_NOTE = "Farthest from the centre of this group: it may only touch on the story."
LANGUAGE_NAMES = {"lt": "Lithuanian", "ru": "Russian", "en": "English", "pl": "Polish", "lv": "Latvian",
                  "et": "Estonian", "uk": "Ukrainian", "be": "Belarusian", "de": "German"}


class ClusteringUnavailable(Exception):
    """The optional clustering packages aren't installed. Shown to users as is."""


class ClusterNameAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, Field(max_length=120)]
    summary: Annotated[str, Field(max_length=400)]


@dataclass
class Grouping:
    labels: list[int]                 # cluster number per post, -1 = fits no cluster
    map_xy: list[tuple[float, float]]  # 0-100, 2 decimals
    centres: dict[int, list[float]] = field(default_factory=dict)
    distance: list[float] = field(default_factory=list)  # cosine distance to its cluster's centre (noise: nan)
    rank: list[int | None] = field(default_factory=list)  # 1 = most typical in its cluster


def _libs():
    try:
        import numpy as np
        import umap
        from sklearn.cluster import HDBSCAN
    except ImportError:
        raise ClusteringUnavailable("Clustering needs extra packages. Install them with:  uv sync --extra clustering") from None
    return np, umap, HDBSCAN


def group(vectors: Sequence[Sequence[float]], seed: int = SEED, min_cluster_size: int | None = None) -> Grouping:
    """Find groups among vectors (the pure part: no database, no AI)."""
    np, umap, HDBSCAN = _libs()
    x = np.asarray(vectors, dtype=np.float64)
    n = len(x)
    x = x / np.clip(np.linalg.norm(x, axis=1, keepdims=True), 1e-12, None)
    neighbours = max(2, min(15, n - 1))
    reduced = umap.UMAP(n_components=min(5, n - 2), n_neighbors=neighbours, min_dist=0.0, metric="cosine",
                        random_state=seed, n_jobs=1, compatibility_layout=True).fit_transform(x)
    size = min_cluster_size or max(5, min(n // 40, MAX_MIN_CLUSTER))
    labels = HDBSCAN(min_cluster_size=size, copy=True).fit(reduced).labels_.tolist()

    flat = umap.UMAP(n_components=2, n_neighbors=neighbours, min_dist=0.1, metric="cosine",
                     random_state=seed, n_jobs=1, compatibility_layout=True).fit_transform(x)
    lo, hi = flat.min(axis=0), flat.max(axis=0)
    span = np.where(hi - lo > 1e-9, hi - lo, 1.0)
    scaled = 2 + (flat - lo) / span * 96
    map_xy = [(round(float(a), 2), round(float(b), 2)) for a, b in scaled]

    def centre_of(members):
        c = x[members].mean(axis=0)
        return c / max(np.linalg.norm(c), 1e-12)

    for label in sorted(set(labels) - {-1}):
        members = [i for i, lab in enumerate(labels) if lab == label]
        centre = centre_of(members)
        for i in members:
            if float(x[i] @ centre) < MEMBER_MIN:
                labels[i] = -1
        if sum(1 for lab in labels if lab == label) < size:
            labels = [-1 if lab == label else lab for lab in labels]  # too few left: not a group

    out = Grouping(labels=labels, map_xy=map_xy, distance=[math.nan] * n, rank=[None] * n)
    for label in sorted(set(labels) - {-1}):
        members = [i for i, lab in enumerate(labels) if lab == label]
        centre = centre_of(members)
        out.centres[label] = centre.tolist()
        dist = {i: float(1 - x[i] @ centre) for i in members}
        for r, i in enumerate(sorted(members, key=lambda i: (dist[i], i)), 1):
            out.distance[i], out.rank[i] = dist[i], r
    return out


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(p * q for p, q in zip(a, b))
    na, nb = math.sqrt(sum(p * p for p in a)), math.sqrt(sum(q * q for q in b))
    return dot / (na * nb) if na and nb else 0.0


def match_narratives(new: dict[int, list[float]], old: dict[int, list[float]], threshold: float = SAME_NARRATIVE) -> dict[int, int]:
    """New cluster -> old cluster with a close enough centre, each old one used once (best pairs first)."""
    pairs = sorted(((_cos(nv, ov), n, o) for n, nv in new.items() for o, ov in old.items()), reverse=True)
    matched: dict[int, int] = {}
    used: set[int] = set()
    for score, n, o in pairs:
        if score < threshold:
            break
        if n not in matched and o not in used:
            matched[n] = o
            used.add(o)
    return matched


# --------------------------------------------------------------------------- the job

def _rows(conn, sql: str, args: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return list(cur.fetchall())


def _rfc3339(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _author(row: dict) -> str:
    if row["platform"] == "telegram" and row["handle"]:
        return "@" + row["handle"]
    return row["display_name"] or row["handle"] or "Unknown author"


def _where(row: dict) -> str:
    platform = PLATFORM_NAMES.get(row["platform"], row["platform"])
    kind = {"channel": "channel", "group": "group", "page": "page", "user": "profile"}.get(row["account_type"] or "", "")
    place = "Website" if row["platform"] == "web" else f"{platform} {kind}".strip()
    language = LANGUAGE_NAMES.get(row["language"] or "")
    return f"{place} · {language}" if language else place


def _ref(row: dict, note: str | None = None) -> dict:
    ref = {"id": str(row["post_id"]), "text": (row["text"] or "")[:280], "author": _author(row), "where": _where(row)}
    if row["url"]:
        ref["url"] = row["url"]
    if note:
        ref["note"] = note
    return ref


def _name(ai: LocalAI, texts: list[str]) -> tuple[str, str]:
    joined = "\n\n".join(f"{i}. {t[:500]}" for i, t in enumerate(texts, 1))
    answer = ai.ask(load_prompt("cluster_name"), ClusterNameAnswer, text=joined)
    return answer.value.name.strip() or "Unnamed narrative", answer.value.summary.strip()


_POST_ROWS = ("SELECT p.post_id, p.text, p.url, p.language, p.published_at, pl.code AS platform,"
              " a.account_id, a.handle, a.display_name, a.account_type"
              " FROM posts p JOIN platforms pl ON pl.platform_id = p.platform_id"
              " LEFT JOIN accounts a ON a.account_id = COALESCE(p.container_account_id, p.account_id)")


def _post_rows(conn, ids: Sequence[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for start in range(0, len(ids), 1000):
        chunk = list(ids[start:start + 1000])
        marks = ", ".join(["%s"] * len(chunk))
        out.update({r["post_id"]: r for r in _rows(
            conn, _POST_ROWS + f" WHERE p.post_id IN ({marks}) AND p.deleted_at IS NULL AND p.is_stub = FALSE", tuple(chunk))})
    return out


def run_clustering(conn, qdrant: Qdrant, ai: LocalAI, *, since: datetime | None = None, until: datetime | None = None,
                   now: datetime | None = None, min_posts: int = MIN_POSTS) -> dict:
    """Cluster the posts published between since and until (no since: every post). Returns a short report."""
    now = now or datetime.now(timezone.utc)
    until = until or now
    flt = ({"must": [{"key": "published_at", "range": {"gte": _rfc3339(since), "lte": _rfc3339(until)}}]}
           if since is not None else None)
    points = [(pid, vec, payload) for pid, vec, payload in qdrant.vectors("posts", flt=flt) if isinstance(payload.get("post_id"), int)]
    rows = _post_rows(conn, [p[2]["post_id"] for p in points])
    points = sorted((p for p in points if p[2]["post_id"] in rows), key=lambda p: p[2]["post_id"])  # stable input order
    if len(points) < min_posts:
        return {"status": "too_few", "posts": len(points),
                "message": f"Only {len(points)} posts in this window; at least {min_posts} are needed. No clusters made."}

    previous = _rows(conn, "SELECT run_id FROM clustering_runs WHERE status = 'done' ORDER BY finished_at DESC, run_id DESC LIMIT 1")
    params = {"umap": {"n_neighbors": 15, "min_dist": 0.0, "dims": 5, "seed": SEED}, "hdbscan": {"min_cluster_size": "max(5, n/40)"},
              "same_narrative": SAME_NARRATIVE}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO clustering_runs (embedding_model_id, algorithm, params, window_start, window_end, status, started_at)"
                    " VALUES (1, 'umap+hdbscan', %s, %s, %s, 'running', %s)",
                    (json.dumps(params), since.replace(tzinfo=None) if since else None, until.replace(tzinfo=None),
                     now.replace(tzinfo=None)))
        run_id = cur.lastrowid
    conn.commit()
    try:
        result = _cluster(conn, qdrant, ai, run_id, points, rows, previous[0]["run_id"] if previous else None, now)
    except BaseException:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("UPDATE clustering_runs SET status = 'failed', finished_at = %s WHERE run_id = %s",
                        (now.replace(tzinfo=None), run_id))
        conn.commit()
        raise
    return result


def _cluster(conn, qdrant: Qdrant, ai: LocalAI, run_id: int, points, rows: dict[int, dict], previous_run: int | None,
             now: datetime) -> dict:
    grouping = group([p[1] for p in points])
    post_ids = [p[2]["post_id"] for p in points]
    labels = sorted(set(grouping.labels) - {-1}, key=lambda lab: (-grouping.labels.count(lab), lab))  # largest first

    old_centres: dict[int, list[float]] = {}
    old_info: dict[int, dict] = {}
    if previous_run is not None:
        for _pid, vec, payload in qdrant.vectors("clusters", flt={"must": [{"key": "run_id", "match": {"value": previous_run}}]}):
            if isinstance(payload.get("cluster_id"), int):
                old_centres[payload["cluster_id"]] = vec
        if old_centres:
            marks = ", ".join(["%s"] * len(old_centres))
            old_info = {r["cluster_id"]: r for r in _rows(conn, f"SELECT cluster_id, size, narrative_id FROM clusters WHERE cluster_id IN ({marks})",
                                                       tuple(old_centres))}
    links = match_narratives({lab: grouping.centres[lab] for lab in labels}, old_centres)

    cluster_ids: dict[int, int] = {}
    summary = []
    for lab in labels:
        members = [i for i, v in enumerate(grouping.labels) if v == lab]
        by_rank = sorted(members, key=lambda i: grouping.rank[i])
        member_rows = [rows[post_ids[i]] for i in members]
        name, text = _name(ai, [rows[post_ids[i]]["text"] or "" for i in by_rank[:NAMING_POSTS]])
        platforms = [PLATFORM_NAMES[c] for c, _ in Counter(r["platform"] for r in member_rows).most_common() if c in PLATFORM_NAMES]
        langs = Counter(r["language"] or "xx" for r in member_rows)
        languages = [{"code": c, "name": LANGUAGE_NAMES.get(c, "Other" if c == "xx" else c.upper()),
                      "share": round(100 * k / len(members), 1)} for c, k in langs.most_common()]
        typical = [_ref(rows[post_ids[i]]) for i in by_rank[:TYPICAL]]
        edge = [_ref(rows[post_ids[i]], EDGE_NOTE) for i in by_rank[::-1][:EDGE] if i not in by_rank[:TYPICAL]]
        dates = sorted(r["published_at"] for r in member_rows if r["published_at"])
        accounts = len({r["account_id"] for r in member_rows if r["account_id"]})
        claim = _rows(conn, "SELECT pc.claim_id, COUNT(*) AS n FROM post_claims pc WHERE pc.post_id IN ("
                            + ", ".join(["%s"] * len(members)) + ") GROUP BY pc.claim_id ORDER BY n DESC, pc.claim_id LIMIT 1",
                      tuple(post_ids[i] for i in members))

        old = old_info.get(links[lab]) if lab in links else None
        narrative_id = old["narrative_id"] if old else None
        change = round((len(members) - old["size"]) / old["size"] * 100, 1) if old and old["size"] else None
        with conn.cursor() as cur:
            if narrative_id is None:
                cur.execute("INSERT INTO narratives (name, description, first_seen_at, last_seen_at) VALUES (%s, %s, %s, %s)",
                            (name, text, dates[0] if dates else None, dates[-1] if dates else None))
                narrative_id = cur.lastrowid
            else:
                cur.execute("UPDATE narratives SET last_seen_at = GREATEST(COALESCE(last_seen_at, %s), %s) WHERE narrative_id = %s",
                            (dates[-1] if dates else now.replace(tzinfo=None), dates[-1] if dates else now.replace(tzinfo=None), narrative_id))
            centre_id = uuid.uuid4()
            cur.execute("INSERT INTO clusters (run_id, label, summary, size, stats, centroid_uuid, narrative_id, main_claim_id,"
                        " change_pct, accounts, first_seen, last_seen) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (run_id, name, text, len(members),
                         json.dumps({"platforms": platforms, "languages": languages, "typical": typical, "edge": edge,
                                     "previous_size": old["size"] if old else None}, ensure_ascii=False),
                         centre_id.bytes, narrative_id, claim[0]["claim_id"] if claim else None, change, accounts,
                         dates[0].date() if dates else None, dates[-1].date() if dates else None))
            cluster_ids[lab] = cur.lastrowid
        conn.commit()
        qdrant.upsert("clusters", [Point(str(centre_id), grouping.centres[lab],
                                         {"cluster_id": cluster_ids[lab], "run_id": run_id, "narrative_id": narrative_id})])
        summary.append({"cluster_id": cluster_ids[lab], "name": name, "size": len(members),
                        "same_narrative_as": links.get(lab), "change_pct": change})

    with conn.cursor() as cur:
        for i, post_id in enumerate(post_ids):
            lab = grouping.labels[i]
            x, y = grouping.map_xy[i]
            cur.execute("INSERT INTO post_clusters (post_id, run_id, cluster_id, distance_to_centroid, rank_in_cluster, map_x, map_y)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (post_id, run_id, cluster_ids.get(lab), None if lab == -1 else round(grouping.distance[i], 6),
                         grouping.rank[i], x, y))
        unclustered = grouping.labels.count(-1)
        cur.execute("UPDATE clustering_runs SET status = 'done', finished_at = %s, post_count = %s, cluster_count = %s,"
                    " unclustered_count = %s WHERE run_id = %s",
                    (now.replace(tzinfo=None), len(post_ids), len(labels), unclustered, run_id))
    conn.commit()
    for lab, cid in cluster_ids.items():
        qdrant.set_payload("posts", [points[i][0] for i, v in enumerate(grouping.labels) if v == lab],
                           {"cluster_id": cid, "clustered_run": run_id})
    noise = [points[i][0] for i, v in enumerate(grouping.labels) if v == -1]
    qdrant.set_payload("posts", noise, {"cluster_id": None, "clustered_run": run_id})
    return {"status": "done", "run_id": run_id, "posts": len(post_ids), "clusters": summary, "unclustered": unclustered}
