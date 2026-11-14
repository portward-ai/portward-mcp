"""The context editor's routes on the local Portward page (#127): GET /context?id=<profile>&tab= opens it, and every
button posts the one form back to POST /context. page.PortwardPage hands it those requests. See context_editor for
the sitting and what a save does.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from portward_free import context_editor as ce
from portward_free import context_editor_html as html
from portward_free import context_told_html as told_html
from portward_free import profile_editor as pe
from portward_free.account import Accounts
from portward_free.context import ContextError, Draft, Meaning
from portward_free.context_served import served
from portward_free.dictionary import DictionaryError, matched, read_entries
from portward_free.keychain import Keychain
from portward_free.link_counts import Measurer
from portward_free.localweb import Response
from portward_free.page_html import Chrome
from portward_free.profile_tools import ProfileTools
from portward_free.profiles import Profile
from portward_free.rule_tools import Opener
from portward_free.schema import TABLE_NOTE
from portward_free.sources import SourceError, Table
from portward_free.storage import Connection, LocalStore
from portward_free.why_hidden import why_hidden

NOT_FOUND = Response.text(404, "Not found.")
ASSUMPTIONS_SHOWN = 50
SAMPLE_VALUES = 5
Catalog = Callable[[Connection], "tuple[Table, ...] | str"]


@dataclass(frozen=True)
class Form:
    """The fields one post carries, read once."""

    fields: Mapping[str, str]

    def get(self, name: str) -> str:
        return self.fields.get(name, "")


class ContextEditorPage:
    def __init__(self, store: LocalStore, keychain: Keychain, opened: Opener, accounts: Accounts | None,
                 editable: bool, catalog: Catalog, catalog_of: Callable[[str], tuple[Table, ...] | None]) -> None:
        self._store, self._keychain, self._opened = store, keychain, opened
        self._tools = ProfileTools(store, keychain, accounts, opened) if accounts is not None else None
        self._editable = editable and accounts is not None
        self._catalog, self._catalog_of = catalog, catalog_of

    def handles(self, path: str) -> bool:
        return path == html.PATH

    def get(self, chrome: Chrome, query: Mapping[str, str]) -> Response:
        profiles = self._store.profiles.all()
        profile = self._store.profiles.by_id(query.get("id", "")) if query.get("id") else (
            profiles[0] if profiles else None)
        if profile is None:
            return Response(body=html.no_profile(chrome)) if not profiles else NOT_FOUND
        view = self._view(profile, ce.Sitting(), Form({}), tab=query.get("tab", ""), said=query.get("said", ""))
        return Response(body=html.editor(chrome, view))

    def post(self, chrome: Chrome, fields: Mapping[str, str]) -> Response:
        form = Form(fields)
        profile = self._store.profiles.by_id(form.get("id"))
        if profile is None:
            return Response(body=html.no_profile(chrome, "That profile isn't there any more."))
        if str(profile.version) != form.get("version"):
            held = self._store.profiles.by_id(profile.id) or profile
            return Response(body=html.editor(chrome, self._view(held, ce.Sitting(), Form({}), problem=(
                f"{held.name} changed since you opened it (it's v{held.version} now, from another app or computer), "
                "so the changes made on this page were not saved. Make them again on this version."))))
        try:
            sitting = ce.Sitting.loads(form.get("sitting"))
        except ContextError as err:
            return Response(body=html.editor(chrome, self._view(profile, ce.Sitting(), Form({}),
                                                                problem=f"Nothing saved: {err}.")))
        if form.get("action") == "save":
            return self._save(chrome, profile, sitting, form)
        return Response(body=html.editor(chrome, self._act(profile, sitting, form)))

    # --- actions within the sitting -----------------------------------------------------------------------------
    def _decide(self, profile: Profile, sitting: ce.Sitting, form: Form, action: str, drafts: Sequence[Draft],
                catalogs: list[tuple[Table, ...]]) -> html.ContextView:
        """Approve or discard the shown draft within the sitting, then show the next one."""
        draft = next((d for d in drafts if d.id == form.get("draft")), None)
        if draft is None:
            raise ContextError("that draft isn't there any more (approved or discarded elsewhere)")
        if action == ce.APPROVE:
            final = ce.approvable(draft, form.fields.get("text"), profile, catalogs)
            sitting = sitting.decided(draft.id, action, final.text if final.text != draft.text else None)
        else:
            sitting = sitting.decided(draft.id, action)
        after = ce.next_draft(drafts, sitting, draft.id)
        return self._view(profile, sitting, form, tab=html.QUEUE, draft=after.id if after else "",
                          said=_decided(draft, action))

    def _act(self, profile: Profile, sitting: ce.Sitting, form: Form) -> html.ContextView:
        action, tab = form.get("action"), form.get("to_tab") or form.get("tab")
        catalogs = self._catalogs(profile)
        drafts = self._store.profiles.drafts(profile.id)
        shown = form.get("draft")
        try:
            if form.get("show"):
                return self._view(profile, sitting, form, tab=html.QUEUE, draft=form.get("show"))
            if action in ce.DECISIONS:
                return self._decide(profile, sitting, form, action, drafts, catalogs)
            if action == "define":
                definition = ce.checked_definition(form.get("def_term"), form.get("def_text"), profile, catalogs)
                sitting = sitting.with_definition(definition.key, (definition.term, definition.text),
                                                  form.get("def_from"))
                return self._view(profile, sitting, Form({}), tab=html.DEFINITIONS,
                                  said=f"{definition.term} joins this sitting. Save to publish it.")
            if form.get("edit_def"):
                return self._edit_definition(profile, sitting, form.get("edit_def"))
            if form.get("remove_def"):
                return self._view(profile, sitting.with_definition(form.get("remove_def"), None), Form({}),
                                  tab=html.DEFINITIONS)
            if form.get("keep_def"):
                return self._view(profile, sitting.without_definition(form.get("keep_def")), Form({}),
                                  tab=html.DEFINITIONS)
            if form.get("assume"):
                return self._from_assumption(profile, sitting, form.get("assume"))
            if action == "add_link":
                link = ce.checked_link(form.get("link_one"), form.get("link_other"), form.get("link_text"), profile,
                                       catalogs)
                return self._view(profile, sitting.with_link(ce.link_id(link), link.stored()), Form({}),
                                  tab=html.LINKS, said=f"The link {link.about} joins this sitting.")
            if form.get("remove_link"):
                return self._view(profile, sitting.with_link(form.get("remove_link"), None), Form({}), tab=html.LINKS)
            if form.get("keep_link"):
                return self._view(profile, sitting.without_link(form.get("keep_link")), Form({}), tab=html.LINKS)
            if action in ("read_file", "import"):
                return self._import(profile, sitting, form, catalogs, take=action == "import")
        except (ContextError, DictionaryError) as err:
            return self._view(profile, sitting, form, tab=tab, draft=shown, problem=str(err))
        return self._view(profile, sitting, form, tab=tab, draft=shown if tab == html.QUEUE else "")

    def _edit_definition(self, profile: Profile, sitting: ce.Sitting, key: str) -> html.ContextView:
        context = sitting.context(profile, self._store.profiles.drafts(profile.id))
        found = next((d for d in context.definitions if d.key == key), None)
        if found is None:
            return self._view(profile, sitting, Form({}), tab=html.DEFINITIONS, problem="That definition is gone.")
        return self._view(profile, sitting, Form({"def_term": found.term, "def_text": found.text}),
                          tab=html.DEFINITIONS)

    def _from_assumption(self, profile: Profile, sitting: ce.Sitting, assumption_id: str) -> html.ContextView:
        found = self._store.assumptions.get(assumption_id)
        if found is None:
            return self._view(profile, sitting, Form({}), tab=html.ASSUMED, problem="That assumption is gone.")
        return self._view(profile, sitting, Form({"def_text": found.text, "def_from": found.id}),
                          tab=html.DEFINITIONS, said="Name the term this assumption defines, check the text, then add it.")

    def _import(self, profile: Profile, sitting: ce.Sitting, form: Form, catalogs: Sequence[Sequence[Table]],
                take: bool) -> html.ContextView:
        if not self._editable:
            raise ContextError(pe.WEB_ONLY.replace("Profiles", "Files"))
        path = Path(form.get("dict_path").strip()).expanduser()
        entries = read_entries(path)
        first = matched(entries, profile.grants)
        mapping = {e.name: form.get(f"map:u{i}") for i, e in enumerate(first.unmatched) if form.get(f"map:u{i}")}
        found = matched(entries, profile.grants, mapping)
        rows = tuple(html.ImportRow(f"m{i}", entry.name, meaning.about, meaning.text,
                                    ce.problem_with(meaning.text, profile, catalogs))
                     for i, (entry, meaning) in enumerate(found.mapped))
        missed = tuple(html.ImportRow(f"u{i}", e.name, "", e.text, "", mapping.get(e.name, ""))
                       for i, e in enumerate(first.unmatched) if e.name not in {m.name for m, _ in found.mapped})
        if take:
            kept = [m for (_, m), row in zip(found.mapped, rows) if not row.problem and form.get(f"keep:{row.key}")]
            if not kept:
                raise ContextError("tick at least one row to take from the file")
            return self._view(profile, sitting.with_meanings(kept), Form({}), tab=html.IMPORT,
                              said=f"{len(kept)} meaning{'s' * (len(kept) != 1)} from {path.name} join this sitting. "
                                   "Save to publish them.")
        reading = html.Reading(str(path), path.name, rows, missed, _granted_columns(profile))
        return replace(self._view(profile, sitting, form, tab=html.IMPORT), reading=reading)

    # --- save ---------------------------------------------------------------------------------------------------
    def _save(self, chrome: Chrome, profile: Profile, sitting: ce.Sitting, form: Form) -> Response:
        def again(problem: str) -> Response:
            return Response(body=html.editor(chrome, self._view(profile, sitting, form, tab=form.get("tab"),
                                                                draft=form.get("draft"), problem=problem)))
        if not self._editable or self._tools is None:
            return again(pe.WEB_ONLY.replace("Profiles", "Context"))
        standing = self._tools.standing()
        if standing.email is None:
            return again(standing.why or "Context needs Portward Personal on this computer's account.")
        drafts = self._store.profiles.drafts(profile.id)
        approved, discarded = sitting.approved(drafts), sitting.discarded(drafts)
        catalogs = self._catalogs(profile)
        try:
            for draft in approved:
                ce.approvable(draft, None, profile, catalogs)
            changed = sitting.edited(profile, drafts, standing.email) if sitting.change_count(drafts) else None
        except ContextError as err:
            return again(f"Not saved: {err}")
        if changed is None and not discarded:
            return again("Nothing to save: no changes since this version.")
        measurer = Measurer(self._store, self._opened, pe.key_getter(self._keychain))  # links' counts (#131)
        saved = self._store.profiles.save(measurer.measured(changed, profile)) if changed is not None else profile
        for draft in (*approved, *discarded):
            self._store.profiles.drop_draft(draft.id)
        for assumption, term in sitting.assumptions.items():
            self._store.assumptions.defined(assumption, term)
        said = (f"Saved as v{saved.version}." if changed is not None else
                f"Discarded {len(discarded)} draft{'s' * (len(discarded) != 1)}; the profile stays v{saved.version}.")
        query = {"id": saved.id, "said": said}
        return Response.redirect(f"{chrome.web.link(html.PATH)}?{urllib.parse.urlencode(query)}")

    # --- the view -----------------------------------------------------------------------------------------------
    def _view(self, profile: Profile, sitting: ce.Sitting, form: Form, *, tab: str = "", draft: str = "",
              problem: str = "", said: str = "") -> html.ContextView:
        drafts = self._store.profiles.drafts(profile.id)
        waiting = [d for d in drafts if d.id not in sitting.decisions]
        tab = tab if tab in html.TAB_KEYS else (html.QUEUE if waiting else html.TOLD)
        current = next((d for d in drafts if d.id == draft), None)
        if current is None and tab == html.QUEUE and not draft:
            current = ce.next_draft(drafts, sitting)
        names = tuple(c.name for c in self._store.connections())
        applied = self._store.profiles.where_applied(profile, names)
        catalogs = self._catalogs(profile)
        drafted = profile.saved(profile.changed_by, context=sitting.context(profile, drafts))
        return html.ContextView(
            profile=profile, profiles=self._store.profiles.all(), applied_to=applied, tab=tab, sitting=sitting,
            drafts=drafts, current=current, text=sitting.texts.get(current.id, current.text) if current else "",
            evidence=self._evidence(profile, current, applied) if current and tab == html.QUEUE else None,
            context=drafted.context, saved_context=profile.context, held=ce.held_back(drafted, catalogs),
            assumptions=self._assumptions(applied), told=self._told(applied) if tab == html.TOLD else (),
            inputs={k: form.get(k) for k in html.INPUTS}, changes=sitting.change_count(drafts),
            discards=len(sitting.discarded(drafts)), problem=problem, said=said, editable=self._editable)

    def _catalogs(self, profile: Profile) -> list[tuple[Table, ...]]:
        names = tuple(c.name for c in self._store.connections())
        found = []
        for name in self._store.profiles.where_applied(profile, names):
            catalog = self._catalog_of(name)
            if catalog is not None:
                found.append(catalog)
        return found

    def _assumptions(self, applied: Sequence[str]):
        found = self._store.assumptions.recent(ASSUMPTIONS_SHOWN)
        return tuple(a for a in found if a.source in applied) if applied else found

    def _evidence(self, profile: Profile, draft: Draft, applied: Sequence[str]) -> html.Evidence:
        item = draft.item
        elsewhere = tuple((p.name, p.version, m.text) for p in self._store.profiles.all() if p.id != profile.id
                          for m in p.context.meanings if isinstance(item, Meaning) and m.key == item.key)
        values: tuple[str, ...] = ()
        where, problem = "", ""
        if isinstance(item, Meaning) and item.column:
            for source in applied:
                connection = self._store.connection(source)
                catalog = self._catalog_of(source) if connection else None
                table = next((t for t in catalog or () if t.name.lower() == item.table.lower()), None)
                if connection is None or table is None:
                    continue
                values, problem = self._values(connection, profile, table, item.column)
                where = source
                break
        return html.Evidence(where, values, problem, elsewhere)

    def _values(self, connection: Connection, profile: Profile, table: Table, column: str) -> tuple[tuple[str, ...], str]:
        """A few values of the column as the AI gets them (stand-ins, totals), read just now for the user only."""
        name = next((c for c in table.columns if c.lower() == column.lower()), None)
        if name is None:
            return (), f"{table.name} has no column {column} now"
        grants, alone = pe.policy_for(self._store, profile, connection.name)
        got = pe.sample(lambda: self._opened(connection), connection.name, grants, alone,
                        replace(table, columns=(name,)), pe.key_getter(self._keychain))
        if got.problem:
            return (), got.problem
        if got.rows:
            return tuple(dict.fromkeys("" if r[0] is None else str(r[0]) for r in got.rows))[:SAMPLE_VALUES], ""
        if got.total_rows:
            return (f"total {got.total_rows[0][0]}",), ""
        return (), "the AI gets no values of it"

    def _told(self, applied: Sequence[str]) -> tuple[html.Told, ...]:
        found = []
        for source in applied:
            connection = self._store.connection(source)
            catalog = self._catalog(connection) if connection else "it isn't connected any more"
            if isinstance(catalog, str):
                found.append(told_html.Told(source, problem=catalog))
                continue
            if not self._store.profiles.on_profiles(source):
                found.append(told_html.Told(source, problem="it runs on block rules, so profiles and their context "
                                                       "don't apply there"))
                continue
            try:
                given = served(self._store, source, catalog, self._catalog_of)
                why = why_hidden(self._store, source, catalog, self._catalog_of)
            except SourceError as err:
                found.append(told_html.Told(source, problem=str(err)))
                continue
            grants = self._store.rules_for(source)
            tables = tuple(told_html.ToldTable(
                t.name, given.notes.get((t.name, TABLE_NOTE)),
                tuple((c, grants.level(t, c) or "", given.notes.get((t.name, c)),
                       given.origins.get((t.name, c))) for c in t.columns),
                given.origins.get((t.name, TABLE_NOTE))) for t in grants.visible(catalog))
            held = tuple(i for i in why.items if i.what not in ("table", "column"))
            found.append(told_html.Told(source, tables, given.definitions, given.links, given.clashes, held))
        return tuple(found)


def _granted_columns(profile: Profile) -> tuple[str, ...]:
    return tuple(dict.fromkeys(f"{g.table}.{g.column}" for g in profile.grants))


def _decided(draft: Draft, action: str) -> str:
    said = {ce.APPROVE: "approved", ce.SKIP: "skipped for now", ce.DISCARD: "will be discarded"}[action]
    return f"{draft.kind.capitalize()} of {draft.about} {said}. Nothing is published until you save."
