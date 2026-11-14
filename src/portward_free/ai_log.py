"""Log every tool response the AI gets, per conversation, so the user can see exactly what left their machine.

`AiLog` is server middleware: every `tools/call` passes through it, whichever tool answers, so a new tool (or a
change to what an existing tool returns) is logged without doing anything. It logs what reaches the model: the
content blocks and the structured content. A result's `_meta`, and app-only tools (`panel_rows`, `value_source`:
only the results panel calls them), are not logged, because the AI never sees them. The one exception is
`put_in_chat` (POSTED): its text is the short note the user posts from the panel (a selection id, no values), so it
reaches the AI as a message rather than a tool response; it is logged exactly as built, sealed like every other
entry. The rows the AI then fetches for that note come back from `get_selection`, a model-visible tool, so they are
logged like any other response.

MCP has no conversation id, and on the 2026-07-28 protocol every request is its own connection, so a conversation
is one run of the server: the local server serves one client over one stdio pipe (Claude Code starts one per
session, Claude Desktop one per app launch). It is created on the run's first tool call.

What was asked and sent (arguments, text, content blocks, structured content) is sealed with the same keychain key
and AES-GCM as kept rows (`kept`), so preview rows never sit in the local file in plaintext. If the keychain can't
be used, only the tool, time and error flag are logged, with the reason; the review says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, InputRequiredResult, TextContent

from portward_free import kept
from portward_free.clients import client_name
from portward_free.keychain import Keychain, KeychainError
from portward_free.storage import LocalStore, LoggedResponse
from portward_free.storage.store import now

logger = logging.getLogger(__name__)

TOOL_CALL = "tools/call"
AI_LOG_DAYS = 30  # conversations older than this are deleted when the server starts (same as kept result rows)
# tool arguments never written to the log: a source password (it lives in the keychain) and the code that confirms
# the user's email for sharing (share_confirm; it is what makes the device's share token)
SECRET_ARGUMENTS = frozenset({"password", "code"})
REDACTED = "(not logged)"
POSTED = frozenset({"put_in_chat"})  # app-only tools whose text the panel posts in the chat: that text reaches the AI
# (put_in_chat's note; the rows it names are logged when the AI fetches them with get_selection)
WHAT = "what was sent to the AI"


@dataclass(frozen=True)
class AiResponse:
    """One tool response as the AI got it. When `withheld` is set the content wasn't stored (or can't be opened
    now), and it says why; arguments, text, content and structured are then empty."""

    conversation_id: str
    tool: str
    is_error: bool
    arguments: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    content: tuple[dict[str, Any], ...] = ()
    structured: dict[str, Any] | None = None
    withheld: str | None = None
    created_at: str = field(default_factory=lambda: now())


def _associated(conversation_id: str) -> str:
    return f"ai-log:{conversation_id}"  # never a pull id, so a log blob can't pass for kept rows or the reverse


class AiLog:
    def __init__(self, store: LocalStore, keychain: Keychain, skip: frozenset[str] = frozenset()) -> None:
        """`skip` names the app-only tools: only the panel calls them and gets their results, never the AI."""
        self._store = store
        self._keychain = keychain
        self._skip = skip
        store.prune_conversations(datetime.now(UTC) - timedelta(days=AI_LOG_DAYS))
        self._conversation_id: str | None = None

    def current(self, session: Any) -> str:
        """This run's conversation id, starting the conversation on the first call (`session` names the client)."""
        if self._conversation_id is None:
            self._conversation_id = self._store.start_conversation(client_name(session)).id
        return self._conversation_id

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        tool = (ctx.params or {}).get("name")
        if ctx.method != TOOL_CALL or (tool in self._skip and tool not in POSTED):
            return await call_next(ctx)
        conversation_id = self.current(ctx.session)
        params = dict(ctx.params or {})
        try:
            result = await call_next(ctx)
        except MCPError as err:
            self._log(conversation_id, params, CallToolResult(
                content=[TextContent(type="text", text=err.error.message)], is_error=True))
            raise
        if isinstance(result, InputRequiredResult) or (isinstance(result, dict) and "inputRequests" in result):
            return result  # a prompt for the user's client (e.g. more_rows approval), not a response to the AI
        sent = _as_tool_result(result)
        if tool in POSTED and (sent is None or sent.is_error):
            return result  # nothing was posted: the panel shows its own error
        if sent is None:
            logger.warning("tool %r returned a result the AI log can't read; not logged", params.get("name"))
        else:
            self._log(conversation_id, params, sent)
        return result

    def _log(self, conversation_id: str, params: dict[str, Any], result: CallToolResult) -> None:
        tool = str(params.get("name", ""))
        sent = {
            "arguments": _without_secrets(params.get("arguments") or {}),
            "text": "\n".join(c.text for c in result.content if isinstance(c, TextContent)),
            "content": [c.model_dump(mode="json", by_alias=True, exclude_none=True) for c in result.content],
            "structured": result.structured_content,
        }
        sealed, withheld = None, None
        try:
            secret = kept.key(self._keychain, create=True)
            assert secret is not None  # create=True always gives a key
            sealed = kept.seal_json(secret, _associated(conversation_id), sent)
        except (KeychainError, kept.RowsLocked) as err:
            withheld = f"the keychain couldn't be used ({err})"
            logger.warning("logged only that %r was called: %s", tool, withheld)
        self._store.log_response(LoggedResponse(conversation_id=conversation_id, tool=tool,
                                                is_error=bool(result.is_error), sealed=sealed, withheld=withheld))

    def responses(self, conversation_id: str) -> tuple[AiResponse, ...]:
        """Every response the AI got in one conversation, in order, opened with the keychain key."""
        logged = self._store.responses(conversation_id)
        secret: bytes | None = None
        locked: str | None = None
        if any(r.sealed is not None for r in logged):
            try:
                secret = kept.stored_key(self._keychain, f"{WHAT} in this conversation")
            except (KeychainError, kept.RowsLocked) as err:
                locked = str(err)
        return tuple(self._opened(r, secret, locked) for r in logged)

    def _opened(self, logged: LoggedResponse, secret: bytes | None, locked: str | None) -> AiResponse:
        meta = {"conversation_id": logged.conversation_id, "tool": logged.tool, "is_error": logged.is_error,
                "created_at": logged.created_at}
        if logged.sealed is None:
            return AiResponse(**meta, withheld=f"isn't stored: {logged.withheld or 'no reason recorded'}")
        if secret is None:
            return AiResponse(**meta, withheld=f"can't be opened: {locked}")
        try:
            sent = kept.open_json(secret, _associated(logged.conversation_id), logged.sealed, WHAT)
        except kept.RowsLocked as err:
            return AiResponse(**meta, withheld=f"can't be opened: {err}")
        return AiResponse(**meta, arguments=sent["arguments"], text=sent["text"], content=tuple(sent["content"]),
                          structured=sent["structured"])


def _without_secrets(arguments: dict[str, Any]) -> dict[str, Any]:
    return {k: REDACTED if k in SECRET_ARGUMENTS and v else v for k, v in arguments.items()}


def _as_tool_result(result: HandlerResult) -> CallToolResult | None:
    if isinstance(result, CallToolResult):
        return result
    if isinstance(result, dict) and "content" in result:
        return CallToolResult.model_validate(result)
    return None
