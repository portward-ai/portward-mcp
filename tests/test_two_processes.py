"""Two server processes on one app-data folder, as Claude Desktop runs them: one gets the AI's calls, the other the
panel's (reading the panel page, panel_rows, put_in_chat, secret_request, save_secret). Nothing the panel needs is in
one process's memory only: the rows lent to the panel, the selections put in the chat (which the AI's process fetches
with get_selection) and the open asks are in the local store, the rows sealed and the asks' tokens and selection ids
only as hashes. Here the two are two in-process servers, each with its own store connection, on one
file and one keychain (the OS keychain is shared by both processes).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import READER_PASSWORD, Database
from harness import Harness, MemoryKeychain, file_bytes

from portward_free import asks
from portward_free.asks import ALLOW, MAX_ATTEMPTS, PANEL_META

ALL_CUSTOMERS = "SELECT id, name, region FROM customers ORDER BY id"


@asynccontextmanager
async def two(home: Path) -> AsyncIterator[tuple[Harness, Harness]]:
    """The AI's server and the panel's, on one app-data folder and one keychain."""
    keychain = MemoryKeychain()
    async with Harness.start(home, panel=True, keychain=keychain) as ai, \
            Harness.start(home, panel=True, keychain=keychain) as panel:
        yield ai, panel


async def test_rows_pulled_in_one_process_reach_the_panel_in_the_other(home: Path, shop_db: Path) -> None:
    async with two(home) as (ai, panel):
        await ai.add_file("shop", shop_db)
        pulled = await ai.call("query", source="shop", question="Customers", sql=ALL_CUSTOMERS)
        pull_id = pulled.ai.structured["pull_id"]
        rows = await panel.call_from_panel("panel_rows", pull_id=pull_id)
        posted = await panel.call_from_panel("put_in_chat", pull_id=pull_id, rows=[1], columns=["name"])
        traced = await panel.call_from_panel("value_source", pull_id=pull_id, row=0, column="name")
        on_disk = file_bytes(home)
    assert set(pulled.ai.structured) >= {"pull_id", "columns", "row_count"} and "rows" not in pulled.ai.structured
    assert not rows.is_error, rows.ai.text
    assert rows.ai.structured["rows"] == [[1, "Ada", "north"], [2, "Grace", "south"], [3, "Linus", "north"]]
    assert not posted.is_error and "Grace" not in posted.ai.text and "name" in posted.ai.text  # the note only
    assert not traced.is_error, traced.ai.text
    assert b"Grace" not in on_disk and b"Linus" not in on_disk  # held for the panel sealed, never in plain text


async def test_a_selection_put_in_the_chat_from_the_panel_is_fetched_by_the_ais_process(
        home: Path, shop_db: Path) -> None:
    async with two(home) as (ai, panel):
        await ai.add_file("shop", shop_db)
        pulled = await ai.call("query", source="shop", question="Customers", sql=ALL_CUSTOMERS)
        posted = await panel.call_from_panel("put_in_chat", pull_id=pulled.ai.structured["pull_id"], rows=[1, 2],
                                             columns=["name"])
        fetched = await ai.call("get_selection", selection_id=posted.ai.structured["selection_id"])
        on_disk = file_bytes(home)
    assert not fetched.is_error, fetched.ai.text
    assert fetched.ai.text.endswith("| name |\n|---|\n| Grace |\n| Linus |")
    assert "Ada" not in fetched.ai.text and "north" not in fetched.ai.text
    assert b"Grace" not in on_disk and posted.ai.structured["selection_id"].encode() not in on_disk


async def test_a_block_rule_made_in_one_process_stops_a_selection_fetched_in_the_other(
        home: Path, shop_db: Path) -> None:
    async with two(home) as (ai, panel):
        await ai.add_file("shop", shop_db)
        pulled = await ai.call("query", source="shop", question="Customers", sql=ALL_CUSTOMERS)
        posted = await panel.call_from_panel("put_in_chat", pull_id=pulled.ai.structured["pull_id"], rows=[0],
                                             columns=["id", "name"])
        await panel.call("block", source="shop", table="customers", column="name")
        fetched = await ai.call("get_selection", selection_id=posted.ai.structured["selection_id"])
    assert fetched.is_error and "block rules changed" in fetched.ai.text and "Ada" not in fetched.ai.text


async def test_a_block_rule_made_in_one_process_stops_the_panel_in_the_other(home: Path, shop_db: Path) -> None:
    async with two(home) as (ai, panel):
        await ai.add_file("shop", shop_db)
        pulled = await ai.call("query", source="shop", question="Customers", sql=ALL_CUSTOMERS)
        await ai.call("block", source="shop", table="customers", column="name")
        rows = await panel.call_from_panel("panel_rows", pull_id=pulled.ai.structured["pull_id"])
    assert rows.is_error and "block rules changed" in rows.ai.text


async def test_a_pull_the_panel_never_held_is_refused(home: Path) -> None:
    async with two(home) as (_, panel):
        rows = await panel.call_from_panel("panel_rows", pull_id="made-up")
    assert rows.is_error and "isn't open any more" in rows.ai.text


async def test_a_file_asked_for_in_one_process_is_added_from_the_panel_in_the_other(
        home: Path, shop_db: Path) -> None:
    async with two(home) as (ai, panel):
        added = await ai.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        form = await panel.call_from_panel("secret_request", request_id=added.ai.structured["request_id"])
        wrong = await panel.call_from_panel("save_secret", request=form.ai.structured["token"], secret="not shop")
        before = await ai.call("list_sources")
        right = await panel.call_from_panel("save_secret", request=form.ai.structured["token"], secret=ALLOW)
        after = await ai.call("list_sources")
        again = await panel.call_from_panel("save_secret", request=form.ai.structured["token"], secret=ALLOW)
        stored = ai.stored("open_asks")
    assert not form.is_error, form.ai.text
    assert form.ai.structured["allow"] is True and ["Source", "shop"] in form.ai.structured["details"]
    assert wrong.is_error and before.ai.structured["sources"] == []
    assert not right.is_error, right.ai.text
    assert right.ai.structured["tables"] == ["customers", "orders"]
    assert [s["name"] for s in after.ai.structured["sources"]] == ["shop"]
    assert again.is_error and "expired or was already used" in again.ai.text
    assert stored == []  # closed once used


async def test_the_open_ask_on_disk_holds_no_token(home: Path, shop_db: Path) -> None:
    async with two(home) as (ai, panel):
        added = await ai.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        token = added.panel.meta[PANEL_META]["token"]
        stored = ai.stored("open_asks")
        on_disk = file_bytes(home)
        form = await panel.call_from_panel("secret_request", request_id=added.ai.structured["request_id"])
        on_disk_after = file_bytes(home)
    assert len(stored) == 1 and stored[0]["kind"] == "file"
    assert token.encode() not in on_disk
    assert form.ai.structured["token"].encode() not in on_disk_after


async def test_wrong_answers_count_across_processes_and_close_the_ask(home: Path, shop_db: Path) -> None:
    async with two(home) as (ai, panel):
        added = await ai.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        token = (await panel.form_in_panel(added))["token"]
        for i in range(MAX_ATTEMPTS):  # alternating: the AI's process's panel, then the other's
            at = ai if i % 2 else panel
            assert (await at.call_from_panel("save_secret", request=token, secret=f"wrong {i}")).is_error
        late = await ai.call_from_panel("save_secret", request=token, secret=ALLOW)
        sources = await ai.call("list_sources")
    assert late.is_error and "expired or was already used" in late.ai.text
    assert sources.ai.structured["sources"] == []


async def test_an_ask_replaced_in_one_process_is_closed_in_the_other(
        home: Path, shop_db: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.sqlite"
    other.write_bytes(shop_db.read_bytes())
    async with two(home) as (ai, panel):
        first = await ai.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        form = await panel.call_from_panel("secret_request", request_id=first.ai.structured["request_id"])
        await ai.call("add_source", name="shop", kind="sqlite", path=str(other))  # a new ask for the same source
        late = await panel.call_from_panel("save_secret", request=form.ai.structured["token"], secret=ALLOW)
        sources = await ai.call("list_sources")
    assert late.is_error and sources.ai.structured["sources"] == []


async def test_an_expired_ask_is_refused_in_the_other_process(
        home: Path, shop_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asks, "REQUEST_MINUTES", 0)  # every ask expires as soon as it is made
    async with two(home) as (ai, panel):
        added = await ai.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        form = await panel.call_from_panel("secret_request", request_id=added.ai.structured["request_id"])
        saved = await panel.call_from_panel("save_secret", request=added.panel.meta[PANEL_META]["token"],
                                            secret=ALLOW)
        sources = await ai.call("list_sources")
    assert form.is_error and "expired" in form.ai.text
    assert saved.is_error and sources.ai.structured["sources"] == []


@pytest.mark.integration
async def test_a_database_password_asked_for_in_one_process_is_typed_in_the_panel_of_the_other(
        home: Path, database: Database) -> None:
    async with two(home) as (ai, panel):
        added = await ai.call("add_source", name="shop", **database.fields())
        ask = await panel.form_in_panel(added)
        saved = await panel.call_from_panel("save_secret", request=ask["token"], secret=READER_PASSWORD)
        changed = await ai.call("update_password", name="shop")
        new = await panel.form_in_panel(changed)
        resaved = await panel.call_from_panel("save_secret", request=new["token"], secret=READER_PASSWORD)
        tested = await ai.call("test_source", name="shop")
    assert ask["label"] == "Password" and not saved.is_error, saved.ai.text
    assert not resaved.is_error, resaved.ai.text
    assert not tested.is_error, tested.ai.text


async def test_a_file_is_allowed_with_a_button_and_a_password_typed_hidden(home: Path, shop_db: Path,
                                                                          database: Database) -> None:
    """Letting the AI read a file is one Allow button, nothing typed; a password stays hidden."""
    async with two(home) as (ai, panel):
        file_form = await panel.form_in_panel(await ai.call("add_source", name="shop", kind="sqlite", path=str(shop_db)))
        password_form = await panel.form_in_panel(await ai.call("add_source", name="db", **database.fields()))
    assert file_form["allow"] is True and password_form["allow"] is False and password_form["hidden"] is True
    page = (Path(asks.__file__).parent / "panel" / "password.html").read_text(encoding="utf-8")
    assert 'ask.hidden === false ? "text" : "password"' in page