"""A pull: check the AI's SQL, run it against one source, and shape what goes back to the AI."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import sqlglot
from sqlglot import exp

from portward_free import row_cells, totals
from portward_free import stand_ins as stand_in
from portward_free.grants import Cells, Grants
from portward_free.guard import CheckedQuery, check, confined
from portward_free.rules import Policy, Rules
from portward_free.sources import Fetched, Source, SourceError, Table
from portward_free.sources.base import BlocksReads, MakesStandIns

PREVIEW_ROWS = 20  # rows the AI gets back from one pull
MAX_FETCH_ROWS = 10_000  # rows read from the source for one pull (kept for the panel, history and the hash)
REAL_VALUE_ROWS = 1_000  # rows whose stand-ins are looked up for the user's panel (results_panel.PANEL_ROWS)
LOOKUP_BATCH = 500  # stand-ins per lookup query

KeyGetter = Callable[[], bytes | None]  # the anonymising key, or None when it can't be had (anonymised -> hidden)
StandIns = Mapping[str, Mapping[str, str]]  # table -> its columns the AI sees as stand-ins -> their link group


class Shown(NamedTuple):
    """A column a pull read, at the level it was shown, with its table's schema (so the AI log can tell which
    applied profiles granted it: a grant may name a schema)."""
    table: str
    column: str
    level: str
    schema: str = ""


@dataclass(frozen=True)
class Pull:
    source: str
    question: str
    sql: str
    tables: tuple[str, ...]
    columns_read: tuple[str, ...]  # every column the SQL references
    columns: tuple[str, ...]  # the result's columns
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool  # the source had more than MAX_FETCH_ROWS rows
    rules: Policy = field(default_factory=Rules)  # the rules or grants it was made under; rows sent later re-check
    note: str = ""  # what the AI should know about the result (small groups of totals-only columns left out)
    shown: tuple[Shown, ...] = ()  # each column it read with its level (and its table's schema), for the AI log
    # (stand-in, real value) for the stand-ins in the first REAL_VALUE_ROWS rows: for the user's panel only, never
    # for the AI (for_ai, the outbox, selections and shares use `rows`, which hold the stand-ins)
    real_values: tuple[tuple[str, Any], ...] = ()

    @property
    def preview(self) -> tuple[tuple[Any, ...], ...]:
        return self.rows[:PREVIEW_ROWS]

    @property
    def result_hash(self) -> str:
        """SHA-256 of the result's columns and rows, value types included: the same data gives the same hash."""
        canonical = json.dumps([self.columns, [[typed_value(v) for v in r] for r in self.rows], self.truncated],
                               separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def for_ai(self) -> tuple[str, dict[str, Any]]:
        """The text and structured content the AI gets: the first PREVIEW_ROWS rows, the total count, the columns."""
        preview = self.preview
        total = f"more than {len(self.rows)}" if self.truncated else str(len(self.rows))
        shown = f"first {len(preview)} of {total} rows" if len(preview) < len(self.rows) else f"{total} rows"
        text = f"{markdown_table(self.columns, preview)}\n\n({shown}; columns: {', '.join(self.columns)})"
        if self.note:
            text += f"\n{self.note}"
        payload = {
            "columns": list(self.columns),
            "rows": [list(r) for r in preview],
            "row_count": len(self.rows),
            "preview_rows": len(preview),
            "truncated": self.truncated,
        }
        return text, payload


def asks_for_hidden(sql: str, dialect: str, catalog: Sequence[Table], visible: Sequence[Table]) -> bool:
    """Whether the SQL names a table of the source that isn't visible, or a column of a table it reads that isn't
    visible there (#105's hidden-asks count). A name the source doesn't have at all (a typo) isn't counted."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        return False
    real = {t.name.lower(): {c.lower() for c in t.columns} for t in catalog}
    shown = {t.name.lower(): {c.lower() for c in t.columns} for t in visible}
    read = {n.name.lower() for n in tree.find_all(exp.Table)} & set(real)
    if read - set(shown):
        return True
    named = {c.name.lower() for c in tree.find_all(exp.Column)}
    hidden = set().union(*(real[t] for t in read)) - set().union(*(shown.get(t, set()) for t in read))
    return bool(named & hidden)


def run_pull(source_name: str, source: Source, question: str, sql: str, rules: Policy,
             stand_in_key: KeyGetter | None = None, on_hidden: Callable[[], None] | None = None) -> Pull:
    """Refuse anything but one SELECT over what `rules` let the AI see (QueryRejected), then fetch at most
    MAX_FETCH_ROWS rows.

    The rules are applied twice: the query only reaches the allowed columns of the allowed tables (`confined`), and
    the result is checked again before it is returned. Columns granted anonymised are read as stand-ins made with
    `stand_in_key`'s key; without a key, or on a source that can't make them, they are hidden. On profiles,
    `on_hidden` is called when the SQL names something no applied profile grants (it is refused or reads nothing).
    """
    catalog = source.tables()
    visible, anonymised = _anonymised(rules, source, rules.visible(catalog), stand_in_key)
    if on_hidden is not None and isinstance(rules, Grants) and asks_for_hidden(sql, source.dialect, catalog, visible):
        on_hidden()
    # block rules say a table is blocked; grants hide it as if it didn't exist, so the AI isn't told it's there
    known = [t.name for t in catalog] if isinstance(rules, Rules) else []
    checked = check(sql, source.dialect, [t.name for t in visible], known_tables=known)
    allowed = {t.name.lower(): t.columns for t in visible}
    only_totals = rules.totals(catalog) if isinstance(rules, Grants) else {}
    people = rules.persons(catalog) if isinstance(rules, Grants) else {}
    cells = _cells(rules, catalog, visible)
    flagged = {t: row_cells.flagged(c, allowed[t]) for t, c in cells.items()}
    summed = totals.limit(checked, only_totals, allowed, people, flagged={t: f for t, f in flagged.items() if f})
    if isinstance(source, BlocksReads):
        holding = rules.covering(catalog)
        ruled = rules.rule_columns(catalog) if isinstance(rules, Grants) else frozenset()
        source.block_reads(holding.blocked_tables,
                           frozenset(c for c in holding.blocked_columns if (c[0].lower(), c[1].lower()) not in ruled))
    sql_to_run = confined(summed.query if summed else checked, source.dialect, allowed, MAX_FETCH_ROWS + 1,
                          row_id_tables=totals.row_id_tables(checked, only_totals) if summed else (),
                          stand_ins=anonymised,
                          stand_in_sql=source.stand_in_sql if anonymised else None,  # type: ignore[attr-defined]
                          cells=cells)
    try:
        fetched = source.fetch(sql_to_run)
    except SourceError as err:
        if summed:
            raise totals.withheld(err) from None
        # most likely the source said "no such column" for a blocked one: say so, so the AI stops trying
        rules.explain_refusal(checked.tables, checked.columns)
        raise
    if summed:
        fetched = Fetched(*summed.shown(fetched.columns, fetched.rows))
    rules.covering(catalog).check_result(checked.tables, fetched.columns)
    return Pull(
        source=source_name, question=question, sql=sql, tables=checked.tables, columns_read=checked.columns,
        columns=fetched.columns, rows=fetched.rows[:MAX_FETCH_ROWS], truncated=len(fetched.rows) > MAX_FETCH_ROWS,
        rules=rules, note=summed.note if summed else "", shown=shown_columns(checked, visible, rules),
        real_values=_real_values(source, checked, visible, anonymised, fetched),
    )


def shown_columns(checked: CheckedQuery, visible: Sequence[Table], rules: Policy) -> tuple[Shown, ...]:
    """The columns a query read, each with the level it was shown at: the visible columns of the tables it reads
    that it names, or all of them when it selects `*` (count(*) reads no column). A column of the same name in two
    of its tables is counted for both: the AI log errs on the side of saying more was seen, never less."""
    star = any(isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count) for n in checked.tree.walk())
    named = set(checked.columns)
    found: list[Shown] = []
    for table in visible:
        if table.name.lower() not in checked.tables:
            continue
        for column in table.columns:
            level = rules.level(table, column)
            if level is not None and (star or column.lower() in named):
                found.append(Shown(table.name, column, level, table.schema))
    return tuple(found)


def _cells(rules: Policy, catalog: Sequence[Table], visible: Sequence[Table]) -> dict[str, Cells]:
    """The visible tables with row rules, each with its cells (grants.Grants.cells)."""
    if not isinstance(rules, Grants) or not rules.reach:
        return {}
    shown = {t.name.lower() for t in visible}
    found = {t.name.lower(): rules.cells(t) for t in catalog if t.name.lower() in shown}
    return {t: c for t, c in found.items() if c is not None}


def _anonymised(rules: Policy, source: Source, visible: tuple[Table, ...],
                stand_in_key: KeyGetter | None) -> tuple[tuple[Table, ...], StandIns]:
    """The visible tables, and which of their columns the AI sees as stand-ins. When stand-ins can't be made here
    (no key, or a source without them) those columns are hidden instead, never shown in full."""
    if not isinstance(rules, Grants):
        return visible, {}
    wanted = {t.name.lower(): {c.lower(): rules.stand_in_group(t, c) for c in t.columns
                               if rules.shape(t, c) == "anonymised"}
              for t in visible}
    wanted = {t: cols for t, cols in wanted.items() if cols}
    if not wanted:
        return visible, {}
    key = stand_in_key() if stand_in_key is not None and isinstance(source, MakesStandIns) else None
    if key is not None:
        source.use_stand_in_key(key)  # type: ignore[union-attr]
        return visible, wanted
    kept = (Table(t.name, tuple(c for c in t.columns if c.lower() not in wanted.get(t.name.lower(), ())),
                  schema=t.schema, numeric=t.numeric) for t in visible)
    return tuple(t for t in kept if t.columns), {}


def _real_values(source: Source, checked: CheckedQuery, visible: tuple[Table, ...], anonymised: StandIns,
                 fetched: Fetched) -> tuple[tuple[str, Any], ...]:
    """The real value behind each stand-in in the result's first rows, for the user's panel, looked up by Portward's
    own query on the anonymised columns the query read. Empty when there are none, or the lookup fails (the panel
    then shows the stand-ins)."""
    found = sorted({v for row in fetched.rows[:REAL_VALUE_ROWS] for v in row
                    if isinstance(v, str) and stand_in.PATTERN.fullmatch(v)})
    places = [(t.name, c, anonymised[t.name.lower()][c.lower()]) for t in visible if t.name.lower() in checked.tables
              for c in t.columns if c.lower() in anonymised.get(t.name.lower(), {})]
    if not found or not places:
        return ()
    pairs: dict[str, Any] = {}
    try:
        for table, column, group in places:
            for start in range(0, len(found), LOOKUP_BATCH):
                lookup = _lookup_sql(source, table, column, group, found[start:start + LOOKUP_BATCH])
                pairs.update((str(s), v) for s, v in source.fetch(lookup).rows)
    except SourceError:
        return ()
    return tuple(sorted(pairs.items()))


def _lookup_sql(source: Source, table: str, column: str, group: str, wanted: Sequence[str]) -> str:
    dialect = source.dialect
    real = exp.column(exp.to_identifier(column, quoted=True))
    made = sqlglot.parse_one(source.stand_in_sql(real.sql(dialect=dialect), group), read=dialect)  # type: ignore[attr-defined]
    return (exp.select(exp.alias_(made.copy(), "s"), exp.alias_(real, "v")).distinct()
            .from_(exp.Table(this=exp.to_identifier(table, quoted=True)))
            .where(exp.In(this=made, expressions=[exp.Literal.string(w) for w in wanted]))
            .sql(dialect=dialect))


def typed_value(value: Any) -> list[Any]:
    """A JSON-safe form that keeps the type, so 1, 1.0, '1' and b'1' hash differently."""
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if value is None or isinstance(value, (bool, int, float, str)):
        return [type(value).__name__, value]
    return [type(value).__name__, str(value)]


def _cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def markdown_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(_cell(c) for c in columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)
