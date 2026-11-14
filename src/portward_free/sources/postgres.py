"""A Postgres database, read through a login that can't administer it.

Read-only besides the guard: every query runs in a `READ ONLY` transaction with a statement timeout, and the
session defaults to read-only transactions. A superuser or administrator login is refused; a login that can write
some tables is accepted with a warning (the read-only transaction still stops any write).

Tables are the ones the login can SELECT in its current schema (the guard refuses schema-qualified names), with the
columns it can SELECT: a login given column privileges (a matching login, #99) lists just those.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

import certifi
import psycopg

from portward_free.sources.base import QUERY_TIMEOUT_SECONDS, Fetched, SourceError, Table
from portward_free.sources.network import CONNECT_TIMEOUT_SECONDS, ServerSettings, writable_warning
from portward_free.stand_ins import HEX_CHARS, PREFIX, checked_group, hmac_pads

DEFAULT_PORT = 5432
# The anonymising key's HMAC pads, set for one transaction with set_config (never in the SQL text, so never in the
# server's logs). The AI can't read them back: the guard refuses current_setting.
PAD_SETTINGS = ("portward.stand_in_ipad", "portward.stand_in_opad")
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
# Roles that make a login an administrator without rolsuper: managed Postgres gives its admin these instead, and
# pg_write_all_data / pg_database_owner can change every table.
ADMIN_ROLES = ("pg_write_all_data", "pg_database_owner", "azure_pg_admin", "rds_superuser", "cloudsqlsuperuser",
               "alloydbsuperuser")
# Why a connection failed, from the driver's message (which names hosts and addresses, so it is never shown as is).
CONNECT_PROBLEMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"password authentication failed|no password supplied"), "wrong user or password"),
    (re.compile(r'database ".*" does not exist'), "that database doesn't exist on the server; check its name"),
    (re.compile(r"certificate|ssl"), "the server's TLS certificate couldn't be verified"),
    (re.compile(r"no pg_hba\.conf entry"), "the server doesn't accept this login from this computer"),
    (re.compile(r"resolve host|translate host name|nodename nor servname|name or service not known"),
     "the host name can't be found; check the host"),
    (re.compile(r"timeout|timed out|connection refused|could not connect|is the server running"),
     "the server didn't answer; check the host and port, and that the database is running"),
)


def connect_problem(err: Exception) -> str:
    message = str(err).lower()
    for pattern, reason in CONNECT_PROBLEMS:
        if pattern.search(message):
            return f"couldn't connect: {reason}"
    return f"couldn't connect ({type(err).__name__})"


class PostgresSource:
    dialect = "postgres"

    def __init__(self, server: ServerSettings, password: str) -> None:
        self._server = server
        self._pads: tuple[str, str] | None = None
        tls = {} if server.is_local else {"sslmode": "verify-full", "sslrootcert": certifi.where()}
        pinned = {"hostaddr": server.address} if server.address else {}  # TLS still checks `host`
        try:
            self._db = psycopg.connect(
                host=server.host, port=server.port, dbname=server.database, user=server.user, password=password,
                connect_timeout=CONNECT_TIMEOUT_SECONDS, passfile=os.devnull, application_name="portward",
                options="-c default_transaction_read_only=on", **tls, **pinned)
        except psycopg.Error as err:
            raise SourceError(connect_problem(err)) from None
        self._db.read_only = True
        try:
            self.warnings = self._check_login()
            self._tables = self._list_tables()
        except BaseException:
            self._db.close()
            raise
        self._db.rollback()

    def _check_login(self) -> tuple[str, ...]:
        """Refuse an administrator; warn about a login that can write some tables."""
        db = self._db
        superuser, create_role, bypass_rls = db.execute(
            "SELECT rolsuper, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
        if superuser:
            raise SourceError("this login is a superuser: connect with a login that can only read")
        powers = [name for name, held in (("CREATEROLE", create_role), ("BYPASSRLS", bypass_rls)) if held]
        powers += [r[0] for r in db.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) AND pg_has_role(current_user, oid, 'member') "
            "ORDER BY rolname", (list(ADMIN_ROLES),)).fetchall()]
        if powers:
            raise SourceError(f"this login is an administrator ({', '.join(powers)}): connect with a login that "
                              "can only read")
        writable = db.execute(
            "SELECT c.relname, p.privilege FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN unnest(%s::text[]) AS p(privilege) "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND has_table_privilege(current_user, c.oid, p.privilege) ORDER BY c.relname",
            (list(WRITE_PRIVILEGES),)).fetchall()
        return (writable_warning([(t, p) for t, p in writable]),) if writable else ()

    def _list_tables(self) -> tuple[Table, ...]:
        # derived: views, materialized views, and partitions or inheritance children (their rows are also another
        # table's); see Table.derived
        rows = self._db.execute(
            "SELECT c.relname, a.attname, c.relkind IN ('v', 'm') OR c.relispartition "
            "OR EXISTS (SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid), ty.typcategory = 'N' "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "JOIN pg_type ty ON ty.oid = a.atttypid "
            "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND has_column_privilege(current_user, c.oid, a.attnum, 'SELECT') ORDER BY c.relname, a.attnum"
        ).fetchall()
        columns: dict[str, list[str]] = {}
        numeric: dict[str, set[str]] = {}  # type category N: integers, numeric, floating point, money
        derived: set[str] = set()
        for table, column, is_derived, is_number in rows:
            columns.setdefault(table, []).append(column)
            if is_number:
                numeric.setdefault(table, set()).add(column)
            if is_derived:
                derived.add(table)
        parents: dict[str, list[str]] = {}
        for child, parent in self._db.execute(  # a partition's or inheritance child's rows are its parent's too
                "SELECT c.relname, p.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() ORDER BY c.relname, i.inhseqno").fetchall():
            parents.setdefault(child, []).append(parent)
        (schema,) = self._db.execute("SELECT current_schema()").fetchone()
        return tuple(Table(t, tuple(c), derived=t in derived, parents=tuple(parents.get(t, ())), schema=schema or "",
                           numeric=frozenset(numeric.get(t, ()))) for t, c in columns.items())

    def tables(self) -> tuple[Table, ...]:
        return self._tables

    def readable_elsewhere(self) -> int:
        """How many tables outside the current schema (and Postgres's own) the login can read any column of: the AI
        never sees them (the guard refuses other schemas), but the login reads more than a profile grants (#99)."""
        try:
            (count,) = self._db.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname <> current_schema() AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg\\_%' AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
                "AND has_schema_privilege(current_user, n.oid, 'USAGE') "
                "AND has_any_column_privilege(current_user, c.oid, 'SELECT')").fetchone()
        except psycopg.Error as err:
            raise SourceError(f"couldn't read this login's privileges: {err.diag.message_primary}") from None
        finally:
            self._db.rollback()
        return int(count)

    def rows_unguarded(self, tables: Sequence[str]) -> tuple[str, ...]:
        """Of `tables` (in the current schema), those whose rows this login reads without a restrictive row
        security policy for it: row security off, no RESTRICTIVE SELECT (or ALL) policy naming the login or PUBLIC,
        or a login that skips row security anyway (superuser, BYPASSRLS, the table's owner without FORCE). The
        policy's condition isn't compared: Portward can't tell a rewritten one from its own (#133, story 40)."""
        if not tables:
            return ()
        try:
            rows = self._db.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_roles r ON r.rolname = current_user "
                "WHERE n.nspname = current_schema() AND c.relname = ANY(%s) AND NOT ("
                "c.relrowsecurity AND NOT r.rolsuper AND NOT r.rolbypassrls "
                "AND (c.relowner <> r.oid OR c.relforcerowsecurity) "
                "AND EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = n.nspname AND p.tablename = c.relname "
                "AND p.permissive = 'RESTRICTIVE' AND p.cmd IN ('SELECT', 'ALL') "
                "AND (current_user = ANY(p.roles) OR 'public' = ANY(p.roles))))",
                (list(tables),)).fetchall()
        except psycopg.Error as err:
            raise SourceError(f"couldn't read this login's row security: {err.diag.message_primary}") from None
        finally:
            self._db.rollback()
        return tuple(sorted(r[0] for r in rows))

    def use_stand_in_key(self, key: bytes) -> None:
        inner, outer = hmac_pads(key)
        self._pads = (inner.hex(), outer.hex())

    def stand_in_sql(self, column_sql: str, group: str) -> str:
        """HMAC-SHA256 from the built-in sha256 (Postgres 11 and later), so no extension is needed."""
        ipad, opad = (f"decode(current_setting('{name}'), 'hex')" for name in PAD_SETTINGS)
        message = f"convert_to('{PREFIX}|{checked_group(group)}|' || CAST({column_sql} AS TEXT), 'UTF8')"
        digest = f"sha256({opad} || sha256({ipad} || {message}))"
        return f"'{PREFIX}_' || substr(encode({digest}, 'hex'), 1, {HEX_CHARS})"

    def fetch(self, sql: str) -> Fetched:
        try:
            with self._db.transaction():
                self._db.execute("SET TRANSACTION READ ONLY")
                self._db.execute("SELECT set_config('statement_timeout', %s, true)",
                                 (str(int(QUERY_TIMEOUT_SECONDS * 1000)),))
                if self._pads is not None:
                    for name, pad in zip(PAD_SETTINGS, self._pads, strict=True):
                        self._db.execute("SELECT set_config(%s, %s, true)", (name, pad))
                cursor = self._db.execute(sql.encode())  # bytes: no client-side % placeholder parsing
                columns = tuple(d.name for d in cursor.description or ())
                rows = tuple(tuple(r) for r in cursor.fetchall())
        except psycopg.errors.QueryCanceled:
            raise SourceError(f"the query took longer than {QUERY_TIMEOUT_SECONDS:g} seconds and was stopped") from None
        except psycopg.Error as err:
            raise SourceError(f"the query failed: {err.diag.message_primary or type(err).__name__}") from None
        return Fetched(columns, rows)

    def close(self) -> None:
        self._db.close()


def open_postgres(settings: Mapping[str, Any], password: str | None = None) -> PostgresSource:
    return PostgresSource(ServerSettings.parse("Postgres", settings, DEFAULT_PORT), password or "")
