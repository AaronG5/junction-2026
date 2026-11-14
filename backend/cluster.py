"""Group recent posts into narratives:  uv run python backend/cluster.py [--days 7]

Needs the optional packages once:  uv sync --extra clustering
Uses the posts published in the last N days (default 7) whose vectors are in
Qdrant, and the local AI to name the groups. The Narratives screen then shows
the new run.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veritas.ai import AiError, AiUnavailable, LocalAI  # noqa: E402
from veritas.clustering import ClusteringUnavailable, run_clustering  # noqa: E402
from veritas.config import Settings  # noqa: E402
from veritas.db import DatabaseUnavailable, connection  # noqa: E402
from veritas.vectors import Qdrant, VectorStoreError, VectorStoreUnavailable  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Group recent posts into narratives.")
    parser.add_argument("--days", type=int, default=7, help="how many days back to look (default 7)")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 365:
        print("Clustering stopped. --days must be between 1 and 365.", file=sys.stderr)
        return 1
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.days)
    try:
        settings = Settings.load()
        with connection(settings) as conn, Qdrant.from_settings(settings) as qdrant, \
                LocalAI.from_settings(settings, timeout=300) as ai:
            result = run_clustering(conn, qdrant, ai, since=since, until=until)
    except (ClusteringUnavailable, DatabaseUnavailable, VectorStoreUnavailable, VectorStoreError,
            AiUnavailable, AiError, ValueError) as exc:
        print(f"Clustering stopped. {exc}", file=sys.stderr)
        return 1
    if result["status"] != "done":
        print(result["message"])
        return 0
    print(f"Run {result['run_id']}: {result['posts']} posts, {len(result['clusters'])} clusters, "
          f"{result['unclustered']} fit no cluster.")
    for c in result["clusters"]:
        same = "continues an earlier narrative" if c["same_narrative_as"] else "new"
        print(f"  {c['size']:>5}  {c['name']}  ({same})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
