"""Demo data: the frontend's example data (shared/demo/demo-data.json) as database rows.

Loaded only on request (`uv run python backend/demo_data.py`, later the launcher's
--demo-data flag). Never loaded automatically.

Safety: demo rows use fixed ids (claim 1, cluster 12, ...) so the screens show
exactly what the frontend's demo shows. So demo data only goes into a database
that has no real data. A database that holds demo data is marked in `settings`
(key "demo_data"); loading again replaces the demo data, and remove() empties it.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pymysql

from ..config import ROOT
from ..ids import NAMESPACE, account_uuid, post_uuid
from ..links import link_key

SEED = ROOT / "shared" / "demo" / "demo-data.json"
MARKER_KEY = "demo_data"

# Tables the demo writes, children first (so deleting in this order respects foreign keys).
CONTENT_TABLES = (
    "stories", "source_profiles", "claim_evidence", "post_claims", "post_clusters",
    "claims", "clusters", "clustering_runs", "posts", "accounts",
)
PLATFORM_CODES = {"Telegram": "telegram", "Facebook": "facebook", "Website": "web"}
# What the screens call a verdict / stance -> the database's value.
VERDICT_TO_DB = {"true": "true", "false": "false", "misleading": "misleading", "unverified": "unverified"}
STANCE_TO_DB = {"supports": "supports", "contradicts": "refutes", "context": "context", "neither": "unrelated"}
ACCOUNT_TYPES = {"channel": "channel", "group": "group", "page": "page", "news site": "website"}


class DemoDataError(Exception):
    """Demo data can't be loaded here. The message is shown to users as is."""


def read_seed(path: Path = SEED) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DemoDataError(f"The demo data file is missing: {path}. Run `npm run export:fixtures` in frontend/.") from None


def _scalar(cur, sql: str, args: tuple = ()) -> Any:
    cur.execute(sql, args)
    row = cur.fetchone()
    return next(iter(row.values())) if row else None


def is_loaded(conn: pymysql.connections.Connection) -> bool:
    with conn.cursor() as cur:
        return bool(_scalar(cur, "SELECT COUNT(*) FROM settings WHERE setting_key = %s", (MARKER_KEY,)))


def _has_content(cur) -> bool:
    return any(_scalar(cur, f"SELECT EXISTS (SELECT 1 FROM `{table}`)") for table in CONTENT_TABLES)


def _empty_content(cur) -> None:
    cur.execute("UPDATE clusters SET main_claim_id = NULL")  # clusters <-> claims point at each other
    for table in CONTENT_TABLES:
        cur.execute(f"DELETE FROM `{table}`")


def remove(conn: pymysql.connections.Connection) -> bool:
    """Delete the demo data. Returns False when this database holds no demo data
    (then nothing is touched)."""
    with conn.cursor() as cur:
        if not is_loaded(conn):
            return False
        _empty_content(cur)
        cur.execute("DELETE FROM settings WHERE setting_key = %s", (MARKER_KEY,))
    conn.commit()
    return True


_DAY = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})(T.*)?$")


def shift_dates(value: Any, days: int) -> Any:
    """Every date in the demo data moved by `days` (days and ISO moments alike)."""
    if isinstance(value, dict):
        return {k: shift_dates(v, days) for k, v in value.items()}
    if isinstance(value, list):
        return [shift_dates(v, days) for v in value]
    if isinstance(value, str) and days:
        m = _DAY.match(value)
        if m:
            return (date.fromisoformat(m.group(1)) + timedelta(days=days)).isoformat() + (m.group(2) or "")
    return value


def load(conn: pymysql.connections.Connection, seed: dict | None = None, today: str | None = None) -> dict[str, int]:
    """Replace the database's content with the demo data. Returns row counts per kind.
    today: move every date so the demo's "today" is this day (the demo's dates are
    relative, e.g. "seen 3 days ago"); None keeps them as exported."""
    seed = seed if seed is not None else read_seed()
    if today is not None:
        seed = shift_dates(seed, (date.fromisoformat(today) - date.fromisoformat(seed["today"])).days)
    conn.begin()
    try:
        with conn.cursor() as cur:
            if not is_loaded(conn) and _has_content(cur):
                raise DemoDataError(
                    "This database already holds real data, so the demo data wasn't loaded. "
                    "Use an empty database for the demo."
                )
            _empty_content(cur)
            counts = _insert(cur, seed)
            cur.execute(
                "INSERT INTO settings (setting_key, value) VALUES (%s, %s)"
                " ON DUPLICATE KEY UPDATE value = VALUES(value)",
                (MARKER_KEY, json.dumps({"today": seed.get("today"), "source": "shared/demo/demo-data.json"})),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return counts


def _platform_ids(cur) -> dict[str, int]:
    cur.execute("SELECT platform_id, code FROM platforms")
    by_code = {row["code"]: row["platform_id"] for row in cur.fetchall()}
    return {name: by_code[code] for name, code in PLATFORM_CODES.items()}


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _insert(cur, seed: dict) -> dict[str, int]:
    platform_ids = _platform_ids(cur)
    run = seed["run"]

    # Claims first (clusters point at their main claim), cluster links added after.
    for c in seed["claims"]:
        cur.execute(
            "INSERT INTO claims (claim_id, uuid, text, language, verdict, verdict_confidence, verdict_explanation,"
            " verdict_method, verdict_at, first_seen, last_seen, post_count, platforms, languages)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (c["id"], uuid.uuid5(NAMESPACE, f"demo:claim:{c['id']}").bytes, c["text"], "en", VERDICT_TO_DB[c["verdict"]],
             c["confidence"], c["explanation"], c["checkedBy"], c["checkedAt"], c["firstSeen"], c["lastSeen"],
             c["posts"], _j(c["platforms"]), _j(c["languages"])),
        )
        for ev in c["evidence"]:
            cur.execute(
                "INSERT INTO claim_evidence (claim_id, url, publisher, stance, excerpt, method) VALUES (%s, %s, %s, %s, %s, 'demo')",
                (c["id"], ev.get("url"), ev["source"], STANCE_TO_DB[ev["stance"]], ev["note"]),
            )

    cur.execute(
        "INSERT INTO clustering_runs (embedding_model_id, algorithm, post_count, cluster_count, unclustered_count,"
        " status, started_at, finished_at) VALUES (1, 'demo', %s, %s, %s, 'done', %s, %s)",
        (run["posts"], run["clusters"], run["unclustered"], run["date"] + " 12:00:00", run["date"] + " 12:00:00"),
    )
    run_id = cur.lastrowid

    claims_by_cluster: dict[int, list[dict]] = {}
    for c in seed["claims"]:
        if c["clusterId"] is not None:
            claims_by_cluster.setdefault(c["clusterId"], []).append(c)
    for cl in seed["clusters"]:
        main = next((c for c in claims_by_cluster.get(cl["id"], []) if c["verdict"] == cl["status"]), None)
        if main is None and cl["status"] != "unverified":
            raise DemoDataError(f"Demo cluster {cl['id']} is {cl['status']!r} but none of its claims is.")
        stats = {k: cl[k] for k in ("platforms", "languages", "typical", "edge")}
        cur.execute(
            "INSERT INTO clusters (cluster_id, run_id, label, size, stats, change_pct, accounts, first_seen, last_seen,"
            " main_claim_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cl["id"], run_id, cl["name"], cl["size"], _j(stats), cl["changePct"], cl["accounts"],
             cl["firstSeen"], cl["lastSeen"], main["id"] if main else None),
        )
    for c in seed["claims"]:
        if c["clusterId"] is not None:
            cur.execute("UPDATE claims SET cluster_id = %s WHERE claim_id = %s", (c["clusterId"], c["id"]))

    # Map points: one placeholder post each (no text), in the map's order.
    rank: dict[int | None, int] = {}
    for i, p in enumerate(seed["points"]):
        external = f"demo-map/{i}"
        cur.execute(
            "INSERT INTO posts (uuid, platform_id, external_id, is_stub) VALUES (%s, %s, %s, TRUE)",
            (post_uuid("telegram", external).bytes, platform_ids["Telegram"], external),
        )
        cid = p["clusterId"]
        position = None
        if cid is not None:
            position = rank[cid] = rank.get(cid, 0) + 1
        cur.execute(
            "INSERT INTO post_clusters (post_id, run_id, cluster_id, rank_in_cluster, map_x, map_y)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (cur.lastrowid, run_id, cid, position, p["x"], p["y"]),
        )

    reports = seed.get("channelReports", {})
    for s in seed["sources"]:
        code = PLATFORM_CODES[s["platform"]]
        kind = s["kind"].split(" · ")[0].lower()
        handle = s["name"][1:] if s["name"].startswith("@") else None
        cur.execute(
            "INSERT INTO accounts (uuid, platform_id, external_id, handle, display_name, account_type)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (account_uuid(code, s["id"]).bytes, platform_ids[s["platform"]], s["id"], handle, s["name"],
             ACCOUNT_TYPES.get(kind, "unknown")),
        )
        report = reports.get(s["id"])
        if report is not None:
            report = {k: v for k, v in report.items() if k not in ("id", "name", "platform")}
        cur.execute(
            "INSERT INTO source_profiles (account_id, source_key, kind_label, posts_30d, original_pct, near_copy_pct,"
            " main_topic, open_flags, report) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cur.lastrowid, s["id"], s["kind"], s["posts30d"], s["originalPct"], s["nearCopyPct"], s["mainTopic"],
             s["openFlags"], _j(report) if report is not None else None),
        )

    for st in seed["stories"]:
        body = {k: v for k, v in st.items() if k not in ("id", "platform", "status", "topic", "firstSeen", "lastSeen", "trend")}
        url = st["post"].get("url")
        cur.execute(
            "INSERT INTO stories (story_key, platform_id, status, topic, first_seen, last_seen, trend_posts,"
            " trend_change_pct, post_link_key, body) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (st["id"], platform_ids[st["platform"]], st["status"], st["topic"], st["firstSeen"], st["lastSeen"],
             st["trend"]["posts"], st["trend"]["changePct"], link_key(url) if url else None, _j(body)),
        )

    return {"claims": len(seed["claims"]), "clusters": len(seed["clusters"]), "points": len(seed["points"]),
            "sources": len(seed["sources"]), "stories": len(seed["stories"])}
