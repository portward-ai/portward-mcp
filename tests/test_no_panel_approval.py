"""Clients without a results panel (e.g. Claude Code): a capped preview, and the user approves before more is sent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import ORDER_COUNT
from harness import connected
from mcp.types import ElicitRequestParams, ElicitResult

from portward_free.modes import AI_ROWS

ALL_ORDERS = "SELECT id, total FROM orders ORDER BY id"


class User:
    """Answers the client's approval prompts (elicitation) and remembers what it was asked."""

    def __init__(self, action: str = "accept", content: dict[str, Any] | None = None) -> None:
        self.action = action
        self.content = content if content is not None else {"approve": True}
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
        self.asked.append(params.message)
        return ElicitResult(action=self.action, content=self.content if self.action == "accept" else None)


def orders(first: int, last: int) -> list[list[Any]]:
    """Rows `first`..`last` (1-based, inclusive) of ALL_ORDERS, as the shop fixture defines them."""
    return [[i, float(i)] for i in range(first, last + 1)]


PROTOCOLS = pytest.mark.parametrize("protocol", ["modern", "legacy"])


async def test_without_a_panel_a_large_result_is_a_capped_preview_with_a_summary(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False, mode="results") as shop:
        reply = await shop.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["rows"] == orders(1, AI_ROWS)
    assert f"| {AI_ROWS + 1} |" not in reply.ai.text
    assert f"{ORDER_COUNT} rows" in reply.ai.text and "columns: id, total" in reply.ai.text
    assert "more_rows" in reply.ai.text  # how to ask for more, which asks the user first


async def test_with_a_panel_the_reply_does_not_point_the_ai_at_the_fallback(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as shop:
        reply = await shop.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    assert "rows" not in reply.ai.structured  # the panel shows them (#40)
    assert "more_rows" not in reply.ai.text


async def test_over_stdio_like_claude_code_the_client_prompt_approves_more(home: Path, shop_db: Path) -> None:
    user = User()
    async with connected(home, shop_db, transport="stdio", protocol="legacy", elicitation=user, mode="results") as shop:
        pulled = await shop.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        more = await shop.call("more_rows", pull_id=pulled.ai.structured["pull_id"], rows=10)
    assert len(user.asked) == 1
    assert more.ai.structured["rows"] == orders(AI_ROWS + 1, AI_ROWS + 10)


async def test_a_result_that_fits_in_the_preview_does_not_offer_more(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as shop:
        reply = await shop.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        more = await shop.call("more_rows", pull_id=reply.ai.structured["pull_id"])
    assert "more_rows" not in reply.ai.text
    assert more.ai.structured["rows"] == [] and "already been sent" in more.ai.text


async def test_more_rows_of_a_pull_this_session_does_not_hold_is_refused(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as shop:
        reply = await shop.call("more_rows", pull_id="not-a-pull")
    assert reply.is_error and "run the query again" in reply.ai.text


@PROTOCOLS
async def test_approving_in_the_client_prompt_sends_the_next_rows(home: Path, shop_db: Path, protocol: str) -> None:
    user = User()
    async with connected(home, shop_db, elicitation=user, protocol=protocol, mode="results") as shop:
        pulled = await shop.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        more = await shop.call("more_rows", pull_id=pulled.ai.structured["pull_id"], rows=50)
    assert not more.is_error, more.ai.text
    assert len(user.asked) == 1 and "50" in user.asked[0] and str(ORDER_COUNT) in user.asked[0]
    assert more.ai.structured["rows"] == orders(AI_ROWS + 1, AI_ROWS + 50)
    assert more.ai.structured["sent_rows"] == AI_ROWS + 50
    assert more.ai.structured["row_count"] == ORDER_COUNT


@PROTOCOLS
@pytest.mark.parametrize("answer", [{"action": "decline"}, {"action": "cancel"}, {"content": {"approve": False}}],
                         ids=["decline", "cancel", "says-no"])
async def test_without_a_yes_in_the_client_prompt_no_more_rows_are_sent(
        home: Path, shop_db: Path, protocol: str, answer: dict[str, Any]) -> None:
    user = User(**answer)
    async with connected(home, shop_db, elicitation=user, protocol=protocol, mode="results") as shop:
        pulled = await shop.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        more = await shop.call("more_rows", pull_id=pulled.ai.structured["pull_id"], rows=50)
        again = await shop.call("more_rows", pull_id=pulled.ai.structured["pull_id"], rows=50, approved=True)
    for reply in (more, again):
        assert not reply.is_error, reply.ai.text
        assert reply.ai.structured["rows"] == []
        assert reply.ai.structured["sent_rows"] == AI_ROWS
        assert f"| {AI_ROWS + 1} |" not in reply.ai.text
    assert len(user.asked) == 2  # the AI saying "approved" doesn't skip the client's prompt


async def test_without_a_client_prompt_the_tool_asks_in_plain_text_and_waits(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as shop:
        pulled = await shop.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        pull_id = pulled.ai.structured["pull_id"]
        asked = await shop.call("more_rows", pull_id=pull_id, rows=50)
        sent = await shop.call("more_rows", pull_id=pull_id, rows=50, approved=True)
    assert not asked.is_error, asked.ai.text
    assert asked.ai.structured["rows"] == [] and asked.ai.structured["awaiting_approval"] is True
    assert "ask the user" in asked.ai.text.lower() and str(ORDER_COUNT) in asked.ai.text
    assert f"| {AI_ROWS + 1} |" not in asked.ai.text
    assert not sent.is_error, sent.ai.text
    assert sent.ai.structured["rows"] == orders(AI_ROWS + 1, AI_ROWS + 50)


async def test_without_a_client_prompt_approval_must_follow_the_question_for_the_same_rows(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as shop:
        pulled = await shop.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        pull_id = pulled.ai.structured["pull_id"]
        unasked = await shop.call("more_rows", pull_id=pull_id, rows=50, approved=True)
        smaller = await shop.call("more_rows", pull_id=pull_id, rows=30, approved=True)
    for reply in (unasked, smaller):
        assert reply.ai.structured["rows"] == [] and reply.ai.structured["awaiting_approval"] is True
