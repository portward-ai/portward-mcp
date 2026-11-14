"""A matching login (#99, Personal): from a connection's applied profiles Portward writes setup SQL for a read-only
login that can read exactly the granted columns, for the user or their DBA to run (it isn't kept). On connect it
compares the login's powers with the grants and warns, offering new SQL, when the login reads more; it never blocks,
and it hides the columns either way.

The Postgres tests run the generated SQL against a real database (free/tests/conftest.py says how to start it). The
password in the SQL is a placeholder; the tests put a random one in its place and never print it."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import psycopg.sql
import pytest
from conftest import ADMIN_PASSWORD, PG_PORT, READER, Database
from harness import Harness, MemoryKeychain
from test_profiles import PlanAccounts, User

from portward_free import weekly_facts
from portward_free.account import Accounts
from portward_free.paths import database_path
from portward_free.storage import LocalStore
from portward_free.grants import Grants
from portward_free.login_sql import PASSWORD_PLACEHOLDER, matching_sql, snowflake_sql
from portward_free.profiles import Grant
from portward_free.sources import Table

LOGIN_DB = "pwfree_login"  # its own database, so these grants touch nothing else
MATCHING = "pwfree_matching"  # the login the generated SQL makes; roles are server-wide, so it's pwfree_ named
SSN = "123-45-6789"

SEED = (
    "CREATE TABLE people (id int PRIMARY KEY, name text, ssn text)",
    f"INSERT INTO people VALUES (1, 'Ada', '{SSN}')",
    "CREATE TABLE orders (id int PRIMARY KEY, person_id int, total numeric)",
    "INSERT INTO orders VALUES (1, 1, 10)",
    "CREATE TABLE notes (id int PRIMARY KEY, body text)",
    "CREATE VIEW cards AS SELECT name, ssn AS number FROM people",
    f"GRANT SELECT ON people, orders, notes, cards TO {READER}",  # the broad login the user connects with first
)


def admin(dbname: str = LOGIN_DB) -> psycopg.Connection:
    return psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD, dbname=dbname,
                           autocommit=True)


def drop_matching_login() -> None:
    with admin("postgres") as db:
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (MATCHING,)).fetchone():
            return
    with admin() as db:
        db.execute(f"DROP OWNED BY {MATCHING}")
    with admin("postgres") as db:
        db.execute(f"DROP ROLE {MATCHING}")


@pytest.fixture()
def login_db(postgres_db: Database) -> Database:
    """A fresh database with a broad reader login, and no matching login yet."""
    with admin("postgres") as db:
        if db.execute("SELECT 1 FROM pg_database WHERE datname = %s", (LOGIN_DB,)).fetchone():
            drop_matching_login()
            db.execute(f"DROP DATABASE {LOGIN_DB} WITH (FORCE)")
        db.execute(f"CREATE DATABASE {LOGIN_DB}")
    with admin() as db:
        for statement in SEED:
            db.execute(statement)
    return postgres_db


def run_as_admin(sql: str, password: str) -> None:
    """What the user's DBA does: put a password in place of the placeholder and run the SQL."""
    assert PASSWORD_PLACEHOLDER in sql or "PASSWORD" not in sql
    with admin() as db:
        db.execute(sql.replace(PASSWORD_PLACEHOLDER, password).encode())


def readable(user: str, password: str) -> set[str]:
    """Every table.column `user` can SELECT in the public schema, asked of Postgres itself."""
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user=user, password=password, dbname=LOGIN_DB) as db:
        rows = db.execute(
            "SELECT c.relname || '.' || a.attname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "WHERE n.nspname = 'public' AND has_column_privilege(c.oid, a.attnum, 'SELECT')").fetchall()
    return {r[0] for r in rows}


@asynccontextmanager
async def personal(home: Path) -> AsyncIterator[Harness]:
    keychain = MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    async with Harness.start(home, keychain=keychain, elicitation=User(),
                             accounts=Accounts(PlanAccounts("active"))) as h:
        yield h


async def on_profile(h: Harness, db: Database, grants: list[str], name: str = "db") -> None:
    added = await h.connect_database(name, db.fields(database=LOGIN_DB), db.password())
    assert not added.is_error, added.ai.text
    made = await h.call("create_profile", name="Sales", description="pipeline", grants=grants)
    assert not made.is_error, made.ai.text
    applied = await h.call("apply_profile", profile="Sales", source=name)
    assert not applied.is_error, applied.ai.text


GRANTED = ["people.id", "people.name", "orders.total"]


@pytest.mark.integration
async def test_the_generated_sql_makes_a_login_that_reads_exactly_the_granted_columns(
        home: Path, login_db: Database) -> None:
    async with personal(home) as h:
        await on_profile(h, login_db, GRANTED)
        made = await h.call("matching_login_sql", source="db", login=MATCHING)
    assert not made.is_error, made.ai.text
    sql = made.ai.structured["sql"]
    password = secrets.token_urlsafe(18)
    run_as_admin(sql, password)

    assert readable(MATCHING, password) == {"people.id", "people.name", "orders.total"}
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user=MATCHING, password=password, dbname=LOGIN_DB) as db:
        assert db.execute("SELECT name FROM people").fetchall() == [("Ada",)]
        for denied in ("SELECT ssn FROM people", "SELECT * FROM people", "SELECT * FROM cards",
                       "SELECT * FROM notes", "SELECT person_id FROM orders"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(denied)
            db.rollback()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute("CREATE TEMP TABLE t (x int)")


def warnings_of(reply) -> list[str]:
    return (reply.ai.structured or {}).get("warnings", [])


def drift_warned(reply) -> bool:
    return any("reads only what's granted" in w for w in warnings_of(reply))


async def make_matching_login(h: Harness) -> str:
    made = await h.call("matching_login_sql", source="db", login=MATCHING)
    assert not made.is_error, made.ai.text
    password = secrets.token_urlsafe(18)
    run_as_admin(made.ai.structured["sql"], password)
    return password


@pytest.mark.integration
async def test_portward_connects_through_the_matching_login_and_sees_the_granted_columns_without_a_warning(
        home: Path, login_db: Database) -> None:
    async with personal(home) as h:
        await on_profile(h, login_db, GRANTED)
        password = await make_matching_login(h)
        tested = await h.connect_database("matched", login_db.fields(user=MATCHING, database=LOGIN_DB), password)
        assert not tested.is_error, tested.ai.text
        applied = await h.call("apply_profile", profile="Sales", source="matched")
        assert not applied.is_error, applied.ai.text
        again = await h.call("test_source", name="matched")
        listed = await h.call("list_sources")
        rows = await h.call("query", source="matched", question="?", sql="SELECT * FROM people")
    assert sorted(tested.ai.structured["tables"]) == ["orders", "people"]
    assert not drift_warned(again), again.ai.text
    (entry,) = [s for s in listed.ai.structured["sources"] if s["name"] == "matched"]
    assert {t["name"]: t["columns"] for t in entry["tables"]} == {"people": ["id", "name"], "orders": ["total"]}
    assert "login_sql" not in entry
    assert not rows.is_error, rows.ai.text
    assert "Ada" in rows.ai.text


@pytest.mark.integration
async def test_a_login_reading_more_than_the_grants_raises_the_drift_warning_with_new_sql(
        home: Path, login_db: Database) -> None:
    async with personal(home) as h:
        await on_profile(h, login_db, GRANTED)
        tested = await h.call("test_source", name="db")
        listed = await h.call("list_sources")
        rows = await h.call("query", source="db", question="?", sql="SELECT * FROM people")
        hidden = await h.call("query", source="db", question="?", sql="SELECT ssn FROM people")
    assert not tested.is_error  # it never blocks
    assert drift_warned(tested), tested.ai.text
    sql = tested.ai.structured["login_sql"]
    assert f'GRANT SELECT ("id", "name") ON TABLE "public"."people" TO "{READER}"' in sql
    (entry,) = listed.ai.structured["sources"]
    assert any("reads only what's granted" in w for w in entry["warnings"]) and entry["login_sql"] == sql
    # the warning gives counts, never the hidden names, and Portward still hides the columns
    for said in (tested, listed):
        assert all(n not in repr(said.ai.structured) for n in ("ssn", "notes", "cards", "person_id"))
    assert not rows.is_error and SSN not in rows.ai.text and "ssn" not in repr(rows.ai.structured)
    assert hidden.is_error and SSN not in hidden.ai.text


@pytest.mark.integration
async def test_the_drift_check_is_kept_as_counts_for_the_weekly_facts(home: Path, login_db: Database) -> None:
    async with personal(home) as h:
        await on_profile(h, login_db, GRANTED)
        assert not (await h.call("test_source", name="db")).is_error
        store = LocalStore.open(database_path(home))
        try:
            (connection,) = weekly_facts.facts(store)["connections"]
        finally:
            store.close()
    drift = connection["drift"]
    assert connection["name"] == "db" and set(drift) == {"columns", "tables", "elsewhere"}
    assert drift["columns"] > 0 and drift["tables"] > 0
    assert all(n not in repr(connection) for n in ("ssn", "notes", "cards", "person_id", "GRANT"))


TIGHT = "pwfree_tight"


@pytest.fixture()
def broad_login(login_db: Database) -> str:
    """A second broad login of its own (so tightening it leaves the shared reader alone); returns its password."""
    password = secrets.token_urlsafe(18)
    with admin("postgres") as db:
        exists = db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (TIGHT,)).fetchone()
        db.execute(psycopg.sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(
            psycopg.sql.SQL("ALTER" if exists else "CREATE"), psycopg.sql.Identifier(TIGHT),
            psycopg.sql.Literal(password)))
    with admin() as db:
        db.execute(f"GRANT SELECT ON people, orders, notes, cards TO {TIGHT}")
    return password


@pytest.mark.integration
async def test_running_the_new_sql_tightens_the_login_and_the_warning_goes(
        home: Path, login_db: Database, broad_login: str) -> None:
    async with personal(home) as h:
        await on_profile(h, login_db, GRANTED)
        await h.connect_database("tight", login_db.fields(user=TIGHT, database=LOGIN_DB), broad_login)
        applied = await h.call("apply_profile", profile="Sales", source="tight")
        assert not applied.is_error, applied.ai.text
        warned = await h.call("test_source", name="tight")
        assert drift_warned(warned), warned.ai.text
        run_as_admin(warned.ai.structured["login_sql"], "unused")
        after = await h.call("test_source", name="tight")
    assert readable(TIGHT, broad_login) == {"people.id", "people.name", "orders.total"}
    assert not drift_warned(after), after.ai.text


@pytest.mark.integration
async def test_a_login_that_can_read_another_schema_reads_more_than_the_grants(
        home: Path, login_db: Database) -> None:
    async with personal(home) as h:
        await on_profile(h, login_db, GRANTED)
        password = await make_matching_login(h)
        with admin() as db:
            db.execute("CREATE SCHEMA vault")
            db.execute("CREATE TABLE vault.keys (id int, secret text)")
            db.execute(f"GRANT USAGE ON SCHEMA vault TO {MATCHING}")
            db.execute(f"GRANT SELECT ON vault.keys TO {MATCHING}")
        await h.connect_database("matched", login_db.fields(user=MATCHING, database=LOGIN_DB), password)
        await h.call("apply_profile", profile="Sales", source="matched")
        tested = await h.call("test_source", name="matched")
    assert drift_warned(tested), tested.ai.text
    assert "1 tables in other schemas" in " ".join(warnings_of(tested))
    assert "vault" not in repr(tested.ai.structured) and "keys" not in repr(tested.ai.structured)


@pytest.mark.integration
async def test_a_connection_on_block_rules_gets_no_matching_login_and_no_warning(
        home: Path, login_db: Database) -> None:
    async with personal(home) as h:
        await h.connect_database("db", login_db.fields(database=LOGIN_DB), login_db.password())
        tested = await h.call("test_source", name="db")
        made = await h.call("matching_login_sql", source="db")
    assert not drift_warned(tested) and "login_sql" not in tested.ai.structured
    assert made.is_error and "apply a profile" in made.ai.text


async def test_files_have_no_login_to_match(home: Path, people_db: Path) -> None:
    async with personal(home) as h:
        await h.add_file("people", people_db)
        made = await h.call("create_profile", name="Sales", description="pipeline", grants=["customers.name"])
        assert not made.is_error, made.ai.text
        await h.call("apply_profile", profile="Sales", source="people")
        tested = await h.call("test_source", name="people")
        sql = await h.call("matching_login_sql", source="people")
    assert not drift_warned(tested) and "login_sql" not in tested.ai.structured
    assert sql.is_error and "Postgres" in sql.ai.text


# --- Snowflake: no Snowflake connection exists yet (wrap mode, #101), so its SQL is checked as written ---------------

SNOW_TABLES = (Table("CUSTOMERS", ("ID", "NAME", "SSN"), schema="SALES"), Table("NOTES", ("BODY",), schema="SALES"),
               Table("CARDS", ("NUMBER",), schema="SALES", derived=True))


def snowflake_granted() -> str:
    granted = Grants.of([Grant("CUSTOMERS", "ID"), Grant("customers", "name"), Grant("CARDS", "NUMBER")])
    return snowflake_sql(granted.visible(SNOW_TABLES), "SHOP", "PORTWARD_SHOP")


def test_snowflake_sql_reads_granted_columns_through_secure_views_only() -> None:
    sql = snowflake_granted()
    assert ('CREATE OR REPLACE SECURE VIEW SHOP."PORTWARD_SHOP"."CUSTOMERS" AS SELECT "ID", "NAME" FROM '
            'SHOP."SALES"."CUSTOMERS";') in sql
    assert 'GRANT SELECT ON VIEW SHOP."PORTWARD_SHOP"."CUSTOMERS" TO ROLE "PORTWARD_SHOP_READER";' in sql
    # nothing ungranted, no view of a view, and no privilege on a table itself
    assert "SSN" not in sql and "NOTES" not in sql and "CARDS" not in sql
    grants = [line for line in sql.splitlines() if line.startswith("GRANT ")]
    assert not any("ON TABLE" in g or "ON ALL" in g or "FUTURE" in g for g in grants)


def test_snowflake_sql_makes_a_key_pair_service_user_with_only_the_reader_role() -> None:
    sql = snowflake_granted()
    assert 'CREATE USER IF NOT EXISTS "PORTWARD_SHOP" TYPE = SERVICE' in sql
    assert PASSWORD_PLACEHOLDER not in sql and "PASSWORD =" not in sql  # service users sign in with a key pair
    assert "DEFAULT_SECONDARY_ROLES = ()" in sql
    assert "DEFAULT_NAMESPACE = 'SHOP.PORTWARD_SHOP'" in sql
    assert 'GRANT ROLE "PORTWARD_SHOP_READER" TO USER "PORTWARD_SHOP";' in sql
    grants = [line for line in sql.splitlines() if line.startswith("GRANT ")]
    assert all(" TO ROLE \"PORTWARD_SHOP_READER\"" in g or " TO USER " in g for g in grants)
    assert not any(p in sql for p in ("OWNERSHIP", "INSERT", "UPDATE", "DELETE", "CREATE TABLE", "ALL PRIVILEGES"))


def test_snowflake_names_are_quoted_so_they_cant_break_out() -> None:
    sql = snowflake_sql((Table('we"ird', ('a"b',), schema="S"),), "D", "L")
    assert '"we""ird"' in sql and '"a""b"' in sql


def test_snowflake_sql_run_again_takes_back_views_no_longer_granted() -> None:
    """CREATE OR REPLACE only touches the views still granted: a view left from an earlier grant would stay
    readable, so the reader role loses SELECT on everything in its schema before the grants are made again
    (REVOKE ... ON ALL VIEWS IN SCHEMA affects the views that exist now: docs.snowflake.com/en/sql-reference/sql/
    revoke-privilege)."""
    lines = snowflake_granted().splitlines()
    revoke = 'REVOKE SELECT ON ALL VIEWS IN SCHEMA SHOP."PORTWARD_SHOP" FROM ROLE "PORTWARD_SHOP_READER";'
    first_grant = next(i for i, line in enumerate(lines) if line.startswith("GRANT SELECT ON VIEW"))
    assert revoke in lines and lines.index(revoke) < first_grant
    assert 'REVOKE SELECT ON ALL TABLES IN SCHEMA SHOP."PORTWARD_SHOP" FROM ROLE "PORTWARD_SHOP_READER";' in lines


def test_snowflake_sql_sets_an_existing_users_defaults_too() -> None:
    """CREATE USER IF NOT EXISTS leaves an existing user as it is, so its default role, namespace and secondary
    roles are set again with ALTER USER (never its key)."""
    alter = next(line for line in snowflake_granted().splitlines() if line.startswith("ALTER USER"))
    assert alter.startswith('ALTER USER "PORTWARD_SHOP" SET DEFAULT_ROLE = "PORTWARD_SHOP_READER"')
    assert "DEFAULT_SECONDARY_ROLES = ()" in alter and "DEFAULT_NAMESPACE" in alter
    assert "RSA_PUBLIC_KEY" not in alter


@pytest.mark.parametrize(("database", "in_sql", "namespace"), [
    ("shop", "shop", "'shop.PORTWARD_SHOP'"),  # unquoted: Snowflake resolves it as SHOP, as the user typed it
    ("SHOP", "SHOP", "'SHOP.PORTWARD_SHOP'"),
    ("my shop", '"my shop"', "'\"my shop\".PORTWARD_SHOP'"),  # not a plain identifier: kept exactly
    ('"shop"', '"shop"', "'\"shop\".PORTWARD_SHOP'"),  # typed quoted: case kept
])
def test_snowflake_database_names_resolve_as_snowflake_would(database: str, in_sql: str, namespace: str) -> None:
    """A double-quoted identifier is case-sensitive and an unquoted one is upper-cased
    (docs.snowflake.com/en/sql-reference/identifiers-syntax): quoting a name the user typed in lower case would
    name a database that doesn't exist."""
    sql = snowflake_sql((Table("CUSTOMERS", ("NAME",), schema="SALES"),), database, "PORTWARD_SHOP",
                        warehouse="reporting")
    assert f'CREATE SCHEMA IF NOT EXISTS {in_sql}."PORTWARD_SHOP";' in sql
    assert f'FROM {in_sql}."SALES"."CUSTOMERS";' in sql
    assert f"DEFAULT_NAMESPACE = {namespace}" in sql
    assert "GRANT USAGE ON WAREHOUSE reporting TO ROLE" in sql


def test_snowflake_sql_says_how_the_login_gets_a_token_for_the_mcp_server() -> None:
    """Snowflake's MCP servers take a programmatic access token; a service user's token needs a network policy and
    a role restriction (docs.snowflake.com/en/user-guide/programmatic-access-tokens)."""
    sql = snowflake_granted()
    assert ("-- ALTER USER \"PORTWARD_SHOP\" ADD PROGRAMMATIC ACCESS TOKEN portward ROLE_RESTRICTION = "
            "'PORTWARD_SHOP_READER'") in sql
    assert "network policy" in sql


def test_snowflake_sql_on_the_logins_own_views_never_makes_a_view_of_itself() -> None:
    """A connection made with a matching login reads its secure views, named like the tables. Tightening it can't
    rebuild a view from itself: the reader role just loses the views that show more than is granted."""
    own = (Table("CUSTOMERS", ("ID", "NAME", "SSN"), schema="PORTWARD_SHOP"),
           Table("NOTES", ("BODY",), schema="PORTWARD_SHOP"))
    grants = Grants.of([Grant("CUSTOMERS", "NAME"), Grant("NOTES", "BODY")])
    sql = matching_sql("snowflake", grants, own, "SHOP", "PORTWARD_SHOP", new=False)
    assert "CREATE OR REPLACE SECURE VIEW" not in sql
    assert 'REVOKE SELECT ON ALL VIEWS IN SCHEMA SHOP."PORTWARD_SHOP"' in sql
    assert 'GRANT SELECT ON VIEW SHOP."PORTWARD_SHOP"."NOTES"' in sql
    assert 'GRANT SELECT ON VIEW SHOP."PORTWARD_SHOP"."CUSTOMERS"' not in sql
    assert "CUSTOMERS" in sql  # named in a comment: make it again from the tables' own connection
