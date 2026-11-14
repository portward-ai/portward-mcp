"""Metrics (#129, spec #114 stories 26-29 and 33): a metric is a named definition written as a calculation (what it
adds up, from which table, the filters it always applies, its time column and grains, the columns it may be grouped
by). Portward writes the SQL from it and runs it through the normal pull path, checks SQL the AI writes against a
metric's table for its filters and grouping, and holds back a metric that names a hidden column (no sealed metrics).

Driven through the MCP client harness, signed in to an in-memory account service on Personal (test_profiles)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from test_context import Reviewer
from test_profiles import apply, make, tool

from portward_free.context_drift import drifted
from portward_free.grants import Grants
from portward_free.guard import check
from portward_free.metric_def import new_metric
from portward_free.metrics import checked, compiled
from portward_free.paths import database_path
from portward_free.profiles import Grant, Profile
from portward_free.proof import AiRead, UsedContext, UsedProfile, valid_entry
from portward_free.proof_report import proof_report
from portward_free.pull import run_pull
from portward_free.sources import Table
from portward_free.sources.sqlite import SqliteSource
from portward_free.storage import LocalStore
from portward_free.sync.profile_records import PROFILE, profile_content, profile_from
from portward_free.why_hidden import Kind, why_hidden

# (id, customer_id, status, region, amount, created_at)
ORDERS = [
    (1, 1, "paid", "north", 100.0, "2026-09-03"),
    (2, 2, "paid", "north", 50.0, "2026-09-20"),
    (3, 3, "void", "north", 999.0, "2026-09-21"),
    (4, 1, "paid", "south", 70.0, "2026-10-02"),
    (5, 4, "paid", "south", 30.0, "2026-10-15"),
    (6, 5, "void", "south", 500.0, "2026-10-16"),
]
ALL = ["orders.id", "orders.customer_id", "orders.status", "orders.region", "orders.amount", "orders.created_at"]
MRR = {"name": "MRR", "measure": "SUM(amount)", "table": "orders", "filters": ["status = 'paid'"],
       "time_column": "created_at", "grains": ["month", "year"], "group_by": ["region", "customer_id"],
       "description": "Monthly recurring revenue: paid orders only"}


@pytest.fixture()
def shop_db(tmp_path: Path) -> Path:
    path = tmp_path / "shop.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT, region TEXT, "
                   "amount REAL, created_at TEXT)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", ORDERS)
    db.close()
    return path


async def define_metric(h, profile: str = "Sales", **changes: Any):
    drafted = await h.call("draft_metric", profile=profile, **{**MRR, **changes})
    assert not drafted.is_error, drafted.ai.text
    (draft_id,) = drafted.ai.structured["drafts"]
    approved = await h.call("approve_context", draft_id=draft_id)
    assert not approved.is_error, approved.ai.text
    return approved


async def sales(h, grants: list[str] = ALL, **levels: Any) -> None:
    await make(h, "Sales", grants, **levels)
    await apply(h, "Sales")


# --- Portward writes the SQL ----------------------------------------------------------------------------------------

async def test_the_same_metric_question_gives_the_same_sql_and_number_every_time(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        first = await h.call("metric", source="people", name="MRR", grain="month", question="MRR by month")
        again = await h.call("metric", source="people", name="mrr", grain="month", question="MRR by month?")
    assert not first.is_error, first.ai.text
    assert first.ai.structured["metric"]["sql"] == again.ai.structured["metric"]["sql"]
    assert first.ai.structured["rows"] == again.ai.structured["rows"] == [["2026-09-01", 150.0],
                                                                           ["2026-10-01", 100.0]]
    assert first.ai.structured["columns"] == ["month", "MRR"]
    assert "WHERE (\"status\" = 'paid')" in first.ai.structured["metric"]["sql"]


async def test_a_metric_can_be_cut_by_its_allowed_group_bys_and_dates(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        cut = await h.call("metric", source="people", name="MRR", group_by=["region"], start="2026-09-01",
                           end="2026-10-01", question="September MRR by region")
        refused = await h.call("metric", source="people", name="MRR", group_by=["status"], question="?")
        bad_grain = await h.call("metric", source="people", name="MRR", grain="week", question="?")
    assert cut.ai.structured["rows"] == [["north", 150.0]]
    assert refused.is_error and "region" in refused.ai.text  # says what it may be grouped by
    assert bad_grain.is_error and "month" in bad_grain.ai.text


# --- names and one-liners up front, the full item on demand ---------------------------------------------------------

async def test_describe_source_gives_names_and_one_liners_and_describe_metric_the_whole_item(
        home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        described = await h.call("describe_source", source="people")
        listed = await h.call("list_sources")
        full = await h.call("describe_metric", source="people", name="MRR")
    brief = [{"name": "MRR", "text": "Monthly recurring revenue: paid orders only"}]
    assert described.ai.structured["metrics"] == brief
    assert listed.ai.structured["sources"][0]["metrics"] == brief
    assert "SUM(amount)" not in repr(described.ai.structured)
    assert full.ai.structured["measure"] == "SUM(amount)"
    assert full.ai.structured["filters"] == ["status = 'paid'"]
    assert full.ai.structured["grains"] == ["month", "year"]
    assert full.ai.structured["version"] == 1 and full.ai.structured["profile"] == "Sales"


async def test_the_user_sees_the_whole_calculation_when_asked_to_approve(home: Path, shop_db: Path) -> None:
    reviewer = Reviewer()
    async with tool(home, shop_db, user=reviewer) as h:
        await sales(h)
        await define_metric(h)
    assert any("MRR = SUM(amount) from orders where status = 'paid'" in asked for asked in reviewer.asked)


@pytest.mark.parametrize("changes, said", [
    ({"measure": "amount * 2"}, "SUM, AVG, COUNT"),
    ({"measure": "SUM(o.amount)"}, "plain name"),
    ({"filters": ["status IN (SELECT status FROM orders)"]}, "compare columns with values"),
    ({"filters": ["lower(status) = 'paid'"]}, "compare columns with values"),
    ({"grains": ["fortnight"]}, "fortnight"),
    ({"time_column": None}, "time column"),
])
async def test_a_metric_that_isnt_a_plain_calculation_is_refused(home: Path, shop_db: Path, changes: dict,
                                                                said: str) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        drafted = await h.call("draft_metric", profile="Sales", **{**MRR, **changes})
    assert drafted.is_error and said in drafted.ai.text, drafted.ai.text


async def test_a_metric_naming_a_column_its_profile_doesnt_grant_cant_be_drafted(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h, [g for g in ALL if g != "orders.status"])
        drafted = await h.call("draft_metric", profile="Sales", **MRR)
    assert drafted.is_error and "orders.status" in drafted.ai.text


# --- SQL the AI writes is checked -----------------------------------------------------------------------------------

def _in_order(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=lambda e: e["created_at"])


async def test_ai_sql_missing_a_metric_filter_is_flagged_and_logged_as_not_followed(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        loose = await h.call("query", source="people", question="What was MRR in October?",
                             sql="SELECT SUM(amount) FROM orders WHERE created_at >= '2026-10-01'")
        strict = await h.call("query", source="people", question="What was MRR in October?",
                              sql="SELECT SUM(o.amount) AS mrr FROM orders o WHERE o.created_at >= '2026-10-01' "
                                  "AND (o.status = 'paid')")
        rows = await h.call("query", source="people", question="List the orders",
                            sql="SELECT id, status FROM orders")
        entries = _in_order((await h.call("ai_log")).ai.structured["entries"])
    assert loose.ai.structured["metric_checks"][0]["followed"] is False
    assert loose.ai.structured["metric_checks"][0]["missing_filters"] == ["status = 'paid'"]
    assert "doesn't follow metric MRR (v1" in loose.ai.text and "status = 'paid'" in loose.ai.text
    assert strict.ai.structured["metric_checks"][0]["followed"] is True
    assert "follows metric MRR (v1" in strict.ai.text
    assert "metric_checks" not in rows.ai.structured  # reading the table isn't computing the metric
    assert [e["context"] for e in entries] == [
        [{"kind": "metric", "about": "MRR", "profile": "Sales", "version": 1, "followed": False}],
        [{"kind": "metric", "about": "MRR", "profile": "Sales", "version": 1, "followed": True}],
        []]


async def test_ai_sql_grouping_by_a_column_the_metric_doesnt_allow_is_flagged(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        grouped = await h.call("query", source="people", question="MRR by status",
                               sql="SELECT status, SUM(amount) FROM orders WHERE status = 'paid' GROUP BY 1")
        allowed = await h.call("query", source="people", question="MRR by region",
                               sql="SELECT region AS r, SUM(amount) FROM orders WHERE 'paid' = status GROUP BY r")
    assert grouped.ai.structured["metric_checks"][0]["not_allowed_group_by"] == ["status"]
    assert allowed.ai.structured["metric_checks"][0]["followed"] is True


async def test_the_metric_tool_logs_its_metric_as_followed_with_the_metric_version(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        await define_metric(h, filters=["status = 'paid'", "region <> 'test'"])  # a new calculation: v2
        pulled = await h.call("metric", source="people", name="MRR", question="Total MRR")
        logged = await h.call("ai_log")
    (entry,) = logged.ai.structured["entries"]
    assert pulled.ai.structured["metric"]["version"] == 2
    assert "metric MRR (v2, profile 'Sales')" in pulled.ai.text
    assert entry["context"] == [{"kind": "metric", "about": "MRR", "profile": "Sales", "version": 2,
                                 "followed": True}]
    assert "relied on metric MRR (Sales v2)" in logged.ai.text


async def test_approving_the_same_calculation_again_keeps_its_version(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        await define_metric(h)
        full = await h.call("describe_metric", source="people", name="MRR")
    assert full.ai.structured["version"] == 1


# --- no sealed metrics ----------------------------------------------------------------------------------------------

async def test_a_metric_that_names_a_hidden_column_is_held_back(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        removed = await h.call("update_profile", profile="Sales", remove=["orders.status"])
        assert not removed.is_error, removed.ai.text
        described = await h.call("describe_source", source="people")
        asked = await h.call("metric", source="people", name="MRR", question="MRR?")
        full = await h.call("describe_metric", source="people", name="MRR")
        loose = await h.call("query", source="people", question="MRR?", sql="SELECT SUM(amount) FROM orders")
    assert "metrics" not in described.ai.structured
    assert asked.is_error and full.is_error
    assert "status" not in asked.ai.text and "status" not in full.ai.text  # the AI isn't told why
    assert "metric_checks" not in loose.ai.structured


async def test_the_user_can_ask_why_a_metric_is_held_back(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h)
        await define_metric(h)
        await h.call("update_profile", profile="Sales", remove=["orders.status"])
        source = SqliteSource(shop_db)
        catalog = source.tables()
        source.close()
    store = LocalStore.open(database_path(home))
    try:
        (item,) = why_hidden(store, "people", catalog).about("metric MRR")
    finally:
        store.close()
    assert item.kind == Kind.HELD_BACK and item.what == "metric"
    assert item.names == ("orders.status",) and "Sales" in item.why


async def test_a_metric_whose_filter_column_is_only_anonymised_is_held_back(home: Path, shop_db: Path) -> None:
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h, [g for g in ALL if g != "orders.status"], anonymised=["orders.status"])
        await define_metric(h)
        asked = await h.call("metric", source="people", name="MRR", question="MRR?")
    assert asked.is_error


# --- compiled SQL obeys levels and totals ---------------------------------------------------------------------------

async def test_compiled_sql_obeys_totals_and_stand_ins_and_the_ai_log_records_the_levels(
        home: Path, shop_db: Path) -> None:
    with sqlite3.connect(shop_db) as db:  # north gets 6 paid orders; south keeps 2
        db.executemany("INSERT INTO orders VALUES (?, ?, 'paid', 'north', ?, '2026-10-20')",
                       [(10 + i, 10 + i, 10.0 + i) for i in range(4)])
    db.close()
    async with tool(home, shop_db, user=Reviewer()) as h:
        await sales(h, [g for g in ALL if g not in ("orders.amount", "orders.customer_id")],
                    totals=["orders.amount"], anonymised=["orders.customer_id"])
        await define_metric(h)
        by_region = await h.call("metric", source="people", name="MRR", group_by=["region"], question="by region")
        by_month = await h.call("metric", source="people", name="MRR", grain="month", question="by month")
        entries = (await h.call("ai_log")).ai.structured["entries"]
    assert not by_region.is_error, by_region.ai.text
    regions = {r[0]: r[1] for r in by_region.ai.structured["rows"]}
    assert regions["north"] == 196.0 and "south" not in regions  # south's 2 paid orders are under the minimum
    assert not by_month.is_error, by_month.ai.text
    levels = {(c["table"], c["column"]): c["level"] for e in entries for c in e["columns"]}
    assert levels[("orders", "amount")] == "totals"


# --- dialects -------------------------------------------------------------------------------------------------------

METRIC = new_metric("MRR", "SUM(amount)", "orders", ["status = 'paid'"], "created_at", ["week", "month", "quarter"],
                    ["region"])
LOWER = Table("orders", ("id", "status", "region", "amount", "created_at"))
UPPER = Table("ORDERS", ("ID", "STATUS", "REGION", "AMOUNT", "CREATED_AT"))  # as Snowflake names them
POSTGRES_SQL = ('SELECT DATE_TRUNC(\'MONTH\', "created_at") AS "month", "region", SUM("amount") AS "MRR" '
                'FROM "orders" WHERE ("status" = \'paid\') AND "created_at" >= \'2026-01-01\' GROUP BY 1, 2 '
                'ORDER BY 1, 2')


def test_postgres_gets_date_trunc_with_its_own_quoting() -> None:
    sql = compiled(METRIC, LOWER, "postgres", "month", ["region"], start="2026-01-01")
    assert sql == POSTGRES_SQL
    check(sql, "postgres", ["orders"])  # the guard takes it, as it will on the pull path


def test_snowflake_gets_the_same_calculation_with_its_upper_case_names() -> None:
    sql = compiled(METRIC, UPPER, "snowflake", "month", ["region"], start="2026-01-01")
    assert sql == ('SELECT DATE_TRUNC(\'MONTH\', "CREATED_AT") AS "month", "REGION", SUM("AMOUNT") AS "MRR" '
                   'FROM "ORDERS" WHERE ("STATUS" = \'paid\') AND "CREATED_AT" >= \'2026-01-01\' GROUP BY 1, 2 '
                   'ORDER BY 1, 2')
    check(sql, "snowflake", ["orders"])
    # the Postgres SQL transpiled to Snowflake is the calculation Portward writes for Snowflake
    (from_postgres,) = sqlglot.transpile(POSTGRES_SQL, read="postgres", write="snowflake")
    written = compiled(METRIC, LOWER, "snowflake", "month", ["region"], start="2026-01-01")
    assert sqlglot.parse_one(from_postgres, read="snowflake") == sqlglot.parse_one(written, read="snowflake")


@pytest.mark.parametrize("dialect", ["sqlite", "postgres", "snowflake"])
def test_ai_sql_is_checked_in_each_dialect(dialect: str) -> None:
    loose = "SELECT region, SUM(amount) FROM orders GROUP BY region"
    strict = "SELECT \"REGION\", SUM(\"AMOUNT\") FROM ORDERS WHERE STATUS = 'paid' GROUP BY 1"
    assert checked(loose, dialect, METRIC, "Sales", "?").missing_filters == ("status = 'paid'",)
    assert checked(strict, dialect, METRIC, "Sales", "?").followed


def test_sqlite_weeks_start_on_monday_and_quarters_on_their_first_month(shop_db: Path) -> None:
    source = SqliteSource(shop_db)
    table = next(t for t in source.tables() if t.name == "orders")
    grants = Grants.of([Grant("orders", c) for c in table.columns])
    try:
        weeks = run_pull("shop", source, "?", compiled(METRIC, table, "sqlite", "week"), grants)
        quarters = run_pull("shop", source, "?", compiled(METRIC, table, "sqlite", "quarter"), grants)
    finally:
        source.close()
    # paid orders on Thu 3 Sep, Sun 20 Sep, Fri 2 Oct and Thu 15 Oct 2026
    assert weeks.rows == (("2026-08-31", 100.0), ("2026-09-14", 50.0), ("2026-09-28", 70.0), ("2026-10-12", 30.0))
    assert quarters.rows == (("2026-07-01", 150.0), ("2026-10-01", 100.0))


def test_a_metric_naming_a_renamed_column_is_listed_as_drifted_with_a_fix() -> None:
    profile = Profile.new("Sales", "", "sam@acme.example", [Grant("orders", c) for c in LOWER.columns])
    profile = profile.saved("sam@acme.example", context=profile.context.with_metric(METRIC))
    renamed = Table("orders", ("id", "state", "region", "amount", "created_at"))
    (item,) = drifted([profile], (renamed,))
    assert item.about == "metric MRR" and item.names == ("orders.status",)
    assert "orders.state" in item.why


# --- sync and the proof report --------------------------------------------------------------------------------------

def test_a_profiles_metrics_sync_with_it_versions_and_all() -> None:
    profile = Profile.new("Sales", "", "sam@acme.example", [Grant("orders", "amount")])
    profile = profile.saved("sam@acme.example", context=profile.context.with_metric(METRIC))
    profile = profile.saved("sam@acme.example", context=profile.context.with_metric(
        new_metric("MRR", "SUM(amount)", "orders", ["status <> 'void'"])))
    content = profile_content(profile)
    assert PROFILE.valid(content)
    assert profile_from(content).context.metrics == profile.context.metrics
    assert profile.context.metrics[0].version == 2
    broken = {**content, "context": {**content["context"], "metrics": [{"name": "MRR"}]}}
    assert not PROFILE.valid(broken)
    older = {**content, "context": {k: v for k, v in content["context"].items() if k != "metrics"}}
    assert PROFILE.valid(older) and profile_from(older).context.metrics == ()


def test_the_proof_report_says_when_a_metric_wasnt_followed() -> None:
    read = AiRead(app="Claude", route="desktop", connection="shop", profiles=(UsedProfile("Sales", 3),), columns=(),
                  row_count=1, result_hash="ab" * 32, created_at="2026-11-01T09:30:00.000001+00:00",
                  context=(UsedContext("metric", "MRR", "Sales", 2, followed=False),
                           UsedContext("metric", "ARR", "Sales", 1, followed=True)))
    assert valid_entry(read.as_dict()) and AiRead.from_dict(read.as_dict()) == read
    (line,) = proof_report([read], "2026-11-01", "2026-11-01").lines
    assert line["context"] == "metric MRR (Sales v2), not followed; metric ARR (Sales v1)"
