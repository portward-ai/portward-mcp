"""Personal's tools only where Personal is on (#116).

Personal's tools (profiles, context, sync, share management, the proof report, web routes) are registered like any
other, through `Recorder`, which notes their names. `PlanGate` then keeps them out of the tool list, and refuses them
as unknown, unless this computer is signed in to an account with Personal on (trial, active or grace). A Free
conversation is shown Free's tools only.

After a lapse, profiles already applied keep applying as frozen profiles (profile_tools.py); the tools that still
matter for them stay listed whatever the plan (STILL_AFTER_A_LAPSE): listing the profiles and taking one off a
connection, which only ever shows less.

The plan is asked of the account service when the client reads the tool list, and again after the account tools run
(signing in finishes in the browser, then the AI calls account_status). When the list changes the server says so:
`notifications/tools/list_changed` on the session for clients on the handshake protocol, and a ToolsListChanged event
on the subscriptions bus for the 2026-07-28 protocol. The account tool's reply says so too, with a restart of the AI
app as the fallback for apps that don't read the list again. When the account service can't be reached, the plan
last seen decides (account.known_plan); with none seen, Free's list."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from mcp.server import ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.mcpserver import MCPServer
from mcp.server.subscriptions import SubscriptionBus
from mcp.shared.subscriptions import ToolsListChanged
from mcp.types import CallToolResult, TextContent

from portward_free.account import Accounts, AccountServiceError, account_token, known_plan, plan_of, remember_plan
from portward_free.keychain import Keychain, KeychainError
from portward_free.storage import LocalStore

logger = logging.getLogger(__name__)

STILL_AFTER_A_LAPSE = frozenset({"list_profiles", "unapply_profile"})
ACCOUNT_TOOLS = frozenset({"account_sign_in", "account_status", "account_sign_out"})
SHOWN = ("Portward Personal's tools are on in this conversation now. If they don't show up, restart the AI app and "
         "they will.")
HIDDEN = "Personal's tools are off in this conversation now."


class Recorder:
    """`server` for the register functions of Personal's tools: registers each tool as usual and keeps its name."""

    def __init__(self, server: MCPServer) -> None:
        self._server = server
        self.names: set[str] = set()

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        register = self._server.tool(*args, **kwargs)

        def named(fn: Callable[..., Any]) -> Any:
            self.names.add(kwargs.get("name") or fn.__name__)
            return register(fn)

        return named

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)


def personal_on(store: LocalStore, keychain: Keychain, accounts: Accounts) -> bool:
    """Whether this computer's account has Personal on now; offline, as last seen."""
    try:
        token = account_token(keychain)
    except KeychainError:
        return False
    if not token:
        return False
    try:
        plan = plan_of(accounts.service.me(token))
    except AccountServiceError as err:
        if err.status == 401:
            return False
        seen = known_plan(store)
        return bool(seen and seen.get("state") != "ended")
    if plan is not None:
        remember_plan(store, plan)
    return bool(plan and plan["personal"])


def known_free(keychain: Keychain, accounts: Accounts) -> bool:
    """This computer is known to be on Free: signed out, or its account's plan has ended. Never when the plan
    couldn't be checked (offers are made only then, offers.py)."""
    try:
        token = account_token(keychain)
        if not token:
            return True
        plan = plan_of(accounts.service.me(token))
    except KeychainError:
        return False
    except AccountServiceError as err:
        return err.status == 401
    return plan is not None and not plan["personal"]


class PlanGate:
    """Middleware: Personal's tools (`personal`) are listed and callable only while Personal is on."""

    def __init__(self, store: LocalStore, keychain: Keychain, accounts: Accounts, personal: frozenset[str],
                 bus: SubscriptionBus) -> None:
        self._store, self._keychain, self._accounts = store, keychain, accounts
        self._personal = personal
        self._bus = bus
        self._hidden: frozenset[str] | None = None

    def hidden(self) -> frozenset[str]:
        """The tools to keep from the AI now, asking for the plan."""
        if personal_on(self._store, self._keychain, self._accounts):
            found: frozenset[str] = frozenset()
        elif self._store.profiles.all():
            found = self._personal - STILL_AFTER_A_LAPSE
        else:
            found = self._personal
        self._hidden = found
        return found

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method == "tools/list":
            hidden = self.hidden()
            return _without(await call_next(ctx), hidden)
        if ctx.method != "tools/call":
            return await call_next(ctx)
        name = str((ctx.params or {}).get("name", ""))
        # A tool hidden when last asked is asked about again: the user may have signed in since the list was read.
        if name in self._last_hidden() and name in self.hidden():
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], is_error=True)
        if name not in ACCOUNT_TOOLS:
            return await call_next(ctx)
        before = self._last_hidden()
        result = await call_next(ctx)
        after = self.hidden()
        if after == before:
            return result
        await self._announce(ctx)
        return _noted(result, HIDDEN if after else SHOWN)

    def _last_hidden(self) -> frozenset[str]:
        """The tools kept from the AI when last asked (asking now if never)."""
        return self._hidden if self._hidden is not None else self.hidden()

    async def _announce(self, ctx: ServerRequestContext[Any, Any]) -> None:
        try:
            await ctx.session.send_tool_list_changed()
        except Exception as err:  # noqa: BLE001 - a client gone away mustn't fail the tool call
            logger.info("tools/list_changed not sent on the session: %s", err)
        await self._bus.publish(ToolsListChanged())


def _without(result: HandlerResult, hidden: frozenset[str]) -> HandlerResult:
    if not hidden or result is None:
        return result
    if isinstance(result, dict):
        return {**result, "tools": [t for t in result.get("tools", []) if t.get("name") not in hidden]}
    return result.model_copy(update={"tools": [t for t in result.tools if t.name not in hidden]})  # type: ignore[attr-defined]


def _noted(result: HandlerResult, note: str) -> HandlerResult:
    """`result` (a tool's reply) with `note` as its last line, and as `tools` in its structured content."""
    if isinstance(result, CallToolResult):
        text = "".join(c.text for c in result.content if isinstance(c, TextContent))
        return result.model_copy(update={
            "content": [TextContent(type="text", text=f"{text}\n\n{note}".strip())],
            "structured_content": {**(result.structured_content or {}), "tools": note}})
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        text = "".join(c.get("text", "") for c in result["content"] if isinstance(c, dict))
        structured = result.get("structuredContent")
        return {**result, "content": [{"type": "text", "text": f"{text}\n\n{note}".strip()}],
                "structuredContent": {**(structured if isinstance(structured, dict) else {}), "tools": note}}
    logger.info("tool reply not noted: %s", json.dumps(type(result).__name__))
    return result
