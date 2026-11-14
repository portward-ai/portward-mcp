"""A stand-in for a Snowflake-managed MCP server (CREATE MCP SERVER ... FROM SPECIFICATION), run over streamable
HTTP by the wrap tests. No Snowflake account and no network: it listens on 127.0.0.1 with a SQLite file behind it.

It is shaped like the real one (docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-mcp and Snowflake's
"Getting started with Snowflake MCP Server" guide):
- remote, streamable HTTP, a PAT (or OAuth token) as `Authorization: Bearer ...`; anything else gets 401;
- tool names are the ones the spec's author chose, so they say nothing about the type. Here: a SYSTEM_EXECUTE_SQL
  tool `sql_exec_tool` (takes `sql`), a CORTEX_SEARCH_SERVICE_QUERY tool `product-search` (query, columns, limit),
  a CORTEX_AGENT_RUN tool `business_data_agent` (text, thread_id, parent_message_id) with the three tools Snowflake
  adds beside an agent (ASYNC_AGENT_RUN_STATUS, AGENT_RUN_RESULT, ASYNC_AGENT_RUN_CANCEL), a CORTEX_ANALYST_MESSAGE
  tool `revenue-analyst` (message), and a GENERIC procedure `lookup_customer`;
- names are upper case, as Snowflake folds unquoted identifiers; SQL errors read like Snowflake's.

The SQL tool's result is JSON in one text block: `resultSetMetaData.rowType` names and `data` rows (the shape of
Snowflake's SQL API). Snowflake's SQL (as Portward rewrites it) is run on SQLite through sqlglot. Every SQL text it
receives is appended to FAKE_MCP_LOG, one JSON line each. Views named in FAKE_SECURE_VIEWS are reported as secure
views (a matching login, #99, reads secure views named like the tables).

Run: python fake_snowflake_mcp.py <port>   (env FAKE_SNOWFLAKE_DB, FAKE_SNOWFLAKE_TOKEN, FAKE_MCP_LOG)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from typing import Any

import sqlglot
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.types import ASGIApp, Receive, Scope, Send

DB = os.environ.get("FAKE_SNOWFLAKE_DB", "")
TOKEN = os.environ.get("FAKE_SNOWFLAKE_TOKEN", "")
LOG = os.environ.get("FAKE_MCP_LOG")
SECURE = {v.strip().lower() for v in os.environ.get("FAKE_SECURE_VIEWS", "").split(",") if v.strip()}
TYPES = {"INTEGER": "NUMBER", "REAL": "FLOAT", "TEXT": "TEXT"}

server = MCPServer("snowflake-mcp")


def _log(sql: str) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as out:
            out.write(json.dumps({"sql": sql}) + "\n")


def _catalog(db: sqlite3.Connection) -> tuple[list[str], list[list[Any]]]:
    rows = []
    for name, kind in db.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        for column in db.execute(f'PRAGMA table_info("{name}")').fetchall():
            secure = "YES" if name.lower() in SECURE else "NO"
            rows.append(["ANALYTICS", name.upper(), column[1].upper(), TYPES.get(column[2].upper(), "TEXT"),
                         "VIEW" if kind == "view" else "BASE TABLE", secure if kind == "view" else None])
    return ["TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "TABLE_TYPE", "IS_SECURE"], rows


def _run(sql: str) -> tuple[list[str], list[list[Any]]]:
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as db:
        if "current_database()" in sql.lower() and "information_schema" not in sql.lower():
            return ["DATABASE"], [["SHOP"]]  # the session's database (not the MCP server object's, SALES)
        if "<> current_schema()" in sql.lower():  # tables the login reads in the database's other schemas
            return ["N"], [[2]]
        if "information_schema" in sql.lower():
            return _catalog(db)
        try:
            cursor = db.execute(sqlglot.transpile(sql, read="snowflake", write="sqlite")[0])
        except sqlite3.OperationalError as err:
            missing = re.search(r"no such column: ([\w.]+)", str(err))
            if missing:
                raise ToolError("SQL compilation error: error line 1 at position 7\ninvalid identifier "
                                   f"'{missing.group(1).upper()}'") from None
            raise ToolError(f"SQL compilation error: {err}") from None
        names = [d[0] for d in cursor.description or ()]
        return names, [list(r) for r in cursor.fetchall()]


def sql_exec_tool(sql: str) -> str:
    _log(sql)
    names, rows = _run(sql)
    return json.dumps({"resultSetMetaData": {"numRows": len(rows), "rowType": [{"name": n} for n in names]},
                       "data": rows})


def product_search(query: str, columns: list[str] | None = None, limit: int = 10) -> str:
    return json.dumps({"results": [{"chunk": "Customer Grace, ssn 987-65-4321"}]})


def business_data_agent(text: str, thread_id: int | None = None, parent_message_id: int | None = None) -> str:
    return "Grace's ssn is 987-65-4321"


def run_status(run_id: str) -> str:
    return "done"


def run_result(run_id: str) -> str:
    return "Grace's ssn is 987-65-4321"


def run_cancel(run_id: str) -> str:
    return "cancelled"


def revenue_analyst(message: str) -> str:
    return "SELECT ssn FROM customers -> 987-65-4321"


def lookup_customer(customer_id: int) -> str:
    return "Grace, 987-65-4321"


for fn, name in ((sql_exec_tool, "sql_exec_tool"), (product_search, "product-search"),
                 (business_data_agent, "business_data_agent"), (run_status, "ASYNC_AGENT_RUN_STATUS"),
                 (run_result, "AGENT_RUN_RESULT"), (run_cancel, "ASYNC_AGENT_RUN_CANCEL"),
                 (revenue_analyst, "revenue-analyst"), (lookup_customer, "lookup_customer")):
    server.add_tool(fn, name=name, description=f"{name} from the MCP server's specification")


class BearerOnly:
    """Snowflake refuses a request without a token it issued (here: the one in FAKE_SNOWFLAKE_TOKEN)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {TOKEN}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"message": "Invalid OAuth access token."}'})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    port = int(sys.argv[1])
    path = "/api/v2/databases/SALES/schemas/ANALYTICS/mcp-servers/PORTWARD_TEST"
    app = server.streamable_http_app(streamable_http_path=path)
    uvicorn.run(BearerOnly(app), host="127.0.0.1", port=port, log_level="warning")
