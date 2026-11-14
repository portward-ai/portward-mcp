"""A login that can write is warned about (an administrator is refused), and only read-only SQL ever runs."""

from pathlib import Path

import pytest
from conftest import READER, WRITER, WRITER_PASSWORD, Database
from harness import Harness, MemoryKeychain

from portward_free.sources import SourceError, open_source

pytestmark = pytest.mark.integration


async def test_a_login_that_can_write_is_added_with_a_warning_naming_the_tables_it_can_change(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.call("add_source", name="shop", **database.fields(user=WRITER))
        typed = h.type_password_in_terminal("shop", WRITER_PASSWORD)
        tested = await h.call("test_source", name="shop")
        listed = await h.call("list_sources")
    assert typed.code == 0, typed.out
    assert "can change data (customers)" in typed.out  # the user sees the warning as they save the password
    for result in (tested.ai.structured, listed.ai.structured["sources"][0]):
        [warning] = result["warnings"]
        assert "can change data (customers)" in warning
        assert "read-only" in warning


async def test_a_login_that_can_only_read_gets_no_warning(home: Path, database: Database,
                                                          keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        added = await h.connect_database("shop", database.fields(user=READER), database.password(READER))
    assert "warnings" not in added.ai.structured


async def test_an_administrator_login_is_refused_and_its_password_is_not_saved(home: Path, database: Database,
                                                                      keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.call("add_source", name="shop", **database.fields(user=database.admin_user))
        refused = h.type_password_in_terminal("shop", database.password(database.admin_user))
    assert refused.code != 0
    assert "administrator" in refused.out or "superuser" in refused.out
    assert "connect with a login that can only read" in refused.out
    assert keychain.passwords == {}


@pytest.mark.parametrize("sql", [
    "INSERT INTO customers VALUES (9, 'Mallory', 'east')",
    "UPDATE customers SET name = 'Mallory'",
    "DELETE FROM customers",
    "SELECT 1; DELETE FROM customers",
])
async def test_a_write_query_through_a_login_that_can_write_is_refused_and_changes_nothing(
        home: Path, database: Database, keychain: MemoryKeychain, sql: str) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("shop", database.fields(user=WRITER), WRITER_PASSWORD)
        reply = await h.call("query", source="shop", question="Change it", sql=sql)
    assert reply.is_error
    assert database.scalar("SELECT count(*) FROM customers WHERE name = 'Mallory' OR id = 9") == 0
    assert database.scalar("SELECT count(*) FROM customers") == 3


@pytest.mark.parametrize("sql", [
    "INSERT INTO customers VALUES (9, 'Mallory', 'east')",
    "UPDATE customers SET name = 'Mallory'",
])
def test_even_past_the_guard_the_source_runs_writes_in_a_read_only_transaction(database: Database, sql: str) -> None:
    source = open_source(database.kind, database.fields(user=WRITER), WRITER_PASSWORD)
    try:
        with pytest.raises(SourceError, match="(?i)read.only"):
            source.fetch(sql)
    finally:
        source.close()
    assert database.scalar("SELECT count(*) FROM customers WHERE name = 'Mallory' OR id = 9") == 0
