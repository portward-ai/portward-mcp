"""What changed between two results: rows added, rows removed, rows whose numbers changed, and totals that moved.

Rows are matched on their non-numeric columns (e.g. a name or a month) when those identify each row on both sides;
otherwise a row is matched only by an identical row. Only the columns both results have are compared.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

Row = tuple[Any, ...]


@dataclass(frozen=True)
class Result:
    columns: tuple[str, ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class MovedTotal:
    column: str
    before: float
    after: float

    @property
    def change(self) -> float:
        return self.after - self.before


@dataclass(frozen=True)
class Comparison:
    columns: tuple[str, ...]  # the columns compared (those both results have)
    only_before: tuple[str, ...]  # columns only the first result has
    only_after: tuple[str, ...]
    added: tuple[Row, ...]
    removed: tuple[Row, ...]
    changed: tuple[tuple[Row, Row], ...]  # (before, after) for rows matched on their key whose numbers differ
    moved_totals: tuple[MovedTotal, ...]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numeric_columns(rows: Sequence[Row], width: int) -> set[int]:
    """Columns whose non-null values are all numbers (and that have at least one)."""
    numeric = set()
    for i in range(width):
        values = [r[i] for r in rows if r[i] is not None]
        if values and all(_is_number(v) for v in values):
            numeric.add(i)
    return numeric


def _project(result: Result, columns: Sequence[str]) -> list[Row]:
    positions = [result.columns.index(c) for c in columns]
    return [tuple(row[p] for p in positions) for row in result.rows]


def _unique_keys(rows: Sequence[Row], key_columns: Sequence[int]) -> dict[Row, Row] | None:
    keyed = {tuple(r[i] for i in key_columns): r for r in rows}
    return keyed if len(keyed) == len(rows) else None


def _multiset_difference(left: Sequence[Row], right: Sequence[Row]) -> tuple[Row, ...]:
    remaining = Counter(right)
    out = []
    for row in left:
        if remaining[row]:
            remaining[row] -= 1
        else:
            out.append(row)
    return tuple(out)


def _total(rows: Sequence[Row], i: int) -> float:
    return sum(r[i] for r in rows if r[i] is not None)


def compare_results(before: Result, after: Result) -> Comparison:
    columns = tuple(c for c in before.columns if c in after.columns)
    old, new = _project(before, columns), _project(after, columns)
    numeric = _numeric_columns(old + new, len(columns))
    key_columns = [i for i in range(len(columns)) if i not in numeric]
    old_keyed = _unique_keys(old, key_columns) if key_columns else None
    new_keyed = _unique_keys(new, key_columns) if key_columns else None
    if old_keyed is not None and new_keyed is not None:
        added = tuple(r for k, r in new_keyed.items() if k not in old_keyed)
        removed = tuple(r for k, r in old_keyed.items() if k not in new_keyed)
        changed = tuple((r, new_keyed[k]) for k, r in old_keyed.items() if k in new_keyed and new_keyed[k] != r)
    else:
        added, removed, changed = _multiset_difference(new, old), _multiset_difference(old, new), ()
    totals = tuple(MovedTotal(columns[i], _total(old, i), _total(new, i)) for i in sorted(numeric))
    return Comparison(
        columns=columns,
        only_before=tuple(c for c in before.columns if c not in after.columns),
        only_after=tuple(c for c in after.columns if c not in before.columns),
        added=added, removed=removed, changed=changed,
        moved_totals=tuple(t for t in totals if t.change != 0),
    )
