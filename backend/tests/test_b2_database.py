"""B2: database connection and migrations.

Plain tests cover the SQL splitter, migration discovery and connection errors.
Integration tests each get a throwaway database (veritas_test_<random>) made
with the root login from .env; it is always dropped afterwards. They are skipped
when MySQL isn't running. Your own veritas_lens database is never changed.
"""
from __future__ import annotations

import dataclasses
import io
import os
import re
import secrets
import socket
import sys
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import pymysql
import pytest

import migrate as migrate_cli
from veritas.config import ROOT, Settings, read_env_file
from veritas.db import DatabaseUnavailable, connect, connection
from veritas.db import migrations as mig
from veritas.db.migrations import Migration, MigrationError, discover, migrate, pending, split_sql

SECRET = "pw-must-never-show-9f3k"
REAL_VERSIONS = [m.version for m in discover()]


def _norm(statement: str) -> str:
    return " ".join(statement.split())


# --------------------------------------------------------------------------- splitter

class TestSplitSql:
    def test_plain_statements(self):
        assert split_sql("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]

    def test_last_statement_without_semicolon(self):
        assert split_sql("SELECT 1;\nSELECT 2") == ["SELECT 1", "SELECT 2"]

    def test_empty_statements_skipped(self):
        assert split_sql(";;  ;\n SELECT 1 ;;") == ["SELECT 1"]
        assert split_sql("") == []
        assert split_sql("-- only a comment; really\n") == []

    @pytest.mark.parametrize("comment", [
        "-- a; b\n",
        "--\ta; b\n",
        "# a; b\n",
        "/* a; b */",
        "/* multi\nline; comment */",
    ])
    def test_comments_removed_and_their_semicolons_ignored(self, comment):
        statements = split_sql(f"SELECT 1 {comment} + 2; SELECT 3;")
        assert [_norm(s) for s in statements] == ["SELECT 1 + 2", "SELECT 3"]

    def test_comment_at_end_of_file_without_newline(self):
        assert split_sql("SELECT 1; -- trailing; comment") == ["SELECT 1"]

    def test_double_dash_without_space_is_not_a_comment(self):
        # MySQL needs whitespace after --, so 1--1 is "1 minus minus 1"
        assert split_sql("SELECT 1--1; SELECT 2") == ["SELECT 1--1", "SELECT 2"]

    @pytest.mark.parametrize("literal", [
        "'a;b'", '"a;b"', "`a;b`",
        "'it''s; fine'", "'back\\'slash; ok'", '"dq\\"x;y"', "'ends with backslash \\\\'",
        "'-- not a comment;'", "'# not a comment;'", "'/* not; a comment */'",
    ])
    def test_semicolons_and_comment_marks_in_quotes_kept(self, literal):
        assert split_sql(f"SELECT {literal}; SELECT 2") == [f"SELECT {literal}", "SELECT 2"]

    def test_backtick_has_no_backslash_escape(self):
        assert split_sql("SELECT `a\\`; SELECT 2") == ["SELECT `a\\`", "SELECT 2"]

    @pytest.mark.parametrize("kept", ["/*!80000 + 1 */", "/*+ NO_INDEX(t) */", "/*!50100 a; b */"])
    def test_executable_comments_kept_as_written(self, kept):
        assert split_sql(f"SELECT 1 {kept}; SELECT 2") == [f"SELECT 1 {kept}", "SELECT 2"]

    @pytest.mark.parametrize("bad", ["SELECT 'open; SELECT 2", 'SELECT "x', "SELECT `x"])
    def test_unclosed_quote_is_an_error(self, bad):
        with pytest.raises(MigrationError, match="never closed"):
            split_sql(bad)

    def test_unclosed_block_comment_is_an_error(self):
        with pytest.raises(MigrationError, match="never closed"):
            split_sql("SELECT 1; /* forgot")

    def test_delimiter_is_refused_plainly(self):
        with pytest.raises(MigrationError, match="DELIMITER isn't supported"):
            split_sql("DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END //\nDELIMITER ;")

    def test_crlf_line_endings(self):
        assert split_sql("SELECT 1; -- note; x\r\nSELECT 2;\r\n") == ["SELECT 1", "SELECT 2"]

    def test_init_sql_splits_into_its_statements(self):
        text = mig.INIT_SQL.read_text(encoding="utf-8")
        statements = split_sql(text)
        # one statement per line that starts one, at column 0
        starts = re.findall(r"^(?:SET|CREATE|INSERT)\b", text, re.MULTILINE)
        assert len(statements) == len(starts) > 25
        for statement in statements:
            assert re.match(r"(SET|CREATE|INSERT)\b", statement), statement[:60]
            assert "-- " not in statement  # comments removed, none swallowed a statement
        tables = [s for s in statements if s.startswith("CREATE TABLE")]
        assert len(tables) == len(re.findall(r"^CREATE TABLE", text, re.MULTILINE))

    @pytest.mark.parametrize("migration", discover()[1:], ids=lambda m: m.version)
    def test_migrations_make_one_change_per_statement(self, migration):
        for statement in mig._read(migration):
            if statement.upper().startswith("ALTER TABLE"):
                # one ADD/DROP/MODIFY per ALTER, so a half-applied run can be repeated
                assert len(re.findall(r"\b(ADD|DROP|MODIFY|CHANGE|RENAME)\b", statement, re.IGNORECASE)) == 1, statement

    def test_0002_statements(self):
        assert len(mig._read(discover()[1])) == 9


# --------------------------------------------------------------------------- reading files

def _write(folder: Path, name: str, sql: str | bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if isinstance(sql, bytes):
        path.write_bytes(sql)
    else:
        path.write_text(sql, encoding="utf-8")
    return path


class TestRead:
    def test_byte_order_mark_is_ignored(self, tmp_path):
        path = _write(tmp_path, "0002_bom.sql", (chr(0xFEFF) + "SELECT 1;").encode("utf-8"))
        assert mig._read(Migration("0002_bom", path)) == ["SELECT 1"]

    def test_not_utf8_is_a_plain_error(self, tmp_path):
        path = _write(tmp_path, "0002_cp.sql", b"SELECT '" + bytes([0xE0]) + b"';")
        with pytest.raises(MigrationError, match="0002_cp.sql isn't saved as UTF-8"):
            mig._read(Migration("0002_cp", path))

    def test_missing_file_is_a_plain_error(self, tmp_path):
        with pytest.raises(MigrationError, match="can't be read"):
            mig._read(Migration("0002_gone", tmp_path / "0002_gone.sql"))

    def test_splitter_errors_name_the_file(self, tmp_path):
        path = _write(tmp_path, "0002_open.sql", "SELECT 'open;")
        with pytest.raises(MigrationError, match="^0002_open.sql: A 'quoted text' is never closed"):
            mig._read(Migration("0002_open", path))

    @pytest.mark.parametrize("use", ["USE mysql", "use other_db", "USE `x`"])
    def test_use_statement_is_refused(self, tmp_path, use):
        path = _write(tmp_path, "0002_use.sql", f"SELECT 1; {use}; SELECT 2;")
        with pytest.raises(MigrationError, match="statement 2: USE isn't allowed"):
            mig._read(Migration("0002_use", path))

    def test_user_column_is_not_a_use_statement(self, tmp_path):
        path = _write(tmp_path, "0002_ok.sql", "SELECT 1 AS used; UPDATE users SET x = 1;")
        assert len(mig._read(Migration("0002_ok", path))) == 2


# --------------------------------------------------------------------------- discovery

class TestDiscover:
    def test_real_migrations(self):
        found = discover()
        assert REAL_VERSIONS[:3] == ["0001_initial", "0002_screens", "0003_value_checks"]
        assert found[0].path == mig.INIT_SQL
        assert [m.number for m in found] == list(range(1, len(found) + 1))
        assert all(m.path.is_file() for m in found)

    def test_order_is_numeric_and_other_files_ignored(self, tmp_path):
        for name in ["0010_ten.sql", "0003_three.sql", "README.md", "0002_two.sql", "0004_x.sql.txt"]:
            _write(tmp_path, name, "SELECT 1;")
        assert [m.version for m in discover(tmp_path)] == ["0001_initial", "0002_two", "0003_three", "0010_ten"]

    def test_missing_folder_means_only_init(self, tmp_path):
        assert [m.version for m in discover(tmp_path / "nope")] == ["0001_initial"]

    @pytest.mark.parametrize("name", [
        "2_short.sql", "0002-dash.sql", "0002_Upper.sql", "0002.sql", "00002_x.sql", "0002_x.SQL",
        "0002_" + "a" * 28 + ".sql",  # version would be 33 characters: doesn't fit VARCHAR(32)
    ])
    def test_badly_named_file_is_an_error(self, tmp_path, name):
        _write(tmp_path, name, "SELECT 1;")
        with pytest.raises(MigrationError, match="must be named like"):
            discover(tmp_path)

    def test_longest_allowed_name_fits(self, tmp_path):
        _write(tmp_path, "0002_" + "a" * 27 + ".sql", "SELECT 1;")
        assert len(discover(tmp_path)[1].version) == 32

    def test_duplicate_number_is_an_error(self, tmp_path):
        _write(tmp_path, "0002_a.sql", "SELECT 1;")
        _write(tmp_path, "0002_b.sql", "SELECT 1;")
        with pytest.raises(MigrationError, match="already used"):
            discover(tmp_path)

    def test_number_0001_is_reserved_for_init_sql(self, tmp_path):
        _write(tmp_path, "0001_again.sql", "SELECT 1;")
        with pytest.raises(MigrationError, match="already used by init.sql"):
            discover(tmp_path)


# --------------------------------------------------------------------------- connection errors (no MySQL needed)

def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # closed again when the block ends


@pytest.mark.parametrize("password", [SECRET, "slaptažodis-" + SECRET])
def test_unreachable_database_message_is_plain_and_has_no_password(password):
    settings = Settings(mysql_port=_closed_port(), mysql_password=password)
    with pytest.raises(DatabaseUnavailable) as info:
        connect(settings)
    text = str(info.value)
    assert "isn't reachable" in text and str(settings.mysql_port) in text
    assert SECRET not in text and SECRET not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


@pytest.mark.parametrize("code, expected", [
    (1045, "refused the login"),
    (1044, "doesn't exist or this login can't use it"),
    (1049, "doesn't exist or this login can't use it"),
    (2003, "isn't reachable"),
    (2013, "isn't reachable"),
    (1040, "MySQL is running at 127.0.0.1:3306 but refused the connection (MySQL error 1040)"),
    (1130, "refused the connection (MySQL error 1130)"),
])
def test_error_codes_get_the_right_message(monkeypatch, code, expected):
    def fail(**kwargs):
        raise pymysql.err.OperationalError(code, f"server text with {SECRET}")
    monkeypatch.setattr(pymysql, "connect", fail)
    with pytest.raises(DatabaseUnavailable) as info:
        connect(Settings(mysql_password=SECRET))
    assert expected in str(info.value)
    assert SECRET not in str(info.value)


def test_unexpected_connect_error_becomes_plain(monkeypatch):
    def fail(**kwargs):
        raise UnicodeEncodeError("latin-1", SECRET, 0, 1, "boom")
    monkeypatch.setattr(pymysql, "connect", fail)
    with pytest.raises(DatabaseUnavailable) as info:
        connect(Settings(mysql_password=SECRET))
    assert SECRET not in repr(info.value) and info.value.__suppress_context__


def test_password_is_sent_as_utf8(monkeypatch):
    seen = {}
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: seen.update(kwargs))
    connect(Settings(mysql_password="ąčę"))
    assert seen["password"] == "ąčę".encode("utf-8")
    assert seen["charset"] == "utf8mb4" and "+00:00" in seen["init_command"]


def test_cli_reports_unreachable_database(monkeypatch, capsys):
    settings = Settings(mysql_port=_closed_port(), mysql_password=SECRET)
    monkeypatch.setattr(migrate_cli.Settings, "load", classmethod(lambda cls, *a, **k: settings))
    assert migrate_cli.main([]) == 1
    out = capsys.readouterr()
    assert "Migration stopped. The database isn't reachable" in out.err
    assert SECRET not in out.err + out.out
    assert "Traceback" not in out.err


def test_cli_reports_bad_settings(monkeypatch, capsys):
    monkeypatch.setenv("MYSQL_PORT", "abc")
    assert migrate_cli.main([]) == 1
    assert "Migration stopped. MYSQL_PORT must be a number" in capsys.readouterr().err


# --------------------------------------------------------------------------- integration

def _root_settings() -> Settings:
    env = read_env_file(ROOT / ".env") if (ROOT / ".env").is_file() else {}
    root_password = os.environ.get("MYSQL_ROOT_PASSWORD") or env.get("MYSQL_ROOT_PASSWORD", "")
    return dataclasses.replace(Settings.load(), mysql_user="root", mysql_password=root_password, mysql_database="")


@contextmanager
def _admin() -> Iterator[pymysql.connections.Connection]:
    try:
        conn = connect(_root_settings(), database="")
    except DatabaseUnavailable as exc:
        pytest.skip(f"MySQL not available for integration tests: {exc}")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _scratch(name: str | None = None) -> Iterator[Settings]:
    """A brand-new empty database, dropped afterwards whatever happens."""
    name = name or f"veritas_test_{secrets.token_hex(6)}"
    with _admin() as admin:
        try:
            with admin.cursor() as cur:
                cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            yield dataclasses.replace(_root_settings(), mysql_database=name)
        finally:
            with admin.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{name}`")


@pytest.fixture
def scratch_db() -> Iterator[Settings]:
    with _scratch() as settings:
        yield settings


def _tables(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name AS t FROM information_schema.tables WHERE table_schema = DATABASE()")
        return {row["t"] for row in cur.fetchall()}


def _versions(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        return [row["version"] for row in cur.fetchall()]


def _attempts(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migration_attempts ORDER BY version")
        return [row["version"] for row in cur.fetchall()]


def _column(conn, table: str, column: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_type AS type, is_nullable AS nullable, column_default AS dflt"
            " FROM information_schema.columns"
            " WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
            (table, column),
        )
        return cur.fetchone()


def _lock_free(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT IS_FREE_LOCK(%s) AS free", (mig._lock_name(conn),))
        return cur.fetchone()["free"] == 1


def _upto(version: str) -> list[Migration]:
    return [m for m in discover() if m.version <= version]


def _real_plus(tmp_path: Path, extra: dict[str, str]) -> list[Migration]:
    """The real migrations plus extra files (numbered 0090+), in a temporary folder."""
    folder = tmp_path / "migrations"
    for path in mig.MIGRATIONS_DIR.glob("*.sql"):
        _write(folder, path.name, path.read_text(encoding="utf-8"))
    for name, sql in extra.items():
        _write(folder, name, sql)
    return discover(folder)


def _mark_started(conn, version: str) -> None:
    """As if an earlier attempt at `version` died partway."""
    with conn.cursor() as cur:
        mig._started_before(cur, version)


EXPECTED_TABLES = set(re.findall(r"^CREATE TABLE IF NOT EXISTS (\w+)", mig.INIT_SQL.read_text(encoding="utf-8"),
                                 re.MULTILINE)) | {"settings", "schema_migration_attempts"}


@pytest.mark.integration
class TestMigrateIntegration:
    def test_empty_database_gets_everything_and_rerun_changes_nothing(self, scratch_db):
        with connection(scratch_db) as conn:
            assert pending(conn) == REAL_VERSIONS
            assert _tables(conn) == set()  # pending() only reads
            assert migrate(conn) == REAL_VERSIONS
            assert _tables(conn) == EXPECTED_TABLES
            assert _versions(conn) == REAL_VERSIONS
            assert _attempts(conn) == []  # finished migrations leave no attempt behind
            notes: list[str] = []
            assert migrate(conn, on_skip=notes.append) == []
            assert notes == []
            assert pending(conn) == []

    def test_existing_install_gets_only_new_migrations_and_keeps_rows(self, scratch_db):
        with connection(scratch_db) as conn:
            assert migrate(conn, _upto("0001_initial")) == ["0001_initial"]
            with conn.cursor() as cur:
                cur.execute("INSERT INTO claims (uuid, text) VALUES (%s, %s)", (uuid.uuid4().bytes, "Kept claim"))
                cur.execute("INSERT INTO clustering_runs (embedding_model_id, algorithm) VALUES (1, 'test')")
                run_id = cur.lastrowid
                cur.execute("INSERT INTO clusters (run_id, label, size) VALUES (%s, 'Kept cluster', 3)", (run_id,))
            assert pending(conn) == REAL_VERSIONS[1:]
            assert migrate(conn) == REAL_VERSIONS[1:]
            with conn.cursor() as cur:
                cur.execute("SELECT text FROM claims")
                assert [r["text"] for r in cur.fetchall()] == ["Kept claim"]
                cur.execute("SELECT label, size, accounts, change_pct, main_claim_id, first_seen FROM clusters")
                assert cur.fetchall() == [{"label": "Kept cluster", "size": 3, "accounts": 0,
                                           "change_pct": None, "main_claim_id": None, "first_seen": None}]
                cur.execute("SELECT COUNT(*) AS n FROM platforms")
                assert cur.fetchone()["n"] == 8  # seed data intact

    def test_new_columns_and_table(self, scratch_db):
        with connection(scratch_db) as conn:
            migrate(conn)
            assert _column(conn, "post_clusters", "map_x") == {"type": "decimal(5,2)", "nullable": "YES", "dflt": None}
            assert _column(conn, "post_clusters", "map_y") == {"type": "decimal(5,2)", "nullable": "YES", "dflt": None}
            assert _column(conn, "clusters", "main_claim_id")["type"] == "bigint unsigned"
            assert _column(conn, "clusters", "change_pct")["type"] == "decimal(12,2)"
            assert _column(conn, "clusters", "accounts") == {"type": "int unsigned", "nullable": "NO", "dflt": "0"}
            assert _column(conn, "clusters", "first_seen")["type"] == "date"
            assert _column(conn, "clusters", "last_seen")["type"] == "date"
            assert _column(conn, "settings", "setting_key")["type"] == "varchar(64)"
            assert _column(conn, "settings", "value")["type"] == "json"
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT delete_rule AS d, referenced_table_name AS t FROM information_schema.referential_constraints"
                    " WHERE constraint_schema = DATABASE() AND constraint_name = 'fk_clusters_main_claim'"
                )
                assert cur.fetchone() == {"d": "SET NULL", "t": "claims"}
                cur.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.statistics"
                    " WHERE table_schema = DATABASE() AND table_name = 'clusters' AND column_name = 'main_claim_id'"
                )
                assert cur.fetchone()["n"] >= 1  # the foreign key is indexed
                cur.execute("SELECT constraint_name AS c FROM information_schema.check_constraints"
                            " WHERE constraint_schema = DATABASE() AND constraint_name LIKE 'chk\\_%%clusters%%' ORDER BY c")
                assert [r["c"] for r in cur.fetchall()] == [
                    "chk_clusters_seen_order", "chk_post_clusters_map_x", "chk_post_clusters_map_y"]

    def test_values_round_trip_and_checks(self, scratch_db):
        with connection(scratch_db) as conn:
            migrate(conn)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO claims (uuid, text) VALUES (%s, %s)", (uuid.uuid4().bytes, "Main claim"))
                claim_id = cur.lastrowid
                cur.execute("INSERT INTO clustering_runs (embedding_model_id, algorithm) VALUES (1, 'test')")
                run_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO clusters (run_id, label, size, main_claim_id, change_pct, accounts, first_seen, last_seen)"
                    " VALUES (%s, 'c', 2, %s, -12.5, 7, '2026-11-01', '2026-11-14')", (run_id, claim_id))
                cluster_id = cur.lastrowid
                cur.execute("INSERT INTO posts (uuid, platform_id, external_id) VALUES (%s, 2, 'p1')", (uuid.uuid4().bytes,))
                post_id = cur.lastrowid
                cur.execute("INSERT INTO post_clusters (post_id, run_id, cluster_id, map_x, map_y) VALUES (%s, %s, %s, 87.87, 0)",
                            (post_id, run_id, cluster_id))
                cur.execute("SELECT map_x, map_y FROM post_clusters")
                assert cur.fetchone() == {"map_x": Decimal("87.87"), "map_y": Decimal("0.00")}
                for bad in ["map_x = 100.01", "map_y = -0.01", "map_x = 150.5"]:  # off the map
                    with pytest.raises(pymysql.MySQLError) as info:
                        cur.execute(f"UPDATE post_clusters SET {bad}")
                    assert info.value.args[0] == 3819  # check constraint violated
                cur.execute("UPDATE post_clusters SET map_x = 100, map_y = NULL")  # edges and "not placed" are fine
                with pytest.raises(pymysql.MySQLError) as info:
                    cur.execute("UPDATE clusters SET first_seen = '2026-11-15'")  # after last_seen
                assert info.value.args[0] == 3819
                cur.execute("UPDATE clusters SET change_pct = 199900")  # huge growth from a tiny week fits
                cur.execute("INSERT INTO settings (setting_key, value) VALUES ('source', %s)", ('{"mode": "local"}',))
                cur.execute("SELECT JSON_EXTRACT(value, '$.mode') AS m FROM settings")
                assert cur.fetchone()["m"] == '"local"'
                cur.execute("DELETE FROM claims WHERE claim_id = %s", (claim_id,))
                cur.execute("SELECT main_claim_id, label FROM clusters")
                assert cur.fetchone() == {"main_claim_id": None, "label": "c"}  # cluster kept, link cleared

    def test_failing_migration_stops_is_named_and_not_recorded(self, scratch_db, tmp_path):
        bad = "CREATE TABLE first_half (id INT PRIMARY KEY);\nSELECT no_such_function();\n"
        migrations = _real_plus(tmp_path, {"0090_bad.sql": bad, "0091_later.sql": "CREATE TABLE later (id INT);"})
        with connection(scratch_db) as conn:
            with pytest.raises(MigrationError) as info:
                migrate(conn, migrations)
            text = str(info.value)
            assert "0090_bad.sql" in text and "statement 2 of 2" in text and "no_such_function" in text
            assert _versions(conn) == REAL_VERSIONS  # earlier ones are kept
            assert _attempts(conn) == ["0090_bad"]
            tables = _tables(conn)
            assert "later" not in tables      # later migrations don't run
            assert "first_half" in tables     # MySQL DDL can't be undone; the re-run copes with it
            assert _lock_free(conn)

            # fix the file and run again: the half-done part is skipped (and reported), the rest applies
            _write(tmp_path / "migrations", "0090_bad.sql",
                   "CREATE TABLE first_half (id INT PRIMARY KEY);\nCREATE TABLE second_half (id INT);\n")
            notes: list[str] = []
            assert migrate(conn, discover(tmp_path / "migrations"), on_skip=notes.append) == ["0090_bad", "0091_later"]
            assert len(notes) == 1 and "0090_bad.sql, statement 1" in notes[0] and "table already exists" in notes[0]
            assert {"first_half", "second_half", "later"} <= _tables(conn)
            assert _attempts(conn) == []

    @pytest.mark.parametrize("sql, code", [
        ("ALTER TABLE clusters DROP COLUMN acounts", "1091"),                 # typo in a drop
        ("CREATE TABLE reports (id INT, title TEXT, title TEXT)", "1060"),    # duplicate column in one table
        ("CREATE TABLE claims (id INT, brand_new_col INT)", "1050"),          # name already taken
        ("ALTER TABLE posts ADD CONSTRAINT fk_clusters_main_claim FOREIGN KEY (account_id)"
         " REFERENCES accounts (account_id)", "1826"),                        # FK names are database-wide
        ("ALTER TABLE clusters ADD COLUMN size INT", "1060"),                 # column already there
        ("ALTER TABLE clusters ADD COLUMN x INT AFTER no_such_col", "1054"),
        ("ALTER TABLE no_such_table ADD COLUMN x INT", "1146"),
    ])
    def test_first_attempt_never_skips_errors(self, scratch_db, tmp_path, sql, code):
        migrations = _real_plus(tmp_path, {"0090_change.sql": sql + ";"})
        with connection(scratch_db) as conn:
            notes: list[str] = []
            with pytest.raises(MigrationError, match="0090_change.sql, statement 1 of 1 failed"):
                migrate(conn, migrations, on_skip=notes.append)
            assert notes == []
            assert "0090_change" not in _versions(conn)

    def test_repeated_attempt_skips_only_matching_statement_kinds(self, scratch_db, tmp_path):
        migrations = _real_plus(tmp_path, {"0090_rename.sql": "RENAME TABLE users TO claims;"})
        with connection(scratch_db) as conn:
            migrate(conn, migrations[:-1])
            _mark_started(conn, "0090_rename")
            with pytest.raises(MigrationError, match="already exists"):  # 1050, but not a CREATE TABLE
                migrate(conn, migrations)

    def test_half_applied_0002_can_be_repeated(self, scratch_db):
        with connection(scratch_db) as conn:
            migrate(conn, _upto("0001_initial"))
            _mark_started(conn, "0002_screens")
            with conn.cursor() as cur:  # as if the first run died after five statements
                cur.execute("ALTER TABLE post_clusters ADD COLUMN map_x DECIMAL(5, 2) NULL")
                cur.execute("ALTER TABLE post_clusters ADD COLUMN map_y DECIMAL(5, 2) NULL")
                cur.execute("ALTER TABLE clusters ADD COLUMN main_claim_id BIGINT UNSIGNED NULL")
                cur.execute("ALTER TABLE clusters ADD CONSTRAINT fk_clusters_main_claim FOREIGN KEY (main_claim_id)"
                            " REFERENCES claims (claim_id) ON DELETE SET NULL")
                cur.execute("ALTER TABLE clusters ADD COLUMN change_pct DECIMAL(7, 2) NULL")
            notes: list[str] = []
            assert migrate(conn, on_skip=notes.append) == REAL_VERSIONS[1:]
            assert len(notes) == 5
            assert sum("column already exists" in n for n in notes) == 4
            assert sum("foreign key already exists" in n for n in notes) == 1
            assert all(n.startswith("0002_screens.sql") for n in notes)
            assert _column(conn, "clusters", "last_seen")["type"] == "date"

    def test_half_applied_check_constraints_can_be_repeated(self, scratch_db):
        with connection(scratch_db) as conn:
            migrate(conn, _upto("0002_screens"))
            _mark_started(conn, "0003_value_checks")
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE post_clusters ADD CONSTRAINT chk_post_clusters_map_x CHECK (map_x BETWEEN 0 AND 100)")
            notes: list[str] = []
            assert migrate(conn, on_skip=notes.append) == ["0003_value_checks"]
            assert len(notes) == 1 and "check already exists" in notes[0]

    def test_another_run_in_progress_is_refused(self, scratch_db, monkeypatch):
        monkeypatch.setattr(mig, "LOCK_SECONDS", 1)
        with connection(scratch_db) as holder, connection(scratch_db) as conn:
            name = mig._lock_name(holder)
            with holder.cursor() as cur:
                cur.execute("SELECT GET_LOCK(%s, 0) AS got", (name,))
                assert cur.fetchone()["got"] == 1
            with pytest.raises(MigrationError, match="in progress"):
                migrate(conn)
            assert _tables(conn) == set()
            with holder.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
            assert migrate(conn)  # free again
            assert _lock_free(conn)  # and released after the run

    def test_lock_name_fits_for_the_longest_database_name(self):
        with _scratch("veritas_test_" + secrets.token_hex(4) + "x" * 43) as settings:  # 64 characters
            assert len(settings.mysql_database) == 64
            with connection(settings) as conn:
                assert migrate(conn) == REAL_VERSIONS

    def test_no_database_selected_is_a_plain_error(self):
        with _admin() as admin:
            with pytest.raises(MigrationError, match="No database is selected"):
                migrate(admin)

    def test_lost_connection_becomes_a_plain_error(self, scratch_db, tmp_path):
        migrations = _real_plus(tmp_path, {"0090_kill.sql": "KILL CONNECTION_ID();"})
        with connection(scratch_db) as conn:
            # the original failure is reported, not the failed lock release that follows it
            with pytest.raises(MigrationError, match="^0090_kill.sql, statement 1 of 1 failed"):
                migrate(conn, migrations)
        with connection(scratch_db) as conn:
            assert _versions(conn) == REAL_VERSIONS
            assert _lock_free(conn)  # MySQL freed it when the connection died

    def test_recorded_without_autocommit(self, scratch_db):
        with connection(scratch_db, autocommit=False) as conn:
            assert migrate(conn) == REAL_VERSIONS
        with connection(scratch_db) as other:
            assert _versions(other) == REAL_VERSIONS

    def test_fresh_connection_is_utf8mb4_and_utc(self, scratch_db):
        with connection(scratch_db) as conn:  # no migration ran on this connection
            with conn.cursor() as cur:
                cur.execute("SELECT @@session.time_zone AS tz, @@character_set_connection AS cs,"
                            " @@character_set_results AS rs")
                assert cur.fetchone() == {"tz": "+00:00", "cs": "utf8mb4", "rs": "utf8mb4"}
        with connection(scratch_db) as conn:
            migrate(conn)
        with connection(scratch_db) as conn:
            with conn.cursor() as cur:
                text = "Ąžuolas šypsosi 🌳 Привет"
                cur.execute("INSERT INTO claims (uuid, text) VALUES (%s, %s)", (uuid.uuid4().bytes, text))
                cur.execute("SELECT text, HEX(text) AS h FROM claims")
                row = cur.fetchone()
                assert row["text"] == text and row["h"] == text.encode("utf-8").hex().upper()

    def test_wrong_password_message(self, scratch_db):
        for wrong in (SECRET, "slaptažodis-" + SECRET):
            with pytest.raises(DatabaseUnavailable) as info:
                connect(dataclasses.replace(scratch_db, mysql_password=wrong))
            assert "refused the login" in str(info.value) and SECRET not in str(info.value)

    def test_non_ascii_password_logs_in(self, scratch_db):
        user, password = f"vt_{secrets.token_hex(4)}", "Žalgiris-ąčę-" + secrets.token_hex(4)
        with _admin() as admin, admin.cursor() as cur:
            cur.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (user, password))
            try:
                with connection(scratch_db, user=user, password=password, database="") as conn:
                    with conn.cursor() as c:
                        c.execute("SELECT CURRENT_USER() AS u")
                        assert c.fetchone()["u"].startswith(user + "@")
            finally:
                cur.execute("DROP USER %s@'%%'", (user,))

    def test_missing_database_message_for_root(self, scratch_db):
        with pytest.raises(DatabaseUnavailable, match="doesn't exist or this login can't use it"):
            connect(scratch_db, database=scratch_db.mysql_database + "_missing")

    def test_app_login_messages(self):
        app = Settings.load()  # the app's own login from .env (not root)
        try:
            connect(app).close()
        except DatabaseUnavailable as exc:
            pytest.skip(f"app login not available: {exc}")
        for database in ("veritas_lens_typo_" + secrets.token_hex(3), "mysql"):
            with pytest.raises(DatabaseUnavailable) as info:
                connect(app, database=database)
            assert f"The database '{database}' doesn't exist or this login can't use it" in str(info.value)

    def test_cli_status_and_apply(self, scratch_db, monkeypatch, capsys):
        monkeypatch.setattr(migrate_cli.Settings, "load", classmethod(lambda cls, *a, **k: scratch_db))
        assert migrate_cli.main(["--status"]) == 0
        out = capsys.readouterr().out
        assert all(f"pending  {v}" in out for v in REAL_VERSIONS)
        with connection(scratch_db) as conn:
            assert _tables(conn) == set()  # --status changes nothing
        assert migrate_cli.main([]) == 0
        out = capsys.readouterr().out
        assert all(f"applied {v}" in out for v in REAL_VERSIONS) and "Done." in out
        assert migrate_cli.main([]) == 0
        assert "up to date" in capsys.readouterr().out
        with connection(scratch_db) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO schema_migrations (version) VALUES ('0999_from_newer_code')")
        assert migrate_cli.main(["--status"]) == 0
        out = capsys.readouterr().out
        assert f"applied  {REAL_VERSIONS[-1]}" in out
        assert "applied  0999_from_newer_code  (not known to this version" in out

    def test_cli_reports_failure_and_what_was_applied(self, scratch_db, monkeypatch, capsys, tmp_path):
        migrations = _real_plus(tmp_path, {"0090_bad.sql": "SELECT no_such_function();"})
        monkeypatch.setattr(migrate_cli.Settings, "load", classmethod(lambda cls, *a, **k: scratch_db))
        monkeypatch.setattr(migrate_cli, "discover", lambda: migrations)
        assert migrate_cli.main([]) == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("Migration stopped. 0090_bad.sql, statement 1 of 1 failed")
        assert "Traceback" not in captured.err
        assert all(f"applied {v}" in captured.out for v in REAL_VERSIONS)

    def test_cli_survives_a_cp1252_console(self, scratch_db, monkeypatch, tmp_path):
        migrations = _real_plus(tmp_path, {"0090_lt.sql": "CREATE TABLE lt (id INT);\nSELECT 'Ąžuolas' FROM nope;"})
        monkeypatch.setattr(migrate_cli.Settings, "load", classmethod(lambda cls, *a, **k: scratch_db))
        monkeypatch.setattr(migrate_cli, "discover", lambda: migrations)
        out, err = io.BytesIO(), io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out, encoding="cp1252", errors="strict"))
        monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(err, encoding="cp1252", errors="strict"))
        assert migrate_cli.main([]) == 1          # first attempt: fails on statement 2
        _write(tmp_path / "migrations", "0090_lt.sql", "CREATE TABLE lt (id INT);\nCREATE TABLE ąžuolas (id INT);")
        assert migrate_cli.main([]) == 0          # repeat: prints a skip note, then Lithuanian table name works
        sys.stdout.flush(), sys.stderr.flush()
        assert b"Migration stopped." in err.getvalue() and b"\\u0104" in err.getvalue()
        assert b"skipped (table already exists)" in out.getvalue() and b"applied 0090_lt" in out.getvalue()
