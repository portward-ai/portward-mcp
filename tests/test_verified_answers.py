"""Verified answers (#130, spec #114 stories 30 and 33): a saved pull the user promotes into a profile's context with
its question, SQL, result hash and version. The AI sees the questions (describe_source, list_sources) and runs one as
written through the normal pull path; on connect each is re-run and compared, and a changed result holds it back and
lists it for the user. One naming a hidden column is held back too.

Driven through the MCP client harness, signed in to an in-memory account service on Personal (test_profiles)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_context import Reviewer
from test_metrics import ALL, sales
from test_profiles import tool

from portward_free.context_drift import drifted
from portward_free.grants import Grants
from portward_free.paths import database_path
from portward_free.profiles import Grant, Profile
from portward_free.proof import UsedContext
from portward_free.remote.serve import COMPUTER_ONLY
from portward_free.sources import Table
from portward_free.sources.sqlite import SqliteSource
from portward_free.storage import LocalStore
from portward_free.sync.profile_records import PROFILE, profile_content, profile_from
from portward_free.verified import hidden_names
from portward_free.verified_def import new_verified
from portward_free.weekly_facts import HELD_BACK
from portward_free.why_hidden import Kind, why_hidden

QUESTION = "What was paid revenue in the north?"
SQL = "SELECT SUM(amount) AS revenue FROM orders WHERE status = 'paid' AND region = 'north'"


@pytest.fixture()
def shop_db(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT, region TEXT, "
                   "amount REAL, created_at TEXT)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", [
            (1, 1, "paid", "north", 100.0, "2026-09-03"), (2, 2, "paid", "north", 50.0, "2026-09-20"),
            (3, 3, "void", "north", 999.0, "2026-09-21"), (4, 1, "paid", "south", 70.0, "2026-10-02")])
    db.close()
    return path


async def saved(h, name: str = "North revenue", sql: str = SQL, question: str = QUESTION) -> str:
    pulled = await h.call("query", source="people", question=question, sql=sql)
    assert not pulled.is_error, pulled.ai.text
    kept = await h.call("save_pull", pull_id=pulled.ai.structured["pull_id"], name=name)
    assert not kept.is_error, kept.ai.text
    return name


async def verify(h, name: str = "North revenue", profile: str = "Sales", **extra):
    promoted = await h.call("promote_saved_pull", name=name, profile=profile, **extra)
    assert not promoted.is_error, promoted.ai.text
    (draft_id,) = promoted.ai.structured["drafts"]
    approved = await h.call("approve_context", draft_id=draft_id)
    assert not approved.is_error, approved.ai.text
    return approved


# --- promoting ------------------------------------------------------------------------------------------------------

async def test_promoting_a_saved_pull_puts_a_verified_answer_in_the_profiles_context(home: Path,
                                                                                    shop_db: Path) -> None:
    reviewer = Reviewer()
    async with tool(home, shop_db, user=reviewer) as h:
        await sales(h)
        await verify(h, await saved(h))
        described = await h.call("describe_source", source="people")
        listed = await h.call("list_sources")
        profiles = await h.call("list_profiles")
    brief = [{"question": QUESTION, "version": 1}]
    assert described.ai.structured["verified_answers"] == brief
    assert listed.ai.structured["sources"][0]["verified_answers"] == brief
    assert "SUM(amount)" not in repr(described.ai.structured)  # the question only; the SQL on demand
    assert any(SQL in asked for asked in reviewer.asked)  # the user sees the SQL they're verifying
    (sales_profile,) = [p for p in profiles.ai.structured["profiles"] if p["name"] == "Sales"]
    assert sales_profile["context"]["verified_answers"][0]["question"] == QUESTION


# --- running it as written ------------------------------------------------------------------------------------------

async def test_running_a_verified_answer_runs_its_exact_sql_through_the_pull_path_and_logs_its_version(
        home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await verify(h, await saved(h))
        full = await h.call("describe_verified_answer", source="people", question=QUESTION)
        ran = await h.call("run_verified_answer", source="people", question=QUESTION.upper())
        history = await h.call("history", limit=1)
        logged = await h.call("ai_log")
    assert full.ai.structured["sql"] == SQL and full.ai.structured["version"] == 1
    assert not ran.is_error, ran.ai.text
    assert ran.ai.structured["verified_answer"] == {
        "question": QUESTION, "version": 1, "profile": "Sales", "checked_at": full.ai.structured["checked_at"],
        "same_result": True}
    assert "verified answer" in ran.ai.text and "v1" in ran.ai.text
    assert history.ai.structured["pulls"][0]["sql"] == SQL  # the normal pull path keeps it in history
    (entry,) = [e for e in logged.ai.structured["entries"] if e["context"]]
    assert entry["context"] == [{"kind": "verified_answer", "about": QUESTION, "profile": "Sales", "version": 1,
                                 "followed": True}]
    assert f"relied on verified answer {QUESTION} (Sales v1)" in logged.ai.text


async def test_verifying_the_same_question_again_with_another_result_is_a_new_version(home: Path,
                                                                                     shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await verify(h, await saved(h))
        await verify(h, await saved(h))  # the same SQL and result: the same version
        same = await h.call("describe_verified_answer", source="people", question=QUESTION)
        await verify(h, await saved(h, sql=SQL.replace("north", "south")))
        moved = await h.call("describe_verified_answer", source="people", question=QUESTION)
    assert same.ai.structured["version"] == 1
    assert moved.ai.structured["version"] == 2 and "south" in moved.ai.structured["sql"]


async def test_the_user_can_reword_the_question_when_approving(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer(edit="North paid revenue")) as h:
        await sales(h)
        await verify(h, await saved(h))
        described = await h.call("describe_source", source="people")
    assert described.ai.structured["verified_answers"] == [{"question": "North paid revenue", "version": 1}]


async def test_an_unknown_or_unpromoted_question_is_refused(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await saved(h)
        ran = await h.call("run_verified_answer", source="people", question=QUESTION)
        missing = await h.call("promote_saved_pull", name="nope", profile="Sales")
    assert ran.is_error and missing.is_error and "saved_pulls" in missing.ai.text


# --- changed results ------------------------------------------------------------------------------------------------

def _add_north_order(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO orders VALUES (9, 9, 'paid', 'north', 25.0, '2026-10-30')")
    db.close()


async def test_a_changed_result_on_connect_holds_it_back_and_lists_it_for_the_user(home: Path,
                                                                                   shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await verify(h, await saved(h))
    _add_north_order(shop_db)
    async with tool(home, shop_db, user=Reviewer()) as h:  # a new connect re-runs it
        described = await h.call("describe_source", source="people")
        ran = await h.call("run_verified_answer", source="people", question=QUESTION)
        full = await h.call("describe_verified_answer", source="people", question=QUESTION)
        catalog = _catalog(shop_db)
    assert "verified_answers" not in described.ai.structured
    assert ran.is_error and full.is_error
    (item,) = _why(home, catalog).about(f"verified answer {QUESTION}")
    assert item.kind == Kind.RESULT_CHANGED and item.what == "verified answer" and "Sales" in item.why
    assert item in _why(home, catalog).needs_you
    assert _held_back_count(home) == 1


async def test_running_it_after_the_data_changed_says_so_and_holds_it_back(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await verify(h, await saved(h))
        await h.call("list_sources")  # checked on connect: still the same
        _add_north_order(shop_db)
        ran = await h.call("run_verified_answer", source="people", question=QUESTION)
        listed = await h.call("list_sources")
        logged = await h.call("ai_log")
    assert not ran.is_error and ran.ai.structured["verified_answer"]["same_result"] is False
    assert "differs from the one the user verified" in ran.ai.text
    assert "verified_answers" not in listed.ai.structured["sources"][0]
    (entry,) = [e for e in logged.ai.structured["entries"] if e["context"]]
    assert entry["context"][0]["followed"] is False
    assert "(Sales v1), result changed" in logged.ai.text


async def test_verifying_it_again_after_a_change_releases_it(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await verify(h, await saved(h))
        _add_north_order(shop_db)
        await h.call("run_verified_answer", source="people", question=QUESTION)
        await verify(h, await saved(h))
        described = await h.call("describe_source", source="people")
    assert described.ai.structured["verified_answers"] == [{"question": QUESTION, "version": 2}]


# --- hidden columns -------------------------------------------------------------------------------------------------

async def test_one_naming_a_hidden_column_is_held_back_and_the_ai_isnt_told_why(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await verify(h, await saved(h))
        removed = await h.call("update_profile", profile="Sales", remove=["orders.status"])
        assert not removed.is_error, removed.ai.text
        described = await h.call("describe_source", source="people")
        ran = await h.call("run_verified_answer", source="people", question=QUESTION)
        catalog = _catalog(shop_db)
    assert "verified_answers" not in described.ai.structured
    assert ran.is_error and "status" not in ran.ai.text
    (item,) = _why(home, catalog).about(f"verified answer {QUESTION}")
    assert item.kind == Kind.HELD_BACK and item.names == ("orders.status",)


@pytest.mark.parametrize("sql", [
    "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid'",
    "WITH p AS (SELECT amount, status FROM orders) SELECT SUM(amount) FROM p WHERE status = 'paid'",
    "SELECT SUM(amount) FROM (SELECT amount FROM orders WHERE status = 'paid') t",
    "SELECT id FROM orders UNION SELECT customer_id FROM orders WHERE status = 'paid'",
    "SELECT region, ROW_NUMBER() OVER (PARTITION BY status ORDER BY amount) FROM orders",
])
def test_every_query_shape_naming_a_hidden_column_is_caught(sql: str) -> None:
    answer = new_verified("Paid?", sql, "people", "sqlite", "ab" * 32, 1, "2026-11-08T00:00:00Z")
    granted = [Grant("orders", c) for c in ("id", "customer_id", "amount", "region")]
    catalog = (Table("orders", ("id", "customer_id", "status", "region", "amount", "created_at")),)
    visible = Grants.of(granted).visible(catalog)
    assert hidden_names(answer, granted, visible, catalog) == ("orders.status",)


async def test_a_saved_pull_naming_something_the_profile_hides_cant_be_promoted(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        name = await saved(h)
        await h.call("create_profile", name="Narrow", description="n", grants=["orders.amount", "orders.region"])
        await h.call("apply_profile", profile="Narrow", source="people")
        promoted = await h.call("promote_saved_pull", name=name, profile="Narrow")
    assert promoted.is_error and "orders.status" in promoted.ai.text


async def test_promoting_needs_the_profile_applied_to_the_saved_pulls_source(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        name = await saved(h)
        await h.call("create_profile", name="Other", description="o", grants=ALL)
        promoted = await h.call("promote_saved_pull", name=name, profile="Other")
    assert promoted.is_error and "apply" in promoted.ai.text


# --- drift, sync, the web, the report -------------------------------------------------------------------------------

def test_one_naming_a_renamed_column_is_listed_as_drifted() -> None:
    answer = new_verified("Paid?", "SELECT SUM(o.amount) FROM orders o WHERE o.status = 'paid'", "people",
                          "sqlite", "ab" * 32, 1, "2026-11-08T00:00:00Z")
    profile = Profile.new("Sales", "", "sam@acme.example", [Grant("orders", c) for c in ("amount", "status")])
    profile = profile.saved("sam@acme.example", context=profile.context.with_verified(answer))
    renamed = (Table("orders", ("id", "state", "amount")),)
    (item,) = drifted([profile], renamed)
    assert item.about == "verified answer Paid?" and item.names == ("orders.status",) and "orders.state" in item.why


def test_a_profiles_verified_answers_sync_with_it() -> None:
    answer = new_verified("Paid?", SQL, "people", "sqlite", "ab" * 32, 2, "2026-11-08T00:00:00Z")
    profile = Profile.new("Sales", "", "sam@acme.example", [Grant("orders", "amount")])
    profile = profile.saved("sam@acme.example", context=profile.context.with_verified(answer))
    content = profile_content(profile)
    assert PROFILE.valid(content)
    assert profile_from(content).context.verified == profile.context.verified
    for broken in ({"question": "x"}, {**answer.stored(), "sql": "DROP TABLE orders"},
                   {**answer.stored(), "result_hash": "not a hash"}):
        assert not PROFILE.valid({**content, "context": {**content["context"], "verified_answers": [broken]}})
    older = {**content, "context": {k: v for k, v in content["context"].items() if k != "verified_answers"}}
    assert PROFILE.valid(older) and profile_from(older).context.verified == ()


def test_promoting_isnt_served_on_the_web() -> None:
    assert "promote_saved_pull" in COMPUTER_ONLY
    assert "run_verified_answer" not in COMPUTER_ONLY


def test_the_proof_report_names_the_verified_answer_and_a_changed_result() -> None:
    assert UsedContext("verified_answer", "Paid?", "Sales", 3, False).said == \
        "verified answer Paid? (Sales v3), result changed"
    assert UsedContext("verified_answer", "Paid?", "Sales", 3, True).said == "verified answer Paid? (Sales v3)"
    assert UsedContext("metric", "MRR", "Sales", 1, False).said == "metric MRR (Sales v1), not followed"


# --- helpers --------------------------------------------------------------------------------------------------------

def _catalog(db_path: Path):
    source = SqliteSource(db_path)
    try:
        return source.tables()
    finally:
        source.close()


def _why(home: Path, catalog):
    store = LocalStore.open(database_path(home))
    try:
        return why_hidden(store, "people", catalog)
    finally:
        store.close()


def _held_back_count(home: Path) -> int:
    store = LocalStore.open(database_path(home))
    try:
        return json.loads(store.setting(HELD_BACK) or "{}").get("people", 0)
    finally:
        store.close()
