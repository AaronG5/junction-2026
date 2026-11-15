"""Load or remove the demo data:  uv run python backend/demo_data.py

  (no option)   load the example data (dates moved so the demo's "today" is today)
  --remove      delete it again
  --as-exported keep the dates exactly as in shared/demo/demo-data.json

Demo data only goes into an empty database (or one that holds demo data already).
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veritas.config import Settings  # noqa: E402
from veritas.db import DatabaseUnavailable, connection  # noqa: E402
from veritas.db import demo  # noqa: E402
from veritas.db.migrations import MigrationError, pending  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Load or remove the Providence demo data.")
    parser.add_argument("--remove", action="store_true", help="delete the demo data")
    parser.add_argument("--as-exported", action="store_true", help="don't move the dates to today")
    args = parser.parse_args(argv)
    try:
        settings = Settings.load()
        with connection(settings) as conn:
            todo = pending(conn)
            if todo:
                print("The database schema isn't up to date. Run first:  uv run python backend/migrate.py", file=sys.stderr)
                return 1
            if args.remove:
                print("Demo data removed." if demo.remove(conn) else "There was no demo data to remove.")
                return 0
            today = None if args.as_exported else datetime.now(timezone.utc).date().isoformat()
            counts = demo.load(conn, today=today)
    except (DatabaseUnavailable, MigrationError, demo.DemoDataError, ValueError) as exc:
        print(f"Demo data not changed. {exc}", file=sys.stderr)
        return 1
    print("Demo data loaded: " + ", ".join(f"{n} {kind}" for kind, n in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
