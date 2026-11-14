"""Each tool's title and whether it reads, changes or deletes something (#147, spec #141).

AI apps and directories read these off tools/list: Claude's directory wants a title plus readOnlyHint or
destructiveHint on every tool, ChatGPT asks the user before any tool that isn't read-only. Every hint is decided here,
in one table, so none is ever left to a client's default:
- READ: changes nothing the user keeps (it may record the read in history and the AI log, or open a page);
- WRITE: adds or changes something, and what was there before stays (a new version, a draft, a setting);
- DELETE: may remove or replace something: a rule, a login, a share, a note, a key.
`world` is True where the tool reaches past this computer and the user's own sources: email, the Portward account,
sync, the hosted engine.

`marked(server)` puts them on every tool registered; a tool missing from MARKS stops the server from being built,
so a new tool can't ship without them (tests/test_tool_marks.py)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import NamedTuple

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


class Does(Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"


READ, WRITE, DELETE = Does.READ, Does.WRITE, Does.DELETE


class Mark(NamedTuple):
    title: str
    does: Does
    world: bool = False

    def annotations(self) -> ToolAnnotations:
        return ToolAnnotations(title=self.title, read_only_hint=self.does is READ,
                               destructive_hint=self.does is DELETE, open_world_hint=self.world)


MARKS: Mapping[str, Mark] = {
    # Pulls and the results panel
    "query": Mark("Run a query", READ),
    "rerun": Mark("Re-run a pull", READ),
    "tweak": Mark("Tweak a pull", READ),
    "run_saved": Mark("Run a saved pull", READ),
    "compare": Mark("Compare two pulls", READ),
    "more_rows": Mark("Send more rows", READ),
    "get_selection": Mark("Get the rows put in the chat", READ),
    "kept_rows": Mark("Show kept rows", READ),
    "export": Mark("Export a pull to a file", WRITE),
    "panel_rows": Mark("Panel: read a pull's rows", READ),
    "put_in_chat": Mark("Panel: put rows in the chat", WRITE),
    "value_source": Mark("Panel: where a value came from", READ),
    "column_menu": Mark("Panel: column menu", READ),
    "column_note": Mark("Panel: note on a column", DELETE),
    "keep_from_claude": Mark("Panel: keep a column from the AI", WRITE),
    "level_preview": Mark("Panel: preview a column's level", READ),
    "personal_offer": Mark("Panel: offer to show", WRITE),
    "dismiss_offer": Mark("Panel: turn an offer off", WRITE),
    "usage_choice": Mark("Panel: usage counts on or off", DELETE),
    "usage_seen": Mark("Panel: usage question shown", WRITE),
    "save_secret": Mark("Panel: save what the user typed", DELETE),
    "secret_request": Mark("Panel: read an open ask", READ),
    "share_link": Mark("Share panel: read the link", READ),
    "share_mail": Mark("Share panel: open in mail app", READ),
    # Sources and the catalog
    "add_source": Mark("Connect a source", DELETE),
    "remove_source": Mark("Remove a source", DELETE),
    "test_source": Mark("Test a source", READ),
    "update_password": Mark("Enter a new database password", DELETE),
    "list_sources": Mark("List sources", READ),
    "describe_source": Mark("Describe a source", READ),
    "set_note": Mark("Write a note", DELETE),
    "get_started": Mark("Get started", READ),
    "set_send_mode": Mark("Set how results reach the AI", WRITE),
    "matching_login_sql": Mark("Write SQL for a matching login", READ),
    # History
    "history": Mark("Show recent pulls", READ),
    "lineage": Mark("Show a pull's lineage", READ),
    "save_pull": Mark("Save a pull", DELETE),
    "saved_pulls": Mark("List saved pulls", READ),
    "keep_rows": Mark("Keep result rows on or off", WRITE),
    "sent_to_ai": Mark("Show what the AI was sent", READ),
    "ai_log": Mark("Show the AI log", READ),
    # Block rules and settings
    "block": Mark("Block a table or column", WRITE),
    "unblock": Mark("Remove a block rule", DELETE),
    "list_rules": Mark("List block rules", READ),
    "usage_counts": Mark("Show usage counts", READ),
    "set_usage_counts": Mark("Turn usage counts on or off", DELETE),
    "open_page": Mark("Open the Portward page", READ),
    # Sharing
    "share": Mark("Share a pull", WRITE, world=True),
    "share_sign_in": Mark("Confirm email for sharing", WRITE, world=True),
    "share_confirm": Mark("Enter the sharing code", WRITE, world=True),
    "shares": Mark("List shares", READ),
    "account_shares": Mark("List the account's shares", READ, world=True),
    "withdraw_share": Mark("Withdraw a share", DELETE, world=True),
    "set_share_expiry": Mark("Change when a share expires", DELETE, world=True),
    # The Portward account
    "account_sign_in": Mark("Sign in to Portward", WRITE, world=True),
    "account_status": Mark("Portward account status", READ, world=True),
    "account_sign_out": Mark("Sign out of Portward", DELETE, world=True),
    "account_delete": Mark("Open the account deletion page", READ, world=True),
    # Profiles (Personal)
    "create_profile": Mark("Create a profile", WRITE),
    "update_profile": Mark("Update a profile", WRITE),
    "apply_profile": Mark("Apply a profile", WRITE),
    "unapply_profile": Mark("Take a profile off", DELETE),
    "list_profiles": Mark("List profiles", READ),
    "use_block_rules": Mark("Switch back to block rules", WRITE),
    "start_profile_from_block_rules": Mark("Start a profile from block rules", WRITE),
    # Context (Personal)
    "draft_meanings": Mark("Draft meanings", WRITE),
    "draft_definition": Mark("Draft a definition", WRITE),
    "draft_link": Mark("Draft a link", WRITE),
    "draft_metric": Mark("Draft a metric", WRITE),
    "context_drafts": Mark("List context drafts", READ),
    "approve_context": Mark("Ask to approve a draft", WRITE),
    "discard_draft": Mark("Discard a draft", DELETE),
    "settle_context": Mark("Settle differing context", WRITE),
    "assumptions": Mark("List assumptions", READ),
    "define_assumption": Mark("Turn an assumption into a definition", WRITE),
    "import_dictionary": Mark("Import a data dictionary", WRITE),
    "describe_metric": Mark("Describe a metric", READ),
    "metric": Mark("Get a metric", READ),
    "promote_saved_pull": Mark("Promote a saved pull", WRITE),
    "describe_verified_answer": Mark("Describe a verified answer", READ),
    "run_verified_answer": Mark("Run a verified answer", READ),
    # Proof, sync and the web routes (Personal)
    "export_proof_report": Mark("Export the proof report", WRITE),
    "approve_browser": Mark("Approve a browser for the report", WRITE, world=True),
    "sync_turn_on": Mark("Turn sync on", WRITE, world=True),
    "sync_now": Mark("Sync now", WRITE, world=True),
    "sync_status": Mark("Sync status", READ),
    "sync_turn_off": Mark("Turn sync off", WRITE),
    "sync_new_recovery_key": Mark("Make a new recovery key", DELETE, world=True),
    "web_route": Mark("Choose a connection's web route", WRITE, world=True),
    "web_routes": Mark("List web routes", READ),
    # The wrap server (wrap/server.py); its provider's SQL tools are marked there
    "list_tables": Mark("List tables", READ),
}


class Unmarked(Exception):
    """A tool with no entry in MARKS."""


def marked(server: MCPServer, extra: Mapping[str, Mark] | None = None) -> MCPServer:
    """`server` with a title and hints on every tool registered on it (`extra` for tools named at run time)."""
    marks = {**MARKS, **(extra or {})}
    manager = server._tool_manager
    missing = sorted(t.name for t in manager.list_tools() if t.name not in marks)
    if missing:
        raise Unmarked(f"no title or hints for {', '.join(missing)}: add them to tool_marks.MARKS")
    for listed in manager.list_tools():
        mark = marks[listed.name]
        manager._tools[listed.name] = listed.model_copy(
            update={"title": mark.title, "annotations": mark.annotations()})
    return server
