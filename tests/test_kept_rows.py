"""Result rows are kept only when the user turns it on: encrypted in the local file, the key in the OS keychain."""

import base64
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness import LockedKeychain, MemoryKeychain, connected, file_bytes

from conftest import ORDER_COUNT

from portward_free.paths import database_path

SQL = "SELECT c.name, o.id, o.total FROM orders o JOIN customers c ON c.id = o.customer_id ORDER BY o.id"


async def test_kept_rows_are_encrypted_in_the_file_and_the_key_is_only_in_the_keychain(
        home: Path, shop_db: Path) -> None:
    keychain = MemoryKeychain()
    async with connected(home, shop_db, keychain=keychain, mode="results") as h:
        turned_on = await h.call("keep_rows", on=True)
        pulled = await h.call("query", source="shop", question="Orders by customer", sql=SQL)
        kept = await h.call("kept_rows", pull_id=pulled.ai.structured["pull_id"])
        entry = (await h.call("history", limit=1)).ai.structured["pulls"][0]
    assert not turned_on.is_error, turned_on.ai.text
    assert not kept.is_error, kept.ai.text
    assert kept.ai.structured["row_count"] == ORDER_COUNT
    assert kept.ai.structured["rows"][:2] == [["Grace", 1, 1.0], ["Linus", 2, 2.0]]
    assert entry["keep_rows"] is True
    assert len(keychain.entries) == 1
    (secret,) = keychain.entries.values()
    stored = file_bytes(home)
    for plaintext in (b"Grace", b"Linus"):
        assert plaintext not in stored
    assert secret.encode() not in stored
    assert base64.b64decode(secret) not in stored


async def test_without_the_key_from_the_keychain_kept_rows_cannot_be_opened(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, keychain=MemoryKeychain()) as h:
        await h.call("keep_rows", on=True)
        pulled = await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
    async with connected(home, shop_db, keychain=MemoryKeychain()) as h:  # same file, keychain entry gone
        kept = await h.call("kept_rows", pull_id=pulled.ai.structured["pull_id"])
    assert kept.is_error
    assert "keychain" in kept.ai.text and "rerun" in kept.ai.text
    assert "Grace" not in kept.ai.text


async def test_rows_are_not_kept_by_default(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        pulled = await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        kept = await h.call("kept_rows", pull_id=pulled.ai.structured["pull_id"])
        assert h.stored("kept_rows") == []
    assert kept.is_error and "weren't kept" in kept.ai.text


async def test_turning_row_keeping_off_stops_storing_rows(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        await h.call("keep_rows", on=True)
        before = await h.call("query", source="shop", question="Kept", sql="SELECT name FROM customers")
        turned_off = await h.call("keep_rows", on=False)
        after = await h.call("query", source="shop", question="Not kept", sql="SELECT name FROM customers")
        kept_after = await h.call("kept_rows", pull_id=after.ai.structured["pull_id"])
        kept_before = await h.call("kept_rows", pull_id=before.ai.structured["pull_id"])
        stored = h.stored("kept_rows")
        flags = {p["question"]: p["keep_rows"] for p in h.stored("pulls")}
    assert not turned_off.is_error
    assert [r["pull_id"] for r in stored] == [before.ai.structured["pull_id"]]
    assert flags == {"Kept": 1, "Not kept": 0}
    assert kept_after.is_error
    assert kept_before.ai.structured["rows"] == [["Ada"], ["Grace"], ["Linus"]]


async def test_the_setting_survives_a_restart(home: Path, shop_db: Path) -> None:
    keychain = MemoryKeychain()
    async with connected(home, shop_db, keychain=keychain, mode="results") as h:
        await h.call("keep_rows", on=True)
    async with connected(home, shop_db, keychain=keychain, mode="results") as h:
        pulled = await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        kept = await h.call("kept_rows", pull_id=pulled.ai.structured["pull_id"])
    assert kept.ai.structured["rows"] == [["Ada"], ["Grace"], ["Linus"]]


def backdate(home: Path, pull_id: str, days: int) -> None:
    """Make a pull look `days` old, as if it had been made then."""
    when = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with sqlite3.connect(database_path(home)) as db:
        db.execute("UPDATE pulls SET created_at = ? WHERE id = ?", (when, pull_id))
    db.close()


async def test_kept_rows_older_than_30_days_are_pruned(home: Path, shop_db: Path) -> None:
    keychain = MemoryKeychain()
    async with connected(home, shop_db, keychain=keychain, mode="results") as h:
        await h.call("keep_rows", on=True)
        ids = [(await h.call("query", source="shop", question=q, sql="SELECT name FROM customers"))
               .ai.structured["pull_id"] for q in ("old", "recent")]
    backdate(home, ids[0], 31)
    backdate(home, ids[1], 29)
    async with connected(home, shop_db, keychain=keychain, mode="results") as h:
        old = await h.call("kept_rows", pull_id=ids[0])
        recent = await h.call("kept_rows", pull_id=ids[1])
        stored = h.stored("kept_rows")
        flags = {p["question"]: p["keep_rows"] for p in h.stored("pulls")}
    assert old.is_error and "weren't kept" in old.ai.text
    assert recent.ai.structured["rows"] == [["Ada"], ["Grace"], ["Linus"]]
    assert [r["pull_id"] for r in stored] == [ids[1]]
    assert flags == {"old": 0, "recent": 1}  # the history entry stays; only its rows go


async def test_when_the_keychain_cant_be_used_rows_are_not_kept_and_the_pull_still_works(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, keychain=MemoryKeychain()) as h:
        await h.call("keep_rows", on=True)
    async with connected(home, shop_db, keychain=LockedKeychain()) as h:
        turned_on = await h.call("keep_rows", on=True)
        pulled = await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        stored = h.stored("kept_rows")
        (entry,) = h.stored("pulls")
    assert turned_on.is_error and "locked" in turned_on.ai.text
    assert not pulled.is_error
    assert "weren't kept" in pulled.ai.text and "locked" in pulled.ai.text
    assert stored == [] and entry["keep_rows"] == 0
    assert b"Grace" not in file_bytes(home)
