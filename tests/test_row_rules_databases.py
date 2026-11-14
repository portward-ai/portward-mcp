"""Row rules on real Postgres and MySQL (#132): the per-cell rewrite (CASE per cell, the row WHERE, the look-up of
values anonymised anywhere, the totals flags) runs in each engine's own SQL. Opt-in like the other database tests:
skipped when the test servers aren't running (see conftest.py)."""

from __future__ import annotations

import re

import psycopg
import pymysql
import pytest
from conftest import ADMIN_PASSWORD, MYSQL_PORT, PG_PORT, READER, Database
from test_row_rules_leaks import COACH, COMPANY_TOTALS, DEALS, EU, EU_IDS, EU_SUM, SHAPES, profile

from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.pull import run_pull
from portward_free.sources import SourceError, open_source

ROWS_DB = "pwfree_rows"
STAND_IN = re.compile(r"anon_[0-9a-f]{16}")


def _values() -> str:
    def sql(v: object) -> str:
        return "NULL" if v is None else (f"'{v}'" if isinstance(v, str) else str(v))
    return ", ".join("(" + ", ".join(sql(v) for v in row) + ")" for row in DEALS)


SEED = ("DROP TABLE IF EXISTS deals", "CREATE TABLE deals (id int PRIMARY KEY, owner_email varchar(40), "
        "region varchar(10), amount decimal(10,2))", f"INSERT INTO deals VALUES {_values()}")


@pytest.fixture(scope="session")
def pg_rows(postgres_db: Database) -> Database:
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname="postgres", autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (ROWS_DB,)).fetchone():
            admin.execute(f"CREATE DATABASE {ROWS_DB}")
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname=ROWS_DB, autocommit=True) as db:
        for statement in (*SEED, f"GRANT SELECT ON deals TO {READER}"):
            db.execute(statement)
    return postgres_db


@pytest.fixture(scope="session")
def mysql_rows(mysql_db: Database) -> Database:
    with pymysql.connect(host="127.0.0.1", port=MYSQL_PORT, user="root", password=ADMIN_PASSWORD,
                         autocommit=True) as admin, admin.cursor() as cur:
        for statement in (f"CREATE DATABASE IF NOT EXISTS {ROWS_DB}", f"USE {ROWS_DB}", *SEED,
                          f"GRANT SELECT ON {ROWS_DB}.* TO {READER}"):
            cur.execute(statement)
    return mysql_db


@pytest.fixture(params=["postgres", "mysql"])
def rows_db(request: pytest.FixtureRequest) -> Database:
    return request.getfixturevalue("pg_rows" if request.param == "postgres" else "mysql_rows")


def pull(db: Database, sql: str, *profiles):
    source = open_source(db.kind, db.fields(database=ROWS_DB), db.password())
    try:
        return run_pull("db", source, "?", sql, Grants.applied(profiles), stand_in_key=lambda: b"k" * 32)
    finally:
        source.close()


def outside(rows) -> bool:
    return any((isinstance(v, str) and "secret" in v) or (v is not None and not isinstance(v, str) and float(v) >= 7000)
               for row in rows for v in row)


@pytest.mark.integration
@pytest.mark.parametrize("sql", [s for s in SHAPES if "owners" not in s and "GROUP_CONCAT" not in s])
def test_rows_outside_the_rule_never_come_back_from_a_database(rows_db: Database, sql: str) -> None:
    try:
        found = pull(rows_db, sql, profile(*EU))
    except (QueryRejected, SourceError):
        return  # an engine that can't run the shape at all (MySQL and some window or set shapes)
    assert not outside(found.rows), found.rows


@pytest.mark.integration
def test_per_cell_levels_and_anonymised_everywhere_hold_in_a_database(rows_db: Database) -> None:
    found = pull(rows_db, "SELECT id, owner_email FROM deals ORDER BY id", profile(*EU), profile(*COACH))
    owners = dict(found.rows)
    assert "ana@acme.io" not in owners.values() and owners[1] == owners[5] and STAND_IN.fullmatch(owners[1])
    assert owners[3] == "eli@acme.io"
    counted = pull(rows_db, "SELECT COUNT(*) FROM deals", profile(*EU))
    assert counted.rows[0][0] == len(EU_IDS)


@pytest.mark.integration
def test_totals_minimums_count_only_totals_cells_in_a_database(rows_db: Database) -> None:
    found = pull(rows_db, "SELECT region, SUM(amount) AS total FROM deals GROUP BY region",
                 profile(*EU), profile(*COMPANY_TOTALS))
    totals = {r[0]: float(r[1]) for r in found.rows}
    assert totals["EU"] == EU_SUM and "APAC" not in totals
    mixed = pull(rows_db, "SELECT SUM(amount) FROM deals WHERE region IN ('EU', 'APAC')", profile(*EU),
                 profile(*COMPANY_TOTALS))
    assert mixed.rows in ((), ((None,),))
