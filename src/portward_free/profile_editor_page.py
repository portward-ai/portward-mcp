"""The profile editor's routes on the local Portward page (#126): the profiles list, the editor, its saves and a new
profile. page.PortwardPage hands them the requests under /profiles and /profile. See profile_editor for what a save
does and why the editor is a single form.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Mapping

from portward_free import profile_editor as pe
from portward_free import profile_editor_html as html
from portward_free.account import Accounts
from portward_free.keychain import Keychain
from portward_free.localweb import Response
from portward_free.page_html import Chrome
from portward_free.profile_tools import ProfileTools
from portward_free.profiles import Profile, ProfileError
from portward_free.rule_tools import Opener
from portward_free.sources import SourceError, Table
from portward_free.storage import Connection, LocalStore
from portward_free.why_hidden import why_hidden

NOT_FOUND = Response.text(404, "Not found.")
Catalog = Callable[[Connection], "tuple[Table, ...] | str"]


class ProfileEditorPage:
    def __init__(self, store: LocalStore, keychain: Keychain, opened: Opener, accounts: Accounts | None,
                 editable: bool, catalog: Catalog, catalog_of: Callable[[str], tuple[Table, ...] | None]) -> None:
        self._store, self._keychain, self._opened = store, keychain, opened
        self._tools = ProfileTools(store, keychain, accounts, opened) if accounts is not None else None
        self._editable = editable and accounts is not None
        self._catalog, self._catalog_of = catalog, catalog_of

    def handles(self, path: str) -> bool:
        return path in (html.LIST_PATH, html.EDIT_PATH, html.NEW_PATH)

    def get(self, chrome: Chrome, path: str, query: Mapping[str, str]) -> Response:
        if path == html.LIST_PATH:
            return Response(body=self._list(chrome, query.get("said", "")))
        if path != html.EDIT_PATH:
            return NOT_FOUND
        profile = self._store.profiles.by_id(query.get("id", ""))
        if profile is None:
            return NOT_FOUND
        return Response(body=html.editor(chrome, self._view(profile, pe.Draft(profile), query.get("source", ""),
                                                            query.get("table", ""), (), said=query.get("said", ""))))

    def post(self, chrome: Chrome, path: str, fields: Mapping[str, str]) -> Response:
        if path == html.NEW_PATH:
            return self._new(chrome, fields)
        if path != html.EDIT_PATH:
            return NOT_FOUND
        profile = self._store.profiles.by_id(fields.get("id", ""))
        if profile is None:
            return Response(body=self._list(chrome, "", "That profile isn't there any more."))
        extra = tuple(t for t in fields.get("tables", "").split("\n") if t.strip())
        table = fields.get("open") or fields.get("table", "")
        action = fields.get("action", "preview")
        if action == "add" and fields.get("add"):
            extra = (*extra, fields["add"])
            table = fields["add"]
        try:
            draft = pe.draft_from(profile, fields)
        except ProfileError as err:
            return Response(body=html.editor(chrome, self._view(profile, pe.Draft(profile), fields.get("source", ""),
                                                                table, extra, problem=f"Not saved: {err}.")))
        if action == "save":
            done = pe.save(self._tools, draft, fields.get("version", ""), fields.get("confirm") == "yes",
                           self._editable)
            if done.saved is not None:
                query = {"id": done.saved.id, "source": fields.get("source", ""), "table": table,
                         "said": f"Saved as v{done.saved.version}."}
                return Response.redirect(f"{chrome.web.link(html.EDIT_PATH)}?{urllib.parse.urlencode(query)}")
            if done.question:
                return Response(body=html.confirm(chrome, done.question, fields))
            return Response(body=html.editor(chrome, self._view(profile, draft, fields.get("source", ""), table,
                                                                extra, problem=done.problem)))
        return Response(body=html.editor(chrome, self._view(profile, draft, fields.get("source", ""), table, extra)))

    # --- the list and a new profile ---------------------------------------------------------------------------------
    def _list(self, chrome: Chrome, said: str, problem: str = "") -> str:
        names = tuple(c.name for c in self._store.connections())
        listed = [html.Listed(p, self._store.profiles.where_applied(p, names)) for p in self._store.profiles.all()]
        return html.profiles_page(chrome, listed, said, problem, self._editable)

    def _new(self, chrome: Chrome, fields: Mapping[str, str]) -> Response:
        if not self._editable or self._tools is None:
            return Response(body=self._list(chrome, "", pe.WEB_ONLY))
        standing = self._tools.standing()
        if standing.email is None:
            return Response(body=self._list(chrome, "", standing.why or ""))
        try:
            made = Profile.new(fields.get("name", ""), fields.get("description", ""), standing.email, ())
            if self._store.profiles.named(made.name) is not None:
                raise ProfileError(f"there is already a profile named {made.name!r}")
        except ProfileError as err:
            return Response(body=self._list(chrome, "", f"Not made: {err}."))
        saved = self._store.profiles.save(made)
        query = {"id": saved.id, "said": f"Made profile {saved.name}. Add a table to grant its columns."}
        return Response.redirect(f"{chrome.web.link(html.EDIT_PATH)}?{urllib.parse.urlencode(query)}")

    # --- the editor's view ------------------------------------------------------------------------------------------
    def _view(self, profile: Profile, draft: pe.Draft, source: str, table: str, extra: tuple[str, ...], *,
              problem: str = "", said: str = "") -> html.EditorView:
        connections = self._store.connections()
        names = tuple(c.name for c in connections)
        applied = self._store.profiles.where_applied(profile, names)
        connection = next((c for c in connections if c.name == source), None) or next(
            (c for c in connections if c.name in applied), None) or (connections[0] if connections else None)
        found = self._catalog(connection) if connection is not None else "no connection is set up"
        catalog = found if isinstance(found, tuple) else None
        shown = pe.preview_of(draft, catalog)
        hidden_here = {g.name: why for g, why in shown.hidden_here} if shown else {}
        groups = pe.groups(draft, catalog, extra, hidden_here)
        listed = {g.table.lower() for g in groups}
        addable = tuple(t.name for t in catalog or () if t.name.lower() not in listed)
        source_name = connection.name if connection is not None else ""
        opened = next((t for t in catalog or () if t.name.lower() == table.lower()), None) if table else None
        if opened is None and catalog and groups and not groups[0].missing:
            opened = next((t for t in catalog if t.name == groups[0].table), None)
        return html.EditorView(
            profile=profile, name=draft.name if draft.name is not None else profile.name,
            description=draft.description if draft.description is not None else profile.description,
            applied_to=applied, connections=names, source=source_name, groups=groups, addable=addable, extra=extra,
            table=opened.name if opened else "", changes=draft.change_count(), preview=shown,
            catalog_problem=found if isinstance(found, str) else "",
            sample=self._sample(connection, draft, opened) if opened and connection else None,
            why=self._why(source_name, catalog), problem=problem, said=said, editable=self._editable,
            rows=pe.rows_preview(self._store, draft, source_name, catalog))

    def _sample(self, connection: Connection, draft: pe.Draft, table: Table) -> pe.Sample:
        try:
            drafted = draft.edited("draft")
        except ProfileError as err:
            return pe.Sample(table.name, problem=str(err))
        grants, alone = pe.policy_for(self._store, drafted, connection.name)
        return pe.sample(lambda: self._opened(connection), connection.name, grants, alone, table,
                         pe.key_getter(self._keychain))

    def _why(self, source: str, catalog: tuple[Table, ...] | None):
        if catalog is None or not source or not self._store.profiles.on_profiles(source):
            return None
        try:
            return why_hidden(self._store, source, catalog, self._catalog_of)
        except SourceError:
            return None
