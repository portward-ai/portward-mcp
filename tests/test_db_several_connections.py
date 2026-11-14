"""Several connections at once, and removing one cleanly (its keychain entry included)."""

from pathlib import Path

import pytest
from conftest import READER_PASSWORD, Database
from harness import Harness, MemoryKeychain

pytestmark = pytest.mark.integration


async def test_postgres_mysql_and_sqlite_connections_live_side_by_side(
        home: Path, postgres_db: Database, mysql_db: Database, shop_db: Path, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        for name, db in (("pg", postgres_db), ("my", mysql_db)):
            added = await h.connect_database(name, db.fields(), db.password())
            assert not added.is_error, added.ai.text
        added = await h.add_file("file", str(shop_db))
        assert not added.is_error, added.ai.text
        # The count is read from the AI's reply, so each source is in the mode that sends results to the AI (#117).
        for name in ("pg", "my", "file"):
            h.choose_mode(name, "results")
        listed = await h.call("list_sources")
        counts = {name: (await h.call("query", source=name, question="How many customers?",
                                      sql="SELECT count(*) AS n FROM customers")).ai.structured["rows"]
                  for name in ("pg", "my", "file")}
    assert [(s["name"], s["kind"]) for s in listed.ai.structured["sources"]] == [
        ("file", "sqlite"), ("my", "mysql"), ("pg", "postgres")]
    assert counts == {"pg": [[3]], "my": [[3]], "file": [[3]]}
    assert keychain.passwords == {"connection:pg": READER_PASSWORD, "connection:my": READER_PASSWORD}


async def test_removing_a_connection_deletes_it_and_its_keychain_entry_and_leaves_the_others(
        home: Path, postgres_db: Database, mysql_db: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("pg", postgres_db.fields(), READER_PASSWORD)
        await h.connect_database("my", mysql_db.fields(), READER_PASSWORD)
        removed = await h.call("remove_source", name="pg")
        listed = await h.call("list_sources")
        query = await h.call("query", source="pg", question="Count", sql="SELECT count(*) FROM customers")
        stored = h.stored("connections")
    assert not removed.is_error, removed.ai.text
    assert removed.ai.structured == {"removed": "pg"}
    assert [s["name"] for s in listed.ai.structured["sources"]] == ["my"]
    assert query.is_error and "no source named 'pg'" in query.ai.text
    assert [c["name"] for c in stored] == ["my"]
    assert keychain.passwords == {"connection:my": READER_PASSWORD}


async def test_removing_a_source_that_isnt_there_says_so(home: Path, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        reply = await h.call("remove_source", name="nope")
    assert reply.is_error
    assert "no source named 'nope'" in reply.ai.text


async def test_replacing_a_database_connection_with_a_file_removes_the_old_password(
        home: Path, postgres_db: Database, shop_db: Path, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("shop", postgres_db.fields(), READER_PASSWORD)
        replaced = await h.add_file("shop", str(shop_db))
    assert not replaced.is_error, replaced.ai.text
    assert keychain.passwords == {}
