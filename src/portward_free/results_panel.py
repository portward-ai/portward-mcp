"""The results panel: the page, the rows it is lent, and the message it puts in the chat.

With a panel the AI gets no rows from a query. The panel reads them through the app-only `panel_rows` tool, the user
picks rows and columns, and `put_in_chat` records exactly that selection (`selections`) and returns a short note
(the selection's id, counts and column names, no values) that the panel posts as a visible chat message from the user
(MCP Apps `ui/message`). The AI then fetches the picked rows with `get_selection`, a tool result the user can expand.
The silent model-context channel is never used.

The rows lent to the panel are held in the local store, sealed with the kept-rows key (`kept`), because the panel's
calls can reach another server process than the query did (Claude Desktop runs two on one app-data folder).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import Any

from portward_free import kept, stand_ins
from portward_free.keychain import Keychain, KeychainError
from portward_free.modes import Receipt, receipt_from
from portward_free.pull import Pull, markdown_table, typed_value
from portward_free.rules import Policy, Rules, policy_from
from portward_free.storage import LocalStore

PANEL_URI = "ui://portward/results.html"
PANEL_ROWS = 1_000  # rows the panel is lent from one pull (the user picks from these)
HELD_PULLS = 50  # recent pulls whose rows the server holds for the panel
HELD_HOURS = 24  # how long the panel can still read a pull's rows
BUTTON = "Put in chat"


class SelectionError(ValueError):
    """The panel asked for a pull, rows or columns that don't exist."""


@dataclass(frozen=True)
class Held:
    pull_id: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]  # at most PANEL_ROWS
    row_count: int  # rows the pull read
    truncated: bool  # the source had more than the pull read
    source: str = ""  # the source and SQL the rows came from (value_source traces a value back through them)
    sql: str = ""
    rules: Policy = Rules()  # the block rules or grants it was pulled under: the rows aren't shown once they change
    expires_at: float = 0.0  # when the panel can no longer read them (a selection from them expires by then too)
    # (stand-in, real value) for anonymised columns: the panel shows the user the real values; `rows` keep the
    # stand-ins, so a selection put in the chat gives the AI stand-ins only
    real_values: tuple[tuple[str, Any], ...] = ()
    # results mode (#117): what the AI got of this result; its kept columns are shown here but never put in chat
    receipt: Receipt | None = None


class HeldPulls:
    """The rows of recent pulls, so the panel can show them without the AI receiving them. Held sealed in the store
    (any server process on it can read them back) and, as they were pulled, in this process's memory. When the
    keychain can't seal them, only in memory: never in the file unsealed."""

    def __init__(self, store: LocalStore, keychain: Keychain, limit: int = HELD_PULLS,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._keychain = keychain
        self._limit = limit
        self._clock = clock
        self._held: OrderedDict[str, tuple[Held, float]] = OrderedDict()  # with when each expires

    def hold(self, pull_id: str, pull: Pull, receipt: Receipt | None = None) -> Held:
        now = self._clock()
        expires = now + HELD_HOURS * 3600
        held = Held(pull_id, pull.columns, pull.rows[:PANEL_ROWS], len(pull.rows), pull.truncated, pull.source,
                    pull.sql, pull.rules, expires, pull.real_values, receipt)
        self._held[pull_id] = (held, expires)
        while len(self._held) > self._limit:
            self._held.popitem(last=False)
        try:
            secret = kept.key(self._keychain, create=True)
            assert secret is not None
            self._store.hold_panel_pull(pull_id, kept.seal_json(secret, _associated(pull_id), _sealable(held)),
                                        now, expires, self._limit)
        except (KeychainError, kept.RowsLocked):
            pass  # this process's panel calls still read them from memory; the file never holds them unsealed
        return held

    def get(self, pull_id: str) -> Held:
        gone = SelectionError(f"pull {pull_id} isn't open any more; ask the question again")
        now = self._clock()
        here = self._held.get(pull_id)
        if here is not None and here[1] > now:
            return here[0]
        found = self._store.panel_pull(pull_id, now)
        if found is None:
            raise gone
        sealed, expires = found
        try:
            data = kept.open_json(kept.stored_key(self._keychain, "the panel's rows"), _associated(pull_id), sealed,
                                  "the panel's rows")
        except (KeychainError, kept.RowsLocked) as err:
            raise SelectionError(f"pull {pull_id} can't be opened ({err}); ask the question again") from None
        return replace(_unsealed(pull_id, data), expires_at=expires)


def _associated(pull_id: str) -> str:
    return f"panel-pull:{pull_id}"  # not a kept-rows blob's (pull id alone): neither can stand in for the other


def _sealable(held: Held) -> dict[str, Any]:
    return {"columns": list(held.columns), "rows": [[typed_value(v) for v in r] for r in held.rows],
            "row_count": held.row_count, "truncated": held.truncated, "source": held.source, "sql": held.sql,
            "rules": held.rules.payload(),
            "real_values": [[s, typed_value(v)] for s, v in held.real_values],
            "receipt": held.receipt.payload() if held.receipt is not None else None}


def _unsealed(pull_id: str, data: dict[str, Any]) -> Held:
    return Held(pull_id, tuple(data["columns"]), tuple(tuple(kept.untyped(v) for v in r) for r in data["rows"]),
                int(data["row_count"]), bool(data["truncated"]), data["source"], data["sql"],
                policy_from(data["rules"]),
                real_values=tuple((str(s), kept.untyped(v)) for s, v in data.get("real_values", [])),
                receipt=receipt_from(data.get("receipt")))


def for_ai(held: Held) -> tuple[str, dict[str, Any]]:
    """What the AI gets from a query shown in the panel: the shape of the result, never its rows."""
    total = f"more than {held.row_count}" if held.truncated else str(held.row_count)
    text = (f"The result ({total} rows; columns {', '.join(held.columns)}) is shown to the user in the Portward "
            f"panel, not to you. The user picks rows and columns and presses '{BUTTON}'; their message then gives a "
            "selection id to fetch with get_selection, and you'll see only those rows and columns. Don't guess at "
            "the values.")
    payload = {"pull_id": held.pull_id, "columns": list(held.columns), "row_count": held.row_count,
               "truncated": held.truncated, "shown_in": "panel"}
    return text, payload


def for_panel(held: Held) -> dict[str, Any]:
    """The rows the panel shows (at most PANEL_ROWS), with the real value in place of each stand-in it knows, and
    the columns where the AI gets stand-ins."""
    real = dict(held.real_values)
    rows = [[real.get(v, v) if isinstance(v, str) else v for v in r] for r in held.rows]
    found = {"pull_id": held.pull_id, "columns": list(held.columns), "rows": rows,
             "row_count": held.row_count, "truncated": held.truncated, "stand_ins": stand_in_columns(held),
             "block_rules": isinstance(held.rules, Rules)}  # #119/#120: the column header menu shows then
    return {**found, "receipt": held.receipt.payload()} if held.receipt is not None else found


def stand_in_columns(held: Held) -> list[str]:
    """The result's columns holding stand-ins (the AI sees those, never the real values)."""
    return [c for i, c in enumerate(held.columns)
            if any(isinstance(r[i], str) and stand_ins.PATTERN.fullmatch(r[i]) for r in held.rows)]


@dataclass(frozen=True)
class Picked:
    """The rows (by position) and columns the user picked from one held pull, in the result's order."""

    pull_id: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    of_rows: int  # rows the pull read
    of_columns: int
    every_row: bool  # every row the pull read (and the source had no more)


def picked(held: Held, rows: Sequence[int], columns: Sequence[str]) -> Picked:
    """Only the chosen rows (by position) and columns, in the result's order. Raises SelectionError for an empty
    selection or one outside the result."""
    chosen_rows = sorted(set(rows))
    unknown = [c for c in columns if c not in held.columns]
    if held.receipt is not None and (kept_out := [c for c in columns if c in held.receipt.kept_columns]):
        raise SelectionError(f"{kept_out[0]} is kept from the AI; it can't be put in the chat")
    if unknown:
        raise SelectionError(f"no column {unknown[0]!r} in this result")
    if not chosen_rows or not columns:
        raise SelectionError("choose at least one row and one column")
    if chosen_rows[0] < 0 or chosen_rows[-1] >= len(held.rows):
        raise SelectionError(f"rows are numbered 0 to {len(held.rows) - 1} in this result")
    keep = [i for i, c in enumerate(held.columns) if c in set(columns)]
    return Picked(held.pull_id, tuple(held.columns[i] for i in keep),
                  tuple(tuple(held.rows[r][i] for i in keep) for r in chosen_rows), held.row_count,
                  len(held.columns), len(chosen_rows) == held.row_count and not held.truncated)


def _rows_shown(chosen: Picked) -> str:
    return f"all {chosen.of_rows}" if chosen.every_row else f"{len(chosen.rows)} of {chosen.of_rows}"


def chat_note(selection_id: str, chosen: Picked) -> str:
    """The short message the panel puts in the chat for a selection: its id, how much was picked and the column
    names. Never a row value: the AI fetches the rows with get_selection."""
    return (f"From Portward: use my selection {selection_id} ({_rows_shown(chosen)} rows, {len(chosen.columns)} of "
            f"{chosen.of_columns} columns: {', '.join(chosen.columns)}).")


def selection_rows(selection_id: str, chosen: Picked) -> tuple[str, dict[str, Any]]:
    """What get_selection gives the AI for a selection: exactly the picked rows and columns, as a table."""
    text = (f"From Portward (pull {chosen.pull_id}), the user's selection {selection_id}: {_rows_shown(chosen)} "
            f"rows and {len(chosen.columns)} of {chosen.of_columns} columns:\n\n"
            f"{markdown_table(list(chosen.columns), [list(r) for r in chosen.rows])}")
    return text, {"selection_id": selection_id, "pull_id": chosen.pull_id, "columns": list(chosen.columns),
                  "row_count": len(chosen.rows)}


def page(name: str = "results.html") -> str:
    """A panel page as one self-contained HTML resource: the host bridge, helpers, tokens and styles inlined."""
    panel = files("portward_free").joinpath("panel")
    html = panel.joinpath(name).read_text(encoding="utf-8")
    if "/*PORTWARD_HOST*/" not in html or "/*PORTWARD_STYLES*/" not in html:
        raise RuntimeError(f"{name} has no host or styles placeholder")
    scripts = "".join(panel.joinpath(script).read_text(encoding="utf-8")
                      for script in ("host.js", "ui.js", "levels.js", "column_menu.js"))  # #120 levels, #119 header menu
    styles = panel.joinpath("tokens.css").read_text(encoding="utf-8") + panel.joinpath("panel.css").read_text(encoding="utf-8")
    return html.replace("/*PORTWARD_STYLES*/", styles).replace("/*PORTWARD_HOST*/", scripts)
