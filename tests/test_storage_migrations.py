"""The local database has a schema version; opening an older file migrates it with nothing lost."""

import sqlite3
from pathlib import Path

import pytest
from harness import Harness, connected

from portward_free.paths import database_path
from portward_free.storage import LATEST_VERSION, MIGRATIONS, LocalStore, NewerFileError, migrate, schema_version


def version_1_file(home: Path, shop_db: Path) -> Path:
    """A local database as the first release wrote it: one connection and one setting, no history table yet."""
    path = database_path(home)
    db = sqlite3.connect(path, isolation_level=None)
    migrate(db, MIGRATIONS[:1])
    db.execute("INSERT INTO connections VALUES ('shop', 'sqlite', ?, '2026-01-01T00:00:00+00:00')",
               (f'{{"path": "{shop_db}"}}',))
    db.execute("INSERT INTO settings VALUES ('usage_counts', 'off')")
    db.close()
    return path


async def test_a_new_file_is_at_the_latest_schema_version(home: Path) -> None:
    async with Harness.start(home):
        pass
    db = sqlite3.connect(database_path(home))
    assert schema_version(db) == LATEST_VERSION >= 2
    db.close()


async def test_an_older_file_is_migrated_with_its_connections_and_settings_kept(home: Path, shop_db: Path) -> None:
    version_1_file(home, shop_db)
    async with Harness.start(home) as h:
        reply = await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        assert not reply.is_error, reply.ai.text  # the connection made by the old version still works
        assert [c["name"] for c in h.stored("connections")] == ["shop"]
        assert {"key": "usage_counts", "value": "off"} in h.stored("settings")
        assert len(h.stored("pulls")) == 1
    db = sqlite3.connect(database_path(home))
    assert schema_version(db) == LATEST_VERSION
    db.close()


async def test_a_file_from_before_block_rules_gets_them_with_its_history_kept(home: Path, shop_db: Path) -> None:
    path = version_1_file(home, shop_db)
    db = sqlite3.connect(path, isolation_level=None)
    migrate(db, MIGRATIONS[:2])  # as the release before rules left it: history, no rules table
    db.execute("INSERT INTO pulls (id, question, sql, source, tables, columns, created_at, row_count, result_hash) "
               "VALUES ('p1', 'Customers', 'SELECT name FROM customers', 'shop', '[]', '[]', '2026-01-01', 3, 'h')")
    db.close()
    async with Harness.start(home) as h:
        blocked = await h.call("block", source="shop", table="customers", column="region")
        assert not blocked.is_error, blocked.ai.text
        assert [p["question"] for p in h.stored("pulls")] == ["Customers"]
        assert [(r["table_name"], r["column_name"]) for r in h.stored("rules")] == [("customers", "region")]


async def test_a_profile_saved_before_definitions_keeps_its_meanings(home: Path, shop_db: Path) -> None:
    path = version_1_file(home, shop_db)
    db = sqlite3.connect(path, isolation_level=None)
    migrate(db, MIGRATIONS[:15])  # as #92 left it: a profile's context is a list of meanings
    db.execute("INSERT INTO profiles VALUES ('p1', 1, 'Sales', 'pipeline', 'a@b.c', 'a@b.c', "
               "'[{\"schema\": null, \"table\": \"customers\", \"column\": \"name\", \"level\": \"full\"}]', "
               "'2026-01-01', '[{\"schema\": null, \"table\": \"customers\", \"column\": \"name\", "
               "\"meaning\": \"legal name\"}]')")
    db.close()
    async with Harness.start(home) as h:
        listed = await h.call("list_profiles")
    (profile,) = listed.ai.structured["profiles"]
    assert profile["context"] == {"meanings": [{"about": "customers.name", "meaning": "legal name"}],
                                  "definitions": [], "links": []}


def test_every_migration_has_its_own_version_and_all_of_them_run(tmp_path: Path) -> None:
    """Each migration is numbered by its place, with no two at one number (#95 and #98 both wrote a v16)."""
    numbers = [int(m.__name__.split("_")[1][1:]) for m in MIGRATIONS]
    assert numbers == list(range(1, len(MIGRATIONS) + 1)) and LATEST_VERSION == len(MIGRATIONS)
    db = sqlite3.connect(tmp_path / "fresh.db", isolation_level=None)
    migrate(db, MIGRATIONS)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    db.close()
    assert {"context_drafts", "context_picks", "ai_reads"} <= tables


async def test_reopening_a_current_file_keeps_its_history(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
    async with Harness.start(home) as h:
        assert [p["question"] for p in h.stored("pulls")] == ["Customers"]


def test_a_file_from_a_newer_version_is_refused_and_left_alone(home: Path) -> None:
    path = database_path(home)
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    db.close()
    with pytest.raises(NewerFileError, match="update Portward"):
        LocalStore.open(path)
    db = sqlite3.connect(path)
    assert schema_version(db) == LATEST_VERSION + 1
    db.close()


def test_a_migration_that_fails_changes_nothing(home: Path, shop_db: Path) -> None:
    path = version_1_file(home, shop_db)

    def broken(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("disk full")

    db = sqlite3.connect(path, isolation_level=None)
    with pytest.raises(RuntimeError):
        migrate(db, [*MIGRATIONS, broken])
    assert schema_version(db) == 1
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"connections", "settings"}
    assert db.execute("SELECT count(*) FROM connections").fetchone()[0] == 1
    db.close()


def test_migrations_are_numbered_in_order() -> None:
    # each migration is named for the version it brings the file to, so two branches adding "v4" can't both land
    assert [m.__name__.split("_")[1] for m in MIGRATIONS] == [f"v{n}" for n in range(1, len(MIGRATIONS) + 1)]
    assert [m.__name__ for m in MIGRATIONS][3:5] == ["_v4_ai_log", "_v5_saved_and_kept"]
