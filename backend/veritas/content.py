"""Posts in MySQL and their vectors in Qdrant, kept in step.

- store_post_vector(): put a post's vector in Qdrant (payload from its MySQL row)
  and mark the post as embedded.
- delete_posts(): delete posts in MySQL and their points in Qdrant together. The
  MySQL delete is committed only after Qdrant confirmed, so a Qdrant failure
  leaves both untouched.
- remove_orphans(): delete Qdrant points whose post no longer exists (e.g. rows
  removed by hand or by a cascade). Safe to run any time.
- content_hash(): posts.content_hash, the sha256 of the text with its whitespace
  normalized, so exact copies are found whatever their line breaks.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Iterable, Sequence

import pymysql

from .vectors import Point, Qdrant

EMBEDDING_MODEL_ID = 1  # embedding_models row for embeddinggemma-300m (init.sql)


def content_hash(text: str | None) -> bytes | None:
    """sha256 (32 bytes) of the text with runs of whitespace collapsed; None for no text."""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).digest()


def _rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # MySQL times are UTC
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def post_payload(conn: pymysql.connections.Connection, post_id: int) -> tuple[str, dict]:
    """The post's point id and the filter fields Qdrant keeps with its vector."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.post_id, p.uuid, p.account_id, p.container_account_id, p.language, p.post_type,"
            " p.published_at, p.origin, pl.code AS platform,"
            " (SELECT pc.cluster_id FROM post_clusters pc JOIN clustering_runs r ON r.run_id = pc.run_id"
            "   WHERE pc.post_id = p.post_id AND r.status = 'done' ORDER BY r.finished_at DESC, r.run_id DESC LIMIT 1)"
            "   AS cluster_id,"
            " (SELECT pc.run_id FROM post_clusters pc JOIN clustering_runs r ON r.run_id = pc.run_id"
            "   WHERE pc.post_id = p.post_id AND r.status = 'done' ORDER BY r.finished_at DESC, r.run_id DESC LIMIT 1)"
            "   AS clustered_run"
            " FROM posts p JOIN platforms pl ON pl.platform_id = p.platform_id WHERE p.post_id = %s",
            (post_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"There is no post {post_id}.")
    payload = {
        "post_id": row["post_id"], "account_id": row["account_id"], "container_account_id": row["container_account_id"],
        "platform": row["platform"], "language": row["language"], "post_type": row["post_type"],
        "published_at": _rfc3339(row["published_at"]), "cluster_id": row["cluster_id"], "origin": row["origin"],
        "clustered_run": row["clustered_run"],  # posts without it are new to clustering (clustering.update_clustering)
    }
    return str(uuid.UUID(bytes=row["uuid"])), {k: v for k, v in payload.items() if v is not None}


def store_post_vector(conn: pymysql.connections.Connection, qdrant: Qdrant, post_id: int,
                      vector: Sequence[float], embedding_model_id: int = EMBEDDING_MODEL_ID) -> str:
    point_id, payload = post_payload(conn, post_id)
    qdrant.upsert("posts", [Point(point_id, vector, payload)])
    with conn.cursor() as cur:
        cur.execute("UPDATE posts SET embedding_model_id = %s, embedded_at = CURRENT_TIMESTAMP(3) WHERE post_id = %s",
                    (embedding_model_id, post_id))
    conn.commit()
    return point_id


def delete_posts(conn: pymysql.connections.Connection, qdrant: Qdrant, post_ids: Iterable[int]) -> int:
    """Delete posts and their vectors. Returns how many posts were deleted.
    Rows that cascade from the posts (metrics, claims links, cluster membership) go too."""
    ids = sorted({int(i) for i in post_ids})
    if not ids:
        return 0
    marks = ", ".join(["%s"] * len(ids))
    conn.begin()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT uuid FROM posts WHERE post_id IN ({marks}) FOR UPDATE", tuple(ids))
            uuids = [row["uuid"] for row in cur.fetchall()]
            cur.execute(f"DELETE FROM posts WHERE post_id IN ({marks})", tuple(ids))
            deleted = cur.rowcount
        qdrant.delete("posts", uuids)  # raises -> rollback below, nothing deleted anywhere
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return deleted


def remove_orphans(conn: pymysql.connections.Connection, qdrant: Qdrant, batch: int = 500) -> int:
    """Delete post points that have no post in MySQL. Returns how many were removed."""
    orphans: list[str] = []
    page: list[str] = []

    def check(ids: list[str]) -> None:
        marks = ", ".join(["%s"] * len(ids))
        with conn.cursor() as cur:
            cur.execute(f"SELECT uuid FROM posts WHERE uuid IN ({marks})", tuple(uuid.UUID(i).bytes for i in ids))
            known = {str(uuid.UUID(bytes=row["uuid"])) for row in cur.fetchall()}
        orphans.extend(i for i in ids if i not in known)

    for point_id in qdrant.ids("posts", batch=batch):
        page.append(point_id)
        if len(page) >= batch:
            check(page)
            page = []
    if page:
        check(page)
    for start in range(0, len(orphans), batch):
        qdrant.delete("posts", orphans[start:start + batch])
    return len(orphans)
