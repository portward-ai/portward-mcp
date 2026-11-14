"""The tools behind the Portward panels: the app-only ones only the panels call (the model never sees them or their
results), and get_selection, the model's side of 'Put in chat'."""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult
from sqlglot.errors import SqlglotError

from portward_free import provenance, results_panel
from portward_free.asks import Refused
from portward_free.clients import client_support
from portward_free.pulls import Pulls
from portward_free.replies import error, reply
from portward_free.runtime import PASSWORD_PANEL_URI, Runtime
from portward_free.sharing import Drafts
from portward_free.sharing.links import SHARE_PANEL_URI, link_for_panel, mail_from_panel
from portward_free.sources import SourceError


def register_panel_tools(rt: Runtime, pulls: Pulls, drafts: Drafts) -> None:
    _register_save_secret(rt)
    _register_rows(rt, pulls)
    _register_value_source(rt, pulls)
    _register_share_link(rt, drafts)


def _register_share_link(rt: Runtime, drafts: Drafts) -> None:
    @rt.app_tool(name="share_link", resource_uri=SHARE_PANEL_URI,
                 description="The share panel reads a new share's link (its # part is the key) to show and copy. "
                             "App-only: the model never sees this tool or its result.")
    def share_link(share_id: str, ctx: Context) -> CallToolResult:
        return link_for_panel(rt.share_links, share_id, ctx)

    @rt.app_tool(name="share_mail", resource_uri=SHARE_PANEL_URI,
                 description="The share panel's 'Open in mail app': opens a draft to the teammate with the link in "
                             "the user's mail app. App-only.")
    def share_mail(share_id: str, ctx: Context) -> CallToolResult:
        return mail_from_panel(rt.share_links, drafts.open, share_id, ctx)


def _not_panel(ctx: Context, tool: str, panel: str = "results panel") -> CallToolResult | None:
    return None if client_support(ctx).panel else error(f"{tool} is for the {panel} only")


def _register_save_secret(rt: Runtime) -> None:
    @rt.app_tool(name="save_secret", resource_uri=PASSWORD_PANEL_URI,
                 description="The panel's form sends what the user typed (a database password, the sharing code) "
                             "for the ask in its _meta (or read with secret_request). App-only: the model never sees "
                             "this tool, its input or result.")
    def save_secret(request: str, secret: str, ctx: Context) -> CallToolResult:
        if (refused := _not_panel(ctx, "save_secret", "Portward panel")) is not None:
            return refused
        try:
            outcome = rt.out_of_band.answer(request, secret)
        except Refused as err:
            return error(str(err))
        return reply(outcome.payload(), outcome.summary())

    @rt.app_tool(name="secret_request", resource_uri=PASSWORD_PANEL_URI,
                 description="The panel reads the form for an open ask (its request_id, in the visible result) when "
                             "the host didn't hand it the result's _meta. App-only: the model never sees this tool or "
                             "its result.")
    def secret_request(request_id: str, ctx: Context) -> CallToolResult:
        if (refused := _not_panel(ctx, "secret_request", "Portward panel")) is not None:
            return refused
        try:
            request = rt.out_of_band.panel_form(request_id)
        except Refused as err:
            return error(str(err))
        return reply(request.form(), "the form for the Portward panel")


def _register_rows(rt: Runtime, pulls: Pulls) -> None:
    @rt.app_tool(name="panel_rows",
                 description="The results panel reads a pull's rows. App-only: the model never sees this result.")
    def panel_rows(pull_id: str, ctx: Context) -> CallToolResult:
        if (refused := _not_panel(ctx, "panel_rows")) is not None:
            return refused
        if (stale := pulls.rules_changed(pull_id)) is not None:
            return stale
        try:
            return reply(results_panel.for_panel(rt.panel_pulls.get(pull_id)), "rows for the results panel")
        except results_panel.SelectionError as err:
            return error(str(err))

    @rt.app_tool(name="put_in_chat",
                 description="The results panel records the user's selection (row positions, column names) and gets "
                             "the short chat message it posts: the selection's id, never its values. App-only.")
    def put_in_chat(pull_id: str, rows: list[int], columns: list[str], ctx: Context) -> CallToolResult:
        if (refused := _not_panel(ctx, "put_in_chat")) is not None:
            return refused
        if (stale := pulls.rules_changed(pull_id)) is not None:
            return stale
        try:
            selection = rt.selections.make(rt.panel_pulls.get(pull_id), rows, columns)
        except results_panel.SelectionError as err:
            return error(str(err))
        text = results_panel.chat_note(selection.selection_id, selection.picked)
        return reply({"selection_id": selection.selection_id, "pull_id": pull_id,
                      "columns": list(selection.picked.columns), "row_count": len(selection.picked.rows),
                      "text": text}, text)


def register_get_selection(server: MCPServer, rt: Runtime) -> None:
    @server.tool(name="get_selection",
                 description="Get the rows the user put in the chat from the Portward results panel: exactly the "
                             "rows and columns they picked. Call it only with a selection id the user's message gave "
                             "you (\"use my selection sel_...\"); don't guess ids or reuse a pull_id. You can read a "
                             "selection again while it lasts (24 hours at most).")
    def get_selection(selection_id: str) -> CallToolResult:
        try:
            selection = rt.selections.get(selection_id)
        except results_panel.SelectionError as err:
            return error(str(err))
        text, payload = results_panel.selection_rows(selection.selection_id, selection.picked)
        return reply(payload, text)


def _schema_and_dialect(rt: Runtime, source_name: str) -> tuple[provenance.Schema | None, str | None]:
    """The source's tables and columns (so SELECT * and joins trace), and its SQL dialect; None when it can't be
    opened now (the trace then uses the SQL alone)."""
    connection = rt.store.connection(source_name)
    if connection is None:
        return None, None
    try:
        with rt.opened(connection) as opened:
            return {t.name: t.columns for t in opened.tables()}, opened.dialect
    except SourceError:
        return None, None


def _register_value_source(rt: Runtime, pulls: Pulls) -> None:
    @rt.app_tool(name="value_source",
                 description="The results panel asks where one value came from (its row position and column "
                             "name): the source tables and columns behind it and the query's filters. App-only.")
    def value_source(pull_id: str, row: int, column: str, ctx: Context) -> CallToolResult:
        if (refused := _not_panel(ctx, "value_source")) is not None:
            return refused
        if (stale := pulls.rules_changed(pull_id)) is not None:
            return stale
        try:
            held = rt.panel_pulls.get(pull_id)
        except results_panel.SelectionError as err:
            return error(str(err))
        if column not in held.columns:
            return error(f"no column {column!r} in this result")
        if not 0 <= row < len(held.rows):
            return error(f"rows are numbered 0 to {len(held.rows) - 1} in this result")
        schema, dialect = _schema_and_dialect(rt, held.source)
        try:
            traced = provenance.trace(held.sql, dialect or "sqlite", column, schema)
        except (ValueError, SqlglotError) as err:
            return error(f"couldn't read where this value came from: {err}")
        payload = {"pull_id": pull_id, "source": held.source, "sql": held.sql,
                   **traced.payload(held.rows[row], held.columns)}
        return reply(payload, f"where {column} came from")
