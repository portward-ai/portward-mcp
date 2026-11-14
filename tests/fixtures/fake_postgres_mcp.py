"""A stand-in for Postgres MCP Pro (crystaldba/postgres-mcp), run as a stdio MCP server by the wrap tests.

It answers the way the real server does: `execute_sql` returns one text block holding the Python repr of a list of
row dicts (or "Error: <message>"), and the other tools return free text Portward can't tie to granted data. Behind it
is a SQLite file (DATABASE_URI=sqlite:///<path>), or a real Postgres (DATABASE_URI=postgresql://...) for the
integration test. Every SQL text it receives is appended to FAKE_MCP_LOG, one JSON line each, so a test can see what
reached the database. The information_schema catalog query is answered from SQLite's own schema.

Run: python fake_postgres_mcp.py [--access-mode=restricted|unrestricted]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

URI = os.environ.get("DATABASE_URI", "")
LOG = os.environ.get("FAKE_MCP_LOG")
ACCESS_MODE = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--access-mode=")), "unrestricted")

server = MCPServer("postgres-mcp")


def _log(sql: str) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as out:
            out.write(json.dumps({"sql": sql, "access_mode": ACCESS_MODE}) + "\n")


def _sqlite_catalog(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = []
    for name, kind in db.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        for column in db.execute(f'PRAGMA table_info("{name}")').fetchall():
            rows.append({"table_schema": "public", "table_name": name, "column_name": column[1],
                         "data_type": column[2].lower(), "table_type": "VIEW" if kind == "view" else "BASE TABLE", "parents": None})
    return rows


def _run(sql: str) -> list[dict[str, Any]]:
    if URI.startswith("sqlite:///"):
        with sqlite3.connect(f"file:{URI.removeprefix('sqlite:///')}?mode=ro", uri=True) as db:
            if "information_schema" in sql:
                return _sqlite_catalog(db)
            cursor = db.execute(sql)
            names = [d[0] for d in cursor.description or ()]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(URI, row_factory=dict_row) as db:
        db.read_only = True
        return list(db.execute(sql.encode()).fetchall())


@server.tool(description="Execute any SQL query")
def execute_sql(sql: str) -> str:
    _log(sql)
    try:
        return str(_run(sql))
    except Exception as err:  # noqa: BLE001 - the real server answers every failure as text
        return f"Error: {err}"


@server.tool(description="List objects in a schema, with their columns")
def list_objects(schema_name: str = "public") -> str:
    return str(_run("SELECT * FROM information_schema.columns"))


@server.tool(description="Analyzes database health")
def analyze_db_health(health_type: str = "all") -> str:
    return "Everything is fine. Biggest value seen in customers.ssn: 987-65-4321"


@server.tool(description="A tool this provider added later")
def summarize_everything() -> str:
    return "A summary that quotes every row"


if __name__ == "__main__":
    server.run("stdio")
