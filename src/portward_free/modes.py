"""Free's two modes per source (#117): 'Claude gets results' and 'I send rows myself'.

- results: the AI gets the result rows straight away, at most AI_ROWS of them, minus the columns the user keeps from
  it (block rules, "Keep from Claude" in the UI). A kept column doesn't fail the query any more: the user's panel
  still shows it, from a second run of the same SQL without the block rules, and the panel's receipt says what the
  AI got and what it didn't.
- pick: today's flow. With a panel the AI gets the result's shape and the user puts rows in the chat. Without one the
  AI gets rows only after the user says yes in the client's own prompt; a client that can't ask gets the shape only.

What the AI gets in results mode always comes from the run under the block rules (pull.run_pull: the allowed-column
rewrite, the engine-level block on SQLite and the result re-check). The user's run is for their panel alone: its
rows, errors and hash never reach the AI, the history, the AI log or kept rows. When the SQL can't run without a kept
column, the AI's run is retried with the outermost selected expressions naming a kept column dropped; if that doesn't
run either, the AI gets no rows and is told which kept columns the SQL uses. None of it turns on whether the user's
run worked: that can depend on a kept value.

A connection on profiles (Personal) has no mode: its grants decide (`Pulls.mode_of` gives None).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import sqlglot
from sqlglot import exp

from portward_free.guard import QueryRejected
from portward_free.pull import KeyGetter, Pull, markdown_table, run_pull
from portward_free.rules import Policy, Rules
from portward_free.sources import Source, SourceError
from portward_free.storage import SEND_MODES, SEND_PICK, SEND_RESULTS

RESULTS, PICK, MODES = SEND_RESULTS, SEND_PICK, SEND_MODES
AI_ROWS = 200  # most rows of one result that reach the AI without the user picking them
LABELS = {RESULTS: "Claude gets results", PICK: "I send rows myself"}  # the modes' names in the UI


@dataclass(frozen=True)
class Ran:
    """One results-mode run: what the AI may get (None: nothing, see `withheld`) and the user's own view (None: the
    same as the AI's)."""
    ai: Pull | None
    user: Pull | None = None
    withheld: tuple[str, ...] = ()  # the kept columns the SQL uses, when the AI got nothing
    told: tuple[str, ...] = ()  # the kept columns the AI is told it didn't get (from its SQL, never the user's run)

    @property
    def shown(self) -> Pull:
        """What the user's panel shows."""
        found = self.user if self.user is not None else self.ai
        assert found is not None  # a run with neither is an error, never a Ran
        return found

    def recorded(self, source: str, question: str, sql: str) -> Pull:
        """What history keeps of this run: the AI's pull, or an empty one when the AI got nothing (never the user's
        rows or their hash: a hash of a small result can be guessed back)."""
        if self.ai is not None:
            return self.ai
        return Pull(source=source, question=question, sql=sql, tables=self.shown.tables, columns_read=(),
                    columns=(), rows=(), truncated=False, rules=self.shown.rules)


@dataclass(frozen=True)
class Receipt:
    """What the AI got of one result, for the panel: the rows and columns sent, and the columns kept from it."""
    row_count: int  # rows the user's result has
    truncated: bool
    sent_rows: int
    sent_columns: tuple[str, ...]
    kept_columns: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {"mode": RESULTS, "row_count": self.row_count, "truncated": self.truncated,
                "sent_rows": self.sent_rows, "sent_columns": list(self.sent_columns),
                "kept_columns": list(self.kept_columns)}


def receipt_of(ran: Ran) -> Receipt:
    shown = ran.shown
    sent = ran.ai.columns if ran.ai is not None else ()
    named = {c.lower() for c in sent}
    return Receipt(len(shown.rows), shown.truncated,
                   min(AI_ROWS, len(ran.ai.rows)) if ran.ai is not None else 0, tuple(sent),
                   tuple(c for c in shown.columns if c.lower() not in named))


def receipt_from(data: Any) -> Receipt | None:
    """A receipt from its `payload()` (None for anything else)."""
    if not isinstance(data, dict) or data.get("mode") != RESULTS:
        return None
    return Receipt(int(data["row_count"]), bool(data["truncated"]), int(data["sent_rows"]),
                   tuple(data["sent_columns"]), tuple(data["kept_columns"]))


def run_results(name: str, source: Source, question: str, sql: str, rules: Policy,
                stand_in_key: KeyGetter | None = None, on_hidden: Callable[[], None] | None = None) -> Ran:
    """Run `sql` for results mode. Raises QueryRejected or SourceError, from the AI's run only, when neither it nor
    its retry without kept columns runs and the SQL names no kept column.

    What the AI gets depends on its own run and the SQL alone, never on the user's run: whether that run fails can
    turn on a kept value (a division by zero on one), so its success or failure would tell the AI one bit."""
    if not isinstance(rules, Rules) or not rules.rules:
        return Ran(run_pull(name, source, question, sql, rules, stand_in_key, on_hidden))
    kept = kept_names(sql, source.dialect, rules)
    told = told_kept(sql, source.dialect, kept)
    try:
        ai: Pull | None = run_pull(name, source, question, sql, rules, stand_in_key, on_hidden)
        refusal: Exception | None = None
    except (QueryRejected, SourceError) as err:
        ai, refusal = None, err
    if ai is None and (trimmed := without_kept(sql, source.dialect, kept)) is not None:
        try:
            ai = replace(run_pull(name, source, question, trimmed, rules, stand_in_key), sql=sql)
        except (QueryRejected, SourceError):
            ai = None
    user = _users_run(name, source, question, sql, rules)
    if ai is not None:
        return Ran(ai, user, told=told)
    withheld = tuple(sorted(used(sql, source.dialect, kept)))
    if not withheld:
        assert refusal is not None
        raise refusal
    if user is None:  # the panel shows an empty result and the receipt (the user's error can quote kept values)
        user = Pull(source=name, question=question, sql=sql, tables=read_tables(sql, source.dialect),
                    columns_read=(), columns=(), rows=(), truncated=False, rules=rules)
    return Ran(None, user, withheld=withheld)


def _users_run(name: str, source: Source, question: str, sql: str, rules: Rules) -> Pull | None:
    """The SQL run for the user's own panel, without block rules (kept columns in); None when it doesn't run."""
    try:
        return replace(run_pull(name, source, question, sql, Rules()), rules=rules)
    except (QueryRejected, SourceError):
        return None


def read_tables(sql: str, dialect: str) -> tuple[str, ...]:
    """The lower-case names of the tables `sql` names."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        return ()
    return tuple(sorted({t.name.lower() for t in tree.find_all(exp.Table)}))


def kept_names(sql: str, dialect: str, rules: Rules) -> frozenset[str]:
    """The lower-case names of the kept columns of the tables `sql` reads (a whole kept table's columns aren't
    named: such a query has nothing to fall back on)."""
    read = set(read_tables(sql, dialect))
    return frozenset(c for t, c in rules.blocked_columns if t in read)


def told_kept(sql: str, dialect: str, kept: frozenset[str]) -> tuple[str, ...]:
    """The kept columns the AI is told it didn't get: those its SQL names, and with a `*` every kept column of the
    tables it reads. Worked out from the SQL alone, so the user's own run can't change it."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        return ()
    if any(isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count) for n in tree.walk()):
        return tuple(sorted(kept))
    return tuple(sorted({c.name.lower() for c in tree.find_all(exp.Column) if c.name.lower() in kept}))


def used(sql: str, dialect: str, kept: frozenset[str]) -> set[str]:
    """The kept names `sql` mentions anywhere."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        return set()
    return {c.name for c in tree.find_all(exp.Column) if c.name.lower() in kept}


def without_kept(sql: str, dialect: str, kept: frozenset[str]) -> str | None:
    """`sql` with the outermost SELECT's expressions that name a kept column dropped, or None when it isn't one
    SELECT, names none of them there, or would have nothing left. Only a convenience: the AI's run of it still goes
    through every check, so a kept column used anywhere else (a filter, a subquery) just fails it."""
    if not kept:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        return None
    if not isinstance(tree, exp.Select):
        return None
    keep = [e for e in tree.expressions if not any(c.name.lower() in kept for c in e.find_all(exp.Column))]
    if not keep or len(keep) == len(tree.expressions):
        return None
    return tree.copy().select(*keep, append=False).sql(dialect=dialect)


def for_ai(ran: Ran, pull_id: str, in_panel: bool) -> tuple[str, dict[str, Any]]:
    """What the AI gets in results mode: the first AI_ROWS rows of its own pull, or, when the SQL needs a kept
    column, no rows and which columns are kept. Nothing here comes from the user's run (`ran.user`)."""
    where = "The user sees the full result and a receipt of what you got in the Portward panel." if in_panel else ""
    if ran.ai is None:
        text = (f"No rows were sent to you: the query uses {', '.join(ran.withheld) or 'columns'}, which the user "
                "keeps from you. Ask again without them. " + where).strip()
        payload = {"pull_id": pull_id, "columns": [], "rows": [], "row_count": 0, "kept_columns": list(ran.withheld),
                   "sent_rows": 0}
        return text, ({**payload, "shown_in": "panel"} if in_panel else payload)
    pull = ran.ai
    rows = pull.rows[:AI_ROWS]
    total = f"more than {len(pull.rows)}" if pull.truncated else str(len(pull.rows))
    shown = f"first {len(rows)} of {total} rows" if len(rows) < len(pull.rows) else f"{total} rows"
    text = f"{markdown_table(pull.columns, rows)}\n\n({shown}; columns: {', '.join(pull.columns)})"
    if pull.note:
        text += f"\n{pull.note}"
    if ran.told:
        text += f"\nKept from you by the user, not sent: {', '.join(ran.told)}."
    if unsent := len(pull.rows) - len(rows):
        text += (f"\n\nThe other {unsent} rows have not been sent. If these aren't enough, call more_rows with "
                 f"pull_id {pull_id!r}; the user is asked before any more are sent.")
    if where:
        text += f"\n{where}"
    payload = {"pull_id": pull_id, "columns": list(pull.columns), "rows": [list(r) for r in rows],
               "row_count": len(pull.rows), "sent_rows": len(rows), "truncated": pull.truncated,
               "kept_columns": list(ran.told)}
    return text, ({**payload, "shown_in": "panel"} if in_panel else payload)


def shape_only(pull_id: str, pull: Pull, source: str, why: str) -> tuple[str, dict[str, Any]]:
    """What the AI gets in pick mode without a panel when the user hasn't said yes: the result's shape only."""
    total = f"more than {len(pull.rows)}" if pull.truncated else str(len(pull.rows))
    text = (f"The result ({total} rows; columns {', '.join(pull.columns)}) was not sent to you: {source} is set to "
            f"'{LABELS[PICK]}', and {why}. Don't guess at the values. The user can see the result in an app with the "
            f"Portward panel (Claude Desktop), or switch {source} to '{LABELS[RESULTS]}'.")
    return text, {"pull_id": pull_id, "columns": list(pull.columns), "rows": [], "row_count": len(pull.rows),
                  "truncated": pull.truncated, "sent_rows": 0, "awaiting_user": True}


def send_question(pull: Pull, source: str) -> str:
    count = min(AI_ROWS, len(pull.rows))
    total = f"more than {len(pull.rows)}" if pull.truncated else str(len(pull.rows))
    rows = f"all {count} rows" if count == len(pull.rows) and not pull.truncated else f"the first {count} of {total} rows"
    return (f"Send {rows} of this result from {source} ({pull.question!r}; columns {', '.join(pull.columns)}) to "
            "the AI?")


def send_state(pull: Pull) -> str:
    """The prompt's request_state: the same question asked again (the client retries the call) gets the answer."""
    digest = hashlib.sha256(f"{pull.source}\n{pull.sql}\n{pull.result_hash}".encode()).hexdigest()[:32]
    return f"send-rows:{digest}"


def names(modes: Sequence[str]) -> str:
    return " or ".join(f"'{m}' ({LABELS[m]})" for m in modes)
