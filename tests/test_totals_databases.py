"""Totals-only grants on real Postgres and MySQL (#94): the threshold and the row numbers it counts run in the
database, and each engine reports which columns are numbers (a totals grant on anything else falls to hidden)."""

from __future__ import annotations

import psycopg
import pymysql
import pytest
from conftest import ADMIN_PASSWORD, MYSQL_PORT, PG_PORT, READER, Database

from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.profiles import Grant
from portward_free.pull import run_pull
from portward_free.sources import SourceError, open_source

TOTALS_DB = "pwfree_totals"
ROWS = ", ".join([f"({i}, 'north', 'rep{i}', {100 + i}.5, 'memo {i}')" for i in range(1, 7)]
                 + ["(7, 'south', 'rep7', 7777.5, 'memo 7')", "(8, 'south', 'rep8', 8888.5, 'memo 8')"])
PG_SEED = (
    "DROP TABLE IF EXISTS sales",
    "CREATE TABLE sales (id int PRIMARY KEY, region text, rep text, amount numeric(10,2), memo text)",
    f"INSERT INTO sales VALUES {ROWS}",
    f"GRANT SELECT ON sales TO {READER}",
)
MYSQL_SEED = (
    f"CREATE DATABASE IF NOT EXISTS {TOTALS_DB}",
    f"USE {TOTALS_DB}",
    "DROP TABLE IF EXISTS sales",
    "CREATE TABLE sales (id int PRIMARY KEY, region varchar(10), rep varchar(10), amount decimal(10,2), "
    "memo varchar(20))",
    f"INSERT INTO sales VALUES {ROWS}",
    f"GRANT SELECT ON {TOTALS_DB}.* TO {READER}",
)


@pytest.fixture(scope="session")
def pg_totals(postgres_db: Database) -> Database:
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname="postgres", autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TOTALS_DB,)).fetchone():
            admin.execute(f"CREATE DATABASE {TOTALS_DB}")
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname=TOTALS_DB, autocommit=True) as db:
        for statement in PG_SEED:
            db.execute(statement)
    return postgres_db


@pytest.fixture(scope="session")
def mysql_totals(mysql_db: Database) -> Database:
    with pymysql.connect(host="127.0.0.1", port=MYSQL_PORT, user="root", password=ADMIN_PASSWORD,
                         autocommit=True) as admin, admin.cursor() as cur:
        for statement in MYSQL_SEED:
            cur.execute(statement)
    return mysql_db


@pytest.fixture(params=["postgres", "mysql"])
def totals_db(request: pytest.FixtureRequest) -> Database:
    return request.getfixturevalue("pg_totals" if request.param == "postgres" else "mysql_totals")


GRANTS = Grants.of([Grant.parse("sales.region"), Grant.parse("sales.rep"),
                    Grant.parse("sales.amount", level="totals"), Grant.parse("sales.memo", level="totals")])


def pull(db: Database, sql: str):
    source = open_source(db.kind, db.fields(database=TOTALS_DB), db.password())
    try:
        return source.tables(), run_pull("db", source, "?", sql, GRANTS)
    finally:
        source.close()


@pytest.mark.integration
def test_a_database_totals_column_drops_small_groups_and_text_falls_to_hidden(totals_db: Database) -> None:
    catalog, found = pull(totals_db, "SELECT region, SUM(amount) AS total, COUNT(*) AS n FROM sales GROUP BY region")
    (sales,) = [t for t in catalog if t.name == "sales"]
    assert {c.lower() for c in sales.numeric} == {"id", "amount"}
    assert [(r[0], float(r[1]), r[2]) for r in found.rows] == [("north", sum(100 + i + 0.5 for i in range(1, 7)), 6)]
    assert [t.columns for t in GRANTS.visible(catalog)] == [("region", "rep", "amount")]


@pytest.mark.integration
@pytest.mark.parametrize("sql", ["SELECT amount FROM sales", "SELECT rep, SUM(amount) FROM sales GROUP BY rep",
                                 "SELECT memo FROM sales", "SELECT SUM(amount) FROM sales WHERE rep = 'rep7'"])
def test_a_single_value_never_comes_back_from_a_database(totals_db: Database, sql: str) -> None:
    try:
        _, found = pull(totals_db, sql)
    except QueryRejected:
        return
    except SourceError as err:  # the database refused: its message must not quote a value
        assert "7777" not in str(err) and "memo 7" not in str(err)
        return
    # small groups merge into one row (#122): only that row may come back, never one rep's own total
    assert all(r[0] == "other (small groups)" for r in found.rows), found.rows
