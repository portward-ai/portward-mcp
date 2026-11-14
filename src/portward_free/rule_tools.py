"""The tools that list, add and remove block rules.

Adding a rule only narrows what the AI sees, so `block` runs straight away. Removing one lets the AI read more, and
the AI is the one writing tool calls, so `unblock` asks the user through the client's own prompt (elicitation) and
removes the rule only on their explicit yes. A client that can't prompt the user can't remove rules from chat.

No `from __future__ import annotations` here: the `Resolve(...)` annotation must hold the resolver itself.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated, Any

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from portward_free.replies import error, reply
from portward_free.rules import Rule, Rules
from portward_free.sources import Source, SourceError, Table
from portward_free.storage import Connection, LocalStore

Opener = Callable[[Connection], AbstractContextManager[Source]]  # opens a stored connection for one call

CANT_ASK = ("this app can't ask you to confirm, so the rule can't be removed from chat; use an app that can ask "
            "(Claude Code does), or the Portward panel")

ON_PROFILES = ("{source} runs on profiles, so block rules don't apply there: anything no applied profile grants is "
               "already hidden. Change what it shows with the profile tools, or use_block_rules to go back.")


class RemoveRule(BaseModel):
    remove: bool = Field(description="Yes, let the AI read it again")


def register_rule_tools(server: MCPServer, store: LocalStore, opened: Opener) -> None:
    _register_block(server, store, opened)
    _register_unblock(server, store)
    _register_list(server, store)


def _stored_rule(store: LocalStore, source: str, table: str, column: str | None) -> Rule | None:
    wanted = Rule(source, table, column if column and column.strip() else None)
    return next((r for r in store.rules(source) if _same_rule(r, wanted)), None)


def _blocked_text(source: str, rule: Rule, rules: Rules, catalog: tuple[Table, ...]) -> tuple[str, tuple[str, ...]]:
    """What the user is told about a new block, and the views it withholds."""
    views = rules.withheld_derived(catalog)
    text = f"Blocked {rule.describe()} in {source}: it is never sent to the AI."
    if notices := rules.notices(catalog):
        text += " " + "; ".join(notices) + "."
    if views:
        text += (f" While {source} has block rules, its views (and partitions) are kept from the AI too, since "
                 f"Portward can't check what they read: {', '.join(views)}.")
    return text, views


def _register_block(server: MCPServer, store: LocalStore, opened: Opener) -> None:
    @server.tool(description=(
        "Block a table, or one column of it, from the AI for good: it is never sent, whatever the query. "
        "Leave `column` out to block the whole table. Use this when the user asks to hide or protect data."))
    def block(source: str, table: str, column: str | None = None) -> CallToolResult:
        connection = store.connection(source)
        if connection is None:
            return error(f"no source named {source!r}; call list_sources")
        if store.profiles.on_profiles(source):
            return error(ON_PROFILES.format(source=source))
        try:
            with opened(connection) as opened_source:
                catalog = opened_source.tables()
                rule = named_rule(source, catalog, table, column)
        except (SourceError, ValueError) as err:
            return error(str(err))
        added = store.add_rule(rule)
        text, views = _blocked_text(source, rule, store.rules_for(source), catalog)
        return reply({"blocked": _rule_payload(rule), "already_blocked": not added, "views_withheld": list(views)},
                     text)


def _register_unblock(server: MCPServer, store: LocalStore) -> None:
    def confirm_removal(ctx: Context, source: str, table: str,
                        column: str | None = None) -> Elicit[RemoveRule] | RemoveRule:
        """Ask the user; answer no without asking when there's nothing to remove or no way to ask."""
        rule = _stored_rule(store, source, table, column)
        if rule is None or not _can_ask(ctx) or store.profiles.on_profiles(source):
            return RemoveRule(remove=False)
        return Elicit(f"Let the AI read {rule.describe()} in {source} again? It is blocked now.", RemoveRule)

    @server.tool(description=(
        "Remove a block rule so the AI can read that table or column again. Portward asks the user to confirm "
        "it themselves; only call this when the user asks for it."))
    def unblock(ctx: Context, source: str, table: str,
                answer: Annotated[ElicitationResult[RemoveRule], Resolve(confirm_removal)],
                column: str | None = None) -> CallToolResult:
        if store.profiles.on_profiles(source):
            return error(ON_PROFILES.format(source=source))
        rule = _stored_rule(store, source, table, column)
        if rule is None:
            shown = Rule(source, table, column or None).describe()
            return error(f"{shown} isn't blocked in {source}; call list_rules")
        if not _can_ask(ctx):
            return error(f"{rule.describe()} stays blocked: {CANT_ASK}")
        if not (isinstance(answer, AcceptedElicitation) and answer.data.remove is True):
            return error(f"{rule.describe()} stays blocked: the user didn't confirm removing the rule")
        store.remove_rule(Rule(source, rule.table, rule.column))  # under every name pointing at the same data
        return reply({"unblocked": _rule_payload(rule)}, f"{rule.describe()} in {source} is no longer blocked.")


def _register_list(server: MCPServer, store: LocalStore) -> None:
    @server.tool(description="List the user's block rules: the tables and columns never sent to the AI. "
                             "Leave `source` out for every source.")
    def list_rules(source: str | None = None) -> CallToolResult:
        rules = store.rules(source)
        lines = [f"- {r.source}: {r.describe()}" for r in rules] or ["No tables or columns are blocked."]
        return reply({"rules": [_rule_payload(r) for r in rules]}, "\n".join(lines))


def _can_ask(ctx: Context) -> bool:
    capabilities = ctx.client_capabilities
    return capabilities is not None and capabilities.elicitation is not None


def named_rule(source: str, tables: tuple[Table, ...], table: str, column: str | None) -> Rule:
    """The rule, with the table and column named as the source names them; ValueError if they don't exist."""
    found = next((t for t in tables if t.name.lower() == table.lower()), None)
    if found is None:
        raise ValueError(f"{source} has no table {table!r}; it has: {', '.join(t.name for t in tables)}")
    if column is None or not column.strip():
        return Rule(source, found.name)
    named = next((c for c in found.columns if c.lower() == column.lower()), None)
    if named is None:
        raise ValueError(f"{found.name} has no column {column!r}; it has: {', '.join(found.columns)}")
    return Rule(source, found.name, named)


def _same_rule(a: Rule, b: Rule) -> bool:
    """The same table and column. The source isn't compared: store.rules(source) already holds every rule that
    applies to the source, including those made under another name for the same file or database."""
    return a.table.lower() == b.table.lower() and (a.column or "").lower() == (b.column or "").lower()


def _rule_payload(rule: Rule) -> dict[str, Any]:
    return {"source": rule.source, "table": rule.table, "column": rule.column}
