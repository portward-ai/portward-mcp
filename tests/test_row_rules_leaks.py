"""Row rules hold through every query shape (#132): rows outside every applicable row rule never reach the AI, each
cell takes the highest level among the profiles whose row rule matches its row, a value anonymised on one row is never
shown in full on another, and totals thresholds apply to each group's totals-only cells. Driven through run_pull (the
one path every tool, the wrap servers and the hosted engine read through), against SQLite."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.metric_def import new_metric
from portward_free.metrics import hidden_names
from portward_free.profiles import Grant, Profile
from portward_free.pull import Pull, run_pull
from portward_free.row_rules import RowRule
from portward_free.sources import SourceError
from portward_free.sources.sqlite import SqliteSource

KEY = b"k" * 32
STAND_IN = re.compile(r"anon_[0-9a-f]{16}")
COLUMNS = ("id", "owner_email", "region", "amount")

# EU deals carry owners that also own deals elsewhere (ana, ben) and EU-only owners; US and APAC rows carry amounts
# from 7000 up and owners marked "secret", so any of them showing up is easy to see.
DEALS = [
    (1, "ana@acme.io", "EU", 1200.0), (2, "ben@acme.io", "EU", 800.0), (3, "eli@acme.io", "EU", 300.0),
    (4, "dev@acme.io", "EU", 2000.0),
    (5, "ana@acme.io", "US", 7100.0), (6, "ben@acme.io", "US", 7200.0), (7, "secret-cara@acme.io", "US", 7300.0),
    (8, "secret-fay@acme.io", "US", 7400.0), (9, "secret-gus@acme.io", "US", 7500.0),
    (10, "secret-hal@acme.io", "US", 7600.0),
    (11, "secret-ivy@acme.io", "APAC", 7700.0), (12, "secret-jo@acme.io", "APAC", 7800.0),
    (13, None, None, 7900.0),
]
EU_IDS = {1, 2, 3, 4}
EU_SUM = 1200.0 + 800.0 + 300.0 + 2000.0


@pytest.fixture()
def deals_db(tmp_path: Path) -> Path:
    path = tmp_path / "deals.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE deals (id INTEGER PRIMARY KEY, owner_email TEXT, region TEXT, amount REAL)")
        db.executemany("INSERT INTO deals VALUES (?, ?, ?, ?)", DEALS)
        db.execute("CREATE TABLE owners (email TEXT, team TEXT)")
        db.executemany("INSERT INTO owners VALUES (?, ?)",
                       [(e, f"team {n}") for n, e in enumerate( sorted({d[1] for d in DEALS if d[1] and "secret" not in d[1]}))])
    db.close()
    return path


def profile(name: str, levels: dict[str, str], rows: str | None = None, *, table: str = "deals",
            extra: Iterable[Grant] = ()) -> Profile:
    grants = [Grant.parse(f"{table}.{c}", level=lv) for c, lv in levels.items()] + list(extra)
    return Profile.new(name, "", "me@example.com", grants,
                       rows=(RowRule.parse(table, rows),) if rows else ())


ALL_FULL = dict.fromkeys(COLUMNS, "full")


def pull(db: Path, sql: str, *profiles: Profile, key: bytes | None = KEY) -> Pull:
    source = SqliteSource(db)
    try:
        return run_pull("deals", source, "?", sql, Grants.applied(profiles), stand_in_key=lambda: key)
    finally:
        source.close()


def values(found: Pull) -> list[Any]:
    return [v for row in found.rows for v in row]


def outside_rows_seen(found: Pull) -> bool:
    """Whether anything only a US, APAC or no-region row holds came back."""
    return any((isinstance(v, str) and "secret" in v) or (isinstance(v, (int, float)) and v >= 7000)
               for v in values(found))


# --- one profile, one row rule --------------------------------------------------------------------------------------

EU = ("EU pipeline", ALL_FULL, "region = 'EU'")

SHAPES = [
    "SELECT * FROM deals",
    "SELECT d.* FROM deals d",
    "SELECT * FROM deals WHERE region <> 'EU'",
    "SELECT * FROM deals WHERE region IS NULL OR region = 'US'",
    "SELECT d.id, e.owner_email, e.amount FROM deals d JOIN deals e ON e.id = d.id + 4",
    "SELECT d.id, e.amount FROM deals d LEFT JOIN deals e ON e.id > d.id",
    "SELECT * FROM deals d CROSS JOIN deals e",
    "SELECT * FROM (SELECT * FROM deals) x",
    "SELECT * FROM (SELECT * FROM (SELECT owner_email, amount FROM deals) a) b",
    "WITH c AS (SELECT * FROM deals) SELECT * FROM c",
    "WITH c AS (SELECT * FROM deals), d AS (SELECT * FROM c) SELECT * FROM d",
    "SELECT owner_email FROM deals UNION SELECT owner_email FROM deals",
    "SELECT amount FROM deals UNION ALL SELECT amount FROM deals",
    "SELECT owner_email, amount, ROW_NUMBER() OVER (ORDER BY amount DESC) AS n FROM deals",
    "SELECT owner_email, SUM(amount) OVER () AS s FROM deals",
    "SELECT MAX(amount), MIN(owner_email), COUNT(*), SUM(amount), GROUP_CONCAT(owner_email) FROM deals",
    "SELECT region, MAX(amount) FROM deals GROUP BY region",
    "SELECT (SELECT MAX(amount) FROM deals) AS top",
    "SELECT * FROM deals WHERE id IN (SELECT id FROM deals WHERE region = 'US')",
    "SELECT * FROM deals d WHERE EXISTS (SELECT 1 FROM deals e WHERE e.amount > 7000)",
    "SELECT o.email, o.team, d.amount FROM owners o LEFT JOIN deals d ON d.owner_email = o.email",
    "SELECT CASE WHEN amount > 5000 THEN owner_email END FROM deals",
    "SELECT * FROM deals ORDER BY amount DESC LIMIT 3",
]


@pytest.mark.parametrize("sql", SHAPES)
def test_rows_outside_the_row_rule_never_reach_the_ai_by_any_query_shape(deals_db: Path, sql: str) -> None:
    eu = profile(*EU, extra=[Grant.parse("owners.email"), Grant.parse("owners.team")])
    found = pull(deals_db, sql, eu)
    assert not outside_rows_seen(found), found.rows


def test_the_ai_gets_every_row_the_rule_keeps(deals_db: Path) -> None:
    found = pull(deals_db, "SELECT id, amount FROM deals ORDER BY id", profile(*EU))
    assert {r[0] for r in found.rows} == EU_IDS
    assert pull(deals_db, "SELECT SUM(amount), COUNT(*) FROM deals", profile(*EU)).rows == ((EU_SUM, 4),)


def test_a_row_rule_can_name_a_column_the_profile_hides(deals_db: Path) -> None:
    eu = profile("EU, no region", {"id": "full", "owner_email": "full", "amount": "full"}, "region = 'EU'")
    found = pull(deals_db, "SELECT * FROM deals", eu)
    assert found.columns == ("id", "owner_email", "amount")
    assert {r[0] for r in found.rows} == EU_IDS
    with pytest.raises((QueryRejected, SourceError)):  # no such column, as for any hidden one
        pull(deals_db, "SELECT * FROM deals WHERE region = 'US'", eu)


def test_a_row_rule_on_a_column_the_table_no_longer_has_matches_no_row(deals_db: Path) -> None:
    gone = profile("Gone", ALL_FULL, "territory = 'EU' OR region = 'US'")
    found = pull(deals_db, "SELECT id FROM deals", gone)
    assert {r[0] for r in found.rows} == {5, 6, 7, 8, 9, 10}  # only what it can still check: narrower, never wider


def test_rows_where_the_rule_cant_be_decided_stay_hidden(deals_db: Path) -> None:
    not_eu = profile("Not EU", ALL_FULL, "region <> 'EU'")
    found = pull(deals_db, "SELECT id FROM deals", not_eu)
    assert 13 not in {r[0] for r in found.rows}  # region NULL: neither in nor out, so out


def test_a_profile_without_a_rule_on_the_table_still_reaches_every_row(deals_db: Path) -> None:
    everything = profile("All deals", {"id": "full"})
    found = pull(deals_db, "SELECT id FROM deals", profile(*EU), everything)
    assert {r[0] for r in found.rows} == {d[0] for d in DEALS}


# --- per cell -------------------------------------------------------------------------------------------------------

COMPANY_TOTALS = ("Company totals", {"region": "full", "amount": "totals"})
COACH = ("Americas and APAC, anonymised", {"id": "full", "owner_email": "anonymised", "region": "full"},
         "region IN ('US', 'APAC')")


def test_a_profile_never_lends_its_level_to_rows_it_doesnt_reach(deals_db: Path) -> None:
    found = pull(deals_db, "SELECT id, region, owner_email FROM deals ORDER BY region", profile(*EU),
                 profile(*COMPANY_TOTALS))
    for row_id, region, owner in found.rows:
        if region == "EU":
            assert row_id in EU_IDS and owner is not None
        else:
            assert row_id is None and owner is None  # Company totals reaches the row but doesn't grant these
    assert not outside_rows_seen(found)


@pytest.mark.parametrize("sql", [
    "SELECT amount FROM deals",
    "SELECT * FROM deals",
    "SELECT MAX(amount) FROM deals",
    "SELECT amount FROM deals WHERE region = 'EU'",
])
def test_a_column_with_totals_cells_is_used_only_in_totals(deals_db: Path, sql: str) -> None:
    with pytest.raises(QueryRejected):
        pull(deals_db, sql, profile(*EU), profile(*COMPANY_TOTALS))


def test_totals_thresholds_apply_to_the_totals_cells_of_each_group(deals_db: Path) -> None:
    found = pull(deals_db, "SELECT region, SUM(amount) AS total FROM deals GROUP BY region ORDER BY region",
                 profile(*EU), profile(*COMPANY_TOTALS))
    totals = dict(found.rows)
    assert totals["EU"] == EU_SUM  # four full cells: no totals cell, so no minimum
    assert totals["US"] == sum(d[3] for d in DEALS if d[2] == "US")  # six totals cells: passes
    assert "APAC" not in totals  # two totals cells: merged into the 'other' row, itself under the minimum


@pytest.mark.parametrize("sql", [
    "SELECT SUM(amount) FROM deals WHERE region IN ('EU', 'APAC')",
    "SELECT COUNT(amount) FROM deals WHERE region <> 'US'",
    "SELECT AVG(amount) FROM deals WHERE region = 'EU' OR region = 'APAC'",
    "SELECT SUM(amount) FROM deals WHERE region IS NULL OR region = 'EU'",
])
def test_full_cells_dont_count_towards_a_groups_minimum(deals_db: Path, sql: str) -> None:
    """Otherwise the EU total, minus EU plus a few APAC deals, gives the APAC deals' amounts."""
    found = pull(deals_db, sql, profile(*EU), profile(*COMPANY_TOTALS))
    assert found.rows in ((), ((None,),)), found.rows


def test_a_value_anonymised_on_one_row_is_never_shown_in_full_on_another(deals_db: Path) -> None:
    found = pull(deals_db, "SELECT id, region, owner_email FROM deals ORDER BY id", profile(*EU), profile(*COACH))
    owners = {row_id: owner for row_id, _, owner in found.rows}
    assert "ana@acme.io" not in owners.values() and "ben@acme.io" not in owners.values()
    assert owners[1] == owners[5] and STAND_IN.fullmatch(owners[1])  # ana: one stand-in, EU and US alike
    assert owners[2] == owners[6] and STAND_IN.fullmatch(owners[2])
    assert owners[3] == "eli@acme.io" and owners[4] == "dev@acme.io"  # only ever full: shown in full
    assert all(STAND_IN.fullmatch(owners[i]) for i in (7, 8, 9, 10, 11, 12))


@pytest.mark.parametrize("sql", [
    "SELECT owner_email FROM deals WHERE region = 'EU'",
    "SELECT * FROM deals WHERE id = 1",
    "WITH c AS (SELECT owner_email FROM deals WHERE id < 5) SELECT * FROM c",
    "SELECT x.owner_email FROM (SELECT * FROM deals) x WHERE x.region = 'EU'",
    "SELECT a.owner_email, b.owner_email FROM deals a JOIN deals b ON a.owner_email = b.owner_email",
    "SELECT o.team, d.owner_email FROM owners o CROSS JOIN deals d WHERE d.id IN (1, 5)",
    "SELECT owner_email FROM deals WHERE owner_email = 'ana@acme.io'",
    "SELECT owner_email FROM deals UNION SELECT owner_email FROM deals",
    "SELECT owner_email, COUNT(*) FROM deals GROUP BY owner_email",
    "SELECT MIN(owner_email), MAX(owner_email), GROUP_CONCAT(owner_email) FROM deals",
    "SELECT owner_email, LAG(owner_email) OVER (ORDER BY id) FROM deals",
])
def test_per_cell_levels_hold_through_joins_subqueries_and_ctes(deals_db: Path, sql: str) -> None:
    owners = Grant.parse("owners.team")  # owners.email is not granted: it can't be joined in the clear
    found = pull(deals_db, sql, profile(*EU, extra=[owners]), profile(*COACH))
    assert not any(isinstance(v, str) and ("ana@" in v or "ben@" in v) for v in values(found)), found.rows
    assert not outside_rows_seen(found)


def test_counting_per_owner_counts_one_person_once(deals_db: Path) -> None:
    found = pull(deals_db, "SELECT owner_email, COUNT(*) FROM deals WHERE owner_email IS NOT NULL "
                           "GROUP BY owner_email", profile(*EU), profile(*COACH))
    counts = sorted(n for _, n in found.rows)
    assert counts.count(2) == 2  # ana and ben, each two deals under one stand-in


def test_without_a_stand_in_key_anonymised_cells_are_hidden_never_full(deals_db: Path) -> None:
    found = pull(deals_db, "SELECT * FROM deals", profile(*EU), profile(*COACH), key=None)
    assert "owner_email" not in found.columns
    assert not outside_rows_seen(found)


def test_cells_no_reaching_profile_grants_are_empty(deals_db: Path) -> None:
    found = pull(deals_db, "SELECT SUM(amount), COUNT(amount) FROM deals", profile(*EU), profile(*COACH))
    assert found.rows == ((EU_SUM, 4),)  # Coach reaches US and APAC rows but doesn't grant amount


def test_anonymised_cells_next_to_totals_cells_in_one_column_count_as_totals(deals_db: Path) -> None:
    anonymised_us = ("US amounts anonymised", {"id": "full", "amount": "anonymised"}, "region = 'US'")
    totals = ("Totals of amounts", {"region": "full", "amount": "totals"})
    found = pull(deals_db, "SELECT SUM(amount) FROM deals WHERE region = 'US'", profile(*anonymised_us),
                 profile(*totals))
    assert found.rows == ((sum(d[3] for d in DEALS if d[2] == "US"),),)
    with pytest.raises(QueryRejected):
        pull(deals_db, "SELECT amount FROM deals", profile(*anonymised_us), profile(*totals))


# --- metrics --------------------------------------------------------------------------------------------------------

def test_a_metric_filtering_on_a_column_with_stand_in_cells_is_held_back(deals_db: Path) -> None:
    by_owner = new_metric("Ana's deals", "SUM(amount)", "deals", ["owner_email = 'ana@acme.io'"])
    eu, coach = profile(*EU), profile(*COACH)
    source = SqliteSource(deals_db)
    try:
        catalog = source.tables()
    finally:
        source.close()
    rules = Grants.applied([eu, coach])
    assert hidden_names(by_owner, eu.grants, rules, rules.visible(catalog), catalog) == ("deals.owner_email",)
    alone = Grants.applied([eu])
    assert hidden_names(by_owner, eu.grants, alone, alone.visible(catalog), catalog) == ()


def test_a_metric_over_a_table_with_a_row_rule_adds_up_only_the_rows_it_reaches(deals_db: Path) -> None:
    found = pull(deals_db, 'SELECT SUM("amount") AS "total" FROM "deals"', profile(*EU))
    assert found.rows == ((EU_SUM,),)
