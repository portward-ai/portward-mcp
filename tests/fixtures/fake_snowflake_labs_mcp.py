"""A stand-in for Snowflake-Labs/mcp (`uvx snowflake-labs-mcp`), run as a stdio MCP server by the wrap tests. No
Snowflake account: a SQLite file is behind it.

Shaped like the real one at github.com/Snowflake-Labs/mcp, main 662cb48 (2026-05-15):
- options --account, --user, --role, --warehouse, --password/--pat, --service-config-file, --transport; the login
  can come from SNOWFLAKE_PASSWORD;
- the service config file (YAML; JSON is YAML too) decides which tools exist: `other_services` turns on
  query_manager (`run_snowflake_query`, argument `statement`), object_manager (create_object, drop_object,
  create_or_alter_object, describe_object, list_objects) and semantic_manager (list_semantic_views,
  describe_semantic_view, show_semantic_dimensions, show_semantic_metrics, get_semantic_view_ddl,
  write_semantic_view_query_tool, query_semantic_view); agent_services / search_services / analyst_services add
  cortex_agent, cortex_search and cortex_analyst;
- `sql_statement_permissions` is checked before run_snowflake_query runs (sqlglot's statement type, as the real
  CheckQueryType middleware does); with none allowed, nothing runs;
- run_snowflake_query returns the rows as a list of dicts (FastMCP sends it as JSON text).

Every statement it receives, the config it was given, the options it was started with and whether a password was in
its environment are appended to FAKE_MCP_LOG, one JSON line each.

Run: python fake_snowflake_labs_mcp.py [options]   (env FAKE_SNOWFLAKE_DB, FAKE_MCP_LOG, SNOWFLAKE_PASSWORD)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any

import sqlglot
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DB = os.environ.get("FAKE_SNOWFLAKE_DB", "")
LOG = os.environ.get("FAKE_MCP_LOG")
ARGS = sys.argv[1:]


def _option(name: str) -> str | None:
    for i, arg in enumerate(ARGS):
        if arg == name and i + 1 < len(ARGS):
            return ARGS[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _log(entry: dict[str, Any]) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as out:
            out.write(json.dumps(entry) + "\n")


CONFIG_PATH = _option("--service-config-file")
CONFIG: dict[str, Any] = json.loads(open(CONFIG_PATH).read()) if CONFIG_PATH else {}
OTHER = CONFIG.get("other_services") or {}
ALLOWED, DISALLOWED = [], []
for entry in CONFIG.get("sql_statement_permissions") or []:
    for kind, allowed in entry.items():
        (ALLOWED if allowed else DISALLOWED).append(kind.lower())
_log({"started": ARGS, "config": CONFIG, "password_in_env": bool(os.environ.get("SNOWFLAKE_PASSWORD"))})

server = MCPServer("Snowflake MCP Server")


def _catalog(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = []
    for name, kind in db.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        for column in db.execute(f'PRAGMA table_info("{name}")').fetchall():
            rows.append({"TABLE_SCHEMA": "ANALYTICS", "TABLE_NAME": name.upper(), "COLUMN_NAME": column[1].upper(),
                         "DATA_TYPE": "NUMBER" if column[2].upper() in ("INTEGER", "REAL") else "TEXT",
                         "TABLE_TYPE": "VIEW" if kind == "view" else "BASE TABLE", "IS_SECURE": None})
    return rows


def run_snowflake_query(statement: str) -> str:
    _log({"sql": statement})
    try:
        kind = type(sqlglot.parse_one(statement, dialect="snowflake")).__name__.lower()
    except sqlglot.errors.ParseError:
        kind = "unknown"
    if "all" not in ALLOWED and (kind in DISALLOWED or kind not in ALLOWED):
        raise ToolError(f"Statement type of {kind} is not allowed. Please review sql statement permissions in "
                        "configuration file.")
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        lowered = statement.lower()
        if "current_database()" in lowered and "information_schema" not in lowered:
            return json.dumps([{"DATABASE_NAME": "SHOP"}])
        if "<> current_schema()" in lowered:
            return json.dumps([{"N": 0}])
        if "information_schema" in lowered:
            return json.dumps(_catalog(db))
        try:
            cursor = db.execute(sqlglot.transpile(statement, read="snowflake", write="sqlite")[0])
        except sqlite3.OperationalError as err:
            raise ToolError(f"Error executing query: {err}") from None
        names = [d[0] for d in cursor.description or ()]
        return json.dumps([dict(zip(names, row, strict=True)) for row in cursor.fetchall()])


def cortex_agent(service_name: str, database_name: str, schema_name: str, query: str) -> str:
    return "Grace's ssn is 987-65-4321"


def cortex_search(service_name: str, database_name: str, schema_name: str, query: str,
                  columns: list[str] | None = None, filter_query: dict[str, Any] | None = None,
                  limit: int = 10) -> str:
    return "Customer Grace, ssn 987-65-4321"


def cortex_analyst(service_name: str, semantic_model: str, query: str) -> str:
    return "SELECT ssn FROM customers -> 987-65-4321"


def _object_tool(object_type: str, target_object: Any) -> str:
    return "done"


def _semantic_tool(database_name: str, schema_name: str, view_name: str = "") -> str:
    return "Grace, 987-65-4321"


if OTHER.get("query_manager"):
    server.add_tool(run_snowflake_query, name="run_snowflake_query", description="Run a SQL query in Snowflake")
if OTHER.get("object_manager"):
    for name in ("create_object", "drop_object", "create_or_alter_object", "describe_object", "list_objects"):
        server.add_tool(_object_tool, name=name, description=name)
if OTHER.get("semantic_manager"):
    for name in ("list_semantic_views", "describe_semantic_view", "show_semantic_dimensions", "show_semantic_metrics",
                 "get_semantic_view_ddl", "write_semantic_view_query_tool", "query_semantic_view"):
        server.add_tool(_semantic_tool, name=name, description=name)
if CONFIG.get("agent_services"):
    server.add_tool(cortex_agent, name="cortex_agent", description="Cortex agent")
if CONFIG.get("search_services"):
    server.add_tool(cortex_search, name="cortex_search", description="Cortex search")
if CONFIG.get("analyst_services"):
    server.add_tool(cortex_analyst, name="cortex_analyst", description="Cortex analyst")


if __name__ == "__main__":
    if _option("--transport") not in (None, "stdio"):
        sys.exit("this fake only speaks stdio")
    server.run("stdio")
