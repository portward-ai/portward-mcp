"""The tools for the user's own settings: usage counts, the local Portward page, offers."""

from __future__ import annotations

import json

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult

from portward_free import offers
from portward_free.clients import client_support
from portward_free.local_page import local_page
from portward_free.plan_tools import known_free
from portward_free.replies import error, reply
from portward_free.runtime import Runtime

OPEN_PAGE_COMMAND = "uvx portward-free page"


def register_usage_panel_tools(rt: Runtime) -> None:
    """The results panel shows the usage question (usage.USAGE_META) and says so; the user can turn counts off
    there. App-only: the AI can't say the user saw it."""

    @rt.app_tool(name="usage_seen", description="The results panel showed the user the usage-counts question. "
                                                "App-only.")
    def usage_seen(ctx: Context) -> CallToolResult:
        if not client_support(ctx).panel:
            return error("usage_seen is for the results panel only")
        rt.usage.seen("panel")
        return reply({"usage_counts": rt.usage.state}, "noted")

    @rt.app_tool(name="usage_choice", description="The user turned usage counts on or off in the results panel. "
                                                  "App-only.")
    def usage_choice(on: bool, ctx: Context) -> CallToolResult:
        if not client_support(ctx).panel:
            return error("usage_choice is for the results panel only")
        try:
            rt.usage.set(on, by_user=True)
        except ValueError as err:
            return error(str(err))
        return reply({"usage_counts": rt.usage.state}, f"usage counts are {rt.usage.state}")


def register_settings_tools(server: MCPServer, rt: Runtime) -> None:
    _register_usage_counts(server, rt)
    _register_open_page(server, rt)


def _register_usage_counts(server: MCPServer, rt: Runtime) -> None:
    @server.tool(description="Anonymous usage counts: whether they're on and exactly what is counted and would be "
                             "sent next.")
    def usage_counts() -> CallToolResult:
        return _usage_status(rt)

    @server.tool(description="Turn anonymous usage counts off (on=false; the counts kept so far are deleted) or back "
                             "on (on=true, once the user has been asked; Portward asks them itself).")
    def set_usage_counts(on: bool) -> CallToolResult:
        try:
            rt.usage.set(on)
        except ValueError as err:
            return error(str(err))
        return _usage_status(rt)


def _usage_status(rt: Runtime) -> CallToolResult:
    status = rt.usage.status()
    lines = [f"Usage counts: {status['usage_counts']}.", "Counted: " + "; ".join(status["counted"]) + ".",
             status["never"]]
    if status["next_reports"]:
        lines.append("Next to be sent, exactly: " + json.dumps(status["next_reports"]))
    return reply(status, "\n".join(lines))


def _register_open_page(server: MCPServer, rt: Runtime) -> None:
    @server.tool(description=(
        "Open the user's Portward page in their web browser, on this computer: their history, saved pulls, block "
        "rules, settings and what the AI was sent. Call it when the user asks to see or manage those. The page opens "
        "for the user; you aren't given its address."))
    def open_page() -> CallToolResult:
        if not rt.browser(local_page(rt).url()):
            return error(f"no web browser could be opened from here. Ask the user to run `{OPEN_PAGE_COMMAND}` in "
                         "a terminal on this computer: it opens the page")
        return reply({"opened": True}, "The Portward page is open in the user's browser.")


def register_offer_panel_tools(rt: Runtime) -> None:
    """#116: offers of Personal are shown in Portward's panel, never relayed by the AI. The panel asks for the offer
    of a moment when the user reaches for it there (a Personal level in a column menu: anonymised or totals), and the
    user can turn a kind off there. App-only; the wording is offers.py's, once per moment, only when this computer is
    known to be on Free."""

    @rt.app_tool(name="personal_offer", description=(
        "The user reached for something of Personal's in the results panel (moment \"anonymised_or_totals\" or "
        "\"profile\"): the offer to show there, the first time only. App-only."))
    def personal_offer(moment: str, ctx: Context) -> CallToolResult:
        if not client_support(ctx).panel:
            return error("personal_offer is for the results panel only")
        if moment not in offers.PANEL_MOMENTS:
            return error(f"no moment called {moment!r}; the moments are {', '.join(offers.PANEL_MOMENTS)}")
        made = (offers.in_panel(rt.store, moment, rt.accounts.service.base_url)
                if known_free(rt.keychain, rt.accounts) else None)
        return reply({"offer": made.payload() if made else None}, "shown" if made else "nothing to show")

    @rt.app_tool(name="dismiss_offer", description=(
        "The user turned one kind of offer off for good in the results panel (\"web\", \"profile\" or "
        "\"anonymised_or_totals\"). App-only."))
    def dismiss_offer(kind: str, ctx: Context) -> CallToolResult:
        if not client_support(ctx).panel:
            return error("dismiss_offer is for the results panel only")
        try:
            offers.dismiss(rt.store, kind)
        except ValueError as err:
            return error(str(err))
        return reply({"dismissed": kind}, "Done: that offer won't be made again on this computer.")
