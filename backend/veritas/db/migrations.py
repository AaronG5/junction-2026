"""Database migrations: bring any MySQL database up to the current schema.

  0001  docker/mysql/init.sql           the original schema
  0002+ docker/mysql/migrations/NNNN_name.sql, applied in number order

Applied versions are recorded in `schema_migrations`, so each file runs once.

MySQL commits schema changes (CREATE, ALTER, ...) immediately, so a failing
migration can't be rolled back. Instead the runner:
  - stops at the failing statement and reports the file and the statement,
  - does not record that migration as applied (later ones don't run either),
  - remembers that the migration was started (`schema_migration_attempts`).
When a started migration is run again (after the fix), statements that already
took effect are skipped: "table already exists" for CREATE TABLE, "column already
exists" for ALTER TABLE ... ADD, and so on. Each skip is reported. On a first
attempt nothing is skipped: every error stops the run, so typos can't hide.

Write one change per statement: a statement that does two things can't be told
apart from one that is half done.

Run it:  uv run python backend/migrate.py
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import pymysql

from ..config import ROOT

INIT_SQL = ROOT / "docker" / "mysql" / "init.sql"
MIGRATIONS_DIR = ROOT / "docker" / "mysql" / "migrations"
INITIAL_VERSION = "0001_initial"
LOCK_SECONDS = 30
LOCK_WAIT_TIMEOUT = 30  # seconds a schema change waits for tables in use before giving up

_ALTER_ADD = r"ALTER\s+TABLE\s+\S+\s+ADD\b"
_ALTER_ADD_CONSTRAINT = r"ALTER\s+TABLE\s+\S+\s+ADD\s+CONSTRAINT\b"
_ALTER_DROP = r"ALTER\s+TABLE\s+\S+\s+DROP\b"

# On a repeated attempt only: MySQL error -> (statement kinds it may be skipped for, reason).
ALREADY_DONE: dict[int, tuple[re.Pattern[str], str]] = {
    code: (re.compile(kinds, re.IGNORECASE), reason)
    for code, kinds, reason in [
        (1050, r"CREATE\s+TABLE\b", "table already exists"),
        (1060, _ALTER_ADD, "column already exists"),
        (1061, rf"{_ALTER_ADD}|CREATE\s+(UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?INDEX\b", "index already exists"),
        (1826, _ALTER_ADD_CONSTRAINT, "foreign key already exists"),
        (3822, _ALTER_ADD_CONSTRAINT, "check already exists"),
        (1091, rf"{_ALTER_DROP}|DROP\s+INDEX\b", "already dropped"),
    ]
}

# version = 4 digits + "_" + name, and must fit schema_migrations.version VARCHAR(32)
_FILE_NAME = re.compile(r"^(\d{4})_[a-z0-9_]{1,27}\.sql$")
_DELIMITER = re.compile(r"^\s*DELIMITER\b", re.IGNORECASE | re.MULTILINE)
_USE = re.compile(r"USE\b", re.IGNORECASE)


@dataclass(frozen=True)
class Migration:
    version: str  # e.g. "0002_screens"
    path: Path

    @property
    def number(self) -> int:
        return int(self.version[:4])


class MigrationError(Exception):
    """A migration failed. The message names the file and the statement."""


def split_sql(sql: str) -> list[str]:
    """Split a SQL script into statements on `;`, ignoring semicolons inside
    quotes ('...', "...", `...`) and comments (-- ..., # ..., /* ... */).
    Comments are dropped, except /*! ... */ and /*+ ... */, which MySQL runs
    (they are kept as written). Empty statements are skipped."""
    if _DELIMITER.search(sql):
        raise MigrationError("DELIMITER isn't supported here (it's a mysql command-line feature). Use plain statements.")
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote != "`" and i + 1 < n:  # backslash escape inside strings
                buf.append(sql[i + 1])
                i += 2
                continue
            if ch == quote:  # a doubled quote ('it''s') closes and reopens: same split
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == "-" and sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\r\n"):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
            continue
        elif ch == "#":
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl
            continue
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise MigrationError("A /* comment */ is never closed.")
            if sql.startswith(("/*!", "/*+"), i):
                buf.append(sql[i:end + 2])  # executable comment: MySQL runs it, so keep it
            else:
                buf.append(" ")
            i = end + 2
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    if quote:
        raise MigrationError(f"A {quote}quoted text{quote} is never closed.")
    stmt = "".join(buf).strip()
    if stmt:
        statements.append(stmt)
    return statements


def discover(migrations_dir: Path = MIGRATIONS_DIR, init_sql: Path = INIT_SQL) -> list[Migration]:
    """All migrations in order: init.sql as 0001, then the numbered files."""
    found = [Migration(INITIAL_VERSION, init_sql)]
    seen: dict[int, Path] = {1: init_sql}
    if migrations_dir.is_dir():
        for path in sorted(migrations_dir.iterdir()):
            if path.suffix.lower() != ".sql":
                continue
            m = _FILE_NAME.match(path.name)
            if not m:
                raise MigrationError(
                    f"{path.name}: migration files must be named like 0002_short_name.sql"
                    " (lowercase, at most 32 characters before .sql)"
                )
            number = int(m.group(1))
            if number in seen:
                raise MigrationError(f"{path.name}: number {number:04d} is already used by {seen[number].name}")
            seen[number] = path
            found.append(Migration(path.stem, path))
    return sorted(found, key=lambda mig: mig.number)


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
        (name,),
    )
    return bool(cur.fetchone()["n"])


def applied_versions(conn: pymysql.connections.Connection) -> set[str]:
    """Versions recorded as applied. Reads only; an empty database has none."""
    with conn.cursor() as cur:
        if not _table_exists(cur, "schema_migrations"):
            return set()
        cur.execute("SELECT version FROM schema_migrations")
        return {row["version"] for row in cur.fetchall()}


def _read(migration: Migration) -> list[str]:
    name = migration.path.name
    try:
        text = migration.path.read_text(encoding="utf-8-sig")  # tolerates a byte-order mark
    except UnicodeDecodeError:
        raise MigrationError(f"{name} isn't saved as UTF-8. Save it as UTF-8 and run again.") from None
    except OSError as exc:
        raise MigrationError(f"{name} can't be read: {exc.strerror or exc}") from None
    try:
        statements = split_sql(text)
    except MigrationError as exc:
        raise MigrationError(f"{name}: {exc}") from None
    for number, statement in enumerate(statements, 1):
        if _USE.match(statement):
            raise MigrationError(f"{name}, statement {number}: USE isn't allowed; migrations run in the configured database.")
    return statements


def _started_before(cur, version: str) -> bool:
    """True when an earlier attempt at this migration failed partway. Marks this attempt."""
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration_attempts ("
        " version VARCHAR(32) PRIMARY KEY,"
        " started_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3))"
    )
    cur.execute("SELECT COUNT(*) AS n FROM schema_migration_attempts WHERE version = %s", (version,))
    if cur.fetchone()["n"]:
        return True
    cur.execute("INSERT INTO schema_migration_attempts (version) VALUES (%s)", (version,))
    return False


def _preview(statement: str) -> str:
    return " ".join(statement.split())[:160]


def _run(conn: pymysql.connections.Connection, migration: Migration, on_skip: Callable[[str], None]) -> None:
    statements = _read(migration)
    name = migration.path.name
    with conn.cursor() as cur:
        retry = _started_before(cur, migration.version)
        conn.commit()
        for number, statement in enumerate(statements, 1):
            try:
                cur.execute(statement)
            except pymysql.MySQLError as exc:
                code = exc.args[0] if exc.args else None
                message = exc.args[1] if len(exc.args) > 1 else str(exc)
                known = ALREADY_DONE.get(code)
                if retry and known and known[0].match(statement):
                    on_skip(f"{name}, statement {number}: skipped ({known[1]}): {_preview(statement)}")
                    continue
                raise MigrationError(
                    f"{name}, statement {number} of {len(statements)} failed: {message}\n  {_preview(statement)}"
                ) from None
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version VARCHAR(32) PRIMARY KEY,"
            " applied_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3))"
        )
        # init.sql records itself, so the row may exist already
        cur.execute("INSERT INTO schema_migrations (version) VALUES (%s) ON DUPLICATE KEY UPDATE version = version",
                    (migration.version,))
        cur.execute("DELETE FROM schema_migration_attempts WHERE version = %s", (migration.version,))
    conn.commit()


def _lock_name(conn: pymysql.connections.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE() AS db")
        database = cur.fetchone()["db"]
    if not database:
        raise MigrationError("No database is selected. Set MYSQL_DATABASE in .env.")
    # MySQL lock names are limited to 64 characters, so use a hash of the database name
    return "veritas_migrate:" + hashlib.sha256(database.encode("utf-8")).hexdigest()[:32]


@contextmanager
def _migration_lock(conn: pymysql.connections.Connection) -> Iterator[None]:
    """Only one migration run per database at a time (a named MySQL lock)."""
    name = _lock_name(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, %s) AS got", (name, LOCK_SECONDS))
        if cur.fetchone()["got"] != 1:
            raise MigrationError("Another migration run is in progress on this database. Try again when it finishes.")
    try:
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
        except pymysql.MySQLError:
            pass  # connection gone: MySQL frees the lock itself; keep the original error


def migrate(conn: pymysql.connections.Connection, migrations: list[Migration] | None = None,
            on_skip: Callable[[str], None] = lambda _note: None,
            on_applied: Callable[[str], None] = lambda _version: None) -> list[str]:
    """Apply every migration not applied yet, in order. Returns the versions applied now.
    Stops at the first failure (MigrationError); later migrations don't run.
    on_skip is told about each statement skipped on a repeated attempt;
    on_applied about each migration as soon as it is recorded."""
    migrations = migrations if migrations is not None else discover()
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION lock_wait_timeout = %s", (LOCK_WAIT_TIMEOUT,))
        with _migration_lock(conn):
            done = applied_versions(conn)
            applied: list[str] = []
            for migration in migrations:
                if migration.version in done:
                    continue
                _run(conn, migration, on_skip)
                applied.append(migration.version)
                on_applied(migration.version)
            return applied
    except pymysql.MySQLError as exc:
        message = exc.args[1] if len(exc.args) > 1 else str(exc)
        raise MigrationError(f"The database connection failed during the migration: {message}") from None


def unknown_versions(conn: pymysql.connections.Connection, migrations: list[Migration] | None = None) -> list[str]:
    """Versions recorded in the database that this code doesn't have (a newer install?)."""
    migrations = migrations if migrations is not None else discover()
    known = {m.version for m in migrations}
    return sorted(applied_versions(conn) - known)


def pending(conn: pymysql.connections.Connection, migrations: list[Migration] | None = None) -> list[str]:
    """Versions not applied yet. Changes nothing."""
    migrations = migrations if migrations is not None else discover()
    done = applied_versions(conn)
    return [m.version for m in migrations if m.version not in done]
