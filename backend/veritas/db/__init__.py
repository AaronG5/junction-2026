"""MySQL access. One short-lived connection per use; PyMySQL (MIT licence).

Every connection uses utf8mb4 and UTC, so text in any language and all
timestamps behave the same everywhere. Problems connecting become
DatabaseUnavailable with a plain-language message (never the password).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pymysql
import pymysql.cursors

from ..config import Settings

# Client-side errors: the server couldn't be reached or the connection broke.
_UNREACHABLE = {2002, 2003, 2005, 2006, 2013}


class DatabaseUnavailable(Exception):
    """MySQL can't be reached or refused the login. The message is shown to users as is."""


def _message(code: int | None, settings: Settings, database: str) -> str:
    where = f"{settings.mysql_host}:{settings.mysql_port}"
    if code == 1045:
        return "The database refused the login. Check MYSQL_USER and MYSQL_PASSWORD in .env."
    if code in (1044, 1049):
        return (f"The database '{database}' doesn't exist or this login can't use it. "
                "Check MYSQL_DATABASE in .env, or run the launcher to set it up.")
    if code is None or code in _UNREACHABLE or code < 1000:
        return f"The database isn't reachable at {where}. Start it with the launcher."
    return f"MySQL is running at {where} but refused the connection (MySQL error {code})."


def connect(settings: Settings, *, database: str | None = None, user: str | None = None,
            password: str | None = None, autocommit: bool = True,
            read_timeout: int = 60) -> pymysql.connections.Connection:
    """Open a connection. database/user/password default to the app's own (from .env).
    database="" connects without selecting a database."""
    db = database if database is not None else settings.mysql_database
    secret = password if password is not None else settings.mysql_password
    try:
        return pymysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=user if user is not None else settings.mysql_user,
            # MySQL hashes passwords as UTF-8; PyMySQL would send a str as Latin-1
            password=secret.encode("utf-8"),
            database=db or None,
            charset="utf8mb4",
            init_command="SET time_zone = '+00:00'",
            autocommit=autocommit,
            connect_timeout=5,
            read_timeout=read_timeout,
            write_timeout=read_timeout,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        raise DatabaseUnavailable(_message(code, settings, db)) from None
    except Exception:  # anything else (bad settings, encoding, OS errors): never show a traceback or the password
        raise DatabaseUnavailable(_message(None, settings, db)) from None


@contextmanager
def connection(settings: Settings, **kwargs) -> Iterator[pymysql.connections.Connection]:
    conn = connect(settings, **kwargs)
    try:
        yield conn
    finally:
        conn.close()
