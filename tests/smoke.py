"""The smoke test every shipped form of the server passes: the package from `uvx`, and the compiled `.mcpb`.

It starts the server the way an install does, then checks what most easily goes missing in a packaged build: the
version, the panel page (package data), and one pull from each kind of source (SQLite, CSV through DuckDB, Excel
through openpyxl), recorded to the local database in the app-data folder.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.types import ElicitResult
from openpyxl import Workbook

from portward_free.paths import database_path

PANEL_URI = "ui://portward/results.html"


def _files(folder: Path, shop_db: Path) -> dict[str, tuple[str, Path, str]]:
    """One source per kind: name -> (kind, path, a query and its expected single value)."""
    folder.mkdir(parents=True, exist_ok=True)
    csv = folder / "sales.csv"
    csv.write_text("region,revenue\nnorth,1200.5\nsouth,800\n")
    book = Workbook()
    book.active.title = "Costs"
    book.active.append(["item", "amount"])
    book.active.append(["laptops", 2400.5])
    xlsx = folder / "budget.xlsx"
    book.save(xlsx)
    return {
        "shop": ("sqlite", shop_db, "SELECT COUNT(*) AS n FROM orders"),
        "sales": ("csv", csv, "SELECT COUNT(*) AS n FROM sales"),
        "budget": ("excel", xlsx, "SELECT COUNT(*) AS n FROM costs"),
    }


async def _user_says_yes(context: object, params: object) -> ElicitResult:
    """The user adds each file: Portward asks before the AI may read a file (file_sources)."""
    return ElicitResult(action="accept", content={"yes": True})


async def smoke_test(server: StdioServerParameters, home: Path, shop_db: Path, *, version: str) -> None:
    """Run the smoke test against a server launched with `home` as its app-data folder."""
    sources = _files(shop_db.parent / "smoke-files", shop_db)
    counts = {}
    async with Client(server, elicitation_callback=_user_says_yes) as client:
        assert (client.server_info.name, client.server_info.version) == ("portward", version)
        page = await client.read_resource(PANEL_URI)
        assert "<html" in page.contents[0].text.lower()
        for name, (kind, path, sql) in sources.items():
            added = await client.call_tool("add_source", {"name": name, "kind": kind, "path": str(path)})
            assert not added.is_error, added.content
            reply = await client.call_tool("query", {"source": name, "question": f"How many in {name}", "sql": sql})
            assert not reply.is_error, reply.content
            counts[name] = reply.structured_content["rows"][0][0]
    assert counts == {"shop": 250, "sales": 2, "budget": 1}
    db = sqlite3.connect(database_path(home))
    try:
        assert {r[0] for r in db.execute("SELECT source FROM pulls")} == set(sources)
    finally:
        db.close()
