"""Postgres and MySQL connections, as an AI client adds, tests, lists and removes them (real databases)."""

import json
import logging
from pathlib import Path

import keyring
import pytest
from conftest import DB_NAME, READER, READER_PASSWORD, Database
from harness import Harness, MemoryKeychain
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from portward_free.keychain import SERVICE, OsKeychain


@pytest.mark.integration
async def test_a_connection_can_be_added_listed_and_queried(home: Path, database: Database,
                                                             keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        added = await h.connect_database("shop", database.fields(), READER_PASSWORD)
        h.choose_mode("shop", "results")  # rows straight to the AI (no panel to pick them in)
        listed = await h.call("list_sources")
        reply = await h.call("query", source="shop", question="Customers in the north",
                             sql="SELECT name FROM customers WHERE region = 'north' ORDER BY id")
    assert not added.is_error, added.ai.text
    assert added.ai.structured["tables"] == ["customers", "orders"]
    assert listed.ai.structured["sources"] == [{
        "name": "shop", "kind": database.kind,
        "tables": [{"name": "customers", "columns": ["id", "name", "region"]},
                   {"name": "orders", "columns": ["id", "customer_id", "total"]}],
    }]
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["rows"] == [["Ada"], ["Linus"]]


@pytest.mark.integration
async def test_numeric_and_date_values_reach_the_ai_as_json(home: Path, database: Database,
                                                            keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("shop", database.fields(), READER_PASSWORD)
        h.choose_mode("shop", "results")
        reply = await h.call("query", source="shop", question="Order totals",
                             sql="SELECT id, total, CAST('2026-10-01' AS DATE) AS day FROM orders ORDER BY id")
    assert not reply.is_error, reply.ai.text
    assert [[r[0], float(r[1]), r[2]] for r in reply.ai.structured["rows"]] == [[1, 10.0, "2026-10-01"],
                                                                               [2, 20.0, "2026-10-01"]]
    assert "| 1 | 10" in reply.ai.text


def _files_containing(folder: Path, marker: str) -> list[str]:
    """Every file under `folder` (the database file, its journal, anything else) whose bytes hold `marker`."""
    return [p.name for p in folder.rglob("*") if p.is_file() and marker.encode() in p.read_bytes()]


@pytest.mark.integration
async def test_the_password_is_kept_in_the_keychain_and_never_in_the_local_file_or_the_logs(
        home: Path, database: Database, keychain: MemoryKeychain, caplog: pytest.LogCaptureFixture,
        capfd: pytest.CaptureFixture[str]) -> None:
    caplog.set_level(logging.DEBUG)
    async with Harness.start(home, keychain=keychain) as h:
        added = await h.call("add_source", name="shop", **database.fields())
        typed = h.type_password_in_terminal("shop", READER_PASSWORD)
        await h.call("add_source", name="other", **database.fields())
        failed = h.type_password_in_terminal("other", READER_PASSWORD + "-wrong")
        tested = await h.call("test_source", name="shop")
        await h.call("query", source="shop", question="Count", sql="SELECT count(*) AS n FROM customers")
        stored = h.stored("connections")
    assert not added.is_error, added.ai.text
    assert not tested.is_error, tested.ai.text
    assert typed.code == 0 and failed.code != 0
    assert keychain.passwords == {"connection:shop": READER_PASSWORD}
    assert [c["name"] for c in stored] == ["shop", "other"]  # "other" waits for a password that works
    assert json.loads(stored[0]["settings"]) == {"host": "127.0.0.1", "port": database.port, "database": DB_NAME,
                                                 "user": READER}
    assert _files_containing(home, READER_PASSWORD) == []
    out, err = capfd.readouterr()
    for text in (caplog.text, out, err, added.ai.text, typed.out, failed.out, tested.ai.text):
        assert READER_PASSWORD not in text


class DictKeyring(KeyringBackend):
    """A keyring backend held in memory, so the adapter is tested through the real `keyring` API."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if self.store.pop((service, username), None) is None:
            raise PasswordDeleteError("not found")


def test_the_os_keychain_stores_reads_and_deletes_through_keyring() -> None:
    previous = keyring.get_keyring()
    keyring.set_keyring(DictKeyring())
    try:
        chain = OsKeychain()
        chain.set("connection:shop", READER_PASSWORD)
        assert keyring.get_password(SERVICE, "connection:shop") == READER_PASSWORD
        assert chain.get("connection:shop") == READER_PASSWORD
        chain.delete("connection:shop")
        chain.delete("connection:shop")  # deleting what isn't there is fine
        assert chain.get("connection:shop") is None
    finally:
        keyring.set_keyring(previous)
