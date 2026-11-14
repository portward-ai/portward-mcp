"""A connection that fails says why, in words the user can act on, and saves nothing."""

from pathlib import Path

import pytest
from conftest import Database
from harness import Harness, MemoryKeychain

pytestmark = pytest.mark.integration

CLOSED_PORT = 1  # nothing listens there


@pytest.mark.parametrize(("change", "password", "reason"), [
    ({}, "not-the-password", "couldn't connect: wrong user or password"),
    ({"user": "pwfree_nobody"}, None, "couldn't connect: wrong user or password"),
    ({"database": "pwfree_missing"}, None, "couldn't connect: that database doesn't exist on the server"),
    ({"port": CLOSED_PORT}, None, "couldn't connect: the server didn't answer; check the host and port"),
    ({"host": "no-such-host.invalid"}, None, "couldn't connect: the host name can't be found; check the host"),
])
async def test_a_failed_connection_gives_the_user_a_plain_reason_and_saves_no_password(
        home: Path, database: Database, keychain: MemoryKeychain, change: dict[str, object], password: str | None,
        reason: str) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.call("add_source", name="shop", **database.fields(**change))
        failed = h.type_password_in_terminal("shop", password or database.password())
        tested = await h.call("test_source", name="shop")
    assert failed.code != 0
    assert f"Not saved: {reason}" in failed.out, failed.out
    assert keychain.passwords == {}
    assert tested.is_error and "isn't in the keychain yet" in tested.ai.text


async def test_testing_a_saved_connection_whose_password_changed_says_so(home: Path, database: Database,
                                                                          keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        ok = await h.connect_database("shop", database.fields(), database.password())
        keychain.set("connection:shop", "changed-since")
        failed = await h.call("test_source", name="shop")
    assert not ok.is_error and ok.ai.structured["ok"] is True
    assert failed.is_error
    assert failed.ai.text == "shop: couldn't connect: wrong user or password"


async def test_a_saved_connection_whose_keychain_entry_is_gone_says_how_to_enter_it_again(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.connect_database("shop", database.fields(), database.password())
        keychain.delete("connection:shop")
        failed = await h.call("test_source", name="shop")
    assert failed.is_error
    assert "isn't in the keychain yet" in failed.ai.text
    assert "update_password" in failed.ai.text and "password shop" in failed.ai.text


@pytest.mark.parametrize(("change", "reason"), [
    ({"host": None}, "needs host"),
    ({"port": "abc"}, "the port must be a number"),
    ({"port": 70000}, "the port must be between 1 and 65535"),
])
async def test_missing_or_malformed_settings_are_named(home: Path, database: Database, keychain: MemoryKeychain,
                                                      change: dict[str, object], reason: str) -> None:
    fields = {k: v for k, v in database.fields(**change).items() if v is not None}
    async with Harness.start(home, keychain=keychain) as h:
        failed = await h.call("add_source", name="shop", **fields)
    assert failed.is_error
    assert reason in failed.ai.text
