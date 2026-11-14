"""Why is this hidden? (#124, Personal): one lookup that says, for the user only, why a table, column or context item
of a connection on profiles is kept from the AI.

It assembles the reasons Portward already acts on, so it can't disagree with what the AI gets:
- not granted: no profile applied here grants it (or no profile is applied at all);
- fell to hidden: a profile grants it, but this connection can't apply the grant as written (`profiles.hidden_why`:
  totals on a column that isn't a number, anonymised where no stand-ins can be made);
- withheld: a granted view or partition, which Portward can't check (`Grants.withheld_derived`);
- held back: context (a meaning, definition or link) that names, or describes, something hidden here
  (the same test `context_served` applies), or a link whose other column the AI can't see anywhere;
- unsettled: two applied profiles describe one thing differently, and the user hasn't picked (`context_served`);
- column gone: context naming a column its table no longer has, renamed or dropped (`context_drift`, #123), with a
  suggested fix. It comes before the other context reasons: rewording is what releases it;
- held back metric (#129): a metric that needs a column the AI can't see here in full (`metrics.hidden_names`);
- verified answer (#130): one whose SQL or question names something hidden here, or not granted by its own profile
  (`verified.hidden_names`, held back), or whose result changed when last re-run here (`Kind.RESULT_CHANGED`,
  listed with column gone under "Needs you": checking it again is what releases it);
- ended: only an application whose end date has passed granted it (#134, `Applied.ended` on `seen_now`);
- other AI apps only: only an application limited to some web AI apps grants it, so desktop and every other AI app
  don't get it (#134, `Applied.for_app`).
- row rule (#132): rows of a table no applied profile's row rule reaches, and columns the AI gets only on some rows
  (`row_reasons`);
- lowered (#132): one applied profile lowers what another shows of a column on its rows (a value anonymised anywhere
  is anonymised everywhere; a column with totals-only cells can only be totalled).
- anonymised with its link: a column granted in full whose link group has a member anonymised or totals only in some
  applied profile, here or on another connection, so it's anonymised too (link_caps).

Never AI-facing: no MCP tool calls this and no tool result carries its sentences. The local Portward page shows it
(page.py, `/hidden`); the profile editor (#126) embeds the same lookup. A connection on block rules has nothing here:
its rules are listed on the page as they are.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from portward_free import link_caps, verified
from portward_free.context import hides_something as _hides
from portward_free.context import mentions, ungranted
from portward_free.context_drift import Drifted, drifted
from portward_free.context_served import CatalogOf, served
from portward_free.grants import Grants
from portward_free.metrics import hidden_names
from portward_free.profiles import Applied, Profile, hidden_why
from portward_free.row_reasons import row_reasons
from portward_free.sources import Table
from portward_free.storage import LocalStore


class Kind(StrEnum):
    NOT_GRANTED = "not_granted"
    FELL_TO_HIDDEN = "fell_to_hidden"
    WITHHELD = "withheld"
    HELD_BACK = "held_back"
    UNSETTLED = "unsettled"
    COLUMN_GONE = "column_gone"
    ENDED = "ended"
    OTHER_APPS = "other_apps"
    RESULT_CHANGED = "result_changed"
    ROWS = "rows"
    LOWERED = "lowered"
    LINKED = "linked"


LABELS = {Kind.NOT_GRANTED: "Not granted", Kind.FELL_TO_HIDDEN: "Fell to hidden", Kind.WITHHELD: "Withheld",
          Kind.HELD_BACK: "Held back", Kind.UNSETTLED: "Not picked yet", Kind.COLUMN_GONE: "Column gone",
          Kind.ENDED: "Ended", Kind.OTHER_APPS: "Other AI apps only",
          Kind.RESULT_CHANGED: "Result changed", Kind.ROWS: "Row rule", Kind.LOWERED: "Lowered by another profile",
          Kind.LINKED: "Anonymised with its link"}


@dataclass(frozen=True)
class Hidden:
    """One thing kept from the AI and why. `what` is table, column, meaning, definition or link."""

    about: str
    what: str
    kind: Kind
    why: str  # one sentence for the user
    profiles: tuple[str, ...] = ()  # the profiles behind the reason, by name
    names: tuple[str, ...] = ()  # for held-back context: the hidden names it mentions (or the columns gone)

    @property
    def label(self) -> str:
        return LABELS[self.kind]


@dataclass(frozen=True)
class WhyHidden:
    source: str
    on_profiles: bool
    applied: tuple[str, ...] = ()  # the applied profiles' names
    items: tuple[Hidden, ...] = ()

    def about(self, name: str) -> tuple[Hidden, ...]:
        """The reasons `name` is hidden: a table, `table.column` (its table's reason when the whole table is hidden),
        a definition's term, or a context item as listed ("meaning of customers.name"). Empty when it isn't hidden
        (or isn't known here). Case doesn't matter."""
        wanted = " ".join(name.split()).lower()
        exact = tuple(i for i in self.items if i.about.lower() == wanted
                      or (i.what == "definition" and i.about.lower() == f"definition {wanted}"))
        if exact or "." not in wanted:
            return exact
        table = wanted.split(".")[-2]
        return tuple(i for i in self.items if i.what == "table" and i.about.lower() == table)

    @property
    def column_gone(self) -> tuple[Hidden, ...]:
        """The context items held back because a column they name is gone: what the user needs to fix."""
        return tuple(i for i in self.items if i.kind is Kind.COLUMN_GONE)

    @property
    def needs_you(self) -> tuple[Hidden, ...]:
        """What the user needs to fix: context naming a column that's gone, and verified answers whose result
        changed (#130)."""
        return tuple(i for i in self.items if i.kind in (Kind.COLUMN_GONE, Kind.RESULT_CHANGED))


def why_hidden(store: LocalStore, source: str, catalog: Sequence[Table],
               catalog_of: CatalogOf | None = None) -> WhyHidden:
    """Every table, column and context item of `source` (with its `catalog`) kept from the AI, and why. `catalog_of`
    opens other connections, to tell whether a link's far column is visible there."""
    if not store.profiles.on_profiles(source):
        return WhyHidden(source, on_profiles=False)
    applied = store.profiles.applied(source)
    profiles = tuple(a.profile for a in applied)
    anywhere = store.profiles.applied_anywhere()
    grants = Grants.applied(profiles, anywhere)
    visible = grants.visible(catalog)
    now, apps = store.profiles.seen_now(), store.profiles.web_apps()
    resting = tuple(_Resting(a, now, apps) for a in store.profiles.held(source)
                    if a.profile.id not in {p.id for p in profiles})
    linked = link_caps.lowered_here(link_caps.caps(profiles, anywhere), grants, catalog)
    items = (*_data(source, catalog, profiles, grants, resting), *_rows(profiles, catalog),
             *(Hidden(c.name, "column", Kind.LINKED, c.text, (c.profile,)) for c in linked),
             *_context(store, source, catalog, catalog_of, profiles, visible))
    return WhyHidden(source, True, tuple(p.name for p in profiles), tuple(items))


# --- tables and columns ---------------------------------------------------------------------------------------------

def _named(profiles: Sequence[Profile]) -> str:
    return ", ".join(repr(p.name) for p in profiles)


def _not_granted(source: str, profiles: Sequence[Profile], what: str) -> str:
    if not profiles:
        return f"no profile is applied to {source}, so the AI sees nothing there; apply one that grants {what}"
    return f"none of the profiles applied to {source} ({_named(profiles)}) grant {what}; grant it in one of them"


@dataclass(frozen=True)
class _Resting:
    """An application held on the connection that doesn't apply here now: ended, or for other AI apps (#134)."""

    applied: Applied
    now: str
    apps: Sequence[tuple[str, str | None]]

    def grants(self, table: Table, columns: Sequence[str]) -> bool:
        return any(Grants.of(self.applied.profile.grants).level(table, c) for c in columns)

    def reason(self, what: str) -> Hidden | None:
        name = self.applied.profile.name
        if self.applied.ended(self.now):
            return None if not self.applied.until else Hidden(
                what, "", Kind.ENDED, f"{name!r} granted {what}, but it was applied until {_when(self.applied.until)}, "
                                      f"which has passed, so it no longer applies; apply it again to show it", (name,))
        labels = dict(self.apps)
        names = ", ".join(labels.get(c) or c for c in self.applied.apps)
        return Hidden(what, "", Kind.OTHER_APPS,
                      f"{name!r} grants {what} only to {names} on the web, so desktop AI apps and other web AI apps "
                      f"don't get it; apply it for every AI app to show it everywhere", (name,))


def _when(until: str) -> str:
    return datetime.fromisoformat(until).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _resting(resting: Sequence[_Resting], table: Table, columns: Sequence[str], about: str,
             what: str) -> Hidden | None:
    """The reason from an application that would grant it but doesn't apply now: other AI apps first (it still
    applies somewhere), then ended."""
    ordered = sorted((r for r in resting if r.grants(table, columns)), key=lambda r: r.applied.ended(r.now))
    for r in ordered:
        if (found := r.reason(about)) is not None:
            return Hidden(about, what, found.kind, found.why, found.profiles)
    return None


def _data(source: str, catalog: Sequence[Table], profiles: Sequence[Profile], grants: Grants,
          resting: Sequence[_Resting] = ()) -> Iterable[Hidden]:
    for table in catalog:
        granted = [c for c in table.columns if grants.level(table, c)]
        if table.derived and granted:
            by = _granting(profiles, table, granted)
            yield Hidden(table.name, "table", Kind.WITHHELD,
                         f"{_named(by)} grant{'s' if len(by) == 1 else ''} {table.name}, but it's a view or "
                         f"partition and Portward can't check what it reads, so it's withheld; grant the tables it "
                         f"reads instead", tuple(p.name for p in by))
        elif not granted and (rest := _resting(resting, table, table.columns, table.name, "table")):
            yield rest  # and the columns no resting application grants, said one by one
            for column in table.columns:
                if not any(r.grants(table, [column]) for r in resting):
                    name = f"{table.name}.{column}"
                    yield Hidden(name, "column", Kind.NOT_GRANTED, _not_granted(source, profiles, name))
        elif not granted:
            yield Hidden(table.name, "table", Kind.NOT_GRANTED,
                         _not_granted(source, profiles, f"any column of {table.name}"))
        else:
            yield from _columns(source, table, profiles, grants, resting)


def _columns(source: str, table: Table, profiles: Sequence[Profile], grants: Grants,
             resting: Sequence[_Resting]) -> Iterable[Hidden]:
    for column in table.columns:
        name = f"{table.name}.{column}"
        level = grants.level(table, column)
        if level is None:
            yield _resting(resting, table, [column], name, "column") or Hidden(
                name, "column", Kind.NOT_GRANTED, _not_granted(source, profiles, name))
        elif why := hidden_why(level, table, column):
            by = _granting(profiles, table, [column])
            yield Hidden(name, "column", Kind.FELL_TO_HIDDEN,
                         f"{_named(by)} grant{'s' if len(by) == 1 else ''} {name} as {level}, but on {source} "
                         f"{why}; it's hidden instead, never shown in full", tuple(p.name for p in by))


def _granting(profiles: Sequence[Profile], table: Table, columns: Sequence[str]) -> tuple[Profile, ...]:
    return tuple(p for p in profiles if any(Grants.of(p.grants).level(table, c) for c in columns))


def _rows(profiles: Sequence[Profile], catalog: Sequence[Table]) -> Iterable[Hidden]:
    found = row_reasons(profiles, catalog)
    for rows in found.rows:
        yield Hidden(rows.table, "rows", Kind.ROWS, rows.text, rows.profiles)
    for some in found.some_rows:
        yield Hidden(f"{some.table}.{some.column}", "column", Kind.ROWS, some.text, tuple(n for n, _, _ in some.reached))
    for lowered in found.lowered:
        yield Hidden(lowered.name, "column", Kind.LOWERED, lowered.text.removesuffix("."), (lowered.by, lowered.of))


# --- context --------------------------------------------------------------------------------------------------------

def _hidden_names(text: str, profile: Profile, visible: Sequence[Table], catalog: Sequence[Table]) -> tuple[str, ...]:
    """The names in `text` that make context_served hold it back: hidden here, or not granted by its profile."""
    found = [n for n in mentions(text) if _hides(n, visible, catalog)]
    found += ungranted(text, profile.grants, [catalog])
    return tuple(dict.fromkeys(found))


def _is_visible(visible: Sequence[Table], table: str, column: str | None, schema: str | None) -> bool:
    for t in visible:
        if t.name.lower() == table.lower() and (not schema or (t.schema or "").lower() == schema.lower()):
            return column is None or column.lower() in {c.lower() for c in t.columns}
    return False


def _held_back(about: str, what: str, profile: Profile, names: tuple[str, ...]) -> Hidden:
    return Hidden(about, what, Kind.HELD_BACK,
                  f"{about} (in {profile.name!r}) names {', '.join(names)}, which the AI can't see here, so it "
                  f"isn't told; reword it or grant what it names", (profile.name,), names)


def _column_gone(item: Drifted) -> Hidden:
    return Hidden(item.about, item.what, Kind.COLUMN_GONE, item.why, (item.profile,), item.names)


def _context(store: LocalStore, source: str, catalog: Sequence[Table], catalog_of: CatalogOf | None,
             profiles: Sequence[Profile], visible: Sequence[Table]) -> Iterable[Hidden]:
    gone = {(item.profile, item.about.lower()): item for item in drifted(profiles, catalog, source)}
    yield from map(_column_gone, gone.values())
    yield from _verified(store, source, catalog, profiles, visible, gone)
    for profile in profiles:
        for meaning in profile.context.meanings:
            subject = f"{meaning.table}.{meaning.column}" if meaning.column else meaning.table
            about = f"meaning of {subject}"
            if (profile.name, about.lower()) in gone:
                continue
            if names := _hidden_names(meaning.words, profile, visible, catalog):
                yield _held_back(about, "meaning", profile, names)
            elif not _is_visible(visible, meaning.table, meaning.column, meaning.schema):
                yield Hidden(about, "meaning", Kind.HELD_BACK,
                             f"{about} (in {profile.name!r}) describes {subject}, which is hidden here, so the AI "
                             f"isn't told it", (profile.name,), (subject,))
        for definition in profile.context.definitions:
            if (profile.name, f"definition {definition.term}".lower()) in gone:
                continue
            if names := _hidden_names(definition.text, profile, visible, catalog):
                yield _held_back(f"definition {definition.term}", "definition", profile, names)
        for metric in profile.context.metrics:  # #129: no sealed metrics
            if (profile.name, f"metric {metric.name}".lower()) in gone:
                continue
            if names := hidden_names(metric, profile.grants, store.rules_for(source), visible, catalog):
                yield Hidden(f"metric {metric.name}", "metric", Kind.HELD_BACK,
                             f"metric {metric.name} (in {profile.name!r}) needs {', '.join(names)}, which the AI "
                             f"can't see here in full, so it isn't told the metric exists; grant what it needs in "
                             f"full or change the metric", (profile.name,), names)
    found = served(store, source, catalog, catalog_of)
    yield from _links(profiles, visible, catalog, found.links, set(gone))
    for clash in found.clashes:
        names = tuple(dict.fromkeys(name for _, name, _ in clash.candidates))
        what = clash.subject.split(":", 1)[0]
        what = "verified answer" if what == "verified" else what
        about = f"meaning of {clash.about}" if what == "meaning" else f"{what} {clash.about}"
        yield Hidden(about, what, Kind.UNSETTLED,
                     f"{' and '.join(repr(n) for n in names)} describe {clash.about} differently, so the AI gets "
                     f"neither until you pick one", names)


def _verified(store: LocalStore, source: str, catalog: Sequence[Table], profiles: Sequence[Profile],
              visible: Sequence[Table], gone: dict) -> Iterable[Hidden]:
    """Verified answers of this connection held back for naming something hidden, or because their result
    changed (#130). (Column gone comes first, from `drifted`.)"""
    changed = {(p.id, a.key): found for p, a, found in verified.changed_on(store, source)}
    for profile in profiles:
        for answer in profile.context.verified:
            about = f"verified answer {answer.question}"
            if not verified.on(answer, source) or (profile.name, about.lower()) in gone:
                continue
            if names := verified.hidden_names(answer, profile.grants, visible, catalog):
                yield Hidden(about, "verified answer", Kind.HELD_BACK,
                             f"{about} (in {profile.name!r}) reads {', '.join(names)}, which the AI can't see here, "
                             f"so it isn't told the answer exists; grant what it reads or verify another query",
                             (profile.name,), names)
            elif (found := changed.get((profile.id, answer.key))) is not None:
                said = "no longer runs" if found.seen == "error" else "no longer gives the result you checked"
                yield Hidden(about, "verified answer", Kind.RESULT_CHANGED,
                             f"{about} (in {profile.name!r}, v{answer.version}) {said} on "
                             f"{answer.checked_at[:10]} (re-run {found.at[:16].replace('T', ' ')}), so the AI isn't "
                             f"offered it; check the result and promote the saved pull again to verify it afresh",
                             (profile.name,))


def _links(profiles: Sequence[Profile], visible: Sequence[Table], catalog: Sequence[Table],
           shown: Sequence[dict], gone: set[tuple[str, str]]) -> Iterable[Hidden]:
    served_columns = {c.lower() for link in shown for c in link["columns"]}
    for profile in profiles:
        for link in profile.context.links:
            if (profile.name, f"link {link.one.name} = {link.other.name}".lower()) in gone:
                continue
            ends = [e for e in (link.one, link.other) if _is_visible(visible, e.table, e.column, e.schema)]
            if not ends:
                continue  # neither column is seen here: the link isn't about this connection
            about = f"link {link.one.name} = {link.other.name}"
            if names := _hidden_names(link.text or "", profile, visible, catalog):
                yield _held_back(about, "link", profile, names)
            elif not any(f"{e.table}.{e.column}".lower() in served_columns for e in ends):
                yield Hidden(about, "link", Kind.HELD_BACK,
                             f"{about} (in {profile.name!r}) isn't told: the AI can't see the other column, here or "
                             f"on another connection {profile.name!r} is applied to", (profile.name,))
