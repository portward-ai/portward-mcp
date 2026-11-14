"""#119: the app-only tools behind a column header's menu in the results panel: what the menu shows, 'Keep from
Claude' and the column's note.

They act on the table column the result column reads, as the server traces it from the pull's SQL (column_menu); a
change applies from the next result on, and the rows on show stay as they were pulled. They read the pull's SQL,
never its rows, so they don't wait on Pulls.rules_changed. The model never sees these tools or their results.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult

from portward_free import column_menu, results_panel
from portward_free.clients import client_support
from portward_free.replies import error, reply
from portward_free.runtime import Runtime


def _held(rt: Runtime, ctx: Context, tool: str, pull_id: str) -> results_panel.Held | CallToolResult:
    if not client_support(ctx).panel:
        return error(f"{tool} is for the results panel only")
    try:
        return rt.panel_pulls.get(pull_id)
    except results_panel.SelectionError as err:
        return error(str(err))


def register_column_menu_tools(rt: Runtime) -> None:
    @rt.app_tool(name="column_menu",
                 description="The results panel opens a column header's menu: the table column behind it, whether "
                             "it is kept from the AI, and its note. App-only.")
    def column_menu_tool(pull_id: str, column: str, ctx: Context) -> CallToolResult:
        held = _held(rt, ctx, "column_menu", pull_id)
        if isinstance(held, CallToolResult):
            return held
        return reply(column_menu.menu(rt.store, rt.opened, held, column), "the column menu")

    @rt.app_tool(name="keep_from_claude",
                 description="The user keeps a result column's table column from the AI, from its header in the "
                             "results panel: a block rule, applied from the next result on. App-only.")
    def keep_from_claude(pull_id: str, column: str, ctx: Context) -> CallToolResult:
        held = _held(rt, ctx, "keep_from_claude", pull_id)
        if isinstance(held, CallToolResult):
            return held
        try:
            target, added = column_menu.keep(rt.store, rt.opened, held, column)
        except column_menu.MenuError as err:
            return error(str(err))
        return reply({"kept": target.payload(), "already_kept": not added},
                     f"{target.table}.{target.column} is kept from the AI from the next result on.")

    @rt.app_tool(name="column_note",
                 description="The user writes or edits the note on a result column's table column from its header "
                             "in the results panel; an empty note removes it. App-only.")
    def column_note(pull_id: str, column: str, note: str, ctx: Context) -> CallToolResult:
        held = _held(rt, ctx, "column_note", pull_id)
        if isinstance(held, CallToolResult):
            return held
        try:
            target, saved = column_menu.write_note(rt.store, rt.opened, held, column, note)
        except column_menu.MenuError as err:
            return error(str(err))
        return reply({"target": target.payload(), "note": saved},
                     f"Note on {target.table}.{target.column} {'saved' if saved else 'removed'}.")
