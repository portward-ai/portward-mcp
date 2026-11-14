"""The preview at the upgrade moment (#120): a column of a result in the panel as the AI would get it at a Personal level.

On Free, the panel's column menu offers "Anonymise" and "Totals only", marked Personal. Choosing one shows that
column's values in this result as the AI would get them: a stand-in beside each of the user's own values, or the
totals in place of the rows. The offer itself (offers.py, through the app-only personal_offer) shows beside it, once.

Two promises:
- **A kept column is never read.** A column the user keeps from the AI (a block rule on any source column behind it,
  or one the result's receipt lists as kept) gets no preview: the answer says it is kept and carries no value. A
  column whose source can't be traced, or whose source can't be opened to check the rules, gets no preview either.
- **Nothing here reaches the AI.** `level_preview` is app-only (the host keeps it and its result from the model,
  and the AI log skips app-only tools); nothing is written to history, kept rows or selections.

Stand-ins are made as Personal makes them: the account's anonymising key (made on first use and kept in the
keychain; sync keeps the copy already on the account) and the column's link group through
`Grants.stand_in_group` (Free has no links, so the column is its own group).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult
from sqlglot.errors import SqlglotError

from portward_free import provenance, results_panel, stand_ins
from portward_free.clients import client_support
from portward_free.grants import Grants
from portward_free.pulls import Pulls
from portward_free.replies import error, reply
from portward_free.results_panel import Held
from portward_free.rules import Rules
from portward_free.runtime import Runtime
from portward_free.sources import SourceError, Table
from portward_free.totals import MIN_GROUP_SIZE

ANONYMISED = "anonymised"
TOTALS = "totals"
LEVELS = (ANONYMISED, TOTALS)
PREVIEW_VALUES = 3  # different values shown beside their stand-ins (the design's three rows)


class NoPreview(ValueError):
    """There is no preview for this column; the message is safe to show the user."""


@dataclass(frozen=True)
class Origin:
    """The source column behind a result column."""

    table: Table
    column: str


def _catalog(rt: Runtime, source: str) -> tuple[tuple[Table, ...], str]:
    connection = rt.store.connection(source)
    if connection is None:
        raise NoPreview(f"{source} isn't connected any more")
    try:
        with rt.opened(connection) as opened:
            return tuple(opened.tables()), opened.dialect
    except SourceError as err:
        raise NoPreview(f"{source} can't be opened now, so Portward can't check what you keep from the AI "
                        f"({err})") from None


def _origins(held: Held, column: str, tables: Sequence[Table], dialect: str) -> tuple[str, tuple[Origin, ...]]:
    """The kind of the result column ("column", "aggregate", "calculated") and the source columns behind it.
    Raises NoPreview when it can't be traced: then nothing proves it reads no kept column."""
    try:
        found = provenance.trace(held.sql, dialect, column, {t.name: t.columns for t in tables})
    except (ValueError, SqlglotError):
        found = None
    by_name = {t.name.lower(): t for t in tables}
    origins = tuple(Origin(by_name[o.table.lower()], o.column) for o in (found.origins if found else ())
                    if o.column is not None and o.table.lower() in by_name)
    if found is None or not found.traced or not origins:
        raise NoPreview(f"Portward can't tell which column of {held.source} {column} comes from, so it shows no "
                        "preview")
    return found.kind, origins


def _kept(held: Held, column: str, origins: Sequence[Origin], rules: Rules) -> bool:
    if held.receipt is not None and column in held.receipt.kept_columns:
        return True
    return any(rules.level(o.table, o.column) is None for o in origins)


def _stand_ins(rt: Runtime, held: Held, column: str, kind: str, origins: Sequence[Origin]) -> list[list[Any]]:
    if kind != "column" or len(origins) != 1:
        raise NoPreview(f"{column} is worked out by the query, not read from one column, so it has no stand-ins")
    try:
        key = stand_ins.account_key(rt.keychain)
    except stand_ins.StandInsUnavailable as err:
        raise NoPreview(str(err)) from None
    group = Grants().stand_in_group(origins[0].table, origins[0].column)
    at = held.columns.index(column)
    values = list(dict.fromkeys(r[at] for r in held.rows if r[at] is not None))[:PREVIEW_VALUES]
    return [[v, stand_ins.stand_in_for(key, _as_text(v), group)] for v in values]


def _as_text(value: Any) -> str:
    """A value as the database turns it to text for a stand-in (CAST(... AS TEXT))."""
    return value if isinstance(value, str) else str(value)


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    return None


def _totals(held: Held, column: str) -> dict[str, Any]:
    at = held.columns.index(column)
    values = [r[at] for r in held.rows]
    found: dict[str, Any] = {"rows": len(values), "minimum": MIN_GROUP_SIZE, "small": len(values) < MIN_GROUP_SIZE}
    if found["small"]:
        return found  # the AI would get nothing for so few rows
    numbers = [_number(v) for v in values if v is not None]
    if numbers and all(n is not None for n in numbers):
        total = sum(n for n in numbers if n is not None)
        found |= {"sum": float(total), "average": float(total / len(numbers))}
    return found


def preview(rt: Runtime, held: Held, column: str, level: str) -> dict[str, Any]:
    """The preview of `column` of `held` at `level`. Raises NoPreview."""
    if level not in LEVELS:
        raise NoPreview(f"no level called {level!r}; the levels are {', '.join(LEVELS)}")
    if column not in held.columns:
        raise NoPreview(f"no column {column!r} in this result")
    if not isinstance(held.rules, Rules):
        raise NoPreview("this connection runs on profiles: set its levels in the profile editor")
    tables, dialect = _catalog(rt, held.source)
    kind, origins = _origins(held, column, tables, dialect)
    found: dict[str, Any] = {"pull_id": held.pull_id, "column": column, "level": level, "source": held.source,
                             "from": [{"table": o.table.name, "column": o.column} for o in origins]}
    if _kept(held, column, origins, Rules(rt.store.rules(held.source)).covering(tables)):
        return found | {"kept": True}  # never read: no value of it goes into the answer
    if level == ANONYMISED:
        return found | {"kept": False, "rows": _stand_ins(rt, held, column, kind, origins)}
    return found | {"kept": False, "totals": _totals(held, column), "truncated": held.truncated}


def register_level_preview(rt: Runtime, pulls: Pulls) -> None:
    @rt.app_tool(name="level_preview", description=(
        "The user chose Anonymise or Totals only from a column's menu in the results panel: that column "
        "of the result as the AI would get it at that level (\"anonymised\" or \"totals\"). Never for a column the "
        "user keeps from the AI. App-only: the model never sees this tool or its result."))
    def level_preview(pull_id: str, column: str, level: str, ctx: Context) -> CallToolResult:
        if not client_support(ctx).panel:
            return error("level_preview is for the results panel only")
        if (stale := pulls.rules_changed(pull_id)) is not None:
            return stale
        try:
            found = preview(rt, rt.panel_pulls.get(pull_id), column, level)
        except (results_panel.SelectionError, NoPreview) as err:
            return error(str(err))
        return reply(found, "a preview for the results panel")
