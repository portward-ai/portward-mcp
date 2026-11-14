"""The first-run guide (get_started): connect a first source, then a first pull, then what else there is.

It reads where the user is (no source yet, a source but no pull, done) and says the one next step. A user with no
file to hand can connect the sample: a small SQLite file written to the app-data folder, so the first pull works
on any machine.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlglot import exp

from portward_free.sources.base import Table

SAMPLE_NAME = "sample"
SAMPLE_FILE = "sample.sqlite"
SAMPLE_QUESTION = "Revenue and number of orders by region"
SAMPLE_SQL = ("SELECT c.region, COUNT(*) AS orders, SUM(o.total) AS revenue FROM orders o "
              "JOIN customers c ON c.id = o.customer_id GROUP BY c.region ORDER BY revenue DESC")
CONNECT, FIRST_PULL = "connect a source", "first pull"
FIRST_ROWS = 10
_CUSTOMERS = [(1, "Northwind Traders", "north"), (2, "Southgate Supply", "south"), (3, "Eastfield Foods", "east"),
              (4, "Westbrook Tools", "west"), (5, "Northpoint Retail", "north"), (6, "Sunvale Market", "south")]

CONNECT_TEXT = (
    "Step 1 of 2: connect a source. Ask the user where their data is, then call add_source:\n"
    "- a SQLite file: kind 'sqlite', path to the file;\n"
    "- a CSV file: kind 'csv' (one table, named after the file); an Excel workbook: kind 'excel' (one table per "
    "sheet);\n"
    "- Postgres or MySQL: kind 'postgres' or 'mysql', as add_source describes.\n"
    "No file to hand? Call get_started with use_sample=true to connect a small sample database."
)
NEXT_TEXT = (
    "Both steps done. From here: history lists past pulls and rerun repeats one exactly; tweak changes one and shows "
    "what changed; save_pull names a pull to run again; describe_source and set_note record what tables and columns "
    "mean; export writes a pull to CSV or Excel; sent_to_ai shows what the AI was sent."
)


@dataclass(frozen=True)
class Progress:
    sources: tuple[str, ...]
    pulls: int


def write_sample(folder: Path) -> Path:
    """The sample SQLite file (customers and orders), rewritten each time so it is always the same."""
    path = folder / SAMPLE_FILE
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL)")
            db.executemany("INSERT INTO customers VALUES (?, ?, ?)", _CUSTOMERS)
            db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL "
                       "REFERENCES customers (id), ordered_on TEXT NOT NULL, total REAL NOT NULL)")
            db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)",
                           [(i, i % len(_CUSTOMERS) + 1, f"2026-{i % 12 + 1:02d}-{i % 28 + 1:02d}",
                             round(20 + (i * 37) % 480 + (i % 7) * 0.25, 2)) for i in range(1, 121)])
    finally:
        db.close()
    return path


def first_query(source: str, table: Table, dialect: str) -> dict[str, str]:
    """A first pull for a source: the sample's own question, or the first rows of a table it may show the AI
    (`table` as block rules leave it, so only its allowed columns are named)."""
    if source == SAMPLE_NAME:
        return {"source": source, "sql": SAMPLE_SQL, "question": SAMPLE_QUESTION}
    columns = [exp.to_identifier(c, quoted=not c.isidentifier()) for c in table.columns]
    sql = (exp.select(*columns).from_(exp.to_table(table.name, dialect=dialect)).limit(FIRST_ROWS)
           .sql(dialect=dialect))
    return {"source": source, "sql": sql, "question": f"The first {FIRST_ROWS} rows of {table.name}"}


def guide(progress: Progress, suggestion: dict[str, str] | None) -> tuple[str, dict[str, Any]]:
    """The guide's text for the AI and its payload: the steps done, the next one, a query to try. It describes Free
    only: no team or governance pitch on first run (#115)."""
    steps = [{"step": CONNECT, "done": bool(progress.sources)}, {"step": FIRST_PULL, "done": progress.pulls > 0}]
    upcoming = next((s["step"] for s in steps if not s["done"]), None)
    if upcoming == CONNECT:
        text = CONNECT_TEXT
    elif upcoming == FIRST_PULL:
        text = (f"Step 2 of 2: a first pull. Connected: {', '.join(progress.sources)}. Ask the user what they want "
                "to know, or try the query in `try` with the query tool: the rows show to the user first.")
    else:
        text = NEXT_TEXT
    payload: dict[str, Any] = {"steps": steps, "next": upcoming, "sources": list(progress.sources)}
    if suggestion is not None and upcoming != CONNECT:
        payload["try"] = suggestion
    return text, payload
