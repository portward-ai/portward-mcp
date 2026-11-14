"""The MCP server: the tools an AI client sees.

Tools that render in the results panel are registered on `apps` (MCP Apps, bound to a ui:// resource); the rest on
the server. `query` reads the client's declared capabilities (clients.client_support): with a panel the AI gets the
result's shape and the panel gets the rows through app-only tools; without one the AI gets the capped preview.

Each area's tools live in their own module (pull_tools, panel_tools, source_tools, catalog_tools, history_tools,
compare_tools, settings_tools, rule_tools, sharing, account, sync); this module wires them to one `Runtime`.
"""

from __future__ import annotations

import os
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING

from mcp.server.mcpserver import MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus

from portward_free import __version__, proof, results_panel
from portward_free.account import Accounts, register_account_tools
from portward_free.ai_log import AiLog
from portward_free.catalog_tools import register_catalog_tools
from portward_free.column_menu_tools import register_column_menu_tools
from portward_free.compare_tools import register_compare
from portward_free.hosted.tools import HostedRefresh, hosted_route, register_web_route_tools
from portward_free.hosted.wanted import WantedRoutes
from portward_free.history_tools import register_history_tools, register_sent_to_ai
from portward_free.keychain import Keychain, OsKeychain
from portward_free.panel_tools import register_get_selection, register_panel_tools
from portward_free.pull_tools import register_export, register_more_rows, register_pull_tools
from portward_free.assumption_tools import register_assumption_tools
from portward_free.context_tools import register_context_tools
from portward_free.dictionary_tools import register_dictionary_tools
from portward_free.verified_tools import (VERIFIED_HELP, register_verified_promoting, register_verified_reading,
                                          register_verified_runs)
from portward_free.metric_tools import (METRIC_HELP, register_metric_drafting, register_metric_reading,
                                        register_metric_runs)
from portward_free.profile_tools import register_profile_tools
from portward_free.browser_report import register_browser_report_tools
from portward_free.proof_tools import register_proof_tools
from portward_free.pulls import Pulls, sql_diff
from portward_free.mode_tools import register_mode_tools
from portward_free.plan_tools import PlanGate, Recorder
from portward_free.tool_marks import marked
from portward_free.rule_tools import register_rule_tools
from portward_free.runtime import PASSWORD_PANEL_URI, RESULTS_PANEL_URI, Runtime, opener_for
from portward_free.settings_tools import (OPEN_PAGE_COMMAND, register_offer_panel_tools, register_settings_tools,
                                          register_usage_panel_tools)
from portward_free.sharing import Sharing, register_share_tools
from portward_free.sharing.links import SHARE_PANEL_URI
from portward_free.sharing.manage import register_manage_tools
from portward_free.source_tools import register_source_panel_tools, register_source_tools
from portward_free.storage import LocalStore
from portward_free.sync import register_sync_tools
from portward_free.level_preview import register_level_preview
from portward_free.usage import Usage
from portward_free.web_app import WebAppScope
from portward_free.weekly_facts import WeeklyFacts

if TYPE_CHECKING:
    from portward_free.remote.signin import HttpSignIn

__all__ = ["INSTRUCTIONS", "OPEN_PAGE_COMMAND", "PASSWORD_PANEL_URI", "RESULTS_PANEL_URI", "build_server",
           "opener_for", "sql_diff"]

INSTRUCTIONS = (
    "Portward lets you query the user's own databases and CSV or Excel files read-only. Call list_sources to see "
    "the sources, tables and columns, then query with one SELECT. Notes on tables and columns are the user's own "
    "words on what they mean: follow them when you write the SQL. Where the user has a results panel, the rows show "
    "to them and you get only the rows they put in the chat: their message gives a selection id, and get_selection "
    "with that id returns those rows. Otherwise you get a short preview. The user can block "
    "tables and columns; those are never sent to you, so don't try to work around a block. A source can run on the "
    "user's profiles instead: then you see only what its applied profiles grant, and nothing else exists for you. "
    "Its definitions are the user's approved meanings of their terms: use them, and cite the ones your answer relies "
    "on. Where the profile tools are listed, you may draft meanings and definitions for the user to approve "
    "(draft_meanings, draft_definition); a draft isn't used until they approve it. " + METRIC_HELP + " "
    + VERIFIED_HELP + " "
    "If the user is new or has no sources yet, call get_started."
)


PERSONAL_PANEL_RESULT_TOOLS = ("metric", "run_verified_answer")  # #129/#130: served on profiles only (#116)
PANEL_RESULT_TOOLS = ("query", "rerun", "run_saved", "tweak", "kept_rows", "compare", "metric",
                      "run_verified_answer")  # shown in the results panel


def _register_apps(rt: Runtime, pulls: Pulls, sharing: Sharing) -> None:
    """The panels and every tool bound to one; these come before the server is made, which takes `rt.apps`."""
    rt.apps.add_html_resource(RESULTS_PANEL_URI, results_panel.page(), title="Portward results", prefers_border=True)
    rt.apps.add_html_resource(PASSWORD_PANEL_URI, results_panel.page("password.html"),
                              title="Portward password", prefers_border=True)
    rt.apps.add_html_resource(SHARE_PANEL_URI, results_panel.page("share.html"), title="Portward share",
                              prefers_border=True)
    register_pull_tools(rt, pulls)
    register_metric_runs(rt, pulls)  # #129: Portward writes a metric's SQL
    register_verified_runs(rt, pulls)  # #130: verified answers, run as written
    register_compare(rt, pulls)
    register_panel_tools(rt, pulls, sharing.drafts)
    register_source_panel_tools(rt)
    register_usage_panel_tools(rt)
    register_offer_panel_tools(rt)  # #116: offers of Personal show in the panel
    register_column_menu_tools(rt)  # #119: keep from Claude and notes from a column header
    register_level_preview(rt, pulls)  # #120: the column as the AI would get it, beside the offer


def _add_middleware(server: MCPServer, rt: Runtime) -> AiLog:
    """The AI log, then usage counts inside it (so the log has the first-run question usage adds)."""
    ai_log = AiLog(rt.store, rt.keychain, skip=frozenset(rt.app_only))
    server.middleware.append(ai_log)
    rt.usage.skip = frozenset(rt.app_only)
    rt.usage.panel_tools = frozenset(PANEL_RESULT_TOOLS)
    server.middleware.append(rt.usage)
    return ai_log


BROWSER_ENV = "PORTWARD_BROWSER"  # "off": this server never opens a web browser (the setup page, open_page)


def open_in_browser(url: str) -> bool:
    """Open `url` in the user's web browser; False when none opens, or PORTWARD_BROWSER=off says not to try."""
    if os.environ.get(BROWSER_ENV, "").strip().lower() == "off":
        return False
    return webbrowser.open(url)


def build_server(store: LocalStore, keychain: Keychain | None = None, usage: Usage | None = None,
                 browser: Callable[[str], bool] = open_in_browser, sharing: Sharing | None = None,
                 accounts: Accounts | None = None, http_sign_in: HttpSignIn | None = None,
                 route: str = proof.DESKTOP) -> MCPServer:
    """The server, reading connections from `store` and their passwords from `keychain` (the OS keychain by
    default). `usage` keeps the anonymous usage counts (by default posted to the collection endpoint, unless the
    environment turns them off). `browser` opens an address in the user's web browser (open_page). `sharing` is the
    share service and the mail app (by default PORTWARD_SHARE_URL and the user's own). `accounts` is where the
    Personal account lives (by default PORTWARD_ACCOUNT_URL). `http_sign_in` makes it the server for MCP over HTTP
    (#102, remote.RemoteMcp): every request needs the account owner's token, and secrets are asked for only in a
    terminal on this computer (no password panel, no local page). `route` is how the AI app reaches this server,
    as the AI log records it (proof.ROUTES: "desktop" here; the relay and the hosted gateway pass theirs)."""
    if route not in proof.ROUTES:
        raise ValueError(f"route must be one of {', '.join(proof.ROUTES)}")
    keychain = keychain if keychain is not None else OsKeychain()
    rt = Runtime(store=store, keychain=keychain, usage=usage if usage is not None else Usage.from_env(store),
                 browser=browser, opened=opener_for(keychain, route), local_only=http_sign_in is not None, route=route)
    sharing = sharing if sharing is not None else Sharing.from_env()
    accounts = accounts if accounts is not None else Accounts.from_env()
    rt.accounts = accounts
    rt.prune()
    pulls = Pulls(rt)
    _register_apps(rt, pulls, sharing)
    auth = ({} if http_sign_in is None else
            {"auth": http_sign_in.settings(), "token_verifier": http_sign_in.verifier})
    bus = InMemorySubscriptionBus()
    server = MCPServer("portward", version=__version__, instructions=INSTRUCTIONS, extensions=[rt.apps],
                       subscriptions=bus, **auth)
    if http_sign_in is not None:  # #134: on the web, each request runs as its OAuth client (per-AI-app profiles)
        from portward_free.remote.signin import client_of  # not at the top: remote imports this module
        server.middleware.insert(0, WebAppScope(client_of, store.profiles.seen_app))
    gate_at = len(server.middleware)  # the plan gate goes here, below, once Personal's tools are known
    ai_log = _add_middleware(server, rt)
    server.middleware.append(WeeklyFacts(store, keychain, accounts, route))  # #105: the account page's facts
    register_more_rows(server, rt)
    register_get_selection(server, rt)
    register_export(server, rt, pulls)
    register_source_tools(server, rt)
    register_catalog_tools(server, rt)
    register_history_tools(server, rt)
    register_sent_to_ai(server, rt, ai_log)
    register_settings_tools(server, rt)
    register_rule_tools(server, store, rt.opened)
    register_mode_tools(server, rt)  # #117: Free's two modes per source
    register_share_tools(server, store, keychain, pulls.run, sharing, rt.out_of_band, accounts, rt.share_links,
                         on_shared=rt.usage.shared)
    register_account_tools(server, store, keychain, accounts, browser)
    personal = _register_personal(server, rt, pulls, accounts, browser, http_sign_in)
    server.middleware.insert(gate_at, PlanGate(store, keychain, accounts, personal, bus))  # #116
    return marked(server)  # #147: every tool's title and hints


def _register_personal(server: MCPServer, rt: Runtime, pulls: Pulls, accounts: Accounts,
                       browser: Callable[[str], bool], http_sign_in: HttpSignIn | None) -> frozenset[str]:
    """Personal's tools (#116: listed only while Personal is on, plan_tools.PlanGate); their names. A new Personal
    tool is registered here, on `personal`; tests/test_tool_plans.py holds Free's list."""
    store, keychain = rt.store, rt.keychain
    personal = Recorder(server)
    collect = None if http_sign_in is not None else hosted_route(store, keychain, accounts).collect  # #104
    register_proof_tools(server, rt, accounts, collect, personal)  # #98: the AI log (Free) and the proof report
    profile_tools = register_profile_tools(personal, store, keychain, accounts, rt.opened)  # #91: profiles
    register_context_tools(personal, profile_tools)  # #95: drafted meanings and named definitions
    register_metric_drafting(personal, profile_tools)  # #129: metrics, approved like other context
    register_metric_reading(personal, rt)
    register_verified_promoting(personal, rt, profile_tools, pulls)  # #130: saved pulls promoted, approved
    register_verified_reading(personal, rt)  # (run_verified_answer is a panel tool: PERSONAL_PANEL_RESULT_TOOLS)
    personal.names.update(PERSONAL_PANEL_RESULT_TOOLS)  # registered with the panels, before the server
    register_assumption_tools(personal, profile_tools)  # #96: what the AI assumed, and turning it into a definition
    register_dictionary_tools(personal, profile_tools)  # #96: importing a data dictionary, mapping confirmed
    register_manage_tools(personal, store, keychain, accounts)  # #56: Personal's share management
    register_sync_tools(personal, store, keychain, accounts, browser, rt.out_of_band)
    if http_sign_in is None:  # #107: a browser is approved for the report from the user's own computer
        register_browser_report_tools(personal, store, keychain, accounts, rt.out_of_band)
    if http_sign_in is None:  # #104: which web route each connection uses is chosen here, on the computer
        register_web_route_tools(personal, store, keychain, accounts)
        server.middleware.append(HostedRefresh(store, keychain, accounts))
        # #106: switches asked for on the account page are made here, where the login is
        server.middleware.append(WantedRoutes(store, keychain, accounts.service,
                                              hosted_route(store, keychain, accounts)))
    return frozenset(personal.names)
