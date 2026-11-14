"""Every response the AI gets is logged per conversation, so the user can review exactly what left their machine."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from harness import Harness, LockedKeychain, MemoryKeychain, Reply, connected, file_bytes
from test_no_panel_approval import ALL_ORDERS, User, orders

from portward_free.ai_log import AI_LOG_DAYS
from portward_free.kept import KEY_ACCOUNT
from portward_free.modes import AI_ROWS

NAMES = "SELECT name FROM customers ORDER BY id"


async def logged_now(h: Harness) -> list[dict]:
    """The responses logged so far in this conversation, as sent_to_ai shows them (before its own reply)."""
    listed: Reply = await h.call("sent_to_ai")
    log = await h.call("sent_to_ai", conversation_id=listed.ai.structured["current"])
    return log.ai.structured["responses"]


async def test_every_response_to_the_ai_is_logged_exactly_as_the_ai_got_it(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        pulled = await h.call("query", source="shop", question="Names", sql=NAMES)
        refused = await h.call("query", source="shop", question="?", sql="DELETE FROM customers")
        listed = await h.call("sent_to_ai")
        conversation = listed.ai.structured["conversations"][0]
        log = await h.call("sent_to_ai", conversation_id=conversation["id"])
    assert listed.ai.structured["current"] == conversation["id"]
    responses = log.ai.structured["responses"]
    tools = [r["tool"] for r in responses]
    assert tools == ["add_source", "query", "query", "sent_to_ai"]
    query = responses[1]
    assert query["text"] == pulled.ai.text
    assert query["structured"] == pulled.ai.structured
    assert query["arguments"] == {"source": "shop", "question": "Names", "sql": NAMES}
    assert query["is_error"] is False
    assert responses[2]["is_error"] is True and responses[2]["text"] == refused.ai.text
    assert "Grace" in log.ai.text  # the review shows what was sent


async def test_the_log_is_kept_in_the_local_database_per_conversation(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("query", source="shop", question="Names", sql=NAMES)
    async with Harness.start(home) as h:
        await h.call("list_sources")
        listed = await h.call("sent_to_ai")
        stored = h.stored("ai_responses")
    conversations = listed.ai.structured["conversations"]
    assert len(conversations) == 2
    latest, earlier = conversations
    assert latest["id"] == listed.ai.structured["current"]
    assert latest["responses"] == 1 and earlier["responses"] == 2  # sent_to_ai is logged after it replies
    by_conversation = {r["conversation_id"] for r in stored}
    assert by_conversation == {latest["id"], earlier["id"]}
    assert all(r["sealed"] for r in stored)


async def test_what_was_sent_is_sealed_with_the_keychain_key_and_never_stored_in_plaintext(
        home: Path, shop_db: Path) -> None:
    keychain = MemoryKeychain()
    async with connected(home, shop_db, keychain=keychain, mode="results") as h:
        await h.call("query", source="shop", question="Names", sql=NAMES)
        stored = h.stored("ai_responses")
        conversation_id = stored[0]["conversation_id"]
    async with Harness.start(home, keychain=keychain) as h:  # a later run reads it back with the same key
        log = await h.call("sent_to_ai", conversation_id=conversation_id)
    assert set(keychain.entries) == {KEY_ACCOUNT}
    assert {"conversation_id", "created_at", "tool", "is_error", "sealed"} <= set(stored[0])
    assert not {"text", "content", "structured", "arguments"} & set(stored[0])
    for marker in (b"Grace", b"Linus"):  # the SQL is in history (pulls) by design; result values are nowhere
        assert marker not in file_bytes(home)
    query = log.ai.structured["responses"][1]
    assert query["structured"]["rows"] == [["Ada"], ["Grace"], ["Linus"]]
    assert query["arguments"]["sql"] == NAMES


async def test_when_the_keychain_cant_be_used_only_what_was_called_is_logged_and_the_review_says_so(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, keychain=LockedKeychain()) as h:
        pulled = await h.call("query", source="shop", question="Names", sql=NAMES)
        stored = h.stored("ai_responses")
        log = await h.call("sent_to_ai", conversation_id=stored[0]["conversation_id"])
    assert not pulled.is_error
    assert [r["tool"] for r in stored] == ["add_source", "query"]
    assert all(r["sealed"] is None for r in stored)
    assert b"Grace" not in file_bytes(home)
    query = log.ai.structured["responses"][1]
    assert query["tool"] == "query" and query["structured"] is None and query["text"] == ""
    assert "locked" in query["withheld"]
    assert "Grace" not in log.ai.text and "isn't stored" in log.ai.text and "locked" in log.ai.text


async def test_a_log_whose_key_is_gone_says_it_cant_be_opened(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("query", source="shop", question="Names", sql=NAMES)
        conversation_id = h.stored("ai_responses")[0]["conversation_id"]
    async with Harness.start(home, keychain=MemoryKeychain()) as h:  # a new, empty keychain: the key is gone
        log = await h.call("sent_to_ai", conversation_id=conversation_id)
    assert not log.is_error
    responses = log.ai.structured["responses"]
    assert [r["tool"] for r in responses] == ["add_source", "query"]
    assert all(r["withheld"] and r["text"] == "" for r in responses)
    assert "can't be opened" in log.ai.text


async def test_conversations_older_than_the_retention_are_pruned_when_the_server_starts(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        await h.call("query", source="shop", question="Names", sql=NAMES)
    old = (datetime.now(UTC) - timedelta(days=AI_LOG_DAYS + 1)).isoformat(timespec="seconds")
    with sqlite3.connect(home / "portward.db") as db:
        db.execute("UPDATE conversations SET started_at = ?", (old,))
        db.execute("UPDATE ai_responses SET created_at = ?", (old,))
    db.close()
    async with Harness.start(home) as h:
        assert h.stored("conversations") == [] and h.stored("ai_responses") == []
        await h.call("list_sources")
        assert len(h.stored("conversations")) == 1


async def test_responses_over_stdio_are_logged_with_the_client_name(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, transport="stdio") as h:
        pulled = await h.call("query", source="shop", question="Names", sql=NAMES)
        (conversation,) = h.stored("conversations")
        log = await h.call("sent_to_ai", conversation_id=conversation["id"])
    assert conversation["client"]
    responses = log.ai.structured["responses"]
    assert [r["tool"] for r in responses] == ["add_source", "query"]
    assert responses[1]["text"] == pulled.ai.text


@pytest.mark.parametrize("protocol", ["modern", "legacy"])
async def test_rows_the_user_approves_with_more_rows_are_logged_as_sent(
        home: Path, shop_db: Path, protocol: str) -> None:
    async with connected(home, shop_db, elicitation=User(), protocol=protocol, mode="results") as h:
        pulled = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        more = await h.call("more_rows", pull_id=pulled.ai.structured["pull_id"], rows=30)
        responses = await logged_now(h)
    assert more.ai.structured["rows"] == orders(AI_ROWS + 1, AI_ROWS + 30)
    logged = [r for r in responses if r["tool"] == "more_rows"]
    assert len(logged) == 1  # the client prompt in between is not a response to the AI
    assert logged[0]["structured"]["rows"] == more.ai.structured["rows"]
    assert logged[0]["text"] == more.ai.text


async def test_without_a_client_prompt_the_question_and_then_the_approved_rows_are_logged(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        pulled = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        pull_id = pulled.ai.structured["pull_id"]
        asked = await h.call("more_rows", pull_id=pull_id, rows=10)
        sent = await h.call("more_rows", pull_id=pull_id, rows=10, approved=True)
        responses = [r for r in await logged_now(h) if r["tool"] == "more_rows"]
    assert asked.ai.structured["awaiting_approval"] is True
    assert [r["structured"]["rows"] for r in responses] == [[], orders(AI_ROWS + 1, AI_ROWS + 10)]
    assert responses[1]["text"] == sent.ai.text


async def test_reviewing_an_unknown_conversation_is_an_error(home: Path) -> None:
    async with Harness.start(home) as h:
        reply = await h.call("sent_to_ai", conversation_id="nope")
    assert reply.is_error


async def test_the_note_put_in_the_chat_and_the_rows_fetched_for_it_are_logged_exactly_and_sealed(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        pulled = await h.call("query", source="shop", question="Names", sql=NAMES)
        posted = await h.call_from_panel("put_in_chat", pull_id=pulled.ai.structured["pull_id"], rows=[1],
                                         columns=["name"])
        await h.call_from_panel("panel_rows", pull_id=pulled.ai.structured["pull_id"])  # the panel's own read
        fetched = await h.call("get_selection", selection_id=posted.ai.structured["selection_id"])
        responses = await logged_now(h)
        on_disk = file_bytes(home)
    note = posted.ai.structured["text"]
    assert "Grace" not in note and "Grace" in fetched.ai.text
    chat = [r for r in responses if r["tool"] == "put_in_chat"]
    assert len(chat) == 1 and chat[0]["text"] == note  # the exact message the panel posts
    sent = [r for r in responses if r["tool"] == "get_selection"]
    assert len(sent) == 1 and sent[0]["text"] == fetched.ai.text  # the rows the AI fetched for it
    assert sent[0]["structured"] == fetched.ai.structured
    assert "panel_rows" not in [r["tool"] for r in responses]  # rows lent to the panel never reached the AI
    assert b"Grace" not in on_disk  # sealed like every other entry


async def test_a_refused_selection_fetch_is_logged_too(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        refused = await h.call("get_selection", selection_id="sel_guessed")
        responses = await logged_now(h)
    assert refused.is_error
    assert [(r["tool"], r["text"]) for r in responses if r["tool"] == "get_selection"] == [
        ("get_selection", refused.ai.text)]


async def test_no_tool_call_means_no_conversation_is_logged(home: Path) -> None:
    async with Harness.start(home) as h:
        await h.tools()
        assert h.stored("conversations") == []
