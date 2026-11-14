"""The matching login mirrors row rules and standing filters (#133): on Postgres, row-level security limits the login
to the rows Portward lets the AI see; on Snowflake, each secure view gets the same WHERE. The rows are those of
grants.Grants.cells (rows any applied profile reaches, every standing filter applied), rendered from the rule's tree,
never from text.

The Postgres test runs the generated SQL against a real database (free/tests/conftest.py says how to start one; it
skips when none is running)."""

from __future__ import annotations

import secrets

import psycopg
import pytest
from conftest import ADMIN_PASSWORD, PG_PORT, READER, Database

from portward_free.grants import Grants
from portward_free.login_sql import PASSWORD_PLACEHOLDER, drift, matching_sql
from portward_free.profiles import Grant, Profile
from portward_free.row_rules import RowRule
from portward_free.sources import Table
from portward_free.sources.postgres import PostgresSource, open_postgres

ROWS_DB = "pwfree_login_rows"
MATCHING = "pwfree_matching_rows"

DEALS = Table("deals", ("id", "region", "is_test", "amount"), schema="public", numeric=frozenset({"id", "amount"}))
NOTES = Table("notes", ("id", "body"), schema="public")


def profile(name: str, grants: list[str], rows: str | None = None, keep: str | None = None,
            table: str = "deals") -> Profile:
    rules = ((RowRule.parse(table, rows),) if rows else ()) + (
        (RowRule.parse(table, keep, standing=True),) if keep else ())
    return Profile.new(name, "", "me@example.com", [Grant.parse(g) for g in grants], rows=rules)


EU = profile("EU", ["deals.id", "deals.amount", "notes.id", "notes.body"], rows="region = 'EU'", keep="is_test = 0")
US = profile("US", ["deals.id"], rows="region IN ('US')")


# --- as written --------------------------------------------------------------------------------------------------

def test_postgres_sql_limits_the_login_with_a_restrictive_row_security_policy() -> None:
    sql = matching_sql("postgres", Grants.applied([EU, US]), (DEALS, NOTES), "shop", "portward_shop")
    assert 'ALTER TABLE "public"."deals" ENABLE ROW LEVEL SECURITY;' in sql
    restrictive = next(line for line in sql.splitlines() if "AS RESTRICTIVE" in line)
    assert restrictive.startswith('CREATE POLICY "portward_shop_rows" ON "public"."deals" AS RESTRICTIVE FOR SELECT '
                                  'TO "portward_shop" USING (')
    assert "\"is_test\" = 0" in restrictive and "\"region\" = 'EU'" in restrictive
    assert "\"region\" IN ('US')" in restrictive
    assert '"notes"' not in "".join(line for line in sql.splitlines() if "POLICY" in line or "ROW LEVEL" in line)


def test_snowflake_sql_adds_the_rows_to_the_secure_view() -> None:
    tables = (Table("DEALS", ("ID", "REGION", "IS_TEST"), schema="SALES"), Table("NOTES", ("BODY",), schema="SALES"))
    eu = profile("EU", ["DEALS.ID", "NOTES.BODY"], rows="REGION = 'EU' AND IS_TEST = 0", table="DEALS")
    sql = matching_sql("snowflake", Grants.applied([eu]), tables, "SHOP", "PORTWARD_SHOP")
    assert ('CREATE OR REPLACE SECURE VIEW SHOP."PORTWARD_SHOP"."DEALS" AS SELECT "ID" FROM SHOP."SALES"."DEALS" '
            "WHERE \"REGION\" = 'EU' AND \"IS_TEST\" = 0;") in sql
    assert ('CREATE OR REPLACE SECURE VIEW SHOP."PORTWARD_SHOP"."NOTES" AS SELECT "BODY" FROM '
            'SHOP."SALES"."NOTES";') in sql


def test_snowflake_sql_wont_grant_the_logins_own_view_of_a_table_with_row_rules_again() -> None:
    """A connection reading through the matching login can't tell which rows its own view keeps, so tightening it
    leaves that view ungranted (named in a comment) rather than guess."""
    own = (Table("DEALS", ("ID", "REGION"), schema="PORTWARD_SHOP"),)
    eu = profile("EU", ["DEALS.ID", "DEALS.REGION"], rows="REGION = 'EU'", table="DEALS")
    sql = matching_sql("snowflake", Grants.applied([eu]), own, "SHOP", "PORTWARD_SHOP", new=False)
    assert 'GRANT SELECT ON VIEW SHOP."PORTWARD_SHOP"."DEALS"' not in sql
    assert "DEALS" in sql.split("stay unreadable")[1]


def test_a_rule_value_cant_break_out_of_the_sql() -> None:
    sneaky = profile("Sneaky", ["deals.id"], rows="region = 'EU''); DROP TABLE deals; --'")
    for dialect, literal in (("postgres", "'EU''); DROP TABLE deals; --'"),
                             ("snowflake", "'EU\\'); DROP TABLE deals; --'")):
        sql = matching_sql(dialect, Grants.applied([sneaky]), (DEALS,), "shop", "portward_shop")
        assert literal in sql
        assert "\nDROP TABLE" not in sql


# --- against a real Postgres -------------------------------------------------------------------------------------

SEED = (
    "CREATE TABLE deals (id int PRIMARY KEY, region text, is_test int, amount numeric)",
    "INSERT INTO deals VALUES (1, 'EU', 0, 10), (2, 'EU', 1, 20), (3, 'US', 0, 30), (4, 'US', 1, 40), "
    "(5, 'APAC', 0, 50), (6, NULL, 0, 60)",
    "CREATE TABLE notes (id int PRIMARY KEY, body text)",
    "INSERT INTO notes VALUES (1, 'a'), (2, 'b')",
    f"GRANT SELECT ON deals, notes TO {READER}",  # another login that reads the tables already
)


def admin(dbname: str = ROWS_DB) -> psycopg.Connection:
    return psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD, dbname=dbname,
                           autocommit=True)


def drop_login() -> None:
    with admin("postgres") as db:
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (MATCHING,)).fetchone():
            return
    with admin() as db:
        db.execute(f"DROP OWNED BY {MATCHING}")
    with admin("postgres") as db:
        db.execute(f"DROP ROLE {MATCHING}")


@pytest.fixture()
def rows_db(postgres_db: Database) -> Database:
    with admin("postgres") as db:
        if db.execute("SELECT 1 FROM pg_database WHERE datname = %s", (ROWS_DB,)).fetchone():
            drop_login()
            db.execute(f"DROP DATABASE {ROWS_DB} WITH (FORCE)")
        db.execute(f"CREATE DATABASE {ROWS_DB}")
    with admin() as db:
        for statement in SEED:
            db.execute(statement)
    yield postgres_db
    drop_login()
    with admin("postgres") as db:
        db.execute(f"DROP DATABASE {ROWS_DB} WITH (FORCE)")


def run(sql: str, password: str) -> None:
    with admin() as db:
        db.execute(sql.replace(PASSWORD_PLACEHOLDER, password).encode())


def ids(user: str, password: str, table: str = "deals") -> set[int]:
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user=user, password=password, dbname=ROWS_DB) as db:
        return {r[0] for r in db.execute(f"SELECT id FROM {table}").fetchall()}


@pytest.mark.integration
def test_the_generated_postgres_sql_limits_the_login_to_the_rows_portward_shows(rows_db: Database) -> None:
    password = secrets.token_urlsafe(18)
    grants = Grants.applied([EU, US])
    run(matching_sql("postgres", grants, (DEALS, NOTES), ROWS_DB, MATCHING), password)

    # EU without test accounts (the standing filter applies to every read, US rows included), US; never APAC or
    # the row with no region; and the rule reads columns the login can't select (region, is_test)
    assert ids(MATCHING, password) == {1, 3}
    assert ids(MATCHING, password, "notes") == {1, 2}  # no rule: every row
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user=MATCHING, password=password, dbname=ROWS_DB) as db:
        assert db.execute("SELECT COUNT(*), SUM(amount) FROM deals").fetchone() == (2, 40)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("SELECT region FROM deals")
    # turning row security on doesn't take rows away from anyone else
    assert ids(READER, rows_db.password(READER)) == {1, 2, 3, 4, 5, 6}

    # run again (tightening): the same rows; then without the rules, every row again
    run(matching_sql("postgres", grants, (DEALS, NOTES), ROWS_DB, MATCHING, new=False), password)
    assert ids(MATCHING, password) == {1, 3}
    plain = [Profile.new("All", "", "me", [Grant.parse("deals.id")])]
    run(matching_sql("postgres", Grants.applied(plain), (DEALS, NOTES), ROWS_DB, MATCHING, new=False), password)
    assert ids(MATCHING, password) == {1, 2, 3, 4, 5, 6}
    assert ids(READER, rows_db.password(READER)) == {1, 2, 3, 4, 5, 6}


@pytest.mark.integration
def test_a_table_that_already_had_row_security_keeps_its_own_policies_for_everyone_else(rows_db: Database) -> None:
    with admin() as db:
        db.execute("ALTER TABLE deals ENABLE ROW LEVEL SECURITY")
        db.execute("CREATE POLICY only_eu ON deals FOR SELECT TO PUBLIC USING (region = 'EU')")
    password = secrets.token_urlsafe(18)
    run(matching_sql("postgres", Grants.applied([US]), (DEALS,), ROWS_DB, MATCHING), password)
    assert ids(READER, rows_db.password(READER)) == {1, 2}  # unchanged: no policy opened the table up
    assert ids(MATCHING, password) == set()  # the table's own policy and Portward's rows both apply


# --- drift: a login that reads rows a row rule keeps from the AI (story 40) -----------------------------------------

def opened_as(user: str, password: str) -> PostgresSource:
    return open_postgres({"host": "127.0.0.1", "port": PG_PORT, "database": ROWS_DB, "user": user}, password)


@pytest.mark.integration
def test_drift_warns_when_a_table_with_row_rules_has_no_row_security_for_the_login(rows_db: Database) -> None:
    password = secrets.token_urlsafe(18)
    columns_only = Grants.applied([Profile.new("Plain", "", "me", [Grant.parse("deals.id")])])
    run(matching_sql("postgres", columns_only, (DEALS, NOTES), ROWS_DB, MATCHING), password)
    ruled = Grants.applied([US])  # the same column, now with a row rule
    source = opened_as(MATCHING, password)
    try:
        before = drift(source, ruled, ROWS_DB, MATCHING)
        assert drift(source, columns_only, ROWS_DB, MATCHING) is None  # columns match and nothing is ruled
    finally:
        source.close()
    assert before is not None and before.columns == 0 and before.rows == 1
    assert "row" in before.warning and "deals" not in before.warning  # counts only, never names
    assert "CREATE POLICY" in before.sql

    run(before.sql, password)  # the DBA runs the new SQL: the login now reads only the ruled rows
    source = opened_as(MATCHING, password)
    try:
        assert drift(source, ruled, ROWS_DB, MATCHING) is None
    finally:
        source.close()
