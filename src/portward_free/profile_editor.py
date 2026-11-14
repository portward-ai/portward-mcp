"""The profile editor's logic (#126, Personal): what the local page's editor shows and saves. Its markup is in
profile_editor_html.py and its routes in page.py.

The editor is one form. Every change made in it stays in the form (a draft: nothing is stored) until the user saves,
and a save publishes exactly one new version, whatever was changed in that sitting. A save goes through the same
rules as update_profile (profile_tools.next_grants, shows_more, more_question), so a change made here is stored, and
enforced, exactly as the same change made by tool. Letting the AI see more through a profile that is applied
somewhere asks first, on a page of its own, as unblocking does; so does dropping or changing a table's person
column, which makes totals count rows again. Saving needs Personal on now (signed by the account's email), and is
refused when the page was opened for the web routes (`editable` False): profiles change only from this computer.

The sample rows are read through `pull.run_pull` under the draft's grants, so they are exactly what the AI would get:
stand-ins for anonymised columns, totals that pass the minimum group, and nothing hidden. They are shown to the user
only; nothing is logged or sent to the AI.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from sqlglot import exp

from portward_free import link_caps, stand_ins
from portward_free.context import Meaning
from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.profile_tools import ProfileTools, more_question, next_grants, shows_more
from portward_free.profiles import ANONYMISED, FULL, LEVELS, TOTALS, Grant, Preview, Profile, ProfileError, preview
from portward_free.pull import run_pull
from portward_free.row_reasons import RowReasons, row_reasons
from portward_free.row_rules import RowRule, RowRuleError
from portward_free.sources import SourceError, Table
from portward_free.storage import LocalStore

HIDDEN = "hidden"
CHOICES = (*LEVELS, HIDDEN)
LEVEL_FIELD, PERSON_FIELD, ROWS_FIELD, FILTER_FIELD = "g:", "p:", "r:", "f:"
SAMPLE_ROWS = 5
SAMPLE_QUESTION = "Profile editor sample (shown to the user only)"
WEB_ONLY = ("Profiles can be changed only from the Portward page on this computer: this page was opened for the web "
            "routes, which can't change profiles.")
CHANGED_SINCE = ("Profile {name!r} changed since you opened it (it's v{version} now, from another app or computer). "
                 "Your edits are still here: check them against the new version and save again.")
PERSON_TEXT = "Each row belongs to the person in {person}."


@dataclass(frozen=True)
class Draft:
    """A profile as saved, and the edits the editor's form holds for it."""

    profile: Profile
    levels: Mapping[str, str] = field(default_factory=dict)  # grant name -> chosen level, as submitted
    persons: Mapping[str, str] = field(default_factory=dict)  # table (lower case) -> person column, "" for none
    name: str | None = None
    description: str | None = None
    rows: Mapping[str, str] = field(default_factory=dict)  # #132: table (lower case) -> row rule text, "" for none
    filters: Mapping[str, str] = field(default_factory=dict)  # #133: table (lower case) -> standing filter text

    def saved_level(self, name: str) -> str:
        key = Grant.parse(name).key
        return next((g.level for g in self.profile.grants if g.key == key), HIDDEN)

    def level(self, name: str) -> str:
        return self.levels.get(name, self.saved_level(name))

    def changed(self) -> tuple[str, ...]:
        """The columns whose level the draft changes."""
        return tuple(n for n, lv in self.levels.items() if lv != self.saved_level(n))

    def grants(self) -> list[Grant]:
        new = [Grant.parse(n, lv) for n, lv in self.levels.items() if lv != HIDDEN and lv != self.saved_level(n)]
        gone = [Grant.parse(n).key for n, lv in self.levels.items() if lv == HIDDEN]
        return next_grants(self.profile.grants, new, gone)

    def person(self, table: str) -> str:
        if table.lower() in self.persons:
            return self.persons[table.lower()]
        return next((m.person or "" for m in self._table_meanings(self.profile.context.meanings, table)), "")

    def person_changes(self) -> tuple[tuple[str, str, str], ...]:
        """(table, person column as saved, as drafted) for each table whose person column the draft changes."""
        found = []
        for table, wanted in self.persons.items():
            held = next((m.person or "" for m in self._table_meanings(self.profile.context.meanings, table)), "")
            if held.lower() != wanted.lower():
                found.append((table, held, wanted))
        return tuple(found)

    def saved_rows(self, table: str, standing: bool = False) -> str:
        rule = self.profile.standing_filter(table) if standing else self.profile.row_rule(table)
        return rule.text if rule else ""

    def row_text(self, table: str, standing: bool = False) -> str:
        """The row rule (with `standing`, the standing filter) the form shows for `table`: as drafted (even when it
        can't be read yet), else as saved."""
        drafted = self.filters if standing else self.rows
        return drafted.get(table.lower(), self.saved_rows(table, standing))

    def row_changes(self, standing: bool = False) -> tuple[tuple[str, str, str], ...]:
        """(table, as saved, as drafted) for each table whose row rule (with `standing`, standing filter) the draft
        changes ("" = none)."""
        found = []
        for table, text in (self.filters if standing else self.rows).items():
            held, wanted = self.saved_rows(table, standing), " ".join(text.split())
            try:
                wanted = RowRule.parse(table, wanted).text if wanted else ""
            except RowRuleError:
                pass  # a change, refused when saved
            if wanted != held:
                found.append((table, held, wanted))
        return tuple(found)

    def row_rules(self, grants: Sequence[Grant]) -> tuple[RowRule, ...]:
        """The row rules and standing filters the draft would save; ProfileError for one outside the grammar."""
        kept = list(self.profile.rows)
        for standing, what in ((False, "row rule"), (True, "standing filter")):
            changed = {t: wanted for t, _, wanted in self.row_changes(standing)}
            kept = [r for r in kept if r.standing != standing or r.table.lower() not in changed]
            for table, text in changed.items():
                if not text:
                    continue
                try:
                    kept.append(RowRule.parse(_table_named(table, grants), text, standing=standing))
                except RowRuleError as err:
                    raise ProfileError(f"the {what} of {table}: {err}") from None
        return tuple(kept)

    def meanings(self, grants: Sequence[Grant]) -> tuple[Meaning, ...]:
        meanings = list(self.profile.context.meanings)
        for table, _, person in self.person_changes():
            if person and not _granted(table, person, grants):
                raise ProfileError(f"{table}.{person} isn't granted, so it can't be the person column of {table}; grant it "
                                   "first or choose no person column")
            own = self._table_meanings(meanings, table)
            if own:
                meanings = [replace(m, person=person or None) if m in own else m for m in meanings]
            elif person:
                name = _table_named(table, grants)
                meanings.append(Meaning(name, None, PERSON_TEXT.format(person=person), person=person))
        return tuple(meanings)

    @staticmethod
    def _table_meanings(meanings: Sequence[Meaning], table: str) -> list[Meaning]:
        return [m for m in meanings if m.column is None and m.table.lower() == table.lower()]

    def edited(self, by: str) -> Profile:
        """The next version with every edit of the draft: one version for the sitting."""
        grants = self.grants()
        context = replace(self.profile.context, meanings=self.meanings(grants))
        return self.profile.saved(by, name=self.name, description=self.description, grants=grants, context=context,
                                  rows=self.row_rules(grants))

    def change_count(self) -> int:
        named = (self.name is not None and " ".join(self.name.split()) != self.profile.name) + (
            self.description is not None and " ".join(self.description.split()) != self.profile.description)
        return (len(self.changed()) + len(self.person_changes()) + len(self.row_changes())
                + len(self.row_changes(standing=True)) + named)

    def looser(self, places: Sequence[str]) -> str | None:
        """What to ask before saving, when the profile is applied to `places` and the draft shows the AI more."""
        if not places:
            return None
        question = more_question(self.profile, shows_more(self.profile.grants, self.grants()), places)
        persons = [(t, held) for t, held, _ in self.person_changes() if held]
        if persons:
            said = "; ".join(f"totals of {t} will no longer count different people by {held}" for t, held in persons)
            question = (f"{question} " if question else "") + (
                f"Change the person column of profile {self.profile.name!r} ({said})? It's applied to "
                f"{', '.join(places)}.")
        widened = [(t, held, wanted) for t, held, wanted in self.row_changes() if held]
        if widened:
            said = "; ".join(f"{t} from rows where {held} to {f'rows where {wanted}' if wanted else 'every row'}"
                             for t, held, wanted in widened)
            question = (f"{question} " if question else "") + (
                f"Change the row rule of profile {self.profile.name!r} ({said})? The AI may see rows it couldn't "
                f"before. It's applied to {', '.join(places)}.")
        loosened = [(t, held, wanted) for t, held, wanted in self.row_changes(standing=True) if held]
        if loosened:
            said = "; ".join(f"{t} from rows where {held} to {f'rows where {wanted}' if wanted else 'no filter'}"
                             for t, held, wanted in loosened)
            question = (f"{question} " if question else "") + (
                f"Change the standing filter of profile {self.profile.name!r} ({said})? Every read of these tables "
                f"may then keep rows it left out before. It's applied to {', '.join(places)}.")
        return question


def _granted(table: str, column: str, grants: Sequence[Grant]) -> bool:
    return any(g.table.lower() == table.lower() and g.column.lower() == column.lower() for g in grants)


def _table_named(table: str, grants: Sequence[Grant]) -> str:
    return next((g.table for g in grants if g.table.lower() == table.lower()), table)


def draft_from(profile: Profile, form: Mapping[str, str]) -> Draft:
    """The draft a submitted editor form holds; ProfileError for a level or name it can't hold."""
    levels: dict[str, str] = {}
    for key, value in form.items():
        if not key.startswith(LEVEL_FIELD):
            continue
        name = key.removeprefix(LEVEL_FIELD)
        if value not in CHOICES:
            raise ProfileError(f"{value!r} isn't a level; choose one of {', '.join(CHOICES)}")
        Grant.parse(name)
        levels[name] = value
    persons = {k.removeprefix(PERSON_FIELD).lower(): v.strip() for k, v in form.items() if k.startswith(PERSON_FIELD)}
    rows = {k.removeprefix(ROWS_FIELD).lower(): v.strip() for k, v in form.items() if k.startswith(ROWS_FIELD)}
    filters = {k.removeprefix(FILTER_FIELD).lower(): v.strip() for k, v in form.items() if k.startswith(FILTER_FIELD)}
    return Draft(profile, levels, persons, form.get("name"), form.get("description"), rows, filters)


# --- the table groups the form shows --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    name: str  # the field's grant name (table.column, or the saved grant's own name)
    column: str
    numeric: bool | None  # None: not in this connection's data
    saved: str
    chosen: str
    why: str = ""  # hidden here instead, and why


@dataclass(frozen=True)
class Group:
    table: str
    schema: str | None
    columns: tuple[Column, ...]
    person: str = ""
    missing: bool = False  # the profile names it, but it isn't in this connection's data
    rows: str = ""  # #132: its row rule's text as the form shows it ("" = every row)
    filter: str = ""  # #133: its standing filter's text as the form shows it ("" = none)

    @property
    def granted(self) -> int:
        return sum(c.chosen != HIDDEN for c in self.columns)


def groups(draft: Draft, catalog: Sequence[Table] | None, extra: Sequence[str], hidden_here: Mapping[str, str]) \
        -> tuple[Group, ...]:
    """A group per table the draft grants, the user added or opened, with every column of it in `catalog`; the
    grants the catalog doesn't have, by table, at the end (all of them when the catalog can't be read)."""
    names = {Grant.parse(n).key: n for n in draft.levels} | {g.key: g.name for g in draft.profile.grants}
    drafted = [Grant.parse(n, lv if lv != HIDDEN else FULL) for n, lv in draft.levels.items()]
    wanted = {g.table.lower() for g in draft.grants()} | {g.table.lower() for g in draft.profile.grants} | {
        t.lower() for t in extra}
    found: list[Group] = []
    placed: set[object] = set()
    for table in catalog or ():
        if table.name.lower() not in wanted:
            continue
        columns = []
        for column in table.columns:
            name = _name_for(names, table, column)
            if name is None:
                continue
            placed.add(Grant.parse(name).key)
            columns.append(Column(name, column, column in table.numeric, draft.saved_level(name), draft.level(name),
                                  hidden_here.get(name, "")))
        found.append(Group(table.name, table.schema, tuple(columns), draft.person(table.name),
                           rows=draft.row_text(table.name), filter=draft.row_text(table.name, standing=True)))
    lost: dict[str, list[Column]] = {}
    for grant in (*draft.profile.grants, *drafted):
        if grant.key in placed or (grant.found_in(catalog) if catalog else None):
            continue
        placed.add(grant.key)
        lost.setdefault(grant.table, []).append(Column(names[grant.key], grant.column, None,
                                                       draft.saved_level(names[grant.key]),
                                                       draft.level(names[grant.key])))
    found += [Group(t, None, tuple(cols), draft.person(t), missing=True) for t, cols in lost.items()]
    return tuple(found)


def _name_for(names: Mapping[object, str], table: Table, column: str) -> str | None:
    """The field name of `column`: the grant naming it (with or without schema), else table.column; None when the
    column can't be named in a grant (a dot in its name)."""
    for key in ((table.schema.lower() if table.schema else None, table.name.lower(), column.lower()),
                (None, table.name.lower(), column.lower())):
        if key in names:
            return names[key]
    try:
        return Grant.parse(f"{table.name}.{column}").name
    except ProfileError:
        return None


# --- what the AI would get ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sample:
    table: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[object, ...], ...] = ()
    stand_ins: tuple[str, ...] = ()  # columns the AI gets as stand-ins
    hidden: tuple[str, ...] = ()  # columns the AI is never told exist
    totals: tuple[str, ...] = ()
    total_columns: tuple[str, ...] = ()
    total_rows: tuple[tuple[object, ...], ...] = ()
    total_note: str = ""
    alone: bool = False  # the profile isn't applied to this connection: shown as if it were the only one
    problem: str = ""


def combined(store: LocalStore, draft_profile: Profile, source: str) -> tuple[tuple[Profile, ...], bool]:
    """The profiles that would apply to `source` with the draft saved: the other applied profiles and the draft; or
    the draft alone (True) when it isn't applied there."""
    applied = [a.profile for a in store.profiles.applied(source)] if store.profiles.on_profiles(source) else []
    here = any(p.id == draft_profile.id for p in applied)
    chosen = (*(p for p in applied if p.id != draft_profile.id), draft_profile) if here else (draft_profile,)
    return chosen, not here


def policy_for(store: LocalStore, draft_profile: Profile, source: str) -> tuple[Grants, bool]:
    """What the AI would get on `source` with the draft saved, combined as store.rules_for combines profiles; or the
    draft alone (True) when it isn't applied there."""
    chosen, alone = combined(store, draft_profile, source)
    return Grants.applied(chosen, elsewhere(store, draft_profile)), alone


def elsewhere(store: LocalStore, draft_profile: Profile) -> tuple[Profile, ...]:
    """The profiles applied on any connection, with the draft in place of its saved version (link_caps)."""
    return tuple(draft_profile if p.id == draft_profile.id else p for p in store.profiles.applied_anywhere())


def rows_preview(store: LocalStore, draft: Draft, source: str, catalog: Sequence[Table] | None
                 ) -> RowReasons | str:
    """What the row rules would do on `source` with the draft saved (row_reasons), mentioning only what involves
    the draft's profile; the problem when the draft can't be read."""
    if catalog is None or not source:
        return RowReasons()
    try:
        drafted = draft.edited("draft")
    except ProfileError as err:
        return str(err)
    chosen, _ = combined(store, drafted, source)
    found = row_reasons(chosen, catalog)
    mine = drafted.name
    others = elsewhere(store, drafted)
    linked = link_caps.lowered_here(link_caps.caps(chosen, others), Grants.applied(chosen, others), catalog)
    return RowReasons(tuple(r for r in found.rows if mine in r.profiles),
                      tuple(r for r in found.some_rows if any(n == mine for n, _, _ in r.reached)),
                      tuple(r for r in found.lowered if mine in (r.of, r.by)), linked)


def sample(opened: Callable[[], object], source: str, grants: Grants, alone: bool, table: Table,
           key: Callable[[], bytes | None]) -> Sample:
    """Up to SAMPLE_ROWS rows of `table` and its totals, read through run_pull under `grants`, as the AI gets them."""
    shown = next((t for t in grants.visible([table])), None)
    columns = shown.columns if shown else ()
    levels = {c: grants.shape(table, c) for c in columns}
    rows_of = tuple(c for c in columns if levels[c] in (FULL, ANONYMISED))
    totals = tuple(c for c in columns if levels[c] == TOTALS)
    base = Sample(table.name, stand_ins=tuple(c for c in rows_of if levels[c] == ANONYMISED),
                  hidden=tuple(c for c in table.columns if c not in columns), totals=totals, alone=alone)
    try:
        with opened() as src:  # type: ignore[attr-defined]
            dialect = src.dialect
            if rows_of:
                sql = exp.select(*(exp.column(exp.to_identifier(c, quoted=True)) for c in rows_of)).from_(
                    exp.Table(this=exp.to_identifier(table.name, quoted=True))).limit(SAMPLE_ROWS).sql(dialect)
                got = run_pull(source, src, SAMPLE_QUESTION, sql, grants, key)
                base = replace(base, columns=got.columns, rows=got.rows[:SAMPLE_ROWS])
            if totals:
                sums = [exp.alias_(exp.Sum(this=exp.column(exp.to_identifier(c, quoted=True))), f"total of {c}",
                                   quoted=True) for c in totals]
                sql = exp.select(*sums).from_(exp.Table(this=exp.to_identifier(table.name, quoted=True))).sql(dialect)
                got = run_pull(source, src, SAMPLE_QUESTION, sql, grants, key)
                base = replace(base, total_columns=got.columns, total_rows=got.rows[:1], total_note=got.note)
    except (QueryRejected, SourceError) as err:
        return replace(base, problem=str(err))
    return base


def key_getter(keychain: object) -> Callable[[], bytes | None]:
    return lambda: stand_ins.key_or_none(keychain)  # type: ignore[arg-type]


# --- saving ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """What a save came to: saved (the new version), a question to ask first, or a problem to show with the draft."""

    saved: Profile | None = None
    question: str | None = None
    problem: str = ""


def save(tools: ProfileTools | None, draft: Draft, version: str, confirmed: bool, editable: bool) -> Outcome:
    if not editable or tools is None:
        return Outcome(problem=WEB_ONLY)
    store = tools.store
    current = store.profiles.by_id(draft.profile.id)
    if current is None:
        return Outcome(problem="That profile isn't there any more.")
    if str(current.version) != version:
        return Outcome(problem=CHANGED_SINCE.format(name=current.name, version=current.version))
    standing = tools.standing()
    if standing.email is None:
        return Outcome(problem=standing.why or "Profiles need Portward Personal on this computer's account.")
    try:
        changed = draft.edited(standing.email)
        clash = store.profiles.named(changed.name)
        if clash is not None and clash.id != current.id:
            raise ProfileError(f"there is already a profile named {changed.name!r}")
    except ProfileError as err:
        return Outcome(problem=f"Not saved: {err}.")
    if not draft.change_count():
        return Outcome(problem="Nothing to save: no changes since this version.")
    places = store.profiles.where_applied(current, tuple(c.name for c in store.connections()))
    question = draft.looser(places)
    if question and not confirmed:
        return Outcome(question=question)
    return Outcome(saved=store.profiles.save(changed))


def preview_of(draft: Draft, catalog: Sequence[Table] | None) -> Preview | None:
    if catalog is None:
        return None
    try:
        return preview(draft.grants(), catalog)
    except ProfileError:
        return None
