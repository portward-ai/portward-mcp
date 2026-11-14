"""The tools that run pulls and hand out their rows: query, rerun, run_saved, tweak, kept_rows, more_rows, export."""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free import kept
from portward_free.approval import MORE_ROWS_DEFAULT, Held, Outbox, ask_in_client
from portward_free.clients import client_support
from portward_free.export import EXPORTS_FOLDER, WRITERS, export_file
from portward_free.keychain import KeychainError
from portward_free.pull import Pull, markdown_table
from portward_free.pulls import Pulls, no_pull
from portward_free.replies import error, reply
from portward_free.runtime import RESULTS_PANEL_URI, Runtime


ASSUMPTIONS = ("When no definition you were given covers part of the question, say what you assumed in "
               "`assumptions`, one short sentence each (\"active customer means an order in the last 90 days\"); "
               "they're kept with this answer on the user's computer, for them to review and turn into definitions.")


def register_pull_tools(rt: Runtime, pulls: Pulls) -> None:
    """The panel-bound tools (registered on `rt.apps`): each pull shows in the results panel where there is one."""
    _register_runs(rt, pulls)
    _register_kept_rows(rt, pulls)


def _register_runs(rt: Runtime, pulls: Pulls) -> None:
    store = rt.store

    @rt.apps.tool(resource_uri=RESULTS_PANEL_URI, name="query",
                  description="Run one read-only SQL SELECT against a source. `question` is the user's question. "
                              f"{ASSUMPTIONS}")
    async def query(source: str, sql: str, question: str, ctx: Context,
              assumptions: list[str] | None = None) -> CallToolResult | InputRequiredResult:
        return await pulls.record(ctx, source, question, sql, assumptions=assumptions)

    @rt.apps.tool(resource_uri=RESULTS_PANEL_URI, name="rerun",
                  description="Re-run a past pull from history exactly: its stored SQL, unchanged, on the same "
                              "source.")
    async def rerun(pull_id: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        past = store.pull(pull_id)
        if past is None:
            return no_pull(pull_id)
        return await pulls.record(ctx, past.source, past.question, past.sql, rerun_of=past)

    @rt.apps.tool(resource_uri=RESULTS_PANEL_URI, name="run_saved",
                  description="Re-run a saved pull by its name (see saved_pulls), exactly as it was saved.")
    async def run_saved(name: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        saved = store.saved_pull(name.strip())
        if saved is None:
            return error(f"no saved pull named {name!r}; call saved_pulls for the names")
        return await pulls.record(ctx, saved.source, saved.question, saved.sql, rerun_of=saved)

    @rt.apps.tool(resource_uri=RESULTS_PANEL_URI, name="tweak",
                  description="Tweak a past pull ('same, but last quarter'): run `sql`, your change to that pull's "
                              "stored SQL, on the same source. The reply shows the SQL difference; the new pull is "
                              "saved as a child of the old one. `question` is the user's request (defaults to the "
                              f"old). {ASSUMPTIONS}")
    async def tweak(pull_id: str, sql: str, ctx: Context, question: str | None = None,
              assumptions: list[str] | None = None) -> CallToolResult | InputRequiredResult:
        past = store.pull(pull_id)
        if past is None:
            return no_pull(pull_id)
        return await pulls.record(ctx, past.source, question or past.question, sql, tweak_of=past,
                            assumptions=assumptions)


def _register_kept_rows(rt: Runtime, pulls: Pulls) -> None:
    store = rt.store

    @rt.apps.tool(resource_uri=RESULTS_PANEL_URI, name="kept_rows",
                  description="Show the rows a past pull returned, if rows were kept for it (see keep_rows), "
                              "without running it again.")
    async def kept_rows(pull_id: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        rt.prune()
        past = store.pull(pull_id)
        if past is None:
            return no_pull(pull_id)
        sealed = store.kept_rows(pull_id)
        if sealed is None:
            return error("rows weren't kept for this pull; rerun it to see its rows")
        if store.rules_added_since(past.source, past.created_at):  # the kept rows may hold what is now blocked
            return error(f"the user's block rules changed since this pull, so its kept rows aren't shown; rerun "
                         f"pull {pull_id} to see its rows under the current rules")
        try:
            rows = kept.open_sealed(rt.keychain, pull_id, sealed)
        except (KeychainError, kept.RowsLocked) as err:
            return error(f"{err}; rerun the pull to see its rows")
        pull = Pull(source=past.source, question=past.question, sql=past.sql, tables=past.tables,
                    columns_read=past.columns, columns=rows.columns, rows=rows.rows, truncated=rows.truncated,
                    rules=store.rules_for(past.source))
        mode = pulls.mode_of(past.source)
        approved = await pulls.approval(ctx, pull, mode)
        if isinstance(approved, InputRequiredResult):
            return approved
        text, payload = pulls.presented(ctx, past.id, pull, mode=mode, approved=approved)
        return reply({**payload, "pulled_at": past.created_at}, f"{text}\n\nKept rows of the pull of {past.created_at}.")


def _send(outbox: Outbox, held: Held, batch: tuple[int, int]) -> CallToolResult:
    first, last = batch
    rows = held.pull.rows[first - 1:last]
    updated = outbox.sent(held, batch)
    payload = {"pull_id": held.pull_id, "columns": list(held.pull.columns), "rows": [list(r) for r in rows],
               "first_row": first, "sent_rows": updated.sent, "row_count": len(held.pull.rows),
               "truncated": held.pull.truncated}
    left = f"{updated.remaining} rows not sent" if updated.remaining else "every row now sent"
    text = f"{markdown_table(held.pull.columns, rows)}\n\n(rows {first}-{last} of {len(held.pull.rows)}; {left})"
    return reply(payload, text)


def _held_or_refusal(rt: Runtime, pull_id: str) -> Held | CallToolResult:
    held = rt.outbox.get(pull_id)
    if held is None:
        return error(f"pull {pull_id} isn't held in this session; run the query again")
    if rt.store.rules_for(held.pull.source) != held.pull.rules:  # held rows may hold what is now blocked
        return error(f"the user's block rules changed since pull {pull_id}; nothing more is sent from it. "
                     "Run the query again.")
    if held.remaining == 0:
        return reply({"pull_id": pull_id, "rows": [], "sent_rows": held.sent},
                     f"every row of pull {pull_id} has already been sent")
    return held


def _asked_in_text(rt: Runtime, held: Held, batch: tuple[int, int], rows: int, approved: bool) -> CallToolResult:
    """No prompt in the client: the AI relays the question, and a second call says the user agreed."""
    if approved and held.asked == batch:
        return _send(rt.outbox, held, batch)
    rt.outbox.ask(held, batch)
    question, pull_id = held.question(batch), held.pull_id
    return reply(
        {"pull_id": pull_id, "rows": [], "sent_rows": held.sent, "awaiting_approval": True, "question": question},
        f"Nothing more has been sent yet. Ask the user, in these words: \"{question}\" Then wait for their answer. "
        f"Only if they say yes, call more_rows again with pull_id {pull_id!r}, rows {rows} and approved true. "
        "Otherwise carry on with the preview.")


def register_more_rows(server: MCPServer, rt: Runtime) -> None:
    @server.tool(description=(
        "Send more rows of a pull than its preview. The user is asked first, every time; only call this when the "
        "preview isn't enough. `rows` is how many more (default 100, at most 500). Set `approved` only after you "
        "asked the user the question this tool gave you and they said yes."))
    async def more_rows(pull_id: str, ctx: Context, rows: int = MORE_ROWS_DEFAULT, approved: bool = False
                        ) -> CallToolResult | InputRequiredResult:
        held = _held_or_refusal(rt, pull_id)
        if not isinstance(held, Held):
            return held
        batch = held.next_batch(rows)
        if not client_support(ctx).elicitation:
            return _asked_in_text(rt, held, batch, rows, approved)
        answer = await ask_in_client(ctx, held.question(batch), batch)
        if isinstance(answer, InputRequiredResult):
            return answer
        if answer:
            return _send(rt.outbox, held, batch)
        return reply({"pull_id": pull_id, "rows": [], "sent_rows": held.sent, "approved": False},
                     "the user didn't approve sending more rows; carry on with the preview")


def register_export(server: MCPServer, rt: Runtime, pulls: Pulls) -> None:
    @server.tool(description="Export a past pull to a file on the user's machine: format 'csv' or 'xlsx' (Excel). "
                             "Runs the pull's stored SQL again and writes every row; you get only the file's path.")
    def export(pull_id: str, format: str = "csv") -> CallToolResult:
        if format not in WRITERS:
            return error(f"format must be one of: {', '.join(sorted(WRITERS))}")
        past = rt.store.pull(pull_id)
        if past is None:
            return no_pull(pull_id)
        pull = pulls.run(past.source, past.question, past.sql)
        if isinstance(pull, CallToolResult):
            return pull
        path = export_file(rt.store.path.parent / EXPORTS_FOLDER, past.saved_name or past.question, past.id, format,
                           pull.columns, pull.rows)
        same = pull.result_hash == past.result_hash
        payload = {"path": str(path), "format": format, "pull_id": past.id, "row_count": len(pull.rows),
                   "truncated": pull.truncated, "same_result": same}
        text = f"Exported {len(pull.rows)} rows to {path}."
        if not same:
            text += f" The data has changed since the pull of {past.created_at}."
        return reply(payload, text)
