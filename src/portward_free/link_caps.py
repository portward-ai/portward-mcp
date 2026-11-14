"""Link groups lowered by an anonymised member (spec #114: "a value anonymised on any row is anonymised on every
row", across a link group).

Linked columns share one stand-in key (stand_ins.Scopes), so if one column of a group were anonymised and another
shown in full, the AI could read the real value off the full column and the stand-in off the anonymised one and map
every stand-in back. So when any column of a link group is anonymised or totals only in any applied profile (here or
on another connection), every column of the group is at most anonymised for the AI (grants.Grants.shape and cells).

`caps` says which columns that lowers and why, for Grants and for the user (why-hidden, the profile editor's
preview). The AI is only told the columns are anonymised, never why.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from portward_free.profiles import ANONYMISED, FULL, TOTALS
from portward_free.stand_ins import Scopes

if TYPE_CHECKING:
    from portward_free.grants import Grants
    from portward_free.profiles import Profile
    from portward_free.sources import Table

Column = tuple[str, str]  # (table, column), lower case
LOWER = (ANONYMISED, TOTALS)


@dataclass(frozen=True)
class LinkCap:
    """`name` is at most anonymised for the AI because `by` (a column of its link group) is `level` in `profile`."""

    name: str  # table.column as the profiles name it
    by: str
    level: str
    profile: str

    @property
    def key(self) -> Column:
        table, column = self.name.lower().rsplit(".", 1)
        return table, column

    @property
    def text(self) -> str:
        if self.by.lower() == self.name.lower():
            lowered = f"{self.name} is {self.level} in {self.profile!r} and is linked to other columns"
        else:
            lowered = f"{self.name} is linked to {self.by}, which is {self.level} in {self.profile!r}"
        return (f"{lowered}. Linked columns share stand-ins, so the AI could match a stand-in to a real value through "
                f"the link: it gets {self.name} anonymised, never in full, wherever the link reaches")


def caps(profiles: Sequence[Profile], elsewhere: Iterable[Profile] = ()) -> tuple[LinkCap, ...]:
    """The columns of a link group with a member anonymised or totals only in any of `profiles` (applied here) or
    `elsewhere` (applied on other connections), each with the member that lowers it."""
    everyone = {p.id: p for p in (*elsewhere, *profiles)}.values()
    names: dict[Column, str] = {}
    pairs = []
    for profile in everyone:
        for link in profile.context.links:
            ends = [(e.table, e.column) for e in (link.one, link.other)]
            for table, column in ends:
                names.setdefault((table.lower(), column.lower()), f"{table}.{column}")
            pairs.append(tuple((t.lower(), c.lower()) for t, c in ends))
    lowered: dict[Column, tuple[str, str]] = {}
    for profile in sorted(everyone, key=lambda p: p.name.lower()):
        for grant in profile.grants:
            key = (grant.table.lower(), grant.column.lower())
            if grant.level in LOWER and key in names:
                lowered.setdefault(key, (grant.level, profile.name))
    found = []
    for members in Scopes.of(pairs).groups():  # type: ignore[arg-type]
        low = sorted(m for m in members if m in lowered)
        if not low:
            continue
        for member in sorted(members):
            by = low[0] if low[0] != member or len(low) == 1 else low[1]
            level, profile_name = lowered[by]
            found.append(LinkCap(names[member], names[by], level, profile_name))
    return tuple(found)


def capped(found: Iterable[LinkCap]) -> frozenset[Column]:
    return frozenset(c.key for c in found)


def lowered_here(found: Iterable[LinkCap], grants: Grants, catalog: Iterable[Table]) -> tuple[LinkCap, ...]:
    """The caps that change something on this catalog: columns granted in full here that the AI gets anonymised
    (or not at all, where stand-ins can't be made) because of their link group."""
    by_key = {c.key: c for c in found}
    return tuple(cap for t in catalog for col in t.columns
                 if (cap := by_key.get((t.name.lower(), col.lower()))) is not None and grants.level(t, col) == FULL)
