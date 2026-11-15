"""Create the vector search collections in Qdrant:  uv run python backend/setup_vectors.py

Creates what's missing from docker/qdrant/collections.json (collections, payload
indexes, aliases). Safe to run any number of times.

  --remove-orphans   also delete post vectors whose post no longer exists in MySQL
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veritas.config import Settings  # noqa: E402
from veritas.content import remove_orphans  # noqa: E402
from veritas.db import DatabaseUnavailable, connection  # noqa: E402
from veritas.vectors import Qdrant, VectorStoreError, VectorStoreUnavailable  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Create the Providence vector search collections.")
    parser.add_argument("--remove-orphans", action="store_true", help="delete vectors of posts that no longer exist")
    args = parser.parse_args(argv)
    try:
        settings = Settings.load()
        with Qdrant.from_settings(settings) as qdrant:
            changes = qdrant.setup()
            for change in changes:
                print(f"  {change}")
            print("Vector search is set up." if changes else "Vector search was already set up.")
            if args.remove_orphans:
                with connection(settings) as conn:
                    removed = remove_orphans(conn, qdrant)
                print(f"Removed {removed} vectors of deleted posts.")
    except (VectorStoreUnavailable, VectorStoreError, DatabaseUnavailable, ValueError) as exc:
        print(f"Vector search setup stopped. {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
