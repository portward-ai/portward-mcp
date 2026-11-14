"""The context the AI gets for one connection (#95): the approved meanings and named definitions of the profiles
applied to it, as the schema's notes and a list of definitions, and the ones a pull's result relies on.

Only approved context is ever read here (drafts live apart, storage `context_drafts`), and only what the profiles
let the AI see on this connection: a meaning of a table or column that isn't visible here, or that its own profile no
longer grants, is left out, and so is any text naming a table or column that is hidden here or that its profile
doesn't grant, or a column its table no longer has (context_drift, #123). Where two applied profiles describe one
column or term differently the AI gets neither until the user picks (settle_context), and the pick holds until
either text changes.

Links (#96) are served on a connection where one of their columns is visible, and only when the other column is
visible too: here (join in SQL) or on another connection the same profile is applied to (pull both and join there).
A link whose other column the AI can't see anywhere isn't mentioned at all.

Metrics (#129) are served by name and one-liner where everything they name is visible (metrics.hidden_names): a
metric that needs a hidden column is held back, and the AI isn't told it exists. Two applied profiles defining one
metric name differently clash like definitions.

Verified answers (#130) are served by question on the connection they were verified on, unless they name something
hidden here, a column that's gone, or their result changed when last re-run here (verified.py): held back, the AI
isn't told they exist. Two applied profiles verifying one question differently clash like definitions.

A connection on block rules keeps Free's notes, unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

from portward_free import verified as verified_check
from portward_free.context import (MANY_TO_MANY, ONE_TO_MANY, ONE_TO_ONE, Clash, End, Link, Meaning, flipped,
                                   mentions, ungranted)
from portward_free.context import hides_something as _hides
from portward_free.context_drift import answer_gone, gone_in
from portward_free.metric_def import Metric
from portward_free.grants import Grants
from portward_free.metrics import hidden_names
from portward_free.profiles import ANONYMISED, FULL, Profile
from portward_free.schema import TABLE_NOTE, Notes
from portward_free.sources import SourceError, Table
from portward_free.storage import LocalStore
from portward_free.verified_def import VerifiedAnswer

if TYPE_CHECKING:
    from portward_free.runtime import Runtime

Candidate = tuple[str, str, str]  # (profile id, profile name, text)
CatalogOf = Callable[[str], Sequence[Table] | None]  # another connection's catalog by name; None if it won't open


@dataclass(frozen=True)
class ServedAnswer:
    """A verified answer the AI may run here (#130), and the profile it's in."""

    answer: VerifiedAnswer
    profile: str  # the profile's name
    profile_id: str


class ServedMetric(NamedTuple):
    """A metric the AI may use on a connection, with its profile's name (#129)."""
    metric: Metric
    profile: str


@dataclass(frozen=True)
class Served:
    notes: Notes = field(default_factory=dict)  # (table, column or TABLE_NOTE) -> text, as schema.for_ai takes
    definitions: tuple[dict[str, Any], ...] = ()  # {term, text, profile, version}
    clashes: tuple[Clash, ...] = ()
    links: tuple[dict[str, Any], ...] = ()  # {columns: [here, other], join, text, source? (the other connection)}
    # each note's profile and that profile's version (#125); none on block rules, where notes are Free's own
    origins: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    metrics: tuple[ServedMetric, ...] = ()  # the metrics the AI may use here (#129)
    verified: tuple[ServedAnswer, ...] = ()  # #130
    # (table, column or TABLE_NOTE) -> {synonyms?, values?, sample_values?}, as schema.for_ai takes (#131)
    details: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def answer(self, question: str) -> ServedAnswer | None:
        wanted = " ".join(str(question or "").split()).lower()
        return next((v for v in self.verified if v.answer.key == wanted), None)

    def metric(self, name: str) -> ServedMetric | None:
        wanted = " ".join(str(name or "").split()).lower()
        return next((m for m in self.metrics if m.metric.key == wanted), None)

    def links_for(self, tables: Sequence[str]) -> list[dict[str, Any]]:
        """The links touching any of `tables` (read by a pull)."""
        read = {t.lower() for t in tables}
        return [k for k in self.links if any(c.split(".")[-2].lower() in read for c in k["columns"])]

    def notices(self, source: str) -> tuple[str, ...]:
        said = []
        for clash in self.clashes:
            names = " and ".join(repr(name) for _, name, _ in clash.candidates)
            said.append(f"Profiles {names} describe {clash.about} differently, so neither is used yet: call "
                        f"settle_context with source {source!r} and about {clash.about!r} so the user picks one.")
        return tuple(said)

    def relied_on(self, tables: Sequence[str], columns: Sequence[str], result_columns: Sequence[str],
                  question: str) -> list[dict[str, Any]]:
        """The context a pull over `tables` relies on: the meanings of those tables and of the columns it read (all
        of theirs when it named none, as with SELECT *), and the definitions naming one of them or whose term is in
        the question. Each is {kind, about, text, profile, version}: the profile it came from and the version of that
        profile, which pins its text (#125)."""
        read = {t.lower() for t in tables}
        named = {c.lower() for c in (*columns, *result_columns)}
        found = []
        for (table, column), text in self.notes.items():
            if table.lower() not in read or (table, column) not in self.origins:
                continue
            if column == TABLE_NOTE or not named or column.lower() in named:
                profile, version = self.origins[(table, column)]
                found.append({"kind": "meaning", "about": table if column == TABLE_NOTE else f"{table}.{column}",
                              "text": text, "profile": profile, "version": version})
        asked = question.lower()
        for definition in self.definitions:
            words = {p.lower() for name in mentions(definition["text"]) for p in name.split(".")}
            if words & (read | named) or definition["term"].lower() in asked:
                found.append({"kind": "definition", "about": definition["term"], "text": definition["text"],
                              "profile": definition["profile"], "version": definition["version"]})
        return found


def served(store: LocalStore, source: str, catalog: Sequence[Table], catalog_of: CatalogOf | None = None) -> Served:
    """`catalog_of` opens other connections, to find the far end of a link there; without it only links within
    this connection are served."""
    if not store.profiles.on_profiles(source):
        return Served(notes=store.notes(source))
    visible = store.rules_for(source).visible(catalog)
    profiles = [a.profile for a in store.profiles.applied(source)]
    meanings: dict[tuple[str, str], list[Candidate]] = {}
    said: dict[tuple[str, tuple[str, str]], Meaning] = {}  # (profile id, key) -> its meaning there
    definitions: dict[str, tuple[str, list[Candidate]]] = {}
    for profile in profiles:
        for key, meaning in _meanings_here(profile, visible, catalog):
            meanings.setdefault(key, []).append((profile.id, profile.name, meaning.described))
            said[(profile.id, key)] = meaning
        for definition in profile.context.definitions:
            if not _holds_back(definition.text, profile, visible, catalog):
                term, found = definitions.setdefault(definition.key, (definition.term, []))
                found.append((profile.id, profile.name, definition.text))
    version_of = {p.id: p.version for p in profiles}
    notes: dict[tuple[str, str], str] = {}
    origins: dict[tuple[str, str], tuple[str, int]] = {}
    details: dict[tuple[str, str], dict[str, Any]] = {}
    clashes: list[Clash] = []
    rules = store.rules_for(source)
    for (table, column), candidates in meanings.items():
        about = table if column == TABLE_NOTE else f"{table}.{column}"
        chosen = _settled(store, Clash(f"meaning:{about.lower()}", about, tuple(candidates)), clashes)
        if chosen is not None:
            meaning = said[(chosen[0], (table, column))]
            notes[(table, column)] = meaning.text
            origins[(table, column)] = (chosen[1], version_of[chosen[0]])
            if extra := _details(meaning, table, column, rules, visible):
                details[(table, column)] = extra
    shown = []
    for key, (term, candidates) in definitions.items():
        chosen = _settled(store, Clash(f"definition:{key}", term, tuple(candidates)), clashes)
        if chosen is not None:
            shown.append({"term": term, "text": chosen[2], "profile": chosen[1], "version": version_of[chosen[0]]})
    links = _links_here(store, source, profiles, visible, catalog, catalog_of)
    metrics = _metrics_here(store, profiles, rules, visible, catalog, clashes)
    answers = _answers_here(store, source, profiles, visible, catalog, clashes)
    return Served(notes, tuple(shown), tuple(clashes), links, origins, metrics, answers, details)


def _details(meaning: Meaning, table: str, column: str, rules: Any, visible: Sequence[Table]) -> dict[str, Any]:
    """A served meaning's synonyms, and, for a column the AI gets in full here, its value meanings and samples
    (#131): they're real values, so never for a column it gets as stand-ins or totals."""
    found: dict[str, Any] = {"synonyms": list(meaning.synonyms)} if meaning.synonyms else {}
    shown = next((t for t in visible if t.name == table), None)
    if column != TABLE_NOTE and shown is not None and _every_row_in_full(rules, shown, column):
        found |= ({"values": dict(meaning.values)} if meaning.values else {}) | (
            {"sample_values": list(meaning.samples)} if meaning.samples else {})
    return found


def _every_row_in_full(rules: Any, table: Table, column: str) -> bool:
    """Whether the AI gets `column` in full on every row of `table`: never when a row rule (#132) keeps some rows
    or cells from it, since an approved sample or value meaning may come from those rows."""
    if rules.shape(table, column) != FULL:
        return False
    cells = rules.cells(table) if isinstance(rules, Grants) else None
    return cells is None


def _answers_here(store: LocalStore, source: str, profiles: Sequence[Profile], visible: Sequence[Table],
                  catalog: Sequence[Table], clashes: list[Clash]) -> tuple[ServedAnswer, ...]:
    found: dict[str, list[ServedAnswer]] = {}
    for profile in profiles:
        for answer in profile.context.verified:
            if (verified_check.on(answer, source)
                    and not verified_check.hidden_names(answer, profile.grants, visible, catalog)
                    and not answer_gone(answer, profile.grants, catalog)
                    and not verified_check.changed(store, source, profile.id, answer)):
                found.setdefault(answer.key, []).append(ServedAnswer(answer, profile.name, profile.id))
    usable = []
    for key, candidates in found.items():
        texts = tuple((c.profile_id, c.profile, c.answer.summary) for c in candidates)
        chosen = _settled(store, Clash(f"verified:{key}", candidates[0].answer.question, texts), clashes)
        if chosen is not None:
            usable.append(next(c for c in candidates if c.profile_id == chosen[0]))
    return tuple(usable)


def _metrics_here(store: LocalStore, profiles: Sequence[Profile], rules: Any, visible: Sequence[Table],
                  catalog: Sequence[Table], clashes: list[Clash]) -> tuple[ServedMetric, ...]:
    found: dict[str, list[tuple[Metric, Profile]]] = {}
    for profile in profiles:
        for metric in profile.context.metrics:
            if not hidden_names(metric, profile.grants, rules, visible, catalog):
                found.setdefault(metric.key, []).append((metric, profile))
    usable = []
    for key, candidates in found.items():
        texts = tuple((p.id, p.name, m.summary) for m, p in candidates)
        chosen = _settled(store, Clash(f"metric:{key}", candidates[0][0].name, texts), clashes)
        if chosen is not None:
            metric, profile = next((m, p) for m, p in candidates if p.id == chosen[0])
            usable.append(ServedMetric(metric, profile.name))
    return tuple(usable)


def _column(end: End, visible: Sequence[Table]) -> tuple[str, str] | None:
    """The real (table, column) names of `end` among the `visible` tables, if the AI can see it there."""
    table = next((t for t in visible if t.name.lower() == end.table.lower()
                  and (not end.schema or (t.schema or "").lower() == end.schema.lower())), None)
    column = next((c for c in table.columns if c.lower() == end.column.lower()), None) if table else None
    return (table.name, column) if table and column else None


def _links_here(store: LocalStore, source: str, profiles: Sequence[Profile], visible: Sequence[Table],
                catalog: Sequence[Table], catalog_of: CatalogOf | None) -> tuple[dict[str, Any], ...]:
    found: dict[tuple, dict[str, Any]] = {}
    for profile in profiles:
        for link in profile.context.links:
            if link.key in found or _holds_back(link.text, profile, visible, catalog):
                continue
            if served_link := _link_here(store, source, profile, link, visible, catalog_of):
                found[link.key] = served_link
    return tuple(found.values())


def _link_here(store: LocalStore, source: str, profile: Profile, link: Link, visible: Sequence[Table],
               catalog_of: CatalogOf | None) -> dict[str, Any] | None:
    one, other = _column(link.one, visible), _column(link.other, visible)
    if one and other:
        (a_table, a_col), (b_table, b_col) = one, other
        return _with_path({"columns": [f"{a_table}.{a_col}", f"{b_table}.{b_col}"], "text": link.text,
                           "join": f"Join these tables on exactly these columns: JOIN {b_table} ON "
                                   f"{a_table}.{a_col} = {b_table}.{b_col}"},
                          link, False, store.rules_for(source), visible)
    here, far = (one, link.other) if one else (other, link.one)
    swapped = one is None
    if here is None or catalog_of is None:
        return None
    for name in store.profiles.where_applied(profile, tuple(c.name for c in store.connections())):
        if name.lower() == source.lower():
            continue
        their_catalog = catalog_of(name)
        if their_catalog is None:
            continue
        there = _column(far, store.rules_for(name).visible(their_catalog))
        if there is None:
            continue
        mine, theirs = f"{here[0]}.{here[1]}", f"{there[0]}.{there[1]}"
        return _with_path({"columns": [mine, theirs], "source": name, "text": link.text,
                           "join": f"{theirs} is in source {name!r}: pull from both sources and join their rows on "
                                   f"exactly {mine} = {theirs}"},
                          link, swapped, store.rules_for(source), visible)
    return None


def _with_path(served_link: dict[str, Any], link: Link, swapped: bool, rules: Any,
               visible: Sequence[Table]) -> dict[str, Any]:
    """A served link with its join path (#131): one-to-one or one-to-many read from the first column served to the
    second, the match counts measured on save, and advice to sum the many side before joining. Counts are left out
    when a column here is now one the AI only gets totals of (counts are only ever measured on columns the AI gets
    in full or as stand-ins)."""
    cardinality = flipped(link.cardinality) if swapped else link.cardinality
    if cardinality is None:
        return served_link
    first, second = served_link["columns"]
    found = dict(served_link, cardinality=cardinality)
    if link.counts and _countable(served_link["columns"], rules, visible):
        near, far = (link.counts.other, link.counts.one) if swapped else (link.counts.one, link.counts.other)
        found["counts"] = {first: near.payload(), second: far.payload()} | (
            {"matching_values": link.counts.matching} if link.counts.matching is not None else {})
    if advice := _advice(cardinality, first, second):
        found["advice"] = advice
        found["join"] = f"{served_link['join']}. {advice}"
    return found


def _countable(columns: Sequence[str], rules: Any, visible: Sequence[Table]) -> bool:
    """Whether no column of the link here is now one the AI only gets totals of (a far column isn't in `visible`)."""
    for name in columns:
        table_name, column = name.rsplit(".", 1)
        table = next((t for t in visible if t.name == table_name), None)
        if table is not None and rules.shape(table, column) not in (FULL, ANONYMISED):
            return False
    return True


def _advice(cardinality: str, first: str, second: str) -> str:
    """What to do before joining so sums don't double-count; nothing for one-to-one."""
    if cardinality == ONE_TO_ONE:
        return ""
    if cardinality == MANY_TO_MANY:
        return (f"Values repeat on both sides, so joining multiplies rows: sum or count each table per {first} and "
                f"{second} before joining")
    one, many = (first, second) if cardinality == ONE_TO_MANY else (second, first)
    many_table, many_column = many.rsplit(".", 1)
    one_table = one.rsplit(".", 1)[0]
    return (f"Each {one} matches many rows of {many_table}: sum or count {many_table} per {many_column} before "
            f"joining, or anything summed from {one_table} counts once per matching {many_table} row")


def _meanings_here(profile: Profile, visible: Sequence[Table],
                   catalog: Sequence[Table]) -> Iterable[tuple[tuple[str, str], Meaning]]:
    """Each meaning of `profile` that describes a table or column visible here, keyed by its real names."""
    for meaning in profile.context.meanings:
        if _holds_back(meaning.words, profile, visible, catalog):  # (a save keeps only meanings of what's granted)
            continue
        table = next((t for t in visible if t.name.lower() == meaning.table.lower()
                      and (not meaning.schema or (t.schema or "").lower() == meaning.schema.lower())), None)
        if table is None:
            continue
        if meaning.column is None:
            yield (table.name, TABLE_NOTE), meaning
            continue
        column = next((c for c in table.columns if c.lower() == meaning.column.lower()), None)
        if column is not None:
            yield (table.name, column), meaning


def _holds_back(text: str, profile: Profile, visible: Sequence[Table], catalog: Sequence[Table]) -> bool:
    """Whether `text` names something hidden here, something its profile doesn't grant, or a column that's gone
    (context_drift, #123)."""
    return (_hides(text, visible, catalog) or bool(ungranted(text, profile.grants, [catalog]))
            or bool(gone_in(text, profile.grants, catalog)))


def _settled(store: LocalStore, clash: Clash, clashes: list[Clash]) -> Candidate | None:
    """The one description to use: the only text there is, or the user's pick between these exact texts. None (and
    the clash noted) while they haven't picked."""
    if len({text for _, _, text in clash.candidates}) == 1:
        return clash.candidates[0]
    picked = store.profiles.pick(clash.subject, clash.fingerprint)
    chosen = next((c for c in clash.candidates if c[0] == picked), None)
    if chosen is None:
        clashes.append(clash)
    return chosen


def clash_named(found: Served, about: str) -> Clash | None:
    wanted = " ".join(about.split()).strip("“”\"'").lower()
    return next((c for c in found.clashes if c.about.lower() == wanted), None)



def catalog_of(rt: Runtime) -> CatalogOf:
    """Other connections' catalogs, opened when a link needs one (None for one that won't open)."""
    def opened(name: str) -> Sequence[Table] | None:
        connection = rt.store.connection(name)
        if connection is None:
            return None
        try:
            with rt.opened(connection) as source:
                return source.tables()
        except SourceError:
            return None
    return opened
