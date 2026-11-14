"""The tools over the user's history: past pulls, saved names, lineage, kept rows, and what the AI was sent."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult

from portward_free import kept
from portward_free.ai_log import AiLog, AiResponse
from portward_free.keychain import KeychainError
from portward_free.pull import markdown_table
from portward_free.pulls import KEEP_ROWS_SETTING, no_pull
from portward_free.replies import error, reply
from portward_free.runtime import Runtime
from portward_free.storage import Conversation, PullRecord

DEFAULT_HISTORY_LIMIT = 10
MAX_HISTORY_LIMIT = 100


def history_entry(entry: PullRecord) -> dict[str, Any]:
    return {**asdict(entry), "tables": list(entry.tables), "columns": list(entry.columns)}


def _history_text(entries: Sequence[PullRecord], empty: str = "No pulls yet.") -> str:
    if not entries:
        return empty
    rows = [(e.created_at, e.saved_name or e.question, e.source, e.row_count, e.id) for e in entries]
    return markdown_table(("time", "question", "source", "rows", "id"), rows)


def _conversations_text(listed: Sequence[tuple[Conversation, int]], current: str) -> str:
    rows = [(c.started_at, c.client or "", n, f"{c.id} (this one)" if c.id == current else c.id) for c, n in listed]
    return markdown_table(("started", "client", "responses", "id"), rows) if rows else "No conversations yet."


def _responses_text(responses: Sequence[AiResponse]) -> str:
    if not responses:
        return "Nothing was sent to the AI in this conversation."
    parts = [f"### {r.created_at} {r.tool}{' (error)' if r.is_error else ''}\n\n"
             f"{f'(What was sent {r.withheld})' if r.withheld else r.text}" for r in responses]
    return "\n\n".join(parts)


def register_history_tools(server: MCPServer, rt: Runtime) -> None:
    _register_history(server, rt)
    _register_saved(server, rt)
    _register_keep_rows(server, rt)


def _register_history(server: MCPServer, rt: Runtime) -> None:
    store = rt.store

    @server.tool(description="Show the user's last pulls, newest first (for 'show my last pulls'). Each has an id "
                             "that rerun takes.")
    def history(limit: int = DEFAULT_HISTORY_LIMIT) -> CallToolResult:
        entries = store.recent_pulls(max(1, min(limit, MAX_HISTORY_LIMIT)))
        return reply({"pulls": [history_entry(e) for e in entries]}, _history_text(entries))

    @server.tool(description="List a pull's lineage: the pull it tweaks (parent, and that one's ancestors) and the "
                             "pulls that tweak it (children).")
    def lineage(pull_id: str) -> CallToolResult:
        entry = store.pull(pull_id)
        if entry is None:
            return no_pull(pull_id)
        ancestors, children = store.ancestors(pull_id), store.children(pull_id)
        payload = {
            "pull": history_entry(entry),
            "parent": history_entry(ancestors[0]) if ancestors else None,
            "ancestors": [history_entry(a) for a in ancestors],
            "children": [history_entry(c) for c in children],
        }
        text = (f"Ancestors (nearest first):\n\n{_history_text(ancestors, 'None: this is the first pull.')}\n\n"
                f"Children:\n\n{_history_text(children, 'None yet.')}")
        return reply(payload, text)


def _register_saved(server: MCPServer, rt: Runtime) -> None:
    store = rt.store

    @server.tool(description="Save a pull from history under a name (e.g. 'Monthly revenue by region') so it can be "
                             "re-run by name with run_saved. Saving a name again moves it to this pull.")
    def save_pull(pull_id: str, name: str) -> CallToolResult:
        name = name.strip()
        if not name:
            return error("give the saved pull a name")
        if store.pull(pull_id) is None:
            return no_pull(pull_id)
        replaced = store.save_name(pull_id, name)
        text = f"Saved as {name!r}."
        if replaced:
            text += " The name now points at this pull instead of the earlier one."
        return reply({"saved": name, "pull_id": pull_id, "replaced": replaced}, text)

    @server.tool(description="List the saved pulls by name. run_saved re-runs one.")
    def saved_pulls() -> CallToolResult:
        entries = store.saved_pulls()
        if not entries:
            return reply({"saved": []}, "No saved pulls yet.")
        text = markdown_table(("name", "question", "source", "pulled at", "id"),
                              [(e.saved_name, e.question, e.source, e.created_at, e.id) for e in entries])
        return reply({"saved": [{**history_entry(e), "name": e.saved_name} for e in entries]}, text)


def _register_keep_rows(server: MCPServer, rt: Runtime) -> None:
    @server.tool(description=f"Turn keeping result rows on or off (off by default). When on, each new pull's rows "
                             f"are kept on this machine, encrypted with a key in the OS keychain, for "
                             f"{kept.KEEP_DAYS} days; kept_rows shows them.")
    def keep_rows(on: bool) -> CallToolResult:
        if on:
            try:
                kept.key(rt.keychain, create=True)
            except (KeychainError, kept.RowsLocked) as err:
                return error(f"rows can't be kept: {err}")
        rt.store.set_setting(KEEP_ROWS_SETTING, "on" if on else "off")
        text = (f"Rows of new pulls will be kept, encrypted, for {kept.KEEP_DAYS} days." if on else
                f"Rows of new pulls won't be kept. Rows kept already are removed when {kept.KEEP_DAYS} days old.")
        return reply({"keep_rows": on}, text)


def register_sent_to_ai(server: MCPServer, rt: Runtime, ai_log: AiLog) -> None:
    store = rt.store

    @server.tool(description="Show what the AI was sent (for 'what did you see?'). With no conversation_id, list the "
                             "conversations, newest first; with one, every response sent in it, in order.")
    def sent_to_ai(ctx: Context, conversation_id: str | None = None) -> CallToolResult:
        if conversation_id is None:
            listed = store.conversations(MAX_HISTORY_LIMIT)
            current = ai_log.current(ctx.session)
            payload = {"current": current, "conversations": [
                {**asdict(c), "responses": n, "current": c.id == current} for c, n in listed]}
            return reply(payload, _conversations_text(listed, current))
        if store.conversation(conversation_id) is None:
            return error(f"no conversation with id {conversation_id!r}; call sent_to_ai for the ids")
        # a response sent before a block rule was added may hold what is now blocked: don't replay it to the AI
        responses = tuple(
            AiResponse(conversation_id=r.conversation_id, tool=r.tool, is_error=r.is_error, created_at=r.created_at,
                       withheld="is hidden: block rules were added since it was sent")
            if r.withheld is None and store.any_rule_added_since(r.created_at) else r
            for r in ai_log.responses(conversation_id))
        payload = {"conversation_id": conversation_id,
                   "responses": [{**asdict(r), "content": list(r.content)} for r in responses]}
        return reply(payload, _responses_text(responses))
