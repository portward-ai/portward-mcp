"""The notes page on the local Portward page (#119): GET /notes?source= shows every table and column of one source
with its note; POST /notes saves the notes the user edited (one form, one save). A saved note reaches the AI with
the schema, as notes set through set_note do. Like set_note, it's computer-only: a page opened for the web routes
reads notes but can't change them. A source on profiles has no Free notes; the page points to its context."""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Mapping

from portward_free import notes_html
from portward_free.column_menu import MAX_NOTE
from portward_free.localweb import Response
from portward_free.page_html import Chrome
from portward_free.schema import TABLE_NOTE
from portward_free.sources import Table
from portward_free.storage import Connection, LocalStore

Catalog = Callable[[Connection], "tuple[Table, ...] | str"]


class NotesPage:
    def __init__(self, store: LocalStore, catalog: Catalog, editable: bool) -> None:
        self._store = store
        self._catalog = catalog
        self._editable = editable

    def handles(self, path: str) -> bool:
        return path == notes_html.PATH

    def get(self, chrome: Chrome, query: Mapping[str, str]) -> Response:
        connection = self._connection(query.get("source", ""))
        if connection is None:
            return Response.text(404, "No such source.")
        return Response(body=notes_html.page(chrome, self._view(connection, query.get("said", ""))))

    def post(self, chrome: Chrome, form: Mapping[str, str]) -> Response:
        connection = self._connection(form.get("source", ""))
        if connection is None:
            return Response.text(404, "No such source.")
        problem = self._refusal(connection)
        if problem is None:
            catalog = self._catalog(connection)
            problem = self._save(connection, catalog, form) if isinstance(catalog, tuple) else (
                f"Nothing was saved: its tables can't be listed right now: {catalog}")
            if problem is None:
                return self._back(chrome, connection.name, "Notes saved. Claude reads them from its next question.")
        return Response(body=notes_html.page(chrome, self._view(connection, problem=problem)))

    def _refusal(self, connection: Connection) -> str | None:
        if not self._editable:
            return notes_html.WEB_ONLY
        if self._store.profiles.on_profiles(connection.name):
            return f"{connection.name} runs on profiles, so its notes can't be changed here."
        return None

    def _save(self, connection: Connection, catalog: tuple[Table, ...], form: Mapping[str, str]) -> str | None:
        try:
            edits = notes_html.changed(form)
        except ValueError as err:
            return f"Nothing was saved: {err}."
        columns = {t.name: (TABLE_NOTE, *t.columns) for t in catalog}
        for table, column, note in edits:
            if column not in columns.get(table, ()):
                return f"Nothing was saved: {connection.name} has no {table}.{column} any more; reload the page."
            if len(note) > MAX_NOTE:
                return f"Nothing was saved: a note can be at most {MAX_NOTE} characters."
        for table, column, note in edits:
            self._store.set_note(connection.name, table, column, note)
        return None

    def _view(self, connection: Connection, said: str = "", problem: str = "") -> notes_html.NotesView:
        name = connection.name
        on_profiles = self._store.profiles.on_profiles(name)
        kept = frozenset((r.table, r.column or TABLE_NOTE) for r in self._store.rules(name))
        return notes_html.NotesView(
            source=name, sources=tuple(c.name for c in self._store.connections()),
            catalog=() if on_profiles else self._catalog(connection), notes=self._store.notes(name), kept=kept,
            on_profiles=on_profiles, editable=self._editable, said=said, problem=problem)

    def _connection(self, name: str) -> Connection | None:
        return self._store.connection(name) if name else None

    @staticmethod
    def _back(chrome: Chrome, source: str, said: str) -> Response:
        query = urllib.parse.urlencode({"source": source, "said": said})
        return Response.redirect(f"{chrome.web.link(notes_html.PATH)}?{query}")
