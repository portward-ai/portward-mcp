"""A yes/no question to the user in the client's own prompt (form elicitation), for a step only they may take.

On handshake connections (e.g. Claude Code today) the server sends the question as its own request. On 2026-07-28
connections the tool returns an InputRequiredResult and the client retries the call with the answer; an answer counts
only for the question it was asked about (`state`). Only an explicit yes is a yes: declined, cancelled or anything
else is no. The AI can't answer: it writes tool arguments, never the client's prompt response.
"""

from __future__ import annotations

from typing import Any

from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import Context
from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from pydantic import BaseModel, Field


class Confirm(BaseModel):
    yes: bool = Field(title="Yes")


async def confirm_in_client(ctx: Context[Any], question: str, key: str, state: str) -> bool | InputRequiredResult:
    """Ask `question`; True only for an explicit yes. `key` names the input request, `state` the question."""
    if ctx.session.can_send_request:
        answer = await ctx.elicit(question, Confirm)
        return answer.action == "accept" and answer.data.yes is True
    response = (ctx.input_responses or {}).get(key)
    if response is None or ctx.request_state != state:
        params = ElicitRequestFormParams(message=question, requested_schema=render_elicitation_schema(Confirm))
        return InputRequiredResult(input_requests={key: ElicitRequest(params=params)}, request_state=state)
    return (isinstance(response, ElicitResult) and response.action == "accept"
            and (response.content or {}).get("yes") is True)
