"""Disclosure with versions (#125, spec #114 story 21): each answer, AI log entry and proof report records which of
the user's approved context it relied on (meanings and definitions now; metrics and verified answers later), and the
version of the profile each came from, so the user can show which version the AI relied on.

Driven through the MCP client harness, signed in to an in-memory account service on Personal (test_profiles)."""

from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path

from pypdf import PdfReader
from test_context import REVENUE, Reviewer, define, draft_meaning
from test_profiles import apply, make, tool

from portward_free.proof import AiRead, ShownColumn, UsedContext, UsedProfile, valid_entry
from portward_free.proof_report import export_report, proof_report

LEGAL_NAME = "The customer's legal name"


async def sales_with_context(h) -> None:
    """Profile "Sales" (v1), then a definition (v2) and a meaning (v3), applied to "people"."""
    await make(h, "Sales", ["orders.id", "orders.customer_id", "orders.total", "customers.id", "customers.name"])
    await apply(h, "Sales")
    assert not (await define(h, "Sales", "revenue", REVENUE)).is_error
    draft_id = await draft_meaning(h, "Sales", "customers", LEGAL_NAME, column="name")
    assert not (await h.call("approve_context", draft_id=draft_id)).is_error


async def test_an_ai_log_entry_names_the_context_the_answer_relied_on_with_versions(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await sales_with_context(h)
        pulled = await h.call("query", source="people", question="What is our revenue?",
                              sql="SELECT sum(total) AS revenue FROM orders")
        assert not pulled.is_error, pulled.ai.text
        (entry,) = (await h.call("ai_log")).ai.structured["entries"]
    assert entry["profiles"] == [{"name": "Sales", "version": 3}]
    assert entry["context"] == [{"kind": "definition", "about": "revenue", "profile": "Sales", "version": 3}]


async def test_the_answer_names_each_meaning_and_definition_with_its_profile_version(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await sales_with_context(h)
        pulled = await h.call("query", source="people", question="Revenue by customer?",
                              sql="SELECT c.name, sum(o.total) AS revenue FROM customers c JOIN orders o "
                                  "ON o.customer_id = c.id GROUP BY c.name")
        assert not pulled.is_error, pulled.ai.text
        (entry,) = (await h.call("ai_log")).ai.structured["entries"]
    assert pulled.ai.structured["definitions"] == [
        {"kind": "meaning", "about": "customers.name", "text": LEGAL_NAME, "profile": "Sales", "version": 3},
        {"kind": "definition", "about": "revenue", "text": REVENUE, "profile": "Sales", "version": 3}]
    assert f"- revenue (Sales v3): {REVENUE}" in pulled.ai.text
    assert entry["context"] == [{"kind": "meaning", "about": "customers.name", "profile": "Sales", "version": 3},
                                {"kind": "definition", "about": "revenue", "profile": "Sales", "version": 3}]
    assert LEGAL_NAME not in str(entry) and REVENUE not in str(entry)  # the version pins the text; no copy is kept


async def test_a_read_on_block_rules_relied_on_no_context(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, plan=None) as h:
        await h.call("query", source="people", question="?", sql="SELECT name FROM customers")
        (entry,) = (await h.call("ai_log")).ai.structured["entries"]
    assert entry["context"] == []


# --- the proof report -----------------------------------------------------------------------------------------------

READ = AiRead(app="Claude", route="desktop", connection="people", profiles=(UsedProfile("Sales", 3),),
              columns=(ShownColumn("orders", "total", "full", ("Sales",)),), row_count=1, result_hash="ab" * 32,
              created_at="2026-11-01T09:30:00.000001+00:00",
              context=(UsedContext("meaning", "customers.name", "Sales", 3),
                       UsedContext("definition", "revenue", "Sales", 3)))


def test_the_proof_report_lists_the_context_each_read_relied_on_with_versions(tmp_path: Path) -> None:
    report = proof_report([READ], "2026-11-01", "2026-11-01")
    csv_path, pdf_path = export_report(tmp_path, report)
    (row,) = csv.DictReader(csv_path.open(newline=""))
    pdf = " ".join(" ".join(p.extract_text() for p in PdfReader(pdf_path).pages).split())
    said = "meaning customers.name (Sales v3); definition revenue (Sales v3)"
    assert row["context"] == said
    assert f"Context relied on (version): {said}" in pdf


def test_entries_from_before_and_from_a_later_portward_still_read() -> None:
    entry = READ.as_dict()
    old = {k: v for k, v in entry.items() if k != "context"}
    later = {**entry, "context": [{"kind": "verified_answer", "about": "Revenue last month?", "profile": "Sales",
                                   "version": 7, "followed": True}]}
    assert valid_entry(old) and AiRead.from_dict(old).context == ()
    assert valid_entry(later)
    (line,) = proof_report([AiRead.from_dict(later)], "2026-11-01", "2026-11-01").lines
    assert line["context"] == "verified answer Revenue last month? (Sales v7)"  # a kind #130 named
    for bad in ("Sales", [{"kind": "meaning", "about": "t", "profile": "Sales"}],
                [{"kind": "meaning", "about": "t", "profile": "Sales", "version": "3"}]):
        assert not valid_entry({**entry, "context": bad})


async def test_the_ai_log_tool_says_what_each_answer_relied_on(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await sales_with_context(h)
        await h.call("query", source="people", question="What is our revenue?",
                     sql="SELECT sum(total) AS revenue FROM orders")
        logged = await h.call("ai_log")
    assert "relied on definition revenue (Sales v3)" in logged.ai.text


def test_a_read_that_relied_on_no_context_says_none() -> None:
    (line,) = proof_report([replace(READ, context=())], "2026-11-01", "2026-11-01").lines
    assert line["context"] == "none"
