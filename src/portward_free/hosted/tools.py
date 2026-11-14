"""The tools for the web routes on this computer (#104): see which route each connection may use, and put one on the
hosted route ("Portward runs this") or back on the direct route.

The AI never sees a login, a key or the bundle: it is told the connection's routes, the hosted engine's address and
label, and whether the engine has the connections. Putting a connection on the hosted route sends its login to
Portward's hosted engine, so the tool says so plainly every time. After a tool changes what governs a hosted
connection (a profile, a rule, a password), the engine is sent the new bundle (`HostedRefresh`)."""

from __future__ import annotations

import logging
from typing import Any, Literal

import anyio.to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free.account import Accounts
from portward_free.clients import client_support
from portward_free.confirm import confirm_in_client
from portward_free.hosted import bundle
from portward_free.hosted.route import HostedRoute, RouteRefused, Sent, hosted_connections, needs_yes
from portward_free.hosted.service import HostedService
from portward_free.keychain import Keychain
from portward_free.remote.routes import DIRECT_LINE, offered_routes
from portward_free.route_names import DIRECT, HOSTED
from portward_free.remote.routes import HOSTED_LABEL as LABEL
from portward_free.remote.routes import HOSTED_LABEL_PATH
from portward_free.replies import error, reply
from portward_free.storage import LocalStore

log = logging.getLogger("portward.hosted")

# tools after which the hosted engine may need a new bundle: they change a connection, its login or what governs it
CHANGES = frozenset({"add_source", "remove_source", "update_password", "block", "unblock", "set_note",
                     "create_profile", "update_profile", "apply_profile", "unapply_profile", "use_block_rules",
                     "start_profile_from_block_rules", "approve_context", "sync_now", "sync_turn_on"})
WHAT_IT_MEANS = (f"Hosted: {LABEL}. Portward's hosted engine holds this connection's login and applied profiles, "
                 "so it works with this computer off; Portward can technically see what passes through it. Direct: "
                 f"the AI reaches this computer and {DIRECT_LINE}.")


CONFIRM_KEY = "confirm_hosted_route"  # the prompt's key in an input_required round
CONFIRM_STATE = "hosted-route:"  # request_state prefix: the source's name and server follow


def question(source: str, settings: dict[str, Any]) -> str:
    """What the user is asked, in their AI app's own prompt, before a login leaves this computer."""
    where = f"{settings.get('user')}@{settings.get('host')}:{settings.get('port')}/{settings.get('database')}"
    return (f"Send the login and password for {source!r} ({where}) to Portward's hosted engine? Portward runs it, "
            "so Portward can technically see that login and what is read through it. It lets claude.ai and ChatGPT "
            f"on the web use {source!r} with this computer off.")


async def _allowed_here(ctx: Context, routes: HostedRoute, store: LocalStore,
                        source: str) -> CallToolResult | InputRequiredResult | None:
    """The user's yes for putting `source` on the hosted route, asked in the client's own prompt and kept
    (HostedRoute.allow); None once it is allowed. A client that can't ask gets the terminal command instead."""
    if routes.is_allowed(source):
        return None
    connection = store.connection(source)
    if connection is None:
        return error(f"there's no source named {source!r}")
    try:
        bundle.hostable(store, source)  # a file or a laptop-only database: say that, don't ask
    except bundle.NotHostable as err:
        return error(str(err))
    if not client_support(ctx).elicitation:
        return error(needs_yes(source))
    asked = question(source, dict(connection.settings))
    answer = await confirm_in_client(ctx, asked, CONFIRM_KEY, f"{CONFIRM_STATE}{source}\n{asked}")
    if isinstance(answer, InputRequiredResult):
        return answer
    if not answer:
        return error(f"the user didn't allow it: {source} stays off the hosted route and nothing was sent")
    try:
        routes.allow(source)
    except RouteRefused as err:
        return error(str(err))
    return None


def label_url(accounts: Accounts) -> str:
    """The security page the label links to: what the hosted engine holds and can read, and where it runs."""
    return accounts.service.base_url.rstrip("/") + HOSTED_LABEL_PATH


def hosted_route(store: LocalStore, keychain: Keychain, accounts: Accounts) -> HostedRoute:
    return HostedRoute(store, keychain, HostedService(accounts.service.base_url))


def _sent(sent: Sent, about: str) -> dict[str, Any]:
    return {"hosted_address": sent.address and sent.address + "/mcp", "label": LABEL, "label_url": about,
            "delivered": sent.delivered}


def register_web_route_tools(server: MCPServer, store: LocalStore, keychain: Keychain, accounts: Accounts) -> None:
    @server.tool(description="List each connection's web routes for claude.ai and ChatGPT on the web: which it may "
                             "use (files and databases only this computer reaches: direct only) and which it's on. "
                             "Also hands the hosted engine anything it's missing and brings its AI log in.")
    def web_routes() -> CallToolResult:
        route = hosted_route(store, keychain, accounts)
        hosted = set(hosted_connections(store))
        rows = [{"source": c.name, "routes": list(offered_routes(c.kind, c.settings)),
                 "on": HOSTED if c.name in hosted else DIRECT} for c in store.connections()]
        about = label_url(accounts)
        payload: dict[str, Any] = {"connections": rows, "about": f"{WHAT_IT_MEANS} What it holds: {about}",
                                   "label_url": about}
        if hosted:
            try:
                payload |= _sent(route.send(), about)
            except RouteRefused as err:
                payload["hosted_problem"] = str(err)
            payload["collected"] = route.collect()
        return reply(payload)

    @server.tool(description="Put a connection on the 'hosted' route (Portward runs this: works with this computer "
                             "off; its login and applied profiles go, sealed, to Portward's hosted engine) or the "
                             f"'direct' route (the AI reaches this computer; {DIRECT_LINE}). Before a "
                             "login goes to the hosted engine, Portward asks the user to confirm it themselves.")
    async def web_route(source: str, route: Literal["hosted", "direct"],
                        ctx: Context) -> CallToolResult | InputRequiredResult:
        routes = hosted_route(store, keychain, accounts)
        if route == HOSTED and (stopped := await _allowed_here(ctx, routes, store, source)) is not None:
            return stopped
        try:
            sent = await anyio.to_thread.run_sync(routes.put_on if route == HOSTED else routes.take_off, source)
        except RouteRefused as err:
            return error(str(err))
        if route == HOSTED:
            text = (f"{source} is on the hosted route ({LABEL}: what it holds and can read is at "
                    f"{label_url(accounts)}). {sent.note} Add the connector address "
                    f"{sent.address}/mcp in claude.ai or ChatGPT and sign in with the Portward account.")
        else:
            text = f"{source} is on the direct route: {DIRECT_LINE}. {sent.note}"
        return reply({"source": source, "on": route, **_sent(sent, label_url(accounts))}, text)


class HostedRefresh:
    """MCP middleware: after a tool in CHANGES, hand the hosted engine the new bundle if anything it holds changed."""

    def __init__(self, store: LocalStore, keychain: Keychain, accounts: Accounts) -> None:
        self.route = hosted_route(store, keychain, accounts)
        self.store = store

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if ctx.method == "tools/call" and (ctx.params or {}).get("name") in CHANGES and hosted_connections(self.store):
            await anyio.to_thread.run_sync(self.route.refresh)
        return result
