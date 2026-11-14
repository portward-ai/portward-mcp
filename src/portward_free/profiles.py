"""Profiles (#91, Personal): a named bundle of what the AI may see, written by the user and applied to connections.

A profile holds a name, a one-line description, who first made it, who last changed it, a version and its rules
(grants). It never holds data, credentials, connection names or where it is applied: the application records the
connection (storage). Saving publishes a whole new version; nothing is edited in place.

A grant names `schema.table.column` (the schema optional, meaning the connection's default schema), matched
case-insensitively, with no wildcards. Granting a table lists its columns by name at that moment, so a column added
later stays hidden. Each grant has a level; levels rank full, then anonymised, then totals (LEVELS), and when
profiles combine a column gets the highest level any of them grants, one level only. All three are built: full,
anonymised (the AI sees stand-ins, see stand_ins) and totals (totals.py enforces totals). A grant a connection can't
apply as written (totals on a column that isn't a number; anonymised where no stand-ins can be made) falls to hidden
and the preview says why (`hidden_why`). Context is the profile's own field (context.Context): the meanings of what it
grants, which a starting profile carries over from Free's notes (starting_profile.py) and the AI drafts for the user
to approve, and named definitions (#95); links join it later. Every save keeps only the meanings of what the profile
still grants.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from portward_free.context import Context, Meaning, links_granted
from portward_free.row_rules import RowRule
from portward_free.sources import Table

__all__ = ["Context", "Grant", "Meaning", "Profile", "ProfileError", "about_granted", "preview"]

FULL, ANONYMISED, TOTALS = "full", "anonymised", "totals"
LEVELS = (FULL, ANONYMISED, TOTALS)  # highest first: combining keeps the first of these any profile grants
BUILT_LEVELS = frozenset({FULL, ANONYMISED, TOTALS})  # the levels the free tool can apply today
NAME_PART = re.compile(r"[^.\s*][^.*]*")  # one identifier: no dots, no wildcard
MAX_NAME, MAX_DESCRIPTION = 80, 200

Key = tuple[str | None, str, str]  # (schema or None, table, column), lower case


class ProfileError(ValueError):
    """A profile or grant that can't be saved as written; the message is safe to show."""


@dataclass(frozen=True)
class Grant:
    table: str
    column: str
    schema: str | None = None
    level: str = FULL

    @classmethod
    def parse(cls, text: str, level: str = FULL) -> Grant:
        """`table.column` or `schema.table.column`; ProfileError otherwise."""
        if level not in BUILT_LEVELS:
            raise ProfileError(f"level {level!r} isn't available yet; grants are {', '.join(sorted(BUILT_LEVELS))}")
        parts = [p.strip() for p in str(text).split(".")]
        if len(parts) not in (2, 3) or not all(NAME_PART.fullmatch(p) for p in parts):
            raise ProfileError(f"{text!r}: name each grant as schema.table.column or table.column (no wildcards); "
                               "to grant a whole table, pass it in `tables` with the `source` to list its columns from")
        return cls(parts[-2], parts[-1], parts[0] if len(parts) == 3 else None, level)

    @property
    def name(self) -> str:
        return ".".join(p for p in (self.schema, self.table, self.column) if p)

    @property
    def key(self) -> Key:
        return (self.schema.lower() if self.schema else None, self.table.lower(), self.column.lower())

    def payload(self) -> dict[str, str]:
        return {"name": self.name, "level": self.level}

    def found_in(self, tables: Iterable[Table]) -> Table | None:
        """The table of `tables` this grant names, if it has the column."""
        for table in tables:
            if table.name.lower() == self.table.lower() and _same_schema(self.schema, table) \
                    and self.column.lower() in {c.lower() for c in table.columns}:
                return table
        return None


def _same_schema(schema: str | None, table: Table) -> bool:
    return schema is None or (bool(table.schema) and schema.lower() == table.schema.lower())


def table_grants(table: str, tables: Sequence[Table], source: str) -> tuple[Grant, ...]:
    """A whole table granted: each of its columns now, by name. ProfileError if the source has no such table."""
    parts = [p.strip() for p in table.split(".")]
    wanted = Grant(parts[-1], "x", parts[0] if len(parts) == 2 else None)
    found = next((t for t in tables if t.name.lower() == wanted.table.lower() and _same_schema(wanted.schema, t)),
                 None)
    if found is None or len(parts) > 2:
        raise ProfileError(f"{source} has no table {table!r}; it has: {', '.join(t.name for t in tables)}")
    return tuple(Grant(found.name, c, wanted.schema and found.schema) for c in found.columns)


def without_repeats(grants: Iterable[Grant]) -> tuple[Grant, ...]:
    """One grant per column (the first kept), in order."""
    seen: dict[Key, Grant] = {}
    for grant in grants:
        seen.setdefault(grant.key, grant)
    return tuple(seen.values())


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    description: str
    made_by: str
    changed_by: str
    version: int
    grants: tuple[Grant, ...]
    saved_at: str = ""
    context: Context = field(default_factory=Context)
    rows: tuple[RowRule, ...] = ()  # #132: at most one row rule and (#133) one standing filter per table it grants

    @classmethod
    def new(cls, name: str, description: str, by: str, grants: Iterable[Grant],
            meanings: Iterable[Meaning] = (), rows: Iterable[RowRule] = ()) -> Profile:
        grants = without_repeats(grants)
        return cls(uuid.uuid4().hex, *_checked(name, description), by, by, 1, grants,
                   context=Context(meanings=about_granted(meanings, grants)), rows=rows_granted(rows, grants))

    def saved(self, by: str, *, name: str | None = None, description: str | None = None,
              grants: Iterable[Grant] | None = None, context: Context | None = None,
              rows: Iterable[RowRule] | None = None) -> Profile:
        """The next version, changed by `by`: a whole new profile, this one untouched."""
        name, description = _checked(self.name if name is None else name,
                                     self.description if description is None else description)
        grants = without_repeats(self.grants if grants is None else grants)
        context = self.context if context is None else context
        return Profile(self.id, name, description, self.made_by, by, self.version + 1, grants,
                       context=replace(context, meanings=about_granted(context.meanings, grants),
                                       links=links_granted(context.links, grants)),
                       rows=rows_granted(self.rows if rows is None else rows, grants))

    def row_rule(self, table: str) -> RowRule | None:
        """Its row rule for `table` (any case), if it has one."""
        return next((r for r in self.rows if r.table.lower() == table.lower() and not r.standing), None)

    def standing_filter(self, table: str) -> RowRule | None:
        """Its standing filter for `table` (any case), if it has one (#133)."""
        return next((r for r in self.rows if r.table.lower() == table.lower() and r.standing), None)

    @property
    def signature(self) -> str:
        return f"made by {self.made_by} · last changed by {self.changed_by}, v{self.version}"

    def payload(self) -> dict[str, object]:
        return {"name": self.name, "description": self.description, "made_by": self.made_by,
                "changed_by": self.changed_by, "version": self.version,
                "grants": [g.payload() for g in self.grants], "context": self.context.payload(self.grants)}


def about_granted(context: Iterable[Meaning], grants: Iterable[Grant]) -> tuple[Meaning, ...]:
    """The meanings that speak only about what `grants` grant: a column's when that column is granted, a table's when
    any of its columns is. Context never speaks about what the profile hides."""
    keys = {g.key for g in grants}
    tables = {(schema, table) for schema, table, _ in keys}
    schema_of = (lambda m: m.schema.lower() if m.schema else None)
    return tuple(m for m in context
                 if ((schema_of(m), m.table.lower(), m.column.lower()) in keys if m.column
                     else (schema_of(m), m.table.lower()) in tables))


def rows_granted(rows: Iterable[RowRule], grants: Iterable[Grant]) -> tuple[RowRule, ...]:
    """The row rules of tables `grants` grant a column of (a rule on a table it no longer grants is dropped, as its
    meanings are); ProfileError for two rules (or two standing filters) on one table (join them into one)."""
    tables = {g.table.lower() for g in grants}
    found: dict[tuple[str, bool], RowRule] = {}
    for rule in rows:
        key = (rule.table.lower(), rule.standing)
        if key in found:
            if rule.standing:
                raise ProfileError(f"{rule.table} has two standing filters; join them into one with AND")
            raise ProfileError(f"{rule.table} has two row rules; join them into one with OR")
        if rule.table.lower() in tables:
            found[key] = rule
    return tuple(found.values())


def _checked(name: str, description: str) -> tuple[str, str]:
    name, description = " ".join(str(name).split()), " ".join(str(description).split())
    if not name or len(name) > MAX_NAME:
        raise ProfileError(f"a profile needs a name of 1 to {MAX_NAME} characters")
    if len(description) > MAX_DESCRIPTION:
        raise ProfileError(f"keep the description to one line ({MAX_DESCRIPTION} characters at most)")
    return name, description


# --- the match preview ----------------------------------------------------------------------------------------------

DERIVED_WHY = "it's a view or partition, and Portward can't check what it reads"
TOTALS_ON_TEXT_WHY = "totals only works on numbers, and this column isn't one (or the source doesn't say)"
NO_STAND_INS_WHY = ("this connection can't make stand-ins (a wrapped server would see the key in its SQL), so "
                    "anonymised falls to hidden")


def hidden_why(level: str, table: Table, column: str) -> str | None:
    """Why a grant at `level` on `column` of `table` can't be applied as written, so the column falls to hidden
    instead (never to full); None when it applies."""
    if level not in BUILT_LEVELS:
        return f"{level} isn't available here yet"
    if level == TOTALS and column.lower() not in {c.lower() for c in table.numeric}:
        return TOTALS_ON_TEXT_WHY
    if level == ANONYMISED and not table.stand_ins:
        return NO_STAND_INS_WHY
    return None


@dataclass(frozen=True)
class Preview:
    """How a profile's grants land on one connection's catalog."""

    matched: tuple[Grant, ...] = ()
    hidden_here: tuple[tuple[Grant, str], ...] = ()  # matched by name but can't be applied as written, and why
    not_in_your_data: tuple[Grant, ...] = ()

    @property
    def nothing_matches(self) -> bool:
        return not self.matched

    def payload(self) -> dict[str, object]:
        return {"counts": {"matched": len(self.matched), "hidden_here": len(self.hidden_here),
                           "not_in_your_data": len(self.not_in_your_data)},
                "matched": [g.name for g in self.matched],
                "hidden_here": [{"grant": g.name, "why": why} for g, why in self.hidden_here],
                "not_in_your_data": [g.name for g in self.not_in_your_data]}

    def text(self) -> str:
        lines = [f"{len(self.matched)} matched, {len(self.hidden_here)} hidden here instead, "
                 f"{len(self.not_in_your_data)} not in your data."]
        lines += [f"- hidden here instead: {g.name} ({why})" for g, why in self.hidden_here]
        if self.not_in_your_data:
            lines.append("- not in your data: " + ", ".join(g.name for g in self.not_in_your_data)
                         + " (each lights up by itself if it turns up later)")
        return "\n".join(lines)


def preview(grants: Iterable[Grant], tables: Iterable[Table]) -> Preview:
    tables = tuple(tables)
    matched: list[Grant] = []
    hidden: list[tuple[Grant, str]] = []
    missing: list[Grant] = []
    for grant in grants:
        found = grant.found_in(tables)
        if found is None:
            missing.append(grant)
        elif found.derived:
            hidden.append((grant, DERIVED_WHY))
        elif why := hidden_why(grant.level, found, grant.column):
            hidden.append((grant, why))
        else:
            matched.append(grant)
    return Preview(tuple(matched), tuple(hidden), tuple(missing))


@dataclass(frozen=True)
class Applied:
    """A profile applied to a connection, with the grants that weren't in the data when last looked."""

    profile: Profile
    missing: tuple[str, ...] = field(default=())  # grant names, as Grant.name
    until: str | None = None  # #134: when it stops applying (UTC ISO time); None: until taken off
    apps: tuple[str, ...] = ()  # #134: the web AI apps (OAuth client ids) it applies for; empty: every AI app

    def ended(self, now: str) -> bool:
        """Whether its end date has passed at `now` (a UTC ISO time, compared as an instant)."""
        return self.until is not None and not later(self.until, now)

    def for_app(self, app: str | None) -> bool:
        """Whether it applies for `app` (web_app.current(): None on desktop, where a limited one never applies)."""
        return not self.apps or (app is not None and app in self.apps)


def later(when: str, than: str) -> bool:
    """Whether ISO time `when` is after `than`, as instants (a time without a zone is UTC)."""
    return _instant(when) > _instant(than)


def _instant(when: str) -> datetime:
    found = datetime.fromisoformat(when)
    return found if found.tzinfo else found.replace(tzinfo=UTC)
