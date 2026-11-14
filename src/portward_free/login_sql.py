"""A matching login (#99, Personal): setup SQL for a read-only database login that can read exactly what a
connection's applied profiles grant, so the database itself enforces "hidden" if anything goes around Portward.

Portward writes the SQL; the user or their DBA runs it. It isn't stored, and it never holds a password: where one is
needed the SQL has PASSWORD_PLACEHOLDER for the user to replace. The SQL names only granted tables and columns (the
AI may see it), taken from what the grants match on the connection now (grants.Grants.visible): a grant for a column
that isn't there yet, or on a view, isn't in it.

On connect (`drift`), Portward compares what the connection's login can read with the grants. When it reads more, the
connection gets a warning and new SQL that tightens the login. It never blocks: Portward hides the columns either way.
The warning gives counts, never the hidden names (to the AI, a hidden column doesn't exist).

Postgres: column privileges (GRANT SELECT (col, ...) ON table). A table-level REVOKE also revokes that table's column
privileges, so tightening an existing login is REVOKE ALL on the schema's tables, then the column grants again.

Snowflake has no column-level SELECT privilege, so the SQL makes a schema of its own holding one secure view per
granted table, selecting only the granted columns under the table's own name, and a role that may use only those
views (`snowflake_sql` says how it maps onto Snowflake's grant model).

Row rules and standing filters (#133) are mirrored too: the login reads only the rows the AI may see (rows any applied
profile reaches, every standing filter applied; `rows_sql`), as row-level security on Postgres and as the secure
view's WHERE on Snowflake. Levels per cell (a column anonymised on some rows) stay Portward's: the login reads a
granted column's real values on every row it reaches, as it always did for anonymised and totals columns.
`drift` also warns when a Postgres login reads a ruled table without a restrictive row security policy for it
(`PostgresSource.rows_unguarded`). On Snowflake the rows aren't checked: the secure view's WHERE can't be read back
reliably, so drift there compares columns only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlglot import exp

from portward_free.grants import Grants
from portward_free.row_rules import NEVER, render
from portward_free.sources import Source, Table

PASSWORD_PLACEHOLDER = "<replace with a new password>"
PUBLIC_KEY_PLACEHOLDER = "<replace with the login's RSA public key>"
WAREHOUSE_PLACEHOLDER = "<replace with a warehouse>"
LOGIN_PREFIX = "portward_"
MAX_LOGIN = 63  # Postgres's identifier limit
POLICY_SUFFIX = "_rows"  # the login's row security policy on each table with row rules: <login>_rows
KEEP_OTHERS = "portward_keep_others"  # made only when Portward's SQL turns row security on for a table


def login_name(source: str) -> str:
    """The default name for a connection's matching login: portward_<connection name>, letters, digits and _."""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", source.lower()).strip("_") or "source"
    return (LOGIN_PREFIX + cleaned)[:MAX_LOGIN]


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _schemas(granted: Sequence[Table]) -> list[str]:
    return sorted({t.schema for t in granted if t.schema})


def _qualified(table: Table) -> str:
    return f"{_quoted(table.schema)}.{_quoted(table.name)}" if table.schema else _quoted(table.name)


RowsSql = Mapping[tuple[str | None, str], str]  # (schema, table) -> the SQL condition of the rows the login may read


def _rows_of(rows: RowsSql, table: Table) -> str | None:
    return rows.get((table.schema, table.name))


def _policy_name(login: str) -> str:
    return _quoted(login[:MAX_LOGIN - len(POLICY_SUFFIX)] + POLICY_SUFFIX)


def _dollar_quoted(body: str) -> str:
    tag = "$pw$"
    while tag in body:
        tag = tag[:-1] + "w$"
    return f"{tag}{body}{tag}"


def _row_security(table: Table, role: str, login: str, where: str | None, new: bool) -> list[str]:
    """Postgres row-level security for one granted table: a RESTRICTIVE policy keeps `login` to `where`. Restrictive
    policies are ANDed with every other policy, so it only ever narrows. Turning row security on for a table that
    didn't have it would leave every other non-owner role with no rows, so in that case (and only then: a table's own
    policies stay as they are) a permissive policy for everyone keeps the table as it was for them."""
    qualified, policy = _qualified(table), _policy_name(login)
    lines = [f"DROP POLICY IF EXISTS {policy} ON {qualified};"] if where is not None or not new else []
    if where is None:
        return lines
    body = (f"\nBEGIN\n  IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = {_literal(qualified)}::regclass) THEN"
            f"\n    DROP POLICY IF EXISTS {_quoted(KEEP_OTHERS)} ON {qualified};"
            f"\n    CREATE POLICY {_quoted(KEEP_OTHERS)} ON {qualified} AS PERMISSIVE FOR ALL TO PUBLIC USING (true) "
            "WITH CHECK (true);"
            f"\n    ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY;\n  END IF;\nEND\n")
    return lines + [f"DO {_dollar_quoted(body)};",
                    f"CREATE POLICY {policy} ON {qualified} AS RESTRICTIVE FOR SELECT TO {role} USING ({where});"]


def postgres_sql(granted: Sequence[Table], database: str, login: str, *, new: bool = True,
                 rows: RowsSql | None = None) -> str:
    """SQL that leaves `login` able to read exactly `granted` (tables with their granted columns) and nothing else
    in their schemas; on tables in `rows`, only those rows (row-level security, `_row_security`). `new` creates the
    login (with the password placeholder); otherwise it tightens an existing one."""
    role = _quoted(login)
    lines = [f"-- A read-only Postgres login that can read only what Portward's profiles grant ({len(granted)} "
             "tables).",
             "-- Run it as the database's owner or an administrator. Portward doesn't keep this SQL."]
    if new:
        lines.append(f"-- Replace {PASSWORD_PLACEHOLDER} with a new password first, then give Portward that login.")
    lines.append("BEGIN;")
    if new:
        lines.append(f"CREATE ROLE {role} LOGIN PASSWORD {_literal(PASSWORD_PLACEHOLDER)} NOSUPERUSER NOCREATEDB "
                     "NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;")
    lines.append(f"ALTER ROLE {role} SET default_transaction_read_only = on;")
    schemas = _schemas(granted)
    if schemas:
        lines.append(f"ALTER ROLE {role} IN DATABASE {_quoted(database)} SET search_path = "
                     f"{', '.join(_quoted(s) for s in schemas)};")
    lines.append(f"GRANT CONNECT ON DATABASE {_quoted(database)} TO {role};")
    for schema in schemas:
        lines.append(f"GRANT USAGE ON SCHEMA {_quoted(schema)} TO {role};")
        if not new:  # drops table and column privileges alike (a table-level REVOKE takes its columns with it)
            lines.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {_quoted(schema)} FROM {role};")
    for table in granted:
        columns = ", ".join(_quoted(c) for c in table.columns)
        lines.append(f"GRANT SELECT ({columns}) ON TABLE {_qualified(table)} TO {role};")
        lines += _row_security(table, role, login, _rows_of(rows or {}, table), new)
    lines.append("COMMIT;")
    if not new:
        lines.append("-- If the login still reads more after this, it gets it from a role it belongs to or from "
                     "PUBLIC: take it away there, or make a new login.")
    return "\n".join(lines) + "\n"


PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def snowflake_name(typed: str) -> str:
    """A Snowflake object name the user typed (a database or warehouse), as SQL that names the same object: a plain
    name stays unquoted (Snowflake upper-cases it), a name already in double quotes is kept, anything else is
    quoted exactly (docs.snowflake.com/en/sql-reference/identifiers-syntax)."""
    typed = typed.strip()
    if len(typed) > 1 and typed.startswith('"') and typed.endswith('"'):
        return typed
    return typed if PLAIN.fullmatch(typed) else _quoted(typed)


def snowflake_typed(stored: str) -> str:
    """A name as Snowflake stores it (CURRENT_DATABASE(), information_schema) in the form `snowflake_name` takes."""
    return stored if PLAIN.fullmatch(stored) and stored == stored.upper() else _quoted(stored)


def snowflake_sql(granted: Sequence[Table], database: str, login: str, *, warehouse: str | None = None,
                  wider: Sequence[str] = (), rows: RowsSql | None = None) -> str:
    """Snowflake setup SQL for a service user that can read exactly `granted`.

    How it maps onto Snowflake's grant model:
    - SELECT is granted on whole tables and views only, so each granted table gets a SECURE VIEW selecting just its
      granted columns, in a schema of Portward's own (named like the login), named like the table. Run as the role
      that owns the tables: a role granted SELECT on a view can use it without privileges on the tables it reads
      (docs.snowflake.com/en/sql-reference/sql/create-view). SECURE keeps the view's definition from the reader.
    - The reader role gets USAGE on the database, that schema and a warehouse, and SELECT on the views, nothing else.
      Every user also has PUBLIC, so anything granted to PUBLIC is readable too; the drift check sees what of that
      is in the connection's database.
    - The user is TYPE = SERVICE with key-pair sign-in (no password), DEFAULT_ROLE the reader role, DEFAULT_NAMESPACE
      the view schema (so tables are found by their own names), and DEFAULT_SECONDARY_ROLES = () so no other role it
      might be given is active. A default role isn't a grant, so the role is granted too. Snowflake's MCP servers
      take a programmatic access token: the SQL says how to make one restricted to the reader role.
    - Safe to run again: the role, schema and user are IF NOT EXISTS, the views CREATE OR REPLACE, the user's
      defaults are set again with ALTER USER, and the reader loses SELECT on everything in its schema before the
      grants are made again, so a view left from an earlier grant isn't readable any more.
    - A granted table already in the view schema is the login's own view (the connection reads through a matching
      login): it can't be rebuilt from itself, so it's granted again only when it shows no more than is granted;
      `wider` names those that show more, and they stay ungranted until made again from the tables' connection.
    - A table in `rows` (row rules and standing filters, #133) gets its view with that WHERE, so the login reads
      only the rows Portward shows.
    """
    upper = login.upper()
    role, user = _quoted(f"{upper}_READER"), _quoted(upper)
    db = snowflake_name(database)
    schema = f"{db}.{_quoted(upper)}"
    house = snowflake_name(warehouse) if warehouse else WAREHOUSE_PLACEHOLDER
    namespace = _literal(f"{db}.{snowflake_typed(upper)}")
    own = [t for t in granted if (t.schema or "").upper() == upper]
    based = [t for t in granted if t not in own]
    readable = [t.name for t in based] + [t.name for t in own if t.name not in set(wider)]
    lines = [f"-- A read-only Snowflake login that can read only what Portward's profiles grant ({len(granted)} "
             "tables).",
             "-- Run it as the role that owns these tables, with a role that can create users and roles (or have "
             "your administrator run those lines). Portward doesn't keep this SQL.",
             f"-- Replace {PUBLIC_KEY_PLACEHOLDER}" + ("" if warehouse else f" and {WAREHOUSE_PLACEHOLDER}")
             + " first.",
             f"CREATE ROLE IF NOT EXISTS {role};",
             f"CREATE SCHEMA IF NOT EXISTS {schema};"]
    for table in based:
        columns = ", ".join(_quoted(c) for c in table.columns)
        where = _rows_of(rows or {}, table)
        lines.append(f"CREATE OR REPLACE SECURE VIEW {schema}.{_quoted(table.name)} AS SELECT {columns} FROM "
                     f"{db}.{_qualified(table)}" + (f" WHERE {where}" if where is not None else "") + ";")
    lines += [f"GRANT USAGE ON DATABASE {db} TO ROLE {role};",
              f"GRANT USAGE ON SCHEMA {schema} TO ROLE {role};",
              f"GRANT USAGE ON WAREHOUSE {house} TO ROLE {role};",
              f"REVOKE SELECT ON ALL VIEWS IN SCHEMA {schema} FROM ROLE {role};",
              f"REVOKE SELECT ON ALL TABLES IN SCHEMA {schema} FROM ROLE {role};"]
    lines += [f"GRANT SELECT ON VIEW {schema}.{_quoted(name)} TO ROLE {role};" for name in readable]
    defaults = (f"DEFAULT_ROLE = {role} DEFAULT_WAREHOUSE = {house} DEFAULT_NAMESPACE = {namespace} "
                "DEFAULT_SECONDARY_ROLES = ()")
    lines += [f"CREATE USER IF NOT EXISTS {user} TYPE = SERVICE RSA_PUBLIC_KEY = {_literal(PUBLIC_KEY_PLACEHOLDER)} "
              f"{defaults};",
              f"ALTER USER {user} SET {defaults};",
              f"GRANT ROLE {role} TO USER {user};"]
    if wider:
        lines.append(f"-- These views show more than is granted and stay unreadable: {', '.join(wider)}. Make them "
                     "again with this SQL from a connection that reads their tables.")
    lines += ["-- Snowflake's MCP servers take a programmatic access token. A service user's token needs a network "
              "policy on the user and a role restriction; then make one (its secret is shown once) and give it to "
              "portward-free wrap-add:",
              f"-- ALTER USER {user} ADD PROGRAMMATIC ACCESS TOKEN portward ROLE_RESTRICTION = "
              f"{_literal(upper + '_READER')} DAYS_TO_EXPIRY = 90;",
              "-- For a Snowflake-managed MCP server, the role also needs privileges on the MCP server object "
              "(GRANT USAGE ON MCP SERVER ...)."]
    return "\n".join(lines) + "\n"


Generator = Callable[[Sequence[Table], str, str], str]
GENERATORS: dict[str, Generator] = {"postgres": postgres_sql, "snowflake": snowflake_sql}  # by Source.dialect


class NoMatchingLogin(ValueError):
    """Portward can't write a matching login for this connection; the message is safe to show."""


def matching_sql(dialect: str, policy: object, catalog: Sequence[Table], database: str, login: str,
                 *, new: bool = True) -> str:
    """The setup SQL for `login` on a connection running `policy` with `catalog`."""
    if not isinstance(policy, Grants):
        raise NoMatchingLogin("a matching login follows a connection's profiles, and this one runs on block rules: "
                              "apply a profile first")
    if dialect not in GENERATORS:
        raise NoMatchingLogin("a matching login is SQL for a database login (Postgres or Snowflake), and this "
                              "source has none")
    granted = policy.visible(catalog)
    rows = rows_sql(policy, catalog, dialect)
    if dialect == "postgres":
        return postgres_sql(granted, database, login, new=new, rows=rows)
    shown = {t.name: len(t.columns) for t in granted}
    wider = [t.name for t in catalog if t.name in shown and (t.schema or "").upper() == login.upper()
             and (len(t.columns) > shown[t.name] or (t.schema, t.name) in rows)]
    return snowflake_sql(granted, database, login, wider=wider, rows=rows)


def rows_sql(grants: Grants, catalog: Sequence[Table], dialect: str) -> dict[tuple[str | None, str], str]:
    """For each table of `catalog` whose rows the grants limit (row rules, standing filters: grants.Grants.cells),
    the SQL condition of the rows the AI may see, rendered from the rules' trees in `dialect`."""
    found = {}
    for table in catalog:
        cells = grants.cells(table)
        if cells is None or cells.rows is None:
            continue
        real = {c.lower(): c for c in cells.real}
        condition = exp.or_(*[render(r, real) for r in cells.rows]) if cells.rows else NEVER.copy()
        found[(table.schema, table.name)] = condition.sql(dialect=dialect)
    return found


@dataclass(frozen=True)
class Drift:
    """The connection's login reads more than the grants: how much (counts only) and SQL to tighten it."""

    columns: int
    tables: int
    elsewhere: int
    sql: str
    rows: int = 0  # tables with row rules whose rows the login reads without row security (Postgres only)

    @property
    def warning(self) -> str:
        said = [f"{self.columns} columns in {self.tables} tables of its schema"] if self.columns else []
        if self.elsewhere:
            said.append(f"{self.elsewhere} tables in other schemas")
        if self.rows:
            said.append(f"every row of {self.rows} tables whose rows a row rule limits")
        found = " and ".join(said)
        return (f"this login can read more than this connection's profiles grant ({found}). Portward still hides "
                "them all, but a login that reads only what's granted makes the database hide them too: show the "
                "user `login_sql` (SQL for their DBA to run) or call matching_login_sql")


def database_of(source: Source, configured: object) -> str:
    """The database a matching login's SQL names: the session's own when the source can say (a wrapped Snowflake
    connection, whose settings hold at most the MCP server object's database), else the connection's setting."""
    found = getattr(source, "current_database", lambda: None)()
    if found and source.dialect == "snowflake":
        return snowflake_typed(str(found))
    return str(found or configured)


def drift(source: Source, policy: object, database: str, login: str) -> Drift | None:
    """What the login of an opened `source` reads beyond `policy`'s grants, or None (it doesn't, or the connection
    isn't on profiles, or Portward can't write SQL for it)."""
    if not isinstance(policy, Grants) or source.dialect not in GENERATORS:
        return None
    database = database_of(source, database)
    catalog = source.tables()
    shown = {(t.name.lower(), c.lower()) for t in policy.visible(catalog) for c in t.columns}
    extra = [(t.name, c) for t in catalog for c in t.columns if (t.name.lower(), c.lower()) not in shown]
    elsewhere = getattr(source, "readable_elsewhere", lambda: 0)()
    ruled = [table for _, table in rows_sql(policy, catalog, source.dialect)]
    unguarded = getattr(source, "rows_unguarded", lambda tables: ())(ruled)
    if not extra and not elsewhere and not unguarded:
        return None
    sql = matching_sql(source.dialect, policy, catalog, database, login, new=False)
    return Drift(len(extra), len({t for t, _ in extra}), elsewhere, sql, len(unguarded))
