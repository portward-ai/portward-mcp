"""Weekly facts for the account page (#105): what this computer reports to the control plane, and nothing else.

Signed in to Personal, the tool posts this week's counts (rows and asks per AI app from its own AI log entries, and
how often an ask named a column no applied profile grants), profile names, versions and grant counts, connection
names and routes, the last drift check's counts and a stable computer id, at most once an hour after a tool call.
Table and column names, context, questions, SQL and history never leave the computer this way."""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_profiles import apply, make, tool
from test_sync import MARKERS, Service, device, marked_db, service  # noqa: F401 (fixtures)

from portward_free import weekly_facts
from portward_free.account import account_token
from portward_free.login_sql import Drift
from portward_free.paths import database_path
from portward_free.storage import LocalStore


def due_now(home: Path) -> None:
    """An hour has passed since the last report (the cadence is the usage counts')."""
    store = LocalStore.open(database_path(home))
    try:
        store.set_setting(weekly_facts.LAST_SENT, (datetime.now(UTC) - timedelta(hours=2)).isoformat())
    finally:
        store.close()


def summary(service: Service, token: str) -> dict[str, Any]:
    request = urllib.request.Request(service.url + "/v1/me/facts", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


async def test_a_signed_in_computer_reports_its_week_to_the_account_page(service: Service, tmp_path: Path,
                                                                         marked_db: Path) -> None:  # noqa: F811
    async with device(service, tmp_path / "laptop", "wren@gmail.example") as laptop:
        h = laptop.h
        assert not (await h.add_file("books", str(marked_db))).is_error
        await make(h, "Ledger", ["zq_table_marker.id", "zq_table_marker.amount"])
        await apply(h, "Ledger", source="books")
        read = await h.call("query", source="books", question="zq-question-marker: amounts?",
                            sql="SELECT id, amount FROM zq_table_marker WHERE amount > 0 -- zq_sql_marker")
        assert not read.is_error, read.ai.text
        hidden = await h.call("query", source="books", question="zq-question-marker",
                              sql="SELECT zq_column_marker FROM zq_table_marker")
        assert hidden.is_error
        due_now(laptop.home)
        assert not (await h.call("ai_log")).is_error  # a tool call: the report goes after it
        token = account_token(laptop.keychain)
        assert token
        got = summary(service, token)

    assert got["counts"]["rows"] == 2 and sum(got["counts"]["asks"].values()) == 1
    assert got["counts"]["hidden_asks"] == 1
    assert got["profiles"] == [{"name": "Ledger", "version": 1,
                                "grants": {"full": 2, "anonymised": 0, "totals": 0, "whole": 0},
                                "applied_to": ["books"], "apps": sorted(got["counts"]["asks"])}]
    assert got["connections"] == [{"name": "books", "route": "desktop", "offered": ["direct"]}]
    (computer,) = got["computers"]
    assert computer["state"] == "awake" and computer["label"]
    assert got["alerts"] == []
    stored, log = service.stored_bytes(), service.log()
    assert "/v1/me/facts" in log
    for marker in MARKERS:
        assert marker.encode() not in stored, marker
        assert marker not in log, marker


def test_the_report_holds_only_the_listed_facts(home: Path) -> None:
    store = LocalStore.open(database_path(home))
    try:
        weekly_facts.remember_drift(store, "warehouse", Drift(4, 1, 2, "REVOKE ALL ON zq_table_marker"))
        weekly_facts.count_hidden_ask(store)
        report = weekly_facts.facts(store)
    finally:
        store.close()
    assert set(report) == {"source", "computer", "version", "week", "rows", "hidden_asks", "asks", "profiles",
                           "connections"}
    assert report["source"] == "computer" and report["hidden_asks"] == 1
    assert report["computer"] and report["week"] == weekly_facts.week_of(datetime.now(UTC))
    assert "zq_table_marker" not in json.dumps(report)  # the drift's SQL stays here; only its counts are kept


def test_drift_is_reported_as_counts_for_a_connection_that_still_exists(home: Path, people_db: Path) -> None:
    store = LocalStore.open(database_path(home))
    try:
        weekly_facts.remember_drift(store, "people", Drift(4, 1, 2, "sql"))
        weekly_facts.remember_drift(store, "gone", Drift(1, 1, 0, "sql"))
        assert weekly_facts.facts(store)["connections"] == []  # neither is a connection here yet
    finally:
        store.close()


async def test_drift_counts_go_with_the_connection_and_clear_when_tightened(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Support", ["customers.id"])
        await apply(h, "Support")
        store = LocalStore.open(database_path(home))
        try:
            weekly_facts.remember_drift(store, "people", Drift(4, 1, 2, "sql"))
            assert weekly_facts.facts(store)["connections"] == [
                {"name": "people", "route": "desktop", "drift": {"columns": 4, "tables": 1, "elsewhere": 2},
                 "offered": ["direct"]}]
            weekly_facts.remember_drift(store, "people", None)
            assert weekly_facts.facts(store)["connections"][0]["drift"] is None
        finally:
            store.close()


async def test_an_ask_naming_an_ungranted_column_or_table_counts_once_and_other_asks_dont(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Support", ["customers.id", "customers.name"])
        await apply(h, "Support")
        for sql in ("SELECT ssn FROM customers", "SELECT name, ssn, ssn FROM customers", "SELECT * FROM notes"):
            assert (await h.call("query", source="people", question="?", sql=sql)).is_error, sql
        for sql in ("SELECT name FROM customers", "SELECT nosuchcolumn FROM customers"):
            await h.call("query", source="people", question="?", sql=sql)
        store = LocalStore.open(database_path(home))
        try:
            assert weekly_facts.facts(store)["hidden_asks"] == 3
        finally:
            store.close()


async def test_block_rules_count_no_hidden_asks(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        assert not (await h.call("block", source="people", table="customers", column="ssn")).is_error
        await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
        store = LocalStore.open(database_path(home))
        try:
            assert weekly_facts.facts(store)["hidden_asks"] == 0
        finally:
            store.close()


def test_the_hosted_engine_reports_as_hosted_with_every_connection_on_the_hosted_route(home: Path) -> None:
    store = LocalStore.open(database_path(home))
    try:
        report = weekly_facts.facts(store, route="hosted")
    finally:
        store.close()
    assert report["source"] == "hosted" and "computer" not in report and "version" not in report
