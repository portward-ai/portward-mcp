"""The compare tool: what changed between two pulls (rows added, removed, changed, totals that moved)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, InputRequiredResult

from portward_free import kept, stand_ins
from portward_free.clients import client_name
from portward_free.compare import Comparison, Result, compare_results
from portward_free.guard import QueryRejected
from portward_free.keychain import KeychainError
from portward_free.pull import Pull, run_pull
from portward_free.pulls import Pulls, log_read, no_pull
from portward_free.replies import error, reply
from portward_free.runtime import RESULTS_PANEL_URI, Runtime
from portward_free.sources import SourceError
from portward_free.storage import PullRecord

COMPARE = ("Compare two pulls from history (e.g. a pull and its tweak): rows added, rows removed, rows whose numbers "
           "changed, and totals that moved, from `pull_id` to `other_pull_id`. Uses the rows both pulls kept (see "
           "keep_rows), else re-runs both. Its rows go where a query's do: the results panel, or a capped preview "
           "with more_rows for the rest.")


def _change_rows(found: Comparison) -> tuple[tuple[Any, ...], ...]:
    """The comparison as one table under a leading `change` column: rows added, removed, each changed row before
    and after, then the moved totals. The totals come last: they sum rows the AI may not have been sent, so they
    count as rows of the result and wait behind the same cap and approval."""
    rows: list[tuple[Any, ...]] = [("added", *r) for r in found.added] + [("removed", *r) for r in found.removed]
    for before, after in found.changed:
        rows += [("changed: before", *before), ("changed: after", *after)]
    if found.moved_totals:
        moved = {t.column: t for t in found.moved_totals}
        for label, value in (("total before", "before"), ("total after", "after"), ("total change", "change")):
            rows.append((label, *(getattr(moved[c], value) if c in moved else None for c in found.columns)))
    return tuple(rows)


def _comparison_pull(rt: Runtime, before: PullRecord, after: PullRecord, found: Comparison) -> Pull:
    return Pull(source=before.source, question=f"Compare {before.id} with {after.id}", sql=after.sql,
                tables=(), columns_read=found.columns, columns=("change", *found.columns),
                rows=_change_rows(found), truncated=False, rules=rt.store.rules_for(before.source))


def _summary(before: PullRecord, after: PullRecord, found: Comparison, rows_from: str) -> dict[str, Any]:
    """The comparison's shape: counts and column names, never a value from either result."""
    return {
        "before": before.id, "after": after.id,
        "columns_only_before": list(found.only_before), "columns_only_after": list(found.only_after),
        "added": len(found.added), "removed": len(found.removed), "changed": len(found.changed),
        "moved_totals": [t.column for t in found.moved_totals],
        # "kept": both pulls' rows as they were when they ran; "rerun": both pulls' stored SQL re-run now, and the
        # stored fingerprints tell whether the results differed back when each pull ran
        "rows_from": rows_from,
        "stored_row_counts": {"before": before.row_count, "after": after.row_count},
        "stored_results_differ": before.result_hash != after.result_hash,
    }


def _summary_text(before: PullRecord, after: PullRecord, found: Comparison, rows_from: str,
                  note: str | None) -> str:
    how = ("using the rows each one kept, as they were when it ran." if rows_from == "kept" else
           "both re-run now from their stored SQL.")
    moved = ", ".join(t.column for t in found.moved_totals) or "none"
    parts = [f"Compared {before.id} ({before.created_at}) with {after.id} ({after.created_at}), {how}"
             + (f" {note}" if note else ""),
             f"Rows added: {len(found.added)}. Rows removed: {len(found.removed)}. Rows whose numbers changed: "
             f"{len(found.changed)}. Totals that moved: {moved}."]
    if found.only_before or found.only_after:
        parts.append(f"Columns only before: {', '.join(found.only_before) or 'none'}; "
                     f"only after: {', '.join(found.only_after) or 'none'}.")
    if rows_from == "rerun" and before.source == after.source and before.sql == after.sql:
        differ = "differed" if before.result_hash != after.result_hash else "were the same"
        parts.append(f"Both pulls ran the same SQL and their rows weren't kept, so re-running shows no change. "
                     f"When they ran, their results {differ} ({before.row_count} rows, then {after.row_count}).")
    return "\n\n".join(parts)


def _kept_results(rt: Runtime, before: PullRecord, after: PullRecord) -> tuple[Result, Result] | str | None:
    """Both pulls' kept rows; None when either didn't keep them, or why they couldn't be opened."""
    rt.prune()
    if any(rt.store.rules_added_since(e.source, e.created_at) for e in (before, after)):
        return None  # kept rows may hold what is now blocked: re-run both under the current rules
    sealed = [rt.store.kept_rows(e.id) if e.keep_rows else None for e in (before, after)]
    if None in sealed:
        return None
    try:
        old, new = (kept.open_sealed(rt.keychain, e.id, b) for e, b in zip((before, after), sealed, strict=True))
    except (KeychainError, kept.RowsLocked) as err:
        return f"Their kept rows couldn't be opened ({err}), so both were re-run."
    return Result(old.columns, old.rows), Result(new.columns, new.rows)


def _result_of(rt: Runtime, entry: PullRecord, app: str | None) -> Result | str:
    """The rows of a past pull: its stored SQL re-run now (or an error message). The read goes in the AI log."""
    connection = rt.store.connection(entry.source)
    if connection is None:
        return f"the source {entry.source!r} of pull {entry.id} is no longer connected"
    try:
        with rt.opened(connection) as opened:
            pull = run_pull(entry.source, opened, entry.question, entry.sql, rt.store.rules_for(entry.source),
                            lambda: stand_ins.key_or_none(rt.keychain))
    except (QueryRejected, SourceError) as err:
        return f"pull {entry.id} could not be re-run: {err}"
    log_read(rt, app, pull)
    return Result(pull.columns, pull.rows)


def _compared(rt: Runtime, before: PullRecord, after: PullRecord, app: str | None) -> tuple[Comparison, str, str | None] | str:
    """The comparison, where its rows came from ("kept" or "rerun") and a note; or why it couldn't be made."""
    from_kept = _kept_results(rt, before, after)
    if isinstance(from_kept, tuple):
        return compare_results(*from_kept), "kept", None
    old, new = _result_of(rt, before, app), _result_of(rt, after, app)
    if isinstance(old, str):
        return old
    if isinstance(new, str):
        return new
    return compare_results(old, new), "rerun", from_kept


def register_compare(rt: Runtime, pulls: Pulls) -> None:
    """Bound to the results panel (on `rt.apps`): with a panel, the comparison's rows show there."""

    @rt.apps.tool(resource_uri=RESULTS_PANEL_URI, name="compare", description=COMPARE)
    async def compare(pull_id: str, other_pull_id: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        before, after = rt.store.pull(pull_id), rt.store.pull(other_pull_id)
        if before is None or after is None:
            return no_pull(pull_id if before is None else other_pull_id)
        compared = _compared(rt, before, after, client_name(ctx.session))
        if isinstance(compared, str):
            return error(compared)
        found, rows_from, note = compared
        comparison_id = f"{before.id}~{after.id}"
        compared_pull = _comparison_pull(rt, before, after, found)
        mode = pulls.stricter_mode(before.source, after.source)
        approved = await pulls.approval(ctx, compared_pull, mode)
        if isinstance(approved, InputRequiredResult):
            return approved
        text, payload = pulls.presented(ctx, comparison_id, compared_pull, mode=mode, approved=approved)
        return reply({**_summary(before, after, found, rows_from), **payload},
                     f"{_summary_text(before, after, found, rows_from, note)}\n\n{text}")
