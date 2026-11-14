"""The notes page's markup (#119): what the user says each table and column of one source means, as one form. Pure
functions to markup, every value escaped, no scripts.

Each note is a textarea named `n:<json [table, column]>` ('' as the column for the table's own note), with the text
the page showed beside it in a hidden `o:` field, so a save changes only the notes the user edited.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from portward_free.page_html import Chrome, e, form, layout, said
from portward_free.schema import TABLE_NOTE
from portward_free.sources import Table

PATH = "/notes"
NOTE_FIELD, SHOWN_FIELD = "n:", "o:"
WEB_ONLY = ("This page was opened for the web routes: notes can be read here but changed only from the Portward "
            "page on this computer.")


def key(table: str, column: str = TABLE_NOTE) -> str:
    return json.dumps([table, column])


def link(chrome: Chrome, source: str) -> str:
    return f"{chrome.web.link(PATH)}?{urllib.parse.urlencode({'source': source})}"


@dataclass(frozen=True)
class NotesView:
    source: str
    sources: tuple[str, ...]  # every connection, for the picker
    catalog: tuple[Table, ...] | str  # its tables, or why they can't be listed
    notes: Mapping[tuple[str, str], str]
    kept: frozenset[tuple[str, str]]  # (table, column or '') kept from Claude by a block rule
    on_profiles: bool = False
    editable: bool = True
    said: str = ""
    problem: str = ""


def page(chrome: Chrome, view: NotesView) -> str:
    head = (f"<div class=\"ce-head\"><p class=\"crumbs\"><a href=\"{e(chrome.web.link('/'))}\">Overview</a> / Notes"
            f"</p><div class=\"ce-top\"><div><h1>Notes on <span class=\"id\">{e(view.source)}</span></h1>"
            "<p class=\"lede\">Your own words on what the tables and columns mean. Claude reads them with the "
            "table and column names before it writes a query. Notes on what you keep from Claude never reach it."
            f"</p></div>{_picker(chrome, view)}</div></div>")
    told = said(view.said)
    if view.problem:
        told += f"<p class=\"problem\" role=\"alert\">{e(view.problem)}</p>"
    return layout(chrome, f"Notes on {view.source}", f"<div class=\"ce notes\">{head}{told}{_body(chrome, view)}</div>",
                  home=False)


def _picker(chrome: Chrome, view: NotesView) -> str:
    others = [s for s in view.sources if s != view.source]
    if not others:
        return ""
    items = "".join(f"<li><a href=\"{e(link(chrome, s))}\">{e(s)}</a></li>" for s in others)
    return f"<nav aria-label=\"Other sources\" class=\"picker\"><p class=\"quiet\">Other sources</p><ul>{items}</ul></nav>"


def _body(chrome: Chrome, view: NotesView) -> str:
    if view.on_profiles:
        return ("<p class=\"empty-state\">This source runs on profiles, so these notes don't apply to it: Claude "
                "is told the meanings in the applied profiles' context instead. "
                f"<a href=\"{e(chrome.web.link('/context'))}\">Edit the context</a></p>")
    if isinstance(view.catalog, str):
        return f"<p class=\"problem\">Its tables can't be listed right now: {e(view.catalog)}</p>"
    if not view.catalog:
        return "<p class=\"empty-state\">This source has no tables to write notes on.</p>"
    tables = "".join(_table(view, t) for t in view.catalog)
    if not view.editable:
        return f"<p class=\"problem\">{e(WEB_ONLY)}</p>{tables}"
    save = ("<div class=\"savebar\"><p class=\"quiet\">Only the notes you change are saved. An empty note is "
            "removed.</p><button type=\"submit\" class=\"primary\">Save notes</button></div>")
    shown = {SHOWN_FIELD + key(t.name, c): view.notes[(t.name, c)]  # none for an empty note: keeps the form small
             for t in view.catalog for c in (TABLE_NOTE, *t.columns) if (t.name, c) in view.notes}
    return form(chrome.web, PATH, {"source": view.source, **shown}, f"{tables}{save}", "notes-form")


def _table(view: NotesView, table: Table) -> str:
    rows = "".join(_entry(view, table.name, c) for c in (TABLE_NOTE, *table.columns))
    return (f"<section class=\"tsec\" aria-labelledby=\"{e(_id(table.name))}\"><h2 id=\"{e(_id(table.name))}\">"
            f"<span class=\"id\">{e(table.name)}</span></h2>{rows}</section>")


def _entry(view: NotesView, table: str, column: str) -> str:
    name = f"{table}.{column}" if column else f"{table} (the whole table)"
    kept = (table, column) in view.kept or (table, TABLE_NOTE) in view.kept
    tag = " <span class=\"tag withheld\">Kept from Claude</span>" if kept else ""
    text = view.notes.get((table, column), "")
    field = _id(table, column)
    if not view.editable:
        shown = f"<p>{e(text)}</p>" if text else "<p class=\"none\">No note</p>"
        return f"<div class=\"entry-row\"><div class=\"k\"><span class=\"id\">{e(name)}</span>{tag}</div>{shown}</div>"
    label = f"<div class=\"k\"><label for=\"{e(field)}\"><span class=\"id\">{e(name)}</span></label>{tag}</div>"
    area = (f"<textarea id=\"{e(field)}\" name=\"{e(NOTE_FIELD + key(table, column))}\" rows=\"2\" "
            f"maxlength=\"2000\">{e(text)}</textarea>")
    return f"<div class=\"entry-row\">{label}{area}</div>"


def _id(table: str, column: str | None = None) -> str:
    """An element id for a table or note field (names may hold any character, so they're hex-coded)."""
    raw = table if column is None else f"{table}\x00{column}"
    return ("t-" if column is None else "n-") + raw.encode("utf-8").hex()


def changed(form: Mapping[str, str]) -> Sequence[tuple[str, str, str]]:
    """(table, column, note) for every note the user edited: its text differs from what the page showed."""
    found = []
    for name, value in form.items():
        if not name.startswith(NOTE_FIELD):
            continue
        shown = form.get(SHOWN_FIELD + name.removeprefix(NOTE_FIELD), "")
        if value.strip() == shown.strip():
            continue
        try:
            table, column = json.loads(name.removeprefix(NOTE_FIELD))
        except (ValueError, TypeError):
            raise ValueError("a note's field couldn't be read; reload the page") from None
        if not isinstance(table, str) or not isinstance(column, str):
            raise ValueError("a note's field couldn't be read; reload the page")
        found.append((table, column, value))
    return found
