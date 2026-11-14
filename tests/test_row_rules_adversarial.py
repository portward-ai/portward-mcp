"""A row rule may name a column the AI can't see (#132): Portward's own rewrite reads it, so SQLite's engine-level
block lets reads of it through (pull.py, Grants.rule_columns), and the swapped-in table is the only thing keeping it
from the AI. These are the shapes an AI would try to name it anyway: plain, quoted and bracketed names, aliases
(Portward's own included), rowid tricks, subqueries, CTEs, joins on it, and filters used as an oracle. None may return
a value of it, or let a filter on it change which rows come back."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_row_rules_leaks import deals_db, profile, pull  # noqa: F401 (fixture)

from portward_free.guard import QueryRejected
from portward_free.pull import Pull
from portward_free.sources import SourceError

# Two profiles, each with a rule on the hidden region column: EU and US rows reach the AI, region never does
SHOWN = {"id": "full", "amount": "full"}
EU = profile("EU", SHOWN, "region = 'EU'")
US = profile("US", SHOWN, "region = 'US'")
REGIONS = ("EU", "US", "APAC")

NAMING = [
    "SELECT region FROM deals",
    'SELECT "region" FROM deals',
    "SELECT `region` FROM deals",
    "SELECT [region] FROM deals",
    'SELECT "REGION" AS r FROM deals',
    "SELECT d.region FROM deals d",
    'SELECT d."region" FROM deals AS d',
    "SELECT deals.region FROM deals",
    'SELECT "deals"."region" FROM "deals"',
    'SELECT id, region FROM "DEALS"',
    "SELECT rowid, * FROM deals",
    "SELECT _rowid_, oid, id FROM deals",
    "SELECT d.rowid, d.region FROM deals d",
    "SELECT * FROM deals d1 JOIN deals d2 ON d1.rowid = d2.rowid",
    "SELECT pw_r.region FROM deals pw_r",
    "SELECT pw_r.region FROM deals AS pw_r WHERE pw_r.id > 0",
    "SELECT (SELECT pw_a.region FROM deals pw_a LIMIT 1) AS r FROM deals",
    "SELECT (SELECT x.region FROM deals x WHERE x.id = d.id) AS r FROM deals d",
    "SELECT * FROM (SELECT * FROM deals) t",
    "SELECT t.region FROM (SELECT * FROM deals) t",
    "WITH x AS (SELECT * FROM deals) SELECT region FROM x",
    "WITH x AS (SELECT region AS r FROM deals) SELECT r FROM x",
    "SELECT group_concat(region) AS g FROM deals",
    "SELECT max(region) AS m, min(region) AS n FROM deals",
    "SELECT typeof(region) AS t, length(region) AS l FROM deals",
    "SELECT id, CASE WHEN region = 'US' THEN 'US' ELSE 'EU' END AS r FROM deals",
    "SELECT * FROM deals JOIN deals d2 USING (region)",
    "SELECT * FROM deals NATURAL JOIN deals",
    "SELECT id FROM deals UNION SELECT region FROM deals",
    "SELECT region FROM deals WHERE id IN (SELECT id FROM deals)",
]


def answer(db: Path, sql: str) -> Pull | None:
    try:
        return pull(db, sql, EU, US)
    except (QueryRejected, SourceError):
        return None


def region_values(found: Pull | None) -> list[object]:
    return [] if found is None else [v for row in found.rows for v in row if v in REGIONS]


@pytest.mark.parametrize("sql", NAMING)
def test_a_hidden_column_a_row_rule_names_never_comes_back(deals_db: Path, sql: str) -> None:  # noqa: F811
    found = answer(deals_db, sql)
    assert region_values(found) == [], (sql, found.rows if found else None)
    if found is not None:
        assert not any(c.lower() == "region" for c in found.columns) or all(
            v is None or isinstance(v, str) and v.lower() == "region" for row in found.rows for v in row), sql


ORACLES = [  # each pair differs only in the region asked about; the AI must not get different rows
    "SELECT id FROM deals WHERE region = '{}' ORDER BY id",
    'SELECT id FROM deals WHERE "region" = \'{}\' ORDER BY id',
    "SELECT id FROM deals d WHERE d.region = '{}' ORDER BY id",
    "SELECT id FROM deals WHERE id IN (SELECT id FROM deals WHERE region = '{}') ORDER BY id",
    "SELECT count(*) AS n FROM deals WHERE region = '{}'",
    "SELECT id FROM deals WHERE EXISTS (SELECT 1 FROM deals x WHERE x.id = deals.id AND x.region = '{}') ORDER BY id",
    "SELECT id FROM deals ORDER BY region = '{}', id",
    "SELECT id, CASE WHEN region = '{}' THEN 1 ELSE 0 END AS f FROM deals ORDER BY id",
    "SELECT sum(CASE WHEN pw_r.region = '{}' THEN 1 ELSE 0 END) AS n FROM deals pw_r",
]


@pytest.mark.parametrize("sql", ORACLES)
def test_a_filter_on_a_hidden_rule_column_doesnt_tell_the_ai_its_values(deals_db: Path, sql: str) -> None:  # noqa: F811
    eu, us = answer(deals_db, sql.format("EU")), answer(deals_db, sql.format("US"))
    assert (eu is None) == (us is None), sql
    if eu is not None and us is not None:
        assert eu.rows == us.rows, sql


def test_the_rows_still_reach_the_ai(deals_db: Path) -> None:  # noqa: F811
    found = answer(deals_db, "SELECT id FROM deals ORDER BY id")
    assert found is not None and [r[0] for r in found.rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
