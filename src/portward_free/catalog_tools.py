"""The tools that show what the sources hold (tables, columns, the user's notes) and the first-run guide."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from portward_free import first_run, weekly_facts
from portward_free.context_drift import drifted_on
from portward_free.context_served import catalog_of, served
from portward_free.profile_tools import lit_up
from portward_free.pull import Pull
from portward_free.pulls import Pulls
from portward_free.replies import error, reply
from portward_free.runtime import Runtime
from portward_free.schema import TABLE_NOTE, describe, for_ai
from portward_free.source_tools import file_settings, login_drift, no_source, warnings
from portward_free.sources import SourceError, Table
from portward_free.storage import Connection
from portward_free.verified import recheck


def register_catalog_tools(server: MCPServer, rt: Runtime) -> None:
    _register_listing(server, rt)
    _register_set_note(server, rt)
    _register_get_started(server, rt)


def _check_drift(rt: Runtime, name: str, catalog: Sequence[Table]) -> None:
    """On connect: re-run the verified answers offered on `name` and compare their results (#130), then count the
    context held back there for naming a column that's gone (#123) or for a changed result, for the weekly facts.
    Only the count is kept; the items are listed on the local page, never to the AI."""
    if not rt.store.profiles.on_profiles(name):
        return
    pulls = Pulls(rt)

    def run(source: str, question: str, sql: str) -> str:
        pulled = pulls.run(source, question, sql)
        return pulled.result_hash if isinstance(pulled, Pull) else "error"

    profiles = [a.profile for a in rt.store.profiles.applied(name)]
    changed = recheck(rt.store, name, profiles, rt.store.rules_for(name).visible(catalog), catalog, run,
                      rt.verified_checked)
    weekly_facts.remember_held_back(rt.store, name, len(drifted_on(rt.store, name, catalog)) + changed)


def _listed(rt: Runtime, connection: Connection) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": connection.name, "kind": connection.kind}
    try:
        with rt.opened(connection) as source:
            rules = rt.store.rules_for(connection.name)
            catalog = source.tables()
            _check_drift(rt, connection.name, catalog)
            context = served(rt.store, connection.name, catalog, catalog_of(rt))
            entry["tables"] = for_ai(rules.visible(catalog), context.notes, context.details)
            if context.definitions:
                entry["definitions"] = [{"term": d["term"], "text": d["text"]} for d in context.definitions]
            if context.links:
                entry["links"] = list(context.links)
            if context.metrics:
                entry["metrics"] = [m.brief() for m, _ in context.metrics]
            if context.verified:
                entry["verified_answers"] = [v.answer.brief() for v in context.verified]
            drift = login_drift(connection, source, rules, rt.store)
            if said := [*warnings(source, rules), *lit_up(rt.store, connection.name, catalog),
                        *context.notices(connection.name), *drift.pop("warnings")]:
                entry["warnings"] = said
            entry.update(drift)
    except SourceError as err:
        entry["error"] = str(err)
    return entry


def _register_listing(server: MCPServer, rt: Runtime) -> None:
    store = rt.store

    @server.tool(description="List the connected sources with the tables and columns the AI may use.")
    def list_sources() -> CallToolResult:
        return reply({"sources": [_listed(rt, c) for c in store.connections()]})

    @server.tool(description="Browse one source's tables and columns, with the user's notes and which are blocked.")
    def describe_source(source: str) -> CallToolResult:
        connection = store.connection(source)
        if connection is None:
            return no_source(source)
        try:
            with rt.opened(connection) as opened:
                tables = opened.tables()
        except SourceError as err:
            return error(str(err))
        policy = store.rules_for(connection.name)
        shown, blocked = policy.browse(tables)
        _check_drift(rt, connection.name, tables)
        context = served(store, connection.name, tables, catalog_of(rt))
        payload: dict[str, Any] = {"source": source,
                                   "tables": describe(shown, context.notes, blocked, context.details)}
        if context.definitions:
            payload["definitions"] = [{"term": d["term"], "text": d["text"]} for d in context.definitions]
        if context.links:
            payload["links"] = list(context.links)
        if context.metrics:
            payload["metrics"] = [m.brief() for m, _ in context.metrics]
        if context.verified:
            payload["verified_answers"] = [v.answer.brief() for v in context.verified]
        if notices := policy.notices(tables):
            payload["notices"] = list(notices)
        return reply(payload)


def _register_set_note(server: MCPServer, rt: Runtime) -> None:
    store = rt.store

    @server.tool(description="Write or edit the user's note on what a table, or one of its columns, means. "
                             "Leave out `column` for the table's own note; an empty note removes it.")
    def set_note(source: str, table: str, note: str, column: str | None = None) -> CallToolResult:
        connection = store.connection(source)
        if connection is None:
            return no_source(source)
        try:
            with rt.opened(connection) as opened:
                columns = {t.name: t.columns for t in opened.tables()}
        except SourceError as err:
            return error(str(err))
        if table not in columns:
            return error(f"{source} has no table {table!r}")
        if column and column not in columns[table]:
            return error(f"{source}.{table} has no column {column!r}")
        store.set_note(source, table, column or TABLE_NOTE, note)
        return reply({"source": source, "table": table, "column": column, "note": note.strip() or None})


def _first_query(rt: Runtime) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
    """A first query on the first source (the sample first) that opens and has a table the AI may read, and every
    source that wouldn't open, with why (so the guide never skips a broken source without saying so)."""
    preferred = sorted(rt.store.connections(), key=lambda c: c.name != first_run.SAMPLE_NAME)
    suggestion: dict[str, str] | None = None
    failed: list[dict[str, str]] = []
    for connection in preferred:
        try:
            with rt.opened(connection) as opened:
                tables = rt.store.rules_for(connection.name).visible(opened.tables())
                if tables and suggestion is None:
                    suggestion = first_run.first_query(connection.name, tables[0], opened.dialect)
        except SourceError as err:
            failed.append({"source": connection.name, "error": str(err)})
    return suggestion, failed


def _failed_text(failed: list[dict[str, str]]) -> str:
    lines = [f"- {f['source']}: {f['error']}" for f in failed]
    return ("\n\nThese sources wouldn't open; tell the user (test_source checks one again, update_password or "
            "add_source fixes it):\n" + "\n".join(lines))


def _register_get_started(server: MCPServer, rt: Runtime) -> None:
    store = rt.store

    @server.tool(description="The first-run guide: call it when the user is new or has no sources. It says the next "
                             "step (connect a source, then a first pull) and gives a query to try. use_sample=true "
                             "connects a small sample database for a user with no file to hand.")
    def get_started(use_sample: bool = False) -> CallToolResult:
        if use_sample and store.connection(first_run.SAMPLE_NAME) is None:
            path = first_run.write_sample(store.path.parent)
            store.add_connection(first_run.SAMPLE_NAME, "sqlite", file_settings(str(path)))
        connections = store.connections()
        progress = first_run.Progress(tuple(c.name for c in connections), len(store.recent_pulls(1)))
        suggestion, failed = _first_query(rt)
        text, payload = first_run.guide(progress, suggestion)
        if failed:
            payload, text = {**payload, "failed_sources": failed}, text + _failed_text(failed)
        return reply(payload, text)
