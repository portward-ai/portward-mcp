"""Block rules on real Postgres and MySQL, where no engine-level lock backs the guard up: views and partitions read
other tables in ways Portward can't see, so they're kept from the AI while a source has block rules."""

from pathlib import Path

import psycopg
import pymysql
import pytest
from conftest import ADMIN_PASSWORD, MYSQL_PORT, PG_PORT, READER, Database
from harness import Harness, MemoryKeychain

RULES_DB = "pwfree_rules"  # its own database: the shared one's table list is asserted exactly elsewhere
SECRETS = ("123-45-6789", "987-65-4321")

PG_RULES_SEED = (
    "DROP VIEW IF EXISTS cards",
    "DROP TABLE IF EXISTS people, events",
    "CREATE TABLE people (id int PRIMARY KEY, name text, ssn text)",
    f"INSERT INTO people VALUES (1, 'Ada', '{SECRETS[0]}'), (2, 'Grace', '{SECRETS[1]}')",
    "CREATE VIEW cards AS SELECT name, ssn AS number FROM people",
    "CREATE TABLE events (id int, at date, secret text) PARTITION BY RANGE (at)",
    "CREATE TABLE events_2026 PARTITION OF events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
    f"INSERT INTO events VALUES (1, '2026-05-01', '{SECRETS[0]}')",
    f"GRANT SELECT ON people, cards, events, events_2026 TO {READER}",
)

MYSQL_RULES_SEED = (
    f"CREATE DATABASE IF NOT EXISTS {RULES_DB}",
    f"USE {RULES_DB}",
    "DROP VIEW IF EXISTS cards",
    "DROP TABLE IF EXISTS people",
    "CREATE TABLE people (id int PRIMARY KEY, name varchar(20), ssn varchar(20))",
    f"INSERT INTO people VALUES (1, 'Ada', '{SECRETS[0]}'), (2, 'Grace', '{SECRETS[1]}')",
    "CREATE VIEW cards AS SELECT name, ssn AS number FROM people",
    f"GRANT SELECT ON {RULES_DB}.* TO {READER}",
)


@pytest.fixture(scope="session")
def pg_rules(postgres_db: Database) -> Database:
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname="postgres", autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (RULES_DB,)).fetchone():
            admin.execute(f"CREATE DATABASE {RULES_DB}")
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname=RULES_DB, autocommit=True) as db:
        for statement in PG_RULES_SEED:
            db.execute(statement)
    return postgres_db


@pytest.fixture(scope="session")
def mysql_rules(mysql_db: Database) -> Database:
    with pymysql.connect(host="127.0.0.1", port=MYSQL_PORT, user="root", password=ADMIN_PASSWORD,
                         autocommit=True) as admin, admin.cursor() as cur:
        for statement in MYSQL_RULES_SEED:
            cur.execute(statement)
    return mysql_db


@pytest.fixture(params=["postgres", "mysql"])
def rules_db(request: pytest.FixtureRequest) -> Database:
    return request.getfixturevalue("pg_rules" if request.param == "postgres" else "mysql_rules")


def leaked(reply) -> bool:
    seen = reply.ai.text + repr(reply.ai.structured)
    return any(s in seen for s in SECRETS)


ROUTES = [
    "SELECT ssn FROM people",
    "SELECT * FROM people WHERE ssn LIKE '1%'",
    "SELECT p.ssn AS x FROM people p",
    "SELECT number FROM cards",
    "SELECT * FROM cards",
    "SELECT name FROM cards WHERE number LIKE '1%'",
]


@pytest.mark.integration
@pytest.mark.parametrize("sql", ROUTES)
async def test_a_blocked_column_never_reaches_the_ai_from_a_database(
        home: Path, rules_db: Database, keychain: MemoryKeychain, sql: str) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        added = await h.connect_database("db", rules_db.fields(database=RULES_DB), rules_db.password())
        assert not added.is_error, added.ai.text
        await h.call("block", source="db", table="people", column="ssn")
        reply = await h.call("query", source="db", question="?", sql=sql)
    assert reply.is_error, reply.ai.text
    assert not leaked(reply)


@pytest.mark.integration
async def test_views_are_kept_from_the_ai_while_a_database_has_block_rules(
        home: Path, rules_db: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("db", rules_db.fields(database=RULES_DB), rules_db.password())
        h.choose_mode("db", "results")  # rows straight to the AI (no panel to pick them in)
        before = await h.call("query", source="db", question="?", sql="SELECT name FROM cards ORDER BY name")
        blocked = await h.call("block", source="db", table="people", column="ssn")
        listed = await h.call("list_sources")
        allowed = await h.call("query", source="db", question="?", sql="SELECT * FROM people ORDER BY id")
    assert not before.is_error, before.ai.text  # no rules yet: the view is usable
    assert "views" in blocked.ai.text.lower()  # the user is told views are now kept from the AI
    tables = {t["name"]: t["columns"] for t in listed.ai.structured["sources"][0]["tables"]}
    assert "cards" not in tables
    assert tables["people"] == ["id", "name"]
    assert not allowed.is_error, allowed.ai.text
    assert allowed.ai.structured["rows"] == [[1, "Ada"], [2, "Grace"]]


@pytest.mark.integration
@pytest.mark.parametrize("sql", ["SELECT p FROM people p", "SELECT row_to_json(p) AS j FROM people p",
                                 "SELECT to_jsonb(p) ->> 'ssn' AS s FROM people p", "SELECT p.* FROM people p",
                                 "SELECT ctid, xmin FROM people"])
async def test_whole_row_and_system_column_references_dont_carry_a_blocked_column(
        home: Path, pg_rules: Database, keychain: MemoryKeychain, sql: str) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("db", pg_rules.fields(database=RULES_DB), pg_rules.password())
        await h.call("block", source="db", table="people", column="ssn")
        reply = await h.call("query", source="db", question="?", sql=sql)
    assert not leaked(reply), reply.ai.text


@pytest.mark.integration
async def test_a_partition_doesnt_get_around_a_rule_on_its_table(
        home: Path, pg_rules: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("db", pg_rules.fields(database=RULES_DB), pg_rules.password())
        await h.call("block", source="db", table="events", column="secret")
        child = await h.call("query", source="db", question="?", sql="SELECT secret FROM events_2026")
        child_star = await h.call("query", source="db", question="?", sql="SELECT * FROM events_2026")
        parent = await h.call("query", source="db", question="?", sql="SELECT * FROM events")
    assert child.is_error and child_star.is_error
    assert not leaked(child) and not leaked(child_star)
    assert not parent.is_error, parent.ai.text
    assert parent.ai.structured["columns"] == ["id", "at"]


@pytest.mark.integration
async def test_a_second_name_for_the_same_database_is_blocked_too(
        home: Path, rules_db: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("db", rules_db.fields(database=RULES_DB), rules_db.password())
        await h.call("block", source="db", table="people", column="ssn")
        fields = {**rules_db.fields(database=RULES_DB), "host": "LOCALHOST"}  # the same server, spelled otherwise
        await h.connect_database("again", fields, rules_db.password())
        reply = await h.call("query", source="again", question="?", sql="SELECT * FROM people")
    assert not reply.is_error, reply.ai.text
    assert not leaked(reply) and reply.ai.structured["columns"] == ["id", "name"]


@pytest.mark.integration
async def test_a_blocked_partition_withholds_its_parent_whose_rows_include_it(
        home: Path, pg_rules: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("db", pg_rules.fields(database=RULES_DB), pg_rules.password())
        blocked = await h.call("block", source="db", table="events_2026")
        parent = await h.call("query", source="db", question="?", sql="SELECT * FROM events")
        listed = await h.call("list_sources")
    assert parent.is_error and not leaked(parent)
    tables = [t["name"] for t in listed.ai.structured["sources"][0]["tables"]]
    assert "events" not in tables and "people" in tables
    assert "events holds events_2026" in blocked.ai.text  # the user is told the parent is withheld, and why


@pytest.mark.integration
async def test_a_blocked_column_on_a_partition_is_blocked_on_its_parent(
        home: Path, pg_rules: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("db", pg_rules.fields(database=RULES_DB), pg_rules.password())
        await h.call("block", source="db", table="events_2026", column="secret")
        parent = await h.call("query", source="db", question="?", sql="SELECT * FROM events")
    assert not parent.is_error, parent.ai.text
    assert not leaked(parent) and parent.ai.structured["columns"] == ["id", "at"]


@pytest.mark.integration
async def test_renaming_a_blocked_column_withholds_its_table_and_says_so(
        home: Path, pg_rules: Database, keychain: MemoryKeychain) -> None:
    admin = psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                            dbname=RULES_DB, autocommit=True)
    admin.execute("DROP TABLE IF EXISTS renamed")
    admin.execute("CREATE TABLE renamed (id int, ssn text)")
    admin.execute("INSERT INTO renamed VALUES (1, %s)", (SECRETS[0],))
    admin.execute(f"GRANT SELECT ON renamed TO {READER}")
    try:
        async with Harness.start(home, keychain=keychain) as h:
            await h.connect_database("db", pg_rules.fields(database=RULES_DB), pg_rules.password())
            await h.call("block", source="db", table="renamed", column="ssn")
            admin.execute("ALTER TABLE renamed RENAME COLUMN ssn TO tax_id")
            reply = await h.call("query", source="db", question="?", sql="SELECT * FROM renamed")
            listed = await h.call("list_sources")
    finally:
        admin.execute("DROP TABLE IF EXISTS renamed")
        admin.close()
    assert reply.is_error and not leaked(reply)
    source = listed.ai.structured["sources"][0]
    assert "renamed" not in [t["name"] for t in source["tables"]]
    assert any("renamed.ssn" in w for w in source["warnings"])
