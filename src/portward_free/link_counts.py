"""Match counts for links (#131, Personal), measured when a profile's context is saved: each end's rows and different
values, and how many values the two ends share, so a link can say it is one-to-many and the AI is told to sum the
many side before joining.

The counts are read through run_pull under the policy the AI would have with the new version saved (profile_editor's
`policy_for`), so an anonymised column is counted on the stand-ins the AI gets (the new link puts both ends in one
stand-in group, so they still match), and a column the AI only gets totals of, or can't see, isn't measured at all:
its link keeps no counts. Both ends in one connection are measured in one query, with the values they share; ends
in two connections are measured apart, without a shared count. Nothing here reaches the AI but the numbers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace

from sqlglot import exp

from portward_free.context import Counts, End, Link, Side
from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.profile_editor import policy_for
from portward_free.profiles import ANONYMISED, FULL, Profile
from portward_free.pull import KeyGetter, run_pull
from portward_free.sources import Source, SourceError, Table
from portward_free.sources.base import MakesStandIns
from portward_free.storage import Connection, LocalStore

QUESTION = "Portward measuring a link's match counts for the user"
COUNTED_LEVELS = (FULL, ANONYMISED)

Opener = Callable[[Connection], AbstractContextManager[Source]]


@dataclass(frozen=True)
class Measurer:
    store: LocalStore
    opened: Opener
    key: KeyGetter

    def measured(self, profile: Profile, earlier: Profile | None = None) -> Profile:
        """`profile` (a version about to be saved) with its new or changed links measured, and any link that has no
        counts yet measured again. A link that can't be measured keeps what it had."""
        before = {k.key: k for k in earlier.context.links} if earlier else {}
        links = tuple(k if k.counts and _same(before.get(k.key), k) else k.measured(self.counts(profile, k))
                      for k in profile.context.links)
        return replace(profile, context=replace(profile.context, links=links))

    def counts(self, profile: Profile, link: Link) -> Counts | None:
        sides: dict[str, Side] = {}
        names = tuple(c.name for c in self.store.connections())
        for name in self.store.profiles.where_applied(profile, names):
            connection = self.store.connection(name)
            if connection is None:
                continue
            try:
                with self.opened(connection) as source:
                    found = self._measure_here(source, name, profile, link, sides)
            except (SourceError, QueryRejected):
                continue
            if found is not None:
                return found
        return Counts(sides["one"], sides["other"]) if len(sides) == 2 else None

    def _measure_here(self, source: Source, name: str, profile: Profile, link: Link,
                      sides: dict[str, Side]) -> Counts | None:
        catalog = source.tables()
        grants, _ = policy_for(self.store, profile, name)
        visible = grants.visible(catalog)
        # without stand-ins here (no key, or a source that can't make them) the AI doesn't get anonymised columns
        levels = COUNTED_LEVELS if isinstance(source, MakesStandIns) and self.key() is not None else (FULL,)
        one, other = _counted(link.one, visible, grants, levels), _counted(link.other, visible, grants, levels)
        if one and other:
            got = self._run(source, name, grants, _both(one, other, source.dialect))
            return Counts(Side(got[0], got[1]), Side(got[2], got[3]), got[4])
        for which, end in (("one", one), ("other", other)):
            if end and which not in sides:
                got = self._run(source, name, grants, _side(end, source.dialect))
                sides[which] = Side(got[0], got[1])
        return None

    def _run(self, source: Source, name: str, grants: Grants, sql: str) -> tuple[int, ...]:
        pulled = run_pull(name, source, QUESTION, sql, grants, self.key)
        (row,) = pulled.rows[:1]
        return tuple(int(v or 0) for v in row)


def _same(earlier: Link | None, link: Link) -> bool:
    return earlier is not None and earlier.stored() == link.stored()


def _counted(end: End, visible: Sequence[Table], grants: Grants,
             levels: Sequence[str]) -> tuple[str, str] | None:
    """The real (table, column) of `end` when the AI gets its values here at one of `levels`."""
    table = next((t for t in visible if t.name.lower() == end.table.lower()
                  and (not end.schema or (t.schema or "").lower() == end.schema.lower())), None)
    column = next((c for c in table.columns if c.lower() == end.column.lower()), None) if table else None
    if table is None or column is None or grants.shape(table, column) not in levels:
        return None
    return table.name, column


def _q(name: str, dialect: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect)


def _side_parts(end: tuple[str, str], dialect: str) -> tuple[str, str]:
    table, column = _q(end[0], dialect), _q(end[1], dialect)
    return (f"(SELECT COUNT({column}) FROM {table})", f"(SELECT COUNT(DISTINCT {column}) FROM {table})")


def _side(end: tuple[str, str], dialect: str) -> str:
    rows, values = _side_parts(end, dialect)
    return f"SELECT {rows} AS pw_rows, {values} AS pw_values"


def _both(one: tuple[str, str], other: tuple[str, str], dialect: str) -> str:
    a_rows, a_values = _side_parts(one, dialect)
    b_rows, b_values = _side_parts(other, dialect)
    a_table, a_column = _q(one[0], dialect), _q(one[1], dialect)
    b_table, b_column = _q(other[0], dialect), _q(other[1], dialect)
    matching = (f"(SELECT COUNT(DISTINCT {a_column}) FROM {a_table} WHERE {a_column} IN "
                f"(SELECT {b_column} FROM {b_table}))")
    return (f"SELECT {a_rows} AS pw_one_rows, {a_values} AS pw_one_values, {b_rows} AS pw_other_rows, "
            f"{b_values} AS pw_other_values, {matching} AS pw_matching")
