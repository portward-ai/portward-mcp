"""The results panel (MCP Apps): with panel support, a pull shows in the panel, and the AI gets only the rows and
columns the user selected and put in the chat.

What reaches the AI is the query's reply (`Reply.ai`), the short note the panel puts in the chat (a selection id,
counts and column names) and what get_selection returns for that id. The panel reads the rows through app-only tools
(`call_from_panel`), which the host keeps from the model.
"""

import re
from pathlib import Path

import pytest
from conftest import ORDER_COUNT
from harness import Harness, Reply, connected, file_bytes

from portward_free import selections
from portward_free.modes import AI_ROWS

ALL_ORDERS = "SELECT id, customer_id, total FROM orders ORDER BY id"


async def test_with_a_panel_the_ai_gets_no_rows_from_the_query(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    assert not reply.is_error, reply.ai.text
    assert reply.panel is not None
    assert "rows" not in reply.ai.structured
    assert reply.ai.structured["row_count"] == ORDER_COUNT
    assert reply.ai.structured["columns"] == ["id", "customer_id", "total"]
    assert "| 1 |" not in reply.ai.text and "250.0" not in reply.ai.text
    assert "Put in chat" in reply.ai.text  # the AI is told how the user gives it rows


async def test_the_panel_reads_the_rows_through_an_app_only_tool(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        rows = await h.call_from_panel("panel_rows", pull_id=reply.ai.structured["pull_id"])
        ai_tools = await h.ai_tools()
    assert not rows.is_error, rows.ai.text
    assert rows.ai.structured["columns"] == ["id", "customer_id", "total"]
    assert rows.ai.structured["rows"][:2] == [[1, 2, 1.0], [2, 3, 2.0]]
    assert len(rows.ai.structured["rows"]) == ORDER_COUNT
    assert "panel_rows" not in ai_tools and "put_in_chat" not in ai_tools
    assert "query" in ai_tools


async def test_put_in_chat_puts_a_short_note_with_no_row_values_in_the_chat(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        pull_id = reply.ai.structured["pull_id"]
        message = await h.call_from_panel("put_in_chat", pull_id=pull_id, rows=[4, 1], columns=["total", "id"])
    assert not message.is_error, message.ai.text
    text = message.ai.structured["text"]  # what the panel puts in the message box: the user sends it
    selection_id = message.ai.structured["selection_id"]
    assert text == f"From Portward: use my selection {selection_id} (2 of 250 rows, 2 of 3 columns: id, total)."
    assert re.fullmatch(r"sel_[A-Za-z0-9_-]{20,}", selection_id)  # random: the AI can't guess another one
    assert "|" not in text and "2.0" not in text and "5.0" not in text  # no row values in the message


async def test_get_selection_gives_the_ai_only_the_picked_rows_and_columns(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        pull_id = reply.ai.structured["pull_id"]
        message = await h.call_from_panel("put_in_chat", pull_id=pull_id, rows=[4, 1], columns=["total", "id"])
        selection_id = message.ai.structured["selection_id"]
        fetched = await h.call("get_selection", selection_id=selection_id)
        again = await h.call("get_selection", selection_id=selection_id)  # the AI may read it again
    assert not fetched.is_error, fetched.ai.text
    text = fetched.ai.text
    assert text.endswith("| id | total |\n|---|---|\n| 2 | 2.0 |\n| 5 | 5.0 |")
    assert "2 of 250 rows and 2 of 3 columns" in text
    assert "customer_id" not in text
    for unselected in ("| 1 |", "| 3 |", "| 4 |", "| 6 |"):
        assert unselected not in text
    assert fetched.ai.structured == {"selection_id": selection_id, "pull_id": pull_id, "columns": ["id", "total"],
                                     "row_count": 2}
    assert again.ai.text == text and again.ai.structured == fetched.ai.structured  # never widened
    ai_saw = reply.ai.text + str(reply.ai.structured) + message.ai.structured["text"] + text
    assert "3.0" not in ai_saw and "250.0" not in ai_saw  # no value outside the selection reached the AI


async def test_get_selection_refuses_an_id_the_user_never_gave(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        await h.call_from_panel("put_in_chat", pull_id=reply.ai.structured["pull_id"], rows=[0], columns=["id"])
        unknown = await h.call("get_selection", selection_id="sel_made-up")
        pull_id = await h.call("get_selection", selection_id=reply.ai.structured["pull_id"])
    for refused in (unknown, pull_id):
        assert refused.is_error and refused.ai.structured is None
        assert "no selection" in refused.ai.text and "| 1 |" not in refused.ai.text


async def test_a_selection_expires(home: Path, shop_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with connected(home, shop_db, panel=True) as h:
        monkeypatch.setattr(selections, "SELECTION_HOURS", 0)  # every selection expires as soon as it is made
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        message = await h.call_from_panel("put_in_chat", pull_id=reply.ai.structured["pull_id"], rows=[0],
                                          columns=["id"])
        late = await h.call("get_selection", selection_id=message.ai.structured["selection_id"])
    assert late.is_error and "expired" in late.ai.text and "| 1 |" not in late.ai.text


async def test_get_selection_is_the_ais_and_the_panels_tools_stay_app_only(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        ai_tools = await h.ai_tools()
        tools = await h.tools()
    assert "get_selection" in ai_tools
    assert "panel_rows" not in ai_tools and "put_in_chat" not in ai_tools
    description = tools["get_selection"].description or ""
    assert "only" in description and "user's message" in description and "guess" in description


async def test_a_selection_is_held_sealed(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, name="shop", panel=True) as h:
        reply = await h.call("query", source="shop", question="Customers",
                             sql="SELECT id, name FROM customers ORDER BY id")
        message = await h.call_from_panel("put_in_chat", pull_id=reply.ai.structured["pull_id"], rows=[1],
                                          columns=["name"])
        stored = h.stored("panel_selections")
        on_disk = file_bytes(home)
    assert not message.is_error, message.ai.text
    assert len(stored) == 1 and stored[0]["pull_id"] == reply.ai.structured["pull_id"]
    assert message.ai.structured["selection_id"] not in str(stored)  # kept only as a hash
    assert b"Grace" not in on_disk


async def test_put_in_chat_refuses_a_selection_outside_the_result(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        pull_id = reply.ai.structured["pull_id"]
        past_end = await h.call_from_panel("put_in_chat", pull_id=pull_id, rows=[ORDER_COUNT], columns=["id"])
        unknown_column = await h.call_from_panel("put_in_chat", pull_id=pull_id, rows=[0], columns=["secret"])
        nothing = await h.call_from_panel("put_in_chat", pull_id=pull_id, rows=[], columns=["id"])
        no_pull = await h.call_from_panel("put_in_chat", pull_id="nope", rows=[0], columns=["id"])
    assert past_end.is_error and "rows are numbered 0 to 249" in past_end.ai.text
    assert unknown_column.is_error and "no column 'secret'" in unknown_column.ai.text
    assert nothing.is_error and "choose at least one row" in nothing.ai.text
    assert no_pull.is_error and "ask the question again" in no_pull.ai.text


async def test_without_a_panel_the_panel_tools_return_no_rows(home: Path, shop_db: Path) -> None:
    """A client without panels may show the model every tool: the panel tools then refuse, so no rows leak."""
    async with connected(home, shop_db, panel=False, mode="results") as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        pull_id = reply.ai.structured["pull_id"]
        rows = await h.client.call_tool("panel_rows", {"pull_id": pull_id})
        message = await h.client.call_tool("put_in_chat", {"pull_id": pull_id, "rows": [100], "columns": ["id"]})
        stored = h.stored("panel_selections")
    assert rows.is_error and rows.structured_content is None
    assert message.is_error and "| 101 |" not in str(message.content)
    assert stored == []  # no selection was made for the AI to fetch
    assert len(reply.ai.structured["rows"]) == reply.ai.structured["sent_rows"]  # the fallback (#41)


async def test_the_ai_sent_log_leaves_out_what_only_the_panel_got(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        pull_id = reply.ai.structured["pull_id"]
        await h.call_from_panel("panel_rows", pull_id=pull_id)
        posted = await h.call_from_panel("put_in_chat", pull_id=pull_id, rows=[0], columns=["id"])
        await h.call("get_selection", selection_id=posted.ai.structured["selection_id"])
        logged = [r["tool"] for r in h.stored("ai_responses")]
        listed = await h.call("sent_to_ai")
        log = await h.call("sent_to_ai", conversation_id=listed.ai.structured["current"])
    # panel_rows went to the panel only; put_in_chat's note is the chat message the AI gets, and get_selection
    # what it fetched: both are logged
    assert logged == ["add_source", "query", "put_in_chat", "get_selection"]
    assert [r["tool"] for r in log.ai.structured["responses"]] == ["add_source", "query", "put_in_chat",
                                                                   "get_selection", "sent_to_ai"]
    assert "250.0" not in log.ai.text


PANEL_TOOLS = ("rerun", "run_saved", "kept_rows", "tweak")


async def _call_each(h: Harness, pull_id: str) -> dict[str, Reply]:
    await h.call("save_pull", pull_id=pull_id, name="All orders")
    return {
        "rerun": await h.call("rerun", pull_id=pull_id),
        "run_saved": await h.call("run_saved", name="All orders"),
        "kept_rows": await h.call("kept_rows", pull_id=pull_id),
        "tweak": await h.call("tweak", pull_id=pull_id, sql=ALL_ORDERS + " LIMIT 240"),
    }


async def test_with_a_panel_every_tool_that_returns_rows_gives_the_ai_only_their_shape(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        await h.call("keep_rows", on=True)
        first = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        replies = await _call_each(h, first.ai.structured["pull_id"])
        lent = {t: await h.call_from_panel("panel_rows", pull_id=r.ai.structured["pull_id"])
                for t, r in replies.items()}
    for tool in PANEL_TOOLS:
        reply = replies[tool]
        assert not reply.is_error, (tool, reply.ai.text)
        assert reply.panel is not None, tool
        assert "rows" not in reply.ai.structured and reply.ai.structured["shown_in"] == "panel", tool
        assert "| 1 |" not in reply.ai.text and "250.0" not in reply.ai.text, tool
        assert lent[tool].ai.structured["rows"][0] == [1, 2, 1.0], tool  # the panel has the rows
    assert replies["kept_rows"].ai.structured["pull_id"] == first.ai.structured["pull_id"]
    assert "sql_diff" in replies["tweak"].ai.structured


async def test_without_a_panel_every_tool_that_returns_rows_gives_the_ai_the_capped_preview(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False, mode="results") as h:
        await h.call("keep_rows", on=True)
        first = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        replies = await _call_each(h, first.ai.structured["pull_id"])
        more = {t: await h.call("more_rows", pull_id=r.ai.structured["pull_id"], rows=5)
                for t, r in replies.items()}
    for tool in PANEL_TOOLS:
        reply = replies[tool]
        assert not reply.is_error, (tool, reply.ai.text)
        assert reply.ai.structured["rows"] == [[i, (i % 3) + 1, float(i)] for i in range(1, AI_ROWS + 1)], tool
        assert f"| {AI_ROWS + 1} |" not in reply.ai.text and "more_rows" in reply.ai.text, tool
        assert more[tool].ai.structured["awaiting_approval"] is True, tool  # the rest is held; the user is asked
