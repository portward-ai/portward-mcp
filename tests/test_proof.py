"""Proof (#98): every read writes an AI log entry (when, which AI app, route, profile and version, the tables and
columns with the level each was shown at, how many rows, a hash of the result), never the rows themselves. The user
exports a proof report of the AI log for a date range, as PDF and CSV, with the same facts in both.

Driven through the MCP client harness, signed in to an in-memory account service on Personal (test_profiles)."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness import NO_ACCOUNTS, NO_SHARING, MemoryKeychain
from mcp import Client
from pypdf import PdfReader
from test_profiles import apply, make, tool

from conftest import SECRETS
from portward_free.paths import database_path
from portward_free.proof import DIRECT, AiRead, ShownColumn, UsedProfile, cutoff_day, valid_entry
from portward_free.proof_report import proof_report
from portward_free.server import build_server
from portward_free.storage import LocalStore
from portward_free.usage import Usage


async def test_a_read_through_a_profile_writes_an_ai_log_entry_with_every_field(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Support", ["customers.id", "customers.name"])
        await apply(h, "Support")
        pulled = await h.call("query", source="people", question="who?", sql="SELECT name FROM customers")
        assert not pulled.is_error, pulled.ai.text
        logged = await h.call("ai_log")
    assert not logged.is_error, logged.ai.text
    (entry,) = logged.ai.structured["entries"]
    assert entry["created_at"]
    assert entry["app"]  # the name the AI app gave when it connected
    assert entry["route"] == "desktop"
    assert entry["connection"] == "people"
    assert entry["profiles"] == [{"name": "Support", "version": 1}]
    assert entry["columns"] == [{"table": "customers", "column": "name", "level": "full", "allowed_by": ["Support"]}]
    assert entry["row_count"] == 3
    assert entry["result_hash"] == pulled_hash(home)


async def test_the_ai_log_never_holds_the_rows(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Everything", ["customers.id", "customers.name", "customers.ssn"])
        await apply(h, "Everything")
        pulled = await h.call("query", source="people", question="all", sql="SELECT * FROM customers")
        assert not pulled.is_error and SECRETS[0] in json.dumps(pulled.ai.structured)
        stored = json.dumps(h.stored("ai_reads"))
        logged = await h.call("ai_log")
    (entry,) = logged.ai.structured["entries"]
    assert [(c["column"], c["level"]) for c in entry["columns"]] == [("id", "full"), ("name", "full"), ("ssn", "full")]
    for value in (*SECRETS, "Ada", "Grace", "Linus"):
        assert value not in stored and value not in json.dumps(logged.ai.structured) + logged.ai.text


async def test_a_count_reads_no_column_and_block_rules_name_no_profile(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, plan=None) as h:
        assert not (await h.call("query", source="people", question="how many?",
                                 sql="SELECT count(*) AS n FROM customers")).is_error
        assert not (await h.call("query", source="people", question="totals",
                                 sql="SELECT o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id"
                                 )).is_error
        entries = (await h.call("ai_log")).ai.structured["entries"]
    counted, joined = entries
    assert counted["columns"] == [] and counted["profiles"] == [] and counted["row_count"] == 1
    assert {(c["table"], c["column"]) for c in joined["columns"]} == {
        ("customers", "id"), ("customers", "name"), ("orders", "customer_id"), ("orders", "total"),
        ("orders", "id")}  # o.id isn't read, but `id` is named (c.id): the log says more, never less


async def test_rerun_run_saved_tweak_and_a_rerun_compare_each_write_an_entry(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Support", ["customers.id", "customers.name"])
        await apply(h, "Support")
        first = await h.call("query", source="people", question="who?", sql="SELECT name FROM customers")
        pull_id = first.ai.structured["pull_id"]
        assert not (await h.call("rerun", pull_id=pull_id)).is_error
        assert not (await h.call("save_pull", pull_id=pull_id, name="Names")).is_error
        assert not (await h.call("run_saved", name="Names")).is_error
        tweaked = await h.call("tweak", pull_id=pull_id, sql="SELECT id, name FROM customers")
        assert not tweaked.is_error, tweaked.ai.text
        compared = await h.call("compare", pull_id=pull_id, other_pull_id=tweaked.ai.structured["pull_id"])
        assert not compared.is_error, compared.ai.text
        entries = (await h.call("ai_log")).ai.structured["entries"]
    assert compared.ai.structured["rows_from"] == "rerun"  # no rows kept: both pulls are read again
    assert len(entries) == 4 + 2
    assert all(e["profiles"] == [{"name": "Support", "version": 1}] for e in entries)


async def test_the_route_is_what_the_server_was_built_for(home: Path, people_db: Path) -> None:
    store = LocalStore.open(database_path(home))
    try:
        store.add_connection("people", "sqlite", {"path": str(people_db)})
        server = build_server(store, MemoryKeychain(), usage=Usage(store, None, disabled="off in tests"),
                              accounts=NO_ACCOUNTS, sharing=NO_SHARING, route=DIRECT)  # no files on hosted
        async with Client(server) as client:
            await client.call_tool("query", {"source": "people", "question": "?", "sql": "SELECT id FROM orders"})
        (read,) = store.reads.between()
    finally:
        store.close()
    assert read.route == "direct"
    with pytest.raises(ValueError):
        build_server(LocalStore.memory(), MemoryKeychain(), route="somewhere")


def test_the_ai_log_keeps_a_year(home: Path) -> None:
    store = LocalStore.memory()
    old = AiRead(app="a", route="desktop", connection="c", profiles=(), columns=(), row_count=1, result_hash="h",
                 created_at="2025-11-05T10:00:00+00:00")
    kept_one = AiRead(app="a", route="desktop", connection="c", profiles=(), columns=(), row_count=2,
                      result_hash="h", created_at="2025-11-06T10:00:00+00:00")
    store.reads.add([old, kept_one])
    store.reads.prune(cutoff_day(datetime(2026, 11, 6, 12, tzinfo=UTC)))
    assert [r.row_count for r in store.reads.between()] == [2]


def read_on(day: str, rows: int, app: str = "Claude", profile: str = "Support") -> AiRead:
    return AiRead(app=app, route="desktop", connection="people", profiles=(UsedProfile(profile, 2),),
                  columns=(ShownColumn("customers", "name", "full"), ShownColumn("orders", "total", "full")),
                  row_count=rows, result_hash="ab" * 32, origin="laptop", created_at=f"{day}T09:30:00.000001+00:00")


def pdf_text(path: Path) -> str:
    return " ".join(" ".join(page.extract_text().split()) for page in PdfReader(path).pages)


async def test_the_proof_report_has_the_ai_logs_row_counts_for_the_range_in_pdf_and_csv(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        store = LocalStore.open(database_path(home))
        store.reads.add([read_on("2026-10-31", 7), read_on("2026-11-01", 3), read_on("2026-11-02", 4, app="Cursor"),
                         read_on("2026-11-02", 5, profile="Finance"), read_on("2026-11-03", 11)])
        store.close()
        logged = (await h.call("ai_log", start="2026-11-01", end="2026-11-02")).ai.structured
        exported = await h.call("export_proof_report", start="2026-11-01", end="2026-11-02")
    assert not exported.is_error, exported.ai.text
    report = exported.ai.structured
    assert report["reads"] == logged["reads"] == 3 and report["rows"] == logged["rows"] == 12
    csv_file, pdf_file = Path(report["csv"]), Path(report["pdf"])
    with csv_file.open(newline="", encoding="utf-8") as f:
        lines = list(csv.DictReader(f))
    assert sorted(int(line["rows"]) for line in lines) == [3, 4, 5]
    assert {line["ai_app"] for line in lines} == {"Claude", "Cursor"}
    assert {line["profiles"] for line in lines} == {"Support v2", "Finance v2"}
    assert lines[0]["columns"] == "customers.name (full); orders.total (full)"
    text = pdf_text(pdf_file)
    assert "2026-11-01 to 2026-11-02" in text and "3 reads, 12 rows" in text
    for line in lines:  # the same facts in both
        for value in line.values():
            assert value in text, value
    assert "2026-10-31" not in text and "2026-11-03" not in text  # outside the range


def test_the_proof_report_never_has_the_values(home: Path) -> None:
    """The report is made from the AI log's summary alone: there are no values in it to leak."""
    report = proof_report([read_on("2026-11-01", 3)], "2026-11-01", "2026-11-01")
    assert set(report.lines[0]) == {"time", "ai_app", "route", "connection", "profiles", "columns", "rules", "context",
                                    "rows", "result_hash"}


@pytest.mark.parametrize("plan", [None, "ended"])
async def test_the_proof_report_is_part_of_personal(home: Path, people_db: Path, plan: str | None) -> None:
    async with tool(home, people_db, plan=plan) as h:
        listed = await h.ai_tools()
        refused = await h.call("export_proof_report", start="2026-11-01", end="2026-11-02")
    assert "export_proof_report" not in listed and "ai_log" in listed  # #116: the AI log is Free's too
    assert refused.is_error and "Unknown tool" in refused.ai.text  # #116: not listed on Free
    assert not (home / "exports").exists()


async def test_the_report_refuses_a_range_that_isnt_dates(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        bad = await h.call("export_proof_report", start="last week", end="2026-11-02")
        backwards = await h.call("export_proof_report", start="2026-11-05", end="2026-11-02")
    assert bad.is_error and "date" in bad.ai.text
    assert backwards.is_error and "before" in backwards.ai.text


# --- rules applied (review of #98): per column, the profiles and level that allowed it ------------------------------

async def test_each_column_in_the_ai_log_names_the_profiles_that_allowed_it_at_its_level(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Support", ["customers.id", "customers.name"])
        await make(h, "Ops", ["customers.name"], anonymised=["customers.id"])
        await apply(h, "Support")
        await apply(h, "Ops")
        assert not (await h.call("query", source="people", question="who?",
                                 sql="SELECT id, name FROM customers")).is_error
        (entry,) = (await h.call("ai_log")).ai.structured["entries"]
    assert entry["columns"] == [  # id: full beats Ops's anonymised, so only Support allowed it at full
        {"table": "customers", "column": "id", "level": "full", "allowed_by": ["Support"]},
        {"table": "customers", "column": "name", "level": "full", "allowed_by": ["Ops", "Support"]}]
    assert [p["name"] for p in entry["profiles"]] == ["Ops", "Support"]  # in the order the profiles are listed


async def test_block_rules_allow_by_no_profile(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, plan=None) as h:
        await h.call("query", source="people", question="?", sql="SELECT name FROM customers")
        (entry,) = (await h.call("ai_log")).ai.structured["entries"]
    assert entry["columns"] == [{"table": "customers", "column": "name", "level": "full", "allowed_by": []}]


def test_the_report_says_which_rules_applied_to_each_column() -> None:
    profiled = AiRead(app="Claude", route="desktop", connection="people",
                      profiles=(UsedProfile("Support", 2), UsedProfile("Ops", 1)),
                      columns=(ShownColumn("customers", "name", "full", ("Support", "Ops")),
                               ShownColumn("orders", "total", "totals", ("Ops",))),
                      row_count=2, result_hash="ab" * 32, created_at="2026-11-01T09:30:00.000001+00:00")
    blocked = replace(profiled, profiles=(), columns=(ShownColumn("customers", "name", "full"),), id="b")
    report = proof_report([profiled, blocked], "2026-11-01", "2026-11-01")
    assert report.lines[0]["rules"] == "customers.name: full by Support, Ops; orders.total: totals by Ops"
    assert report.lines[1]["rules"] == "block rules (nothing read was blocked)"


def test_an_entry_written_before_rules_applied_were_kept_still_reads_and_says_so() -> None:
    old = {"id": "x" * 32, "created_at": "2026-10-01T09:30:00.000001+00:00", "origin": "laptop", "app": "Claude",
           "route": "desktop", "connection": "people", "profiles": [{"name": "Support", "version": 1}],
           "columns": [{"table": "customers", "column": "name", "level": "full"}], "row_count": 3,
           "result_hash": "ab" * 32}
    assert valid_entry(old)
    read = AiRead.from_dict(old)
    assert read.columns == (ShownColumn("customers", "name", "full"),)
    line = proof_report([read], "2026-10-01", "2026-10-01").lines[0]
    assert line["rules"] == "customers.name: full (profile not recorded)"
    assert not valid_entry({**old, "columns": [{**old["columns"][0], "allowed_by": "Support"}]})


def pulled_hash(home: Path) -> str:
    """The result hash the history entry of the one pull holds."""
    store = LocalStore.open(database_path(home))
    try:
        (pull,) = store.all_pulls()
        return pull.result_hash
    finally:
        store.close()
