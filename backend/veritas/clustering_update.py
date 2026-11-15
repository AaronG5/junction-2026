"""Cluster updates without recomputing (the dashboard's "Update clusters" button).

Only the posts that aren't in the latest clustering run yet are handled; everything
already placed keeps its cluster and its place on the map:
  a. a new post joins the existing cluster whose centre is closest, when it is about as close
     to it as the cluster's typical member is (RADIUS_PERCENTILE, RADIUS_MARGIN; never looser
     than MEMBER_MIN) in the original 768 numbers;
  b. the rest, together with the earlier posts that fit no cluster, are grouped among
     themselves (UMAP + HDBSCAN on that small set only). A group whose centre is close to
     an existing one (SAME_NARRATIVE) joins it; any other becomes a new cluster, named by Gemma;
  c. each new post gets a map position from its nearest already-placed posts (Qdrant search),
     in its own cluster when it has one;
  d. only the clusters that changed get their numbers, ranks, typical and edge posts refreshed.
The run is updated in place (its params keep a log of the updates), so the cost grows with
the new posts, not with the history. Without any run yet, the first one is a full run over
every post. A full rebuild (clustering.run_clustering) is there for when the map has drifted.

Which posts are already in the run is kept in Qdrant's payload ("clustered_run"), so
finding the new ones is one filtered scroll, not a comparison against MySQL.
"""
from __future__ import annotations

import json
import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Sequence

from .ai import LocalAI
from .clustering import (
    EDGE, EDGE_NOTE, LANGUAGE_NAMES, MEMBER_MIN, MIN_POSTS, NAMING_POSTS, SAME_NARRATIVE, TYPICAL, _name, _post_rows,
    _ref, _rfc3339, _rows, group, run_clustering,
)
from .queries import PLATFORM_NAMES
from .vectors import Point, Qdrant

MAP_NEIGHBOURS = 5        # placed posts a new post's map position is taken from
# How close a new post must be to join: as close as a typical member (the 75th percentile of the
# members' distances), with a little room, because the members shaped the centre and a later post
# telling the same story sits slightly farther out. Not the farthest member: one loose member would
# let everything in (on real data a cosine of 0.5 is "the same language and topic area", not "the same story").
RADIUS_PERCENTILE = 0.75
RADIUS_MARGIN = 1.10
UP_TO_DATE = "No new posts since the last update."


def latest_run(conn) -> int | None:
    row = _rows(conn, "SELECT run_id FROM clustering_runs WHERE status = 'done' ORDER BY finished_at DESC, run_id DESC LIMIT 1")
    return row[0]["run_id"] if row else None


def _unit(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(p * q for p, q in zip(a, b))


def _json(value, default):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _jitter(post_id: int) -> tuple[float, float]:
    """A small fixed offset per post, so posts placed from the same neighbours don't share one dot."""
    h = (post_id * 2654435761) & 0xFFFFFFFF
    return ((h & 0xFFFF) / 0xFFFF - 0.5) * 3, ((h >> 16) / 0xFFFF - 0.5) * 3


def place(conn, qdrant: Qdrant, run_id: int, post_id: int, vector, cluster_id: int | None) -> tuple[float, float]:
    """Map position: the similarity-weighted middle of the nearest posts already on the map."""
    must: list[dict] = [{"key": "clustered_run", "match": {"value": run_id}}]
    if cluster_id is not None:
        must.append({"key": "cluster_id", "match": {"value": cluster_id}})
    hits = [h for h in qdrant.search("posts", vector, limit=MAP_NEIGHBOURS, flt={"must": must})
            if isinstance(h.payload.get("post_id"), int) and h.payload["post_id"] != post_id]
    spots: dict[int, tuple[float, float]] = {}
    if hits:
        ids = [h.payload["post_id"] for h in hits]
        marks = ", ".join(["%s"] * len(ids))
        spots = {r["post_id"]: (float(r["map_x"]), float(r["map_y"])) for r in _rows(
            conn, f"SELECT post_id, map_x, map_y FROM post_clusters WHERE run_id = %s AND post_id IN ({marks})"
                  " AND map_x IS NOT NULL AND map_y IS NOT NULL", (run_id, *ids))}
    weighted = [(max(h.score, 0.01), spots[h.payload["post_id"]]) for h in hits if h.payload["post_id"] in spots]
    if weighted:
        total = sum(w for w, _ in weighted)
        x = sum(w * p[0] for w, p in weighted) / total
        y = sum(w * p[1] for w, p in weighted) / total
    else:
        x, y = 50.0, 50.0
    dx, dy = _jitter(post_id)
    return round(min(98.0, max(2.0, x + dx)), 2), round(min(98.0, max(2.0, y + dy)), 2)


def refresh_cluster(conn, run_id: int, cluster_id: int) -> None:
    """A cluster's numbers, ranks, typical and edge posts again from its members. Keeps the
    highlights already found for posts that stay typical (cluster_insight adds the rest)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE post_clusters pc JOIN (SELECT post_id, ROW_NUMBER() OVER (ORDER BY distance_to_centroid, post_id) AS r"
                    " FROM post_clusters WHERE run_id = %s AND cluster_id = %s) t ON t.post_id = pc.post_id"
                    " SET pc.rank_in_cluster = t.r WHERE pc.run_id = %s", (run_id, cluster_id, run_id))
    members = _rows(conn, "SELECT post_id FROM post_clusters WHERE run_id = %s AND cluster_id = %s ORDER BY rank_in_cluster",
                    (run_id, cluster_id))
    rows = _post_rows(conn, [m["post_id"] for m in members])
    ordered = [rows[m["post_id"]] for m in members if m["post_id"] in rows]
    current = _rows(conn, "SELECT stats, narrative_id FROM clusters WHERE cluster_id = %s", (cluster_id,))[0]
    stats = _json(current["stats"], {})
    known = {ref["id"]: ref for ref in stats.get("typical", []) + stats.get("edge", [])}

    def ref(row, note=None):
        fresh = _ref(row, note)
        old = known.get(fresh["id"])
        for key in ("segments", "highlights"):
            if old and key in old:
                fresh[key] = old[key]
        return fresh

    typical_rows = ordered[:TYPICAL]
    langs = Counter(r["language"] or "xx" for r in ordered)
    stats.update({
        "platforms": [PLATFORM_NAMES[c] for c, _ in Counter(r["platform"] for r in ordered).most_common() if c in PLATFORM_NAMES],
        "languages": [{"code": c, "name": LANGUAGE_NAMES.get(c, "Other" if c == "xx" else c.upper()),
                       "share": round(100 * k / max(1, len(ordered)), 1)} for c, k in langs.most_common()],
        "typical": [ref(r) for r in typical_rows],
        "edge": [ref(r, EDGE_NOTE) for r in ordered[::-1][:EDGE] if r not in typical_rows],
    })
    previous = stats.get("previous_size")
    change = round((len(ordered) - previous) / previous * 100, 1) if previous else None
    dates = sorted(r["published_at"] for r in ordered if r["published_at"])
    with conn.cursor() as cur:
        cur.execute("UPDATE clusters SET size = %s, stats = %s, change_pct = %s, accounts = %s, first_seen = %s, last_seen = %s"
                    " WHERE cluster_id = %s",
                    (len(ordered), json.dumps(stats, ensure_ascii=False), change,
                     len({r["account_id"] for r in ordered if r["account_id"]}),
                     dates[0].date() if dates else None, dates[-1].date() if dates else None, cluster_id))
        if dates and current["narrative_id"]:
            cur.execute("UPDATE narratives SET last_seen_at = GREATEST(COALESCE(last_seen_at, %s), %s) WHERE narrative_id = %s",
                        (dates[-1], dates[-1], current["narrative_id"]))
    conn.commit()


def _skip_already_in_run(conn, qdrant: Qdrant, run_id: int, fresh: list) -> list:
    """Posts that are in the run but whose vector lost its mark (clustered before marks existed,
    or embedded again): mark them again instead of treating them as new."""
    inside: dict[int, int | None] = {}
    ids = [p[2]["post_id"] for p in fresh]
    for start in range(0, len(ids), 1000):
        chunk = ids[start:start + 1000]
        marks = ", ".join(["%s"] * len(chunk))
        inside.update({r["post_id"]: r["cluster_id"] for r in _rows(
            conn, f"SELECT post_id, cluster_id FROM post_clusters WHERE run_id = %s AND post_id IN ({marks})", (run_id, *chunk))})
    if inside:
        by_cluster: dict[int | None, list[str]] = {}
        for p in fresh:
            if p[2]["post_id"] in inside:
                by_cluster.setdefault(inside[p[2]["post_id"]], []).append(p[0])
        for cluster_id, points in by_cluster.items():
            qdrant.set_payload("posts", points, {"cluster_id": cluster_id, "clustered_run": run_id})
    return [p for p in fresh if p[2]["post_id"] not in inside]


def _radii(conn, run_id: int) -> dict[int, float]:
    """Per cluster: the distance a new post may have from its centre to join it."""
    distances: dict[int, list[float]] = {}
    for r in _rows(conn, "SELECT cluster_id, distance_to_centroid AS d FROM post_clusters"
                         " WHERE run_id = %s AND cluster_id IS NOT NULL AND distance_to_centroid IS NOT NULL", (run_id,)):
        distances.setdefault(r["cluster_id"], []).append(float(r["d"]))
    out = {}
    for cluster_id, ds in distances.items():
        ds.sort()
        out[cluster_id] = RADIUS_MARGIN * ds[min(len(ds) - 1, int(RADIUS_PERCENTILE * len(ds)))]
    return out


def _best(vector: Sequence[float], centres: dict[int, list[float]]) -> tuple[int, float] | None:
    return max(((c, _dot(vector, centre)) for c, centre in centres.items()), key=lambda t: (t[1], -t[0]), default=None)


def update_clustering(conn, qdrant: Qdrant, ai: LocalAI, *, now: datetime | None = None, min_posts: int = MIN_POSTS,
                      progress: Callable[[str], None] | None = None) -> dict:
    """Add the posts that aren't clustered yet to the latest run. Returns a short report;
    "clusters" lists the clusters that changed (cluster_insight looks at those)."""
    now = now or datetime.now(timezone.utc)
    say = progress or (lambda message: None)
    run_id = latest_run(conn)
    if run_id is None:
        say("Grouping every post for the first time. This can take a few minutes.")
        return {**run_clustering(conn, qdrant, ai, now=now, min_posts=min_posts), "mode": "full"}

    say("Finding the posts that are new since the last update.")
    fresh = [p for p in qdrant.vectors("posts", flt={"must_not": [{"key": "clustered_run", "match": {"value": run_id}}]})
             if isinstance(p[2].get("post_id"), int)]
    rows = _post_rows(conn, [p[2]["post_id"] for p in fresh])
    fresh = sorted((p for p in fresh if p[2]["post_id"] in rows), key=lambda p: p[2]["post_id"])
    fresh = _skip_already_in_run(conn, qdrant, run_id, fresh)
    if not fresh:
        return {"status": "up_to_date", "mode": "update", "run_id": run_id, "posts": 0, "joined": 0, "new_clusters": 0,
                "clusters": [], "message": UP_TO_DATE}

    say(f"Adding {len(fresh):,} new posts to the clusters.")
    centres = {payload["cluster_id"]: _unit(vec) for _pid, vec, payload in
               qdrant.vectors("clusters", flt={"must": [{"key": "run_id", "match": {"value": run_id}}]})
               if isinstance(payload.get("cluster_id"), int)}
    radius = _radii(conn, run_id)

    # a. Into an existing cluster, when inside its radius.
    placed: dict[int, tuple[int | None, float | None]] = {}     # new post -> (cluster, distance)
    leftover = []
    for p in fresh:
        best = _best(_unit(p[1]), centres)
        if best and best[0] in radius and best[1] >= max(MEMBER_MIN, 1 - radius[best[0]]):
            placed[p[2]["post_id"]] = (best[0], round(1 - best[1], 6))
        else:
            leftover.append(p)
    joined_existing = len(placed)

    # b. The rest, with the earlier posts that fit no cluster, grouped among themselves.
    old_noise = [p for p in qdrant.vectors("posts", flt={"must": [{"key": "clustered_run", "match": {"value": run_id}},
                                                                   {"is_empty": {"key": "cluster_id"}}]})
                 if isinstance(p[2].get("post_id"), int)]
    pool = sorted(leftover + old_noise, key=lambda p: p[2]["post_id"])
    rows.update(_post_rows(conn, [p[2]["post_id"] for p in old_noise]))
    pool = [p for p in pool if p[2]["post_id"] in rows]
    fresh_ids = {p[2]["post_id"] for p in fresh}
    joined: dict[int, tuple[int, float]] = {}                  # earlier noise post -> (cluster, distance)
    new_clusters: list[dict] = []
    if len(pool) >= min_posts:
        say(f"Looking for new narratives among {len(pool):,} posts that fit no cluster.")
        grouping = group([p[1] for p in pool])
        for lab in sorted(set(grouping.labels) - {-1}, key=lambda lab: (-grouping.labels.count(lab), lab)):
            members = [i for i, v in enumerate(grouping.labels) if v == lab]
            same = _best(grouping.centres[lab], centres)
            if same and same[1] >= SAME_NARRATIVE:
                target = same[0]
            else:
                say(f"Naming new narrative {len(new_clusters) + 1}.")
                by_rank = sorted(members, key=lambda i: grouping.rank[i])
                name, summary = _name(ai, [rows[pool[i][2]["post_id"]]["text"] or "" for i in by_rank[:NAMING_POSTS]])
                dates = sorted(d for d in (rows[pool[i][2]["post_id"]]["published_at"] for i in members) if d)
                centre_id = uuid.uuid4()
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO narratives (name, description, first_seen_at, last_seen_at) VALUES (%s, %s, %s, %s)",
                                (name, summary, dates[0] if dates else None, dates[-1] if dates else None))
                    narrative_id = cur.lastrowid
                    cur.execute("INSERT INTO clusters (run_id, label, summary, size, stats, centroid_uuid, narrative_id, accounts)"
                                " VALUES (%s, %s, %s, 0, %s, %s, %s, 0)",
                                (run_id, name, summary, json.dumps({"previous_size": None}), centre_id.bytes, narrative_id))
                    target = cur.lastrowid
                conn.commit()
                qdrant.upsert("clusters", [Point(str(centre_id), grouping.centres[lab],
                                                 {"cluster_id": target, "run_id": run_id, "narrative_id": narrative_id})])
                centres[target] = _unit(grouping.centres[lab])
                new_clusters.append({"cluster_id": target, "name": name})
            for i in members:
                post_id = pool[i][2]["post_id"]
                entry = (target, round(1 - _dot(_unit(pool[i][1]), centres[target]), 6))
                if post_id in fresh_ids:
                    placed[post_id] = entry
                else:
                    joined[post_id] = entry

    # c. Map positions and rows for the new posts; earlier noise that found a cluster moves into it.
    say("Placing the new posts on the map.")
    by_id = {p[2]["post_id"]: p for p in fresh}
    touched: set[int] = set()
    with conn.cursor() as cur:
        for post_id, (_pid, vec, _payload) in by_id.items():
            cluster_id, distance = placed.get(post_id, (None, None))
            # A brand-new cluster has nothing on the map yet: place by any neighbour.
            x, y = place(conn, qdrant, run_id, post_id, vec, cluster_id if cluster_id in radius else None)
            cur.execute("INSERT INTO post_clusters (post_id, run_id, cluster_id, distance_to_centroid, map_x, map_y)"
                        " VALUES (%s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE cluster_id = VALUES(cluster_id),"
                        " distance_to_centroid = VALUES(distance_to_centroid), map_x = VALUES(map_x), map_y = VALUES(map_y)",
                        (post_id, run_id, cluster_id, distance, x, y))
            if cluster_id is not None:
                touched.add(cluster_id)
        for post_id, (cluster_id, distance) in joined.items():
            cur.execute("UPDATE post_clusters SET cluster_id = %s, distance_to_centroid = %s WHERE post_id = %s AND run_id = %s",
                        (cluster_id, distance, post_id, run_id))
            touched.add(cluster_id)
    conn.commit()
    point_of = {p[2]["post_id"]: p[0] for p in fresh + old_noise}
    for cluster_id in sorted(touched):
        ids = [point_of[p] for p, (c, _) in list(placed.items()) + list(joined.items()) if c == cluster_id]
        qdrant.set_payload("posts", ids, {"cluster_id": cluster_id, "clustered_run": run_id})
    qdrant.set_payload("posts", [point_of[p] for p in by_id if placed.get(p, (None, None))[0] is None],
                       {"cluster_id": None, "clustered_run": run_id})

    # d. Refresh only what changed.
    for cluster_id in sorted(touched):
        refresh_cluster(conn, run_id, cluster_id)
    counts = _rows(conn, "SELECT COUNT(*) AS posts, SUM(cluster_id IS NULL) AS noise, COUNT(DISTINCT cluster_id) AS clusters"
                         " FROM post_clusters WHERE run_id = %s", (run_id,))[0]
    params = _json(_rows(conn, "SELECT params FROM clustering_runs WHERE run_id = %s", (run_id,))[0]["params"], {})
    params["updates"] = (params.get("updates", []) + [{"at": _rfc3339(now), "new_posts": len(fresh),
                                                       "in_clusters": joined_existing, "new_clusters": len(new_clusters)}])[-50:]
    noise = int(counts["noise"] or 0)
    with conn.cursor() as cur:
        cur.execute("UPDATE clustering_runs SET post_count = %s, cluster_count = %s, unclustered_count = %s, window_end = %s,"
                    " finished_at = %s, params = %s WHERE run_id = %s",
                    (counts["posts"], counts["clusters"], noise, now.replace(tzinfo=None), now.replace(tzinfo=None),
                     json.dumps(params), run_id))
    conn.commit()
    return {"status": "done", "mode": "update", "run_id": run_id, "posts": len(fresh), "joined": joined_existing,
            "new_clusters": len(new_clusters), "clusters": [{"cluster_id": c} for c in sorted(touched)], "unclustered": noise}
