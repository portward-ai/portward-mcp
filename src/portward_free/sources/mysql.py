"""A MySQL 8 database, read through a login that can't administer it.

Read-only besides the guard: the session's transactions are read-only, and every query runs in its own
`START TRANSACTION READ ONLY` that is always rolled back, with `max_execution_time` as the time limit. A login with
administrator privileges is refused; a login that can write some tables is accepted with a warning.
"""

from __future__ import annotations

import math
import re
import socket
import ssl
from collections.abc import Mapping
from typing import Any

import certifi
import pymysql

from portward_free.sources.base import QUERY_TIMEOUT_SECONDS, Fetched, SourceError, Table, is_numeric_type
from portward_free.sources.network import CONNECT_TIMEOUT_SECONDS, ServerSettings, writable_warning
from portward_free.stand_ins import HEX_CHARS, PREFIX, checked_group, hmac_pads

DEFAULT_PORT = 3306
# The anonymising key's HMAC pads, bound into session variables for one query and cleared after (never in the SQL
# text). The AI can't read them: the guard refuses variables.
PAD_VARS = ("@pw_stand_in_ipad", "@pw_stand_in_opad")
TIMEOUT_ERRORS = (3024, 1317, 2013)  # max_execution_time exceeded, query interrupted, connection lost (read timeout)
# MySQL's own texts name the client's address, so connection failures are told by code.
CONNECT_PROBLEMS = {
    1045: "wrong user or password",
    # MySQL says 1044 (not 1049) for a missing database when the login can't see it, so the two read the same
    1044: "that database doesn't exist on the server, or this login can't open it; check its name",
    1049: "that database doesn't exist on the server; check its name",
    2003: "the server didn't answer; check the host and port, and that the database is running",
    2005: "the host name can't be found; check the host",
    2026: "the server's TLS certificate couldn't be verified",
}
# Privileges that let the login change a table, and (granted ON *.*) ones that make it an administrator.
WRITE_PRIVILEGES = frozenset({"ALL", "ALL PRIVILEGES", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
                              "INDEX", "TRIGGER"})
ADMIN_PRIVILEGES = frozenset({"ALL", "ALL PRIVILEGES", "SUPER", "FILE", "SHUTDOWN", "RELOAD", "CREATE USER",
                              "SYSTEM_USER"})
GRANT_LINE = re.compile(r"^GRANT (.+?) ON (?:TABLE )?(\*|`(?:[^`]|``)*`)\.(\*|`(?:[^`]|``)*`) TO ")
ROLE_LINE = re.compile(r"^GRANT (`.+`@`.*`) TO ")


def _code(err: pymysql.Error) -> int | None:
    return err.args[0] if err.args and isinstance(err.args[0], int) else None


UNKNOWN_HOST = re.compile(r"nodename nor servname|name or service not known|getaddrinfo|no address associated|"
                          r"temporary failure in name resolution", re.I)


def connect_problem(err: pymysql.Error) -> str:
    code = _code(err) or 0
    if code == 2003 and UNKNOWN_HOST.search(str(err)):
        code = 2005  # PyMySQL reports a host it can't resolve as "can't connect"
    reason = CONNECT_PROBLEMS.get(code)
    return f"couldn't connect: {reason}" if reason else f"couldn't connect ({type(err).__name__})"


def _unquote(name: str) -> str:
    return name if name == "*" else name[1:-1].replace("``", "`")


def _like(pattern: str, value: str) -> bool:
    """MySQL grants name databases with LIKE wildcards (_ and %, \\-escaped); match case-insensitively (fail closed)."""
    regex = "".join(".*" if p == "%" else "." if p == "_" else re.escape(p[-1])
                    for p in re.findall(r"\\.|.", pattern, re.S))
    return re.fullmatch(regex, value, re.I | re.S) is not None


def _tls(server: ServerSettings) -> ssl.SSLContext | None:
    return None if server.is_local else ssl.create_default_context(cafile=certifi.where())


def _dial(address: str, port: int) -> socket.socket:
    try:
        sock = socket.create_connection((address, port), CONNECT_TIMEOUT_SECONDS)
    except OSError:
        raise SourceError("couldn't connect: the server didn't answer; check the host and port, and that the "
                          "database is running") from None
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    return sock


class MySqlSource:
    dialect = "mysql"

    def __init__(self, server: ServerSettings, password: str) -> None:
        self._server = server
        self._pads: tuple[str, str] | None = None
        try:
            self._db = pymysql.connect(
                host=server.host, port=server.port, user=server.user, password=password, database=server.database,
                charset="utf8mb4", connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=math.ceil(QUERY_TIMEOUT_SECONDS) + 5, autocommit=True, ssl=_tls(server),
                defer_connect=server.address is not None)
            if server.address is not None:  # the checked address (egress.py); TLS still checks `host`
                self._db.connect(sock=_dial(server.address, server.port))
        except pymysql.Error as err:
            raise SourceError(connect_problem(err)) from None
        try:
            with self._db.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                cur.execute("SET SESSION max_execution_time = %s", (int(QUERY_TIMEOUT_SECONDS * 1000),))
                self.warnings = self._check_login(cur)
                self._tables = self._list_tables(cur)
        except pymysql.Error as err:
            self._db.close()
            raise SourceError(f"couldn't read the database's tables ({type(err).__name__})") from None
        except BaseException:
            self._db.close()
            raise

    def _check_login(self, cur: Any) -> tuple[str, ...]:
        writable = self._write_grants(cur)
        return (writable_warning(writable),) if writable else ()

    def _write_grants(self, cur: Any) -> list[tuple[str, str]]:
        """(table or "*", privilege) the login can write in this database, from SHOW GRANTS expanded with every role
        granted to it. Administrators are refused outright."""
        cur.execute("SHOW GRANTS")
        lines = [r[0] for r in cur.fetchall()]
        roles = [m.group(1) for line in lines if (m := ROLE_LINE.match(line))]
        if roles:
            cur.execute(f"SHOW GRANTS FOR CURRENT_USER() USING {', '.join(roles)}")
            lines = [r[0] for r in cur.fetchall()]
        found = []
        for line in lines:
            match = GRANT_LINE.match(line)
            if match is None:
                continue  # role grants and PROXY
            privileges = {p.strip().upper() for p in re.sub(r"\([^)]*\)", "", match.group(1)).split(",")}
            db, table = _unquote(match.group(2)), _unquote(match.group(3))
            admin = privileges & ADMIN_PRIVILEGES or {p for p in privileges if p.endswith("_ADMIN")}
            if db == "*" and (admin or line.endswith("WITH GRANT OPTION")):
                raise SourceError(f"this login is an administrator ({', '.join(sorted(admin)) or 'GRANT OPTION'}): "
                                  "connect with a login that can only read")
            writes = privileges & WRITE_PRIVILEGES
            if writes and (db == "*" or _like(db, self._server.database)):
                found.append((table, sorted(writes)[0]))
        return found

    def _list_tables(self, cur: Any) -> tuple[Table, ...]:
        # a view is derived (see Table.derived); anything but a plain base table counts as one
        cur.execute("SELECT c.TABLE_NAME, c.COLUMN_NAME, COALESCE(t.TABLE_TYPE, '') <> 'BASE TABLE', c.DATA_TYPE "
                    "FROM information_schema.COLUMNS c LEFT JOIN information_schema.TABLES t "
                    "ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
                    "WHERE c.TABLE_SCHEMA = %s ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION", (self._server.database,))
        columns: dict[str, list[str]] = {}
        numeric: dict[str, set[str]] = {}
        derived: set[str] = set()
        for table, column, is_derived, data_type in cur.fetchall():
            columns.setdefault(table, []).append(column)
            if is_numeric_type(data_type):
                numeric.setdefault(table, set()).add(column)
            if is_derived:
                derived.add(table)
        return tuple(Table(t, tuple(c), derived=t in derived, schema=self._server.database,
                           numeric=frozenset(numeric.get(t, ()))) for t, c in columns.items())

    def tables(self) -> tuple[Table, ...]:
        return self._tables

    def use_stand_in_key(self, key: bytes) -> None:
        inner, outer = hmac_pads(key)
        self._pads = (inner.hex(), outer.hex())

    def stand_in_sql(self, column_sql: str, group: str) -> str:
        """HMAC-SHA256 from SHA2 and the pads (MySQL has no HMAC function)."""
        message = f"CAST(CONVERT(CONCAT('{PREFIX}|{checked_group(group)}|', CAST({column_sql} AS CHAR)) USING utf8mb4) AS BINARY)"
        digest = f"SHA2(CONCAT({PAD_VARS[1]}, UNHEX(SHA2(CONCAT({PAD_VARS[0]}, {message}), 256))), 256)"
        return f"CONCAT('{PREFIX}_', LOWER(LEFT({digest}, {HEX_CHARS})))"

    def fetch(self, sql: str) -> Fetched:
        try:
            with self._db.cursor() as cur:
                cur.execute("START TRANSACTION READ ONLY")
                try:
                    if self._pads is not None:
                        cur.execute(f"SET {PAD_VARS[0]} = UNHEX(%s), {PAD_VARS[1]} = UNHEX(%s)", self._pads)
                    cur.execute(sql)  # no args: PyMySQL leaves % alone
                    columns = tuple(d[0] for d in cur.description or ())
                    rows = tuple(tuple(r) for r in cur.fetchall())
                finally:
                    cur.execute("ROLLBACK")
                    if self._pads is not None:
                        cur.execute(f"SET {PAD_VARS[0]} = NULL, {PAD_VARS[1]} = NULL")
        except pymysql.Error as err:
            if _code(err) in TIMEOUT_ERRORS:
                raise SourceError(f"the query took longer than {QUERY_TIMEOUT_SECONDS:g} seconds and was stopped") from None
            detail = err.args[1] if len(err.args) > 1 else type(err).__name__
            raise SourceError(f"the query failed: {detail}") from None
        return Fetched(columns, rows)

    def close(self) -> None:
        self._db.close()


def open_mysql(settings: Mapping[str, Any], password: str | None = None) -> MySqlSource:
    return MySqlSource(ServerSettings.parse("MySQL", settings, DEFAULT_PORT), password or "")
