"""Per-column share options (#52), applied to the snapshot on the sender's machine before it is sealed.

Each column of a share is one of: include (the default), hide, anonymise or totals.

Anonymise replaces each value with `anon-` and the first 16 hex digits of HMAC-SHA256(secret, value as JSON), where
the secret is 32 random bytes made for this one share and then dropped: never stored, never sealed into the share.
So the same value shows the same code everywhere in a share (rows can still be counted and matched up, across
columns too), but nobody can turn a code back into its value by guessing values (the sender included, once the
share is made), and the same value gets an unrelated code in every other share, so shares can't be linked through
it. Nulls stay null.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from typing import Any

INCLUDE, HIDE, ANONYMISE, TOTALS = "include", "hide", "anonymise", "totals"
_SPELLINGS = {INCLUDE: INCLUDE, HIDE: HIDE, ANONYMISE: ANONYMISE, "anonymize": ANONYMISE, TOTALS: TOTALS}
ANON_PREFIX = "anon-"
_ANON_HEX = 16  # 64 bits: no collisions to speak of within a share's 10,000 rows
SECRET_BYTES = 32
MIN_GROUP_ROWS = 5  # a totals-only group smaller than this is left out of the share
GROUP_COUNT = "row count"


class ShareOptionError(ValueError):
    """The options don't fit the table (a column it doesn't have, an unknown option, nothing left to share)."""


def chosen(columns: list[str], options: Mapping[str, str] | None) -> list[str]:
    """Each column's option, in column order; columns not named are included."""
    picked = {}
    for name, option in (options or {}).items():
        if name not in columns:
            raise ShareOptionError(f"this share has no column {name!r}; its columns are {', '.join(columns)}")
        spelled = str(option).strip().lower()
        if spelled not in _SPELLINGS:
            raise ShareOptionError(f"{option!r} isn't an option for {name!r}: use include, hide, anonymise or totals")
        picked[name] = _SPELLINGS[spelled]
    return [picked.get(name, INCLUDE) for name in columns]


def anonymiser(secret: bytes) -> Callable[[Any], Any]:
    """value -> its code under `secret` (the same value, the same code)."""
    def code(value: Any) -> Any:
        if value is None:
            return None
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        return ANON_PREFIX + hmac.new(secret, raw, hashlib.sha256).hexdigest()[:_ANON_HEX]
    return code


def _same(value: Any) -> Any:
    return value


def with_options(snapshot: dict[str, Any], options: Mapping[str, str] | None, *,
                 secret: bytes | None = None) -> dict[str, Any]:
    """The snapshot as shared under `options`, as a new snapshot (`snapshot` is left as it was). `secret` is the
    anonymising key: tests pass one; a share leaves it out and gets a fresh one that nothing keeps."""
    columns = list(snapshot["columns"])
    kinds = chosen(columns, options)
    kept = [i for i, kind in enumerate(kinds) if kind != HIDE]
    if not kept:
        raise ShareOptionError("every column is hidden, so there would be nothing to share")
    anon = anonymiser(secret if secret is not None else secrets.token_bytes(SECRET_BYTES))
    shown = [(i, anon if kinds[i] == ANONYMISE else _same) for i in kept]
    table = {**snapshot, "columns": [columns[i] for i in kept],
             "rows": [[show(row[i]) for i, show in shown] for row in snapshot["rows"]],
             "anonymised": [columns[i] for i in kept if kinds[i] == ANONYMISE]}
    totals = [columns[i] for i in kept if kinds[i] == TOTALS]
    return _totalled(table, totals) if totals else table


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _totalled(table: dict[str, Any], totals: list[str]) -> dict[str, Any]:
    """The table grouped by its other columns, with a row count and each totals column's sum (or, for a column that
    isn't all numbers, its count of values). Groups of fewer than MIN_GROUP_ROWS rows are left out, and there is
    no grand total, so no single row's value can be read off a total or worked out by subtracting from one."""
    columns, rows = table["columns"], table["rows"]
    keys = [i for i, name in enumerate(columns) if name not in totals]
    summed = [i for i, name in enumerate(columns) if name in totals]
    numeric = {i: all(_is_number(row[i]) for row in rows if row[i] is not None) for i in summed}
    groups: dict[tuple[Any, ...], list[list[Any]]] = {}
    for row in rows:  # typed keys, so 1, 1.0 and true stay apart; dicts keep first-seen order
        groups.setdefault(tuple((type(row[i]).__name__, row[i]) for i in keys), []).append(row)
    big = [members for members in groups.values() if len(members) >= MIN_GROUP_ROWS]
    if not big:
        raise ShareOptionError(
            f"every group has fewer than {MIN_GROUP_ROWS} rows, so its totals would show single values: hide or "
            "total more of the other columns so rows group together")

    def total(i: int, members: list[list[Any]]) -> Any:
        values = [row[i] for row in members if row[i] is not None]
        return sum(values) if numeric[i] else len(values)

    labels = [columns[i] for i in keys] + [GROUP_COUNT] + [
        f"{columns[i]} ({'total' if numeric[i] else 'count'})" for i in summed]
    return {**table, "columns": labels,
            "rows": [[members[0][i] for i in keys] + [len(members)] + [total(i, members) for i in summed]
                     for members in big],
            "row_count": len(big),
            "totals": {"columns": totals, "min_group_rows": MIN_GROUP_ROWS, "from_rows": len(rows),
                       "rows_left_out": len(rows) - sum(len(members) for members in big)}}
