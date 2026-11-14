"""Sending more of a pull than its preview, only after the user approves.

The AI gets a pull's first PREVIEW_ROWS rows. Every later batch is approved by the user first: in the client's own
prompt (elicitation) where it has one, otherwise by a plain-text question the AI relays, and a second call that
says the user agreed. Rows are held in memory for this session only; nothing more is stored.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import Context
from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from pydantic import BaseModel, Field

from portward_free.pull import Pull

MORE_ROWS_DEFAULT = 100  # rows one approval sends when the AI doesn't say how many
MORE_ROWS_MAX = 500  # most rows one approval can send
HELD_PULLS = 50  # recent pulls this session can still send more of
APPROVAL_KEY = "approve_more_rows"  # the input request's key in an input_required round


class Approve(BaseModel):
    """The form the client shows the user."""

    approve: bool = Field(title="Send these rows to the AI?")


@dataclass(frozen=True)
class Held:
    pull_id: str
    pull: Pull
    sent: int  # rows the AI has had, from the top
    asked: tuple[int, int] | None = None  # the batch (first, last) a plain-text question is waiting on

    @property
    def remaining(self) -> int:
        return len(self.pull.rows) - self.sent

    def next_batch(self, rows: int) -> tuple[int, int]:
        """The 1-based (first, last) rows of the next batch of at most `rows` (capped at MORE_ROWS_MAX)."""
        count = min(max(rows, 1), MORE_ROWS_MAX, self.remaining)
        return self.sent + 1, self.sent + count

    def question(self, batch: tuple[int, int]) -> str:
        first, last = batch
        total = f"more than {len(self.pull.rows)}" if self.pull.truncated else str(len(self.pull.rows))
        return (f"Send {last - first + 1} more rows (rows {first}-{last} of {total}) of pull {self.pull_id} "
                f"({self.pull.question!r}, columns {', '.join(self.pull.columns)}) to the AI?")


class Outbox:
    """The pulls of this session the AI can ask for more of, and how far each has been sent."""

    def __init__(self, keep: int = HELD_PULLS) -> None:
        self._keep = keep
        self._held: OrderedDict[str, Held] = OrderedDict()

    def hold(self, pull_id: str, pull: Pull, sent: int | None = None) -> None:
        """Hold `pull`, its first `sent` rows (the preview by default) already with the AI."""
        self._put(Held(pull_id, pull, sent=len(pull.preview) if sent is None else sent))
        while len(self._held) > self._keep:
            self._held.popitem(last=False)

    def get(self, pull_id: str) -> Held | None:
        return self._held.get(pull_id)

    def ask(self, held: Held, batch: tuple[int, int]) -> None:
        self._put(replace(held, asked=batch))

    def sent(self, held: Held, batch: tuple[int, int]) -> Held:
        updated = replace(held, sent=batch[1], asked=None)
        self._put(updated)
        return updated

    def _put(self, held: Held) -> None:
        self._held[held.pull_id] = held


def _batch_state(batch: tuple[int, int]) -> str:
    return f"rows:{batch[0]}-{batch[1]}"


async def ask_in_client(ctx: Context[Any], question: str, batch: tuple[int, int]) -> bool | InputRequiredResult:
    """Ask the user in the client's own prompt. True only for an explicit yes.

    On handshake connections (e.g. Claude Code today) the server sends the question as its own request. On
    2026-07-28 connections the tool returns an InputRequiredResult and the client retries the call with the
    answer; the answer counts only for the batch it was asked about (`request_state`).
    """
    if ctx.session.can_send_request:
        answer = await ctx.elicit(question, Approve)
        return answer.action == "accept" and answer.data.approve
    response = (ctx.input_responses or {}).get(APPROVAL_KEY)
    if response is None or ctx.request_state != _batch_state(batch):
        params = ElicitRequestFormParams(message=question, requested_schema=render_elicitation_schema(Approve))
        return InputRequiredResult(input_requests={APPROVAL_KEY: ElicitRequest(params=params)},
                                   request_state=_batch_state(batch))
    return (isinstance(response, ElicitResult) and response.action == "accept"
            and (response.content or {}).get("approve") is True)
