"""What Portward knows about each provider's MCP server it can wrap: how to reach it, which of its tools take SQL it
can rewrite, why the others stay hidden, how to ask it for the catalog, and how to read its results.

A provider is one `Provider` in `PROVIDERS`; its connection kind is `wrap-<name>`. Tools are allow-listed: only a
tool the provider recognises as its SQL tool is governed, and every other tool, including ones the server adds
later, stays hidden (never listed, and refused if called by name). Postgres MCP (#100) runs as a child process;
Snowflake's MCP server (#101) is remote, reached over streamable HTTP.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from mcp.types import CallToolResult, TextContent, Tool

from portward_free.sources.base import Fetched, SourceError, Table, is_numeric_type
from portward_free.wrap.child import ChildServer
from portward_free.wrap.pyrepr import NotReadable, rows_from_repr

KIND_PREFIX = "wrap-"
UNTIED = "Portward can't tie what it returns to the columns your profiles grant"
UNREADABLE = "the wrapped server returned a result Portward can't read, so it wasn't passed on"
UNEVEN = "the wrapped server returned rows of different shapes, so they weren't passed on"


@dataclass(frozen=True)
class Classified:
    governed: tuple[str, ...]  # the provider's SQL tools this server has: shown, with the rewrite
    hidden: tuple[tuple[str, str], ...]  # (tool, why it stays hidden)


class Provider(Protocol):
    name: str  # "postgres": the connection kind is wrap-<name>
    label: str  # "Postgres MCP"
    dialect: str  # the sqlglot dialect its SQL is parsed and rewritten in
    secret_env: str  # the login's name, as wrap-add reads it from the environment (kept in the keychain)
    catalog_sql: str  # sent through the SQL tool to read the tables and columns the login can see

    def settings(self, command: Sequence[str], url: str | None, env: Mapping[str, str]) -> dict[str, Any]:
        """The connection's settings (never the login) from what the user gave wrap-add; SourceError if unusable."""
        ...

    def open_child(self, settings: Mapping[str, Any], secret: str) -> ChildServer:
        """The server running (or reached) with the login, with whatever makes it read-only forced on."""
        ...

    def sql_argument(self, tool: Tool) -> str | None:
        """The argument that holds the SQL if `tool` is one of the provider's SQL tools, else None."""
        ...

    def why_hidden(self, tool: Tool) -> str:
        """Why a tool that isn't a SQL tool stays hidden."""
        ...

    def catalog(self, result: CallToolResult) -> tuple[Table, ...]:
        """The catalog from the SQL tool's answer to `catalog_sql`."""
        ...

    def rows(self, result: CallToolResult) -> Fetched:
        """A SQL tool's result as columns and rows; SourceError (in Portward's words) when it failed."""
        ...

    def looks_like(self, line: Sequence[str]) -> bool:
        """Whether an AI app's server entry (its command line and URL) reaches this provider's server, to spot the
        original still set up there."""
        ...


def classify(provider: Provider, tools: Sequence[Tool]) -> Classified:
    governed = tuple(t.name for t in tools if provider.sql_argument(t))
    hidden = tuple((t.name, provider.why_hidden(t)) for t in tools if not provider.sql_argument(t))
    return Classified(governed, hidden)


def _text(result: CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, TextContent))


# Why a query failed, from the child's error text, which may quote data: only these say more than "it failed", and
# each names only what the AI wrote itself.
FAILURES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Postgres quotes a bare name (column "ssn") but not a qualified one (column c.id)
    (re.compile(r'column "?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)"? does not exist'),
     "no column {0} is available there"),
    (re.compile(r"no such column: ([A-Za-z_][\w$.]*)"), "no column {0} is available there"),
    # Snowflake names only identifiers the query itself used (Portward's rewrite names only columns that exist)
    (re.compile(r"invalid identifier '([A-Za-z_][\w$.]*)'"), "no column {0} is available there"),
    (re.compile(r"syntax error"), "the database couldn't parse the query"),
    (re.compile(r"canceling statement due to statement timeout|timeout", re.I), "the query took too long and was "
                                                                               "stopped"),
)


def failure(message: str) -> str:
    for pattern, said in FAILURES:
        found = pattern.search(message)
        if found:
            return f"the query failed: {said.format(*found.groups())}"
    return "the query failed in the wrapped server"


class PostgresMcp:
    """Postgres MCP Pro (crystaldba/postgres-mcp). `execute_sql` is governed and runs in its restricted mode (one
    read-only transaction, its own SQL check); every other tool shows schema, plans or other sessions' SQL."""

    name = "postgres"
    label = "Postgres MCP"
    dialect = "postgres"
    secret_env = "DATABASE_URI"
    sql_tools: Mapping[str, str] = {"execute_sql": "sql"}
    hidden_because: Mapping[str, str] = {
        "list_schemas": "it lists schemas your profiles don't grant",
        "list_objects": "it lists tables your profiles don't grant",
        "get_object_details": "it shows columns your profiles hide",
        "explain_query": "its plans and row counts can describe hidden columns",
        "analyze_workload_indexes": "it reads other sessions' queries",
        "analyze_query_indexes": "its plans and row counts can describe hidden columns",
        "analyze_db_health": "it reports on tables your profiles don't grant",
        "get_top_queries": "it shows other sessions' SQL",
    }
    # the tables and columns the login can see in its current schema; views count as derived (a view can read a
    # hidden column under another name), and so does a partition or inheritance child (its rows are its parent's)
    catalog_sql = (
        "SELECT c.table_schema, c.table_name, c.column_name, c.data_type, t.table_type, "
        "(SELECT string_agg(p.relname, ',' ORDER BY i.inhseqno) FROM pg_catalog.pg_inherits i "
        "JOIN pg_catalog.pg_class k ON k.oid = i.inhrelid JOIN pg_catalog.pg_class p ON p.oid = i.inhparent "
        "JOIN pg_catalog.pg_namespace n ON n.oid = k.relnamespace "
        "WHERE k.relname = c.table_name AND n.nspname = c.table_schema) AS parents "
        "FROM information_schema.columns c JOIN information_schema.tables t "
        "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = current_schema() ORDER BY c.table_name, c.ordinal_position"
    )

    def settings(self, command: Sequence[str], url: str | None, env: Mapping[str, str]) -> dict[str, Any]:
        if url or not command:
            raise SourceError("Postgres MCP runs on this computer: give the command that starts it after --, for "
                              "example: -- uvx postgres-mcp")
        return {"command": list(command), "env": dict(env)}

    def open_child(self, settings: Mapping[str, Any], secret: str) -> ChildServer:
        command = settings.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(p, str) for p in command):
            raise SourceError("no command is set up for this wrapped server; set it up again with portward-free "
                              "wrap-add")
        env = {str(k): str(v) for k, v in (settings.get("env") or {}).items()}
        return ChildServer([command[0], *self.child_args(command[1:])], {**env, self.secret_env: secret})

    def child_args(self, args: Sequence[str]) -> list[str]:
        kept = [a for a in args if not a.startswith("--access-mode")]
        return [*kept, "--access-mode=restricted"]

    def sql_argument(self, tool: Tool) -> str | None:
        return self.sql_tools.get(tool.name)

    def why_hidden(self, tool: Tool) -> str:
        return self.hidden_because.get(tool.name, UNTIED)

    def rows(self, result: CallToolResult) -> Fetched:
        text = _text(result).strip()
        if result.is_error or text.startswith("Error:"):
            raise SourceError(failure(text))
        if text == "No results":
            return Fetched((), ())
        try:
            found = rows_from_repr(text)
        except NotReadable:
            raise SourceError(UNREADABLE) from None
        return _from_dicts(found)

    def catalog(self, result: CallToolResult) -> tuple[Table, ...]:
        fetched = self.rows(result)
        index = {c: i for i, c in enumerate(fetched.columns)}
        tables: dict[str, dict[str, Any]] = {}
        for row in fetched.rows:
            name = str(row[index["table_name"]])
            entry = tables.setdefault(name, {"columns": [], "schema": str(row[index["table_schema"]] or ""),
                                             "type": row[index["table_type"]],
                                             "parents": row[index["parents"]], "numeric": []})
            column = str(row[index["column_name"]])
            entry["columns"].append(column)
            # a column whose type isn't said isn't a number: totals on it falls to hidden, never to full
            if "data_type" in index and is_numeric_type(row[index["data_type"]]):
                entry["numeric"].append(column)
        return tuple(Table(name, tuple(e["columns"]), derived=e["type"] != "BASE TABLE" or bool(e["parents"]),
                           parents=tuple(str(e["parents"]).split(",")) if e["parents"] else (),
                           schema=e["schema"], numeric=frozenset(e["numeric"]))
                     for name, e in tables.items())

    def looks_like(self, line: Sequence[str]) -> bool:
        return any("postgres-mcp" in part or "postgres_mcp" in part for part in line)


def _from_dicts(found: Sequence[Any]) -> Fetched:
    if not all(isinstance(r, dict) for r in found):
        raise SourceError(UNREADABLE)
    columns = tuple(found[0]) if found else ()
    if any(tuple(r) != columns for r in found):
        raise SourceError(UNEVEN)
    return Fetched(columns, tuple(tuple(r.values()) for r in found))


LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def https_url(url: str | None) -> str:
    """`url` if it's https (or http to this computer itself), since the login travels with every request."""
    parts = urlsplit((url or "").strip())
    if not parts.hostname or not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK)):
        raise SourceError("give the MCP server's https URL, for example https://<account>.snowflakecomputing.com/"
                          "api/v2/databases/<db>/schemas/<schema>/mcp-servers/<name>")
    if parts.username or parts.password:
        raise SourceError("keep the token out of the URL: wrap-add asks for it and keeps it in the keychain")
    return parts.geturl()


class SnowflakeMcp:
    """Snowflake's MCP servers, either of two:

    - The Snowflake-managed MCP server (CREATE MCP SERVER ... FROM SPECIFICATION), given by its URL: remote,
      streamable HTTP, with a programmatic access token (or OAuth token) as the bearer. Its tool names are whatever
      the specification's author chose, so tools are told apart by their input: a SYSTEM_EXECUTE_SQL tool takes one
      string, `sql`. Cortex agents and coding agents (`text`, `thread_id`...; plus the run status/result/cancel tools
      Snowflake adds beside them), Cortex Search (`query`, `columns`, `limit`), Cortex Analyst (`message`) and custom
      functions or procedures stay hidden. See docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-mcp.
    - Snowflake-Labs/mcp (`uvx snowflake-labs-mcp`), given by its command: run over stdio. Its SQL tool is
      `run_snowflake_query`, taking `statement`; Portward starts it with a service config of its own that turns on
      only that tool and allows only SELECT, and passes the login (a password or programmatic access token) in
      SNOWFLAKE_PASSWORD. Its other tools (Cortex agent, search and analyst, object manager, semantic views) stay
      hidden. See github.com/Snowflake-Labs/mcp (tools as of main 662cb48)."""

    name = "snowflake"
    label = "Snowflake MCP"
    dialect = "snowflake"
    secret_env = "SNOWFLAKE_PAT"
    AGENT_RUN_TOOLS = frozenset({"ASYNC_AGENT_RUN_STATUS", "AGENT_RUN_RESULT", "ASYNC_AGENT_RUN_CANCEL"})
    LABS_SQL_TOOL = "run_snowflake_query"
    # Snowflake-Labs/mcp options Portward sets itself, or that would put a login in the saved command
    LABS_OWN = ("--service-config-file", "--transport")
    LABS_LOGIN = ("--password", "--pat", "--passcode", "--private-key", "--private-key-file",
                  "--private-key-file-pwd")
    LABS_CONFIG = {"other_services": {"query_manager": True, "object_manager": False, "semantic_manager": False},
                   "sql_statement_permissions": [{"Select": True}]}  # JSON is YAML, which it reads
    AGENT = "it's a Cortex agent: it reads data and writes its own answer, which Portward can't tie to granted columns"
    SEARCH = "it's a Cortex Search service: it returns indexed text, which Portward can't tie to granted columns"
    ANALYST = ("it's Cortex Analyst: it writes and runs its own SQL over a semantic model, out of reach of Portward's "
               "rewrite")
    CUSTOM = ("it runs a custom function or procedure in Snowflake, and Portward can't tie what it returns to the "
              "columns your profiles grant")
    OBJECTS = "it creates, changes or drops Snowflake objects, and the AI only reads through Portward"
    CATALOG = "it lists or describes objects and columns your profiles may not grant"
    SEMANTIC = ("it writes or runs queries over a semantic view, out of reach of Portward's rewrite")
    LABS_HIDDEN: Mapping[str, str] = {
        "cortex_agent": AGENT, "cortex_search": SEARCH, "cortex_analyst": ANALYST,
        "create_object": OBJECTS, "drop_object": OBJECTS, "create_or_alter_object": OBJECTS,
        "describe_object": CATALOG, "list_objects": CATALOG, "list_semantic_views": CATALOG,
        "describe_semantic_view": CATALOG, "show_semantic_dimensions": CATALOG, "show_semantic_metrics": CATALOG,
        "get_semantic_view_ddl": CATALOG, "write_semantic_view_query_tool": SEMANTIC, "query_semantic_view": SEMANTIC,
    }
    # tables, views and their columns in the login's default schema (a matching login's, #99: its secure views are
    # named like the tables). A plain view counts as derived (it can read a hidden column under another name); a
    # secure view there is what the matching login was given, so it isn't.
    catalog_sql = (
        "SELECT c.table_schema, c.table_name, c.column_name, c.data_type, t.table_type, v.is_secure "
        "FROM information_schema.columns c JOIN information_schema.tables t "
        "ON t.table_catalog = c.table_catalog AND t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "LEFT JOIN information_schema.views v "
        "ON v.table_catalog = c.table_catalog AND v.table_schema = c.table_schema AND v.table_name = c.table_name "
        "WHERE c.table_schema = CURRENT_SCHEMA() ORDER BY c.table_name, c.ordinal_position"
    )
    TABLE_TYPES = frozenset({"BASE TABLE", "TEMPORARY TABLE", "EXTERNAL TABLE"})
    # for the matching login's drift check (#99): the session's database (the MCP server object's database in its
    # URL can be another), and how many tables the login can see in that database's other schemas
    # (information_schema shows what the current role has privileges on: docs.snowflake.com/en/sql-reference/
    # info-schema)
    database_sql = "SELECT CURRENT_DATABASE() AS database_name"
    elsewhere_sql = ("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema <> CURRENT_SCHEMA() "
                     "AND table_schema <> 'INFORMATION_SCHEMA'")

    def settings(self, command: Sequence[str], url: str | None, env: Mapping[str, str]) -> dict[str, Any]:
        if command and url:
            raise SourceError("give either the Snowflake-managed MCP server's https URL with --url, or the command "
                              "that starts Snowflake-Labs/mcp after --, not both")
        if command:
            return {"command": self._labs_command(command), "env": dict(env)}
        found = https_url(url)
        database = re.search(r"/databases/([^/]+)/", urlsplit(found).path)
        return {"url": found, **({"database": unquote(database.group(1))} if database else {})}

    def _labs_command(self, command: Sequence[str]) -> list[str]:
        """The command without the options Portward sets itself or that carry a login (and their values)."""
        dropped = (*self.LABS_OWN, *self.LABS_LOGIN)
        kept, skip = [], False
        for part in command:
            if skip:
                skip = False
            elif part in dropped:
                skip = True
            elif not part.startswith(tuple(f"{d}=" for d in dropped)):
                kept.append(part)
        return kept

    def open_child(self, settings: Mapping[str, Any], secret: str) -> ChildServer:
        command = settings.get("command")
        if command is not None:
            return self._open_labs(command, settings.get("env") or {}, secret)
        url = settings.get("url")
        return ChildServer.over_http(https_url(url if isinstance(url, str) else None),
                                     {"Authorization": f"Bearer {secret}"})

    def _open_labs(self, command: Any, env: Mapping[str, Any], secret: str) -> ChildServer:
        if not isinstance(command, list) or not command or not all(isinstance(p, str) for p in command):
            raise SourceError("no command is set up for this wrapped server; set it up again with portward-free "
                              "wrap-add")
        folder = tempfile.TemporaryDirectory(prefix="portward-snowflake-")
        config = Path(folder.name) / "portward_tools_config.yaml"
        config.write_text(json.dumps(self.LABS_CONFIG), encoding="utf-8")
        args = [*self._labs_command(command), "--service-config-file", str(config), "--transport", "stdio"]
        try:
            child = ChildServer(args, {**{str(k): str(v) for k, v in env.items()}, "SNOWFLAKE_PASSWORD": secret})
        except BaseException:
            folder.cleanup()
            raise
        child.on_close(folder.cleanup)
        return child

    @staticmethod
    def _inputs(tool: Tool) -> Mapping[str, Any]:
        found = (tool.input_schema or {}).get("properties")
        return found if isinstance(found, dict) else {}

    def sql_argument(self, tool: Tool) -> str | None:
        inputs = self._inputs(tool)
        argument = "statement" if tool.name == self.LABS_SQL_TOOL else "sql"
        found = inputs.get(argument)
        if set(inputs) == {argument} and isinstance(found, dict) and found.get("type") == "string":
            return argument
        return None

    def why_hidden(self, tool: Tool) -> str:
        if tool.name in self.LABS_HIDDEN:
            return self.LABS_HIDDEN[tool.name]
        inputs = set(self._inputs(tool))
        agent_run = "text" in inputs and bool(inputs & {"thread_id", "parent_message_id"})
        if tool.name.upper() in self.AGENT_RUN_TOOLS or agent_run:
            return self.AGENT
        if "query" in inputs and inputs & {"columns", "limit", "filter"}:
            return self.SEARCH
        if "message" in inputs:
            return self.ANALYST
        return self.CUSTOM

    def rows(self, result: CallToolResult) -> Fetched:
        text = _text(result).strip()
        if result.is_error:
            raise SourceError(failure(text))
        try:
            payload = json.loads(text)
        except ValueError:
            payload = result.structured_content
        return self._table(payload, wrapped=True)

    def _table(self, payload: Any, wrapped: bool = False) -> Fetched:
        if isinstance(payload, list):
            return _from_dicts(payload)
        if not isinstance(payload, dict):
            raise SourceError(UNREADABLE)
        if "data" in payload and "resultSetMetaData" in payload:  # the shape of Snowflake's SQL API
            return self._result_set(payload["resultSetMetaData"], payload["data"])
        if wrapped and len(payload) == 1:  # {"result_set": {...}} and the like: one level only
            return self._table(next(iter(payload.values())))
        raise SourceError(UNREADABLE)

    @staticmethod
    def _result_set(meta: Any, data: Any) -> Fetched:
        types = meta.get("rowType") if isinstance(meta, dict) else None
        if not isinstance(types, list) or not isinstance(data, list) \
                or not all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in types):
            raise SourceError(UNREADABLE)
        columns = tuple(t["name"] for t in types)
        if not all(isinstance(r, list) and len(r) == len(columns) for r in data):
            raise SourceError(UNEVEN)
        return Fetched(columns, tuple(tuple(r) for r in data))

    def catalog(self, result: CallToolResult) -> tuple[Table, ...]:
        fetched = self.rows(result)
        index = {c.lower(): i for i, c in enumerate(fetched.columns)}
        tables: dict[str, dict[str, Any]] = {}
        try:
            for row in fetched.rows:
                name = str(row[index["table_name"]])
                kind, secure = str(row[index["table_type"]] or ""), str(row[index["is_secure"]] or "")
                derived = kind not in self.TABLE_TYPES and not (kind == "VIEW" and secure.upper() == "YES")
                entry = tables.setdefault(name, {"columns": [], "numeric": [], "derived": derived,
                                                 "schema": str(row[index["table_schema"]] or "")})
                column = str(row[index["column_name"]])
                entry["columns"].append(column)
                if is_numeric_type(row[index["data_type"]]):
                    entry["numeric"].append(column)
        except KeyError:
            raise SourceError(UNREADABLE) from None
        return tuple(Table(name, tuple(e["columns"]), derived=e["derived"], schema=e["schema"],
                           numeric=frozenset(e["numeric"])) for name, e in tables.items())

    def looks_like(self, line: Sequence[str]) -> bool:
        markers = ("snowflake-labs-mcp", "mcp_server_snowflake", "mcp-server-snowflake")
        return any(("snowflakecomputing.com" in part and "/mcp-servers/" in part)
                   or any(m in part for m in markers) for part in line)


PROVIDERS: Mapping[str, Provider] = {"postgres": PostgresMcp(), "snowflake": SnowflakeMcp()}
KINDS = frozenset(f"{KIND_PREFIX}{name}" for name in PROVIDERS)


def provider_for(kind: str) -> Provider:
    found = PROVIDERS.get(kind.removeprefix(KIND_PREFIX)) if kind.startswith(KIND_PREFIX) else None
    if found is None:
        raise SourceError(f"{kind!r} isn't a server Portward can wrap; it wraps: {', '.join(sorted(PROVIDERS))}")
    return found
