"""Group posts into narratives:  uv run python backend/cluster.py [--rebuild [--days N]] [--no-read]

Needs the optional packages once:  uv sync --extra clustering

  (no option)  add the posts that aren't clustered yet to the clusters, without recomputing
               the rest (the dashboard's "Update clusters" button does the same). The very
               first run groups every post.
  --rebuild    group everything again from scratch: every post, or the last N days (--days).
  --no-read    skip reading the typical posts (highlights and claims, the slow Gemma part).

Uses the posts whose vectors are in Qdrant, and the local AI to name the groups and read their
typical posts. The Narratives and Claims screens show the result.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veritas.ai import AiError, AiUnavailable, LocalAI  # noqa: E402
from veritas.clustering import ClusteringUnavailable  # noqa: E402
from veritas.clustering_job import cluster_and_read, summary  # noqa: E402
from veritas.config import Settings  # noqa: E402
from veritas.db import DatabaseUnavailable, connection  # noqa: E402
from veritas.vectors import Qdrant, VectorStoreError, VectorStoreUnavailable  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Group posts into narratives.")
    parser.add_argument("--rebuild", action="store_true", help="group everything again from scratch")
    parser.add_argument("--days", type=int, default=None, help="with --rebuild: only the last N days (default: every post)")
    parser.add_argument("--no-read", action="store_true", help="don't read the typical posts (highlights, claims)")
    args = parser.parse_args(argv)
    if args.days is not None and not args.rebuild:
        print("Clustering stopped. --days only applies with --rebuild; an update always adds every new post.", file=sys.stderr)
        return 1
    if args.days is not None and not 1 <= args.days <= 365:
        print("Clustering stopped. --days must be between 1 and 365.", file=sys.stderr)
        return 1
    try:
        settings = Settings.load()
        with connection(settings, read_timeout=3600) as conn, Qdrant.from_settings(settings) as qdrant, \
                LocalAI.from_settings(settings, timeout=300) as ai:
            result = cluster_and_read(conn, qdrant, ai, mode="rebuild" if args.rebuild else "update", days=args.days,
                                      say=lambda message: print(f"  {message}", flush=True), read=not args.no_read)
    except (ClusteringUnavailable, DatabaseUnavailable, VectorStoreUnavailable, VectorStoreError,
            AiUnavailable, AiError, ValueError) as exc:
        print(f"Clustering stopped. {exc}", file=sys.stderr)
        return 1
    status, message = summary(result)
    print(message if status != "done" else f"Run {result['run_id']}: {message}")
    for c in result.get("clusters", []):
        if "name" in c:
            same = "continues an earlier narrative" if c.get("same_narrative_as") else "new"
            print(f"  {c['size']:>5}  {c['name']}  ({same})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
