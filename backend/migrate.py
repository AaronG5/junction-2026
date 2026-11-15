"""Bring the database schema up to date:  uv run python backend/migrate.py

  (no option)   apply every migration not applied yet
  --status      list what is applied and what is pending; changes nothing

Uses the database and login from .env. Safe to run any number of times.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veritas.config import Settings  # noqa: E402
from veritas.db import DatabaseUnavailable, connection  # noqa: E402
from veritas.db.migrations import MigrationError, discover, migrate, pending, unknown_versions  # noqa: E402

# Schema changes on big tables can take a while; don't give up after a minute.
READ_TIMEOUT = 3600


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # printing Lithuanian text to a cp1252 pipe must not crash a migration halfway
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Bring the Providence database schema up to date.")
    parser.add_argument("--status", action="store_true", help="only show applied and pending migrations")
    args = parser.parse_args(argv)
    try:
        settings = Settings.load()
        migrations = discover()
        with connection(settings, read_timeout=READ_TIMEOUT) as conn:
            if args.status:
                todo = set(pending(conn, migrations))
                for mig in migrations:
                    print(f"  {'pending' if mig.version in todo else 'applied'}  {mig.version}")
                for version in unknown_versions(conn, migrations):
                    print(f"  applied  {version}  (not known to this version of Providence)")
                return 0
            applied = migrate(conn, migrations,
                              on_skip=lambda note: print(f"  note: {note}", flush=True),
                              on_applied=lambda version: print(f"  applied {version}", flush=True))
    except (DatabaseUnavailable, MigrationError, ValueError) as exc:
        print(f"Migration stopped. {exc}", file=sys.stderr)
        return 1
    print("Done." if applied else "The database is up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
