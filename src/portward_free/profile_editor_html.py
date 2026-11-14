"""The profile editor's markup (#126): the profiles list, the editor (design "Personal: profile editor A", a ledger of
columns with a four-step level control and the match preview beside it, plus C's sample rows as the AI gets them) and
the page that asks before the AI sees more. Pure functions to markup, every value escaped, no scripts: the editor is
one form, and its buttons preview, add a table, open a table's sample or save.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from portward_free import why_hidden_html
from portward_free.page_html import Chrome, _empty, _hidden, e, form, layout, said
from portward_free.profile_editor import CHOICES, HIDDEN, LEVEL_FIELD, PERSON_FIELD, ROWS_FIELD, FILTER_FIELD, Group, Sample
from portward_free.profiles import ANONYMISED, FULL, TOTALS, Preview, Profile
from portward_free.row_reasons import RowReasons
from portward_free.why_hidden import WhyHidden

LIST_PATH, EDIT_PATH, NEW_PATH = "/profiles", "/profile", "/profiles/new"
CONTEXT_PATH = "/context"  # the context editor (#127)
LABELS = {FULL: "Full", ANONYMISED: "Anonymised", TOTALS: "Totals only", HIDDEN: "Hidden"}


@dataclass(frozen=True)
class Listed:
    profile: Profile
    applied_to: tuple[str, ...]


@dataclass(frozen=True)
class EditorView:
    profile: Profile
    name: str
    description: str
    applied_to: tuple[str, ...]
    connections: tuple[str, ...]
    source: str
    groups: tuple[Group, ...]
    addable: tuple[str, ...]  # tables of the connection not shown yet
    extra: tuple[str, ...]  # tables the user added in this sitting
    table: str  # the table whose sample is open
    changes: int
    preview: Preview | None
    catalog_problem: str = ""
    sample: Sample | None = None
    why: WhyHidden | None = None
    problem: str = ""
    said: str = ""
    editable: bool = True
    rows: RowReasons | str = RowReasons()  # #132: what row rules do here with the draft, or why that can't be shown


def context_link(chrome: Chrome, profile: Profile) -> str:
    return f"{chrome.web.link(CONTEXT_PATH)}?{urllib.parse.urlencode({'id': profile.id})}"


def edit_link(chrome: Chrome, profile: Profile, **more: str) -> str:
    return f"{chrome.web.link(EDIT_PATH)}?{urllib.parse.urlencode({'id': profile.id, **more})}"


# --- the profiles list ----------------------------------------------------------------------------------------------

def profiles_page(chrome: Chrome, listed: Sequence[Listed], said_text: str = "", problem: str = "",
                  editable: bool = True) -> str:
    rows = "".join(
        f"<li class=\"entry\"><div class=\"what\"><p class=\"question\"><a href=\"{e(edit_link(chrome, i.profile))}\">"
        f"{e(i.profile.name)}</a></p><p class=\"meta\"><span>v{i.profile.version}</span>"
        f"<span>{len(i.profile.grants)} grants</span><span>applied to {e(', '.join(i.applied_to) or 'nothing')}"
        f"</span></p><p class=\"quiet\">{e(i.profile.description)}</p>"
        f"<p><a href=\"{e(context_link(chrome, i.profile))}\">Context of {e(i.profile.name)}</a></p></div></li>"
        for i in listed)
    body = f"<ol class=\"ledger\">{rows}</ol>" if listed else _empty("No profiles yet.")
    new = ""
    if editable:
        inner = ("<div class=\"pe-fields\"><label class=\"field\"><span>Name</span><input type=\"text\" name=\"name\" "
                 "required maxlength=\"80\" placeholder=\"For example: Sales and orders\"></label>"
                 "<label class=\"field\"><span>What it's for</span><input type=\"text\" name=\"description\" "
                 "maxlength=\"200\" placeholder=\"One line, for you and for the AI\"></label></div>"
                 "<div class=\"actions\"><button type=\"submit\" class=\"primary\">New profile</button></div>")
        new = ("<section class=\"card\" aria-labelledby=\"new-h\"><h2 id=\"new-h\">New profile</h2>"
               f"{form(chrome.web, NEW_PATH, {}, inner)}</section>")
    intro = ("<div class=\"intro\"><p class=\"eyebrow\">Personal</p><h1>Profiles</h1><p class=\"lede\">What the AI may "
             "see through each profile, column by column. Open one to change it.</p></div>")
    problem_html = f"<p class=\"problem\" role=\"alert\">{e(problem)}</p>" if problem else ""
    return layout(chrome, "Profiles", intro + said(said_text) + problem_html + body + new, home=False)


# --- the editor -----------------------------------------------------------------------------------------------------

def _radio(group: Group, name: str, column: str, chosen: str) -> str:
    choices = "".join(
        f"<label><input type=\"radio\" name=\"{e(LEVEL_FIELD + name)}\" value=\"{e(lv)}\""
        f"{' checked' if lv == chosen else ''}><span class=\"lv\" data-lv=\"{e(lv)}\" aria-hidden=\"true\"></span>"
        f"{e(LABELS[lv])}</label>" for lv in CHOICES)
    return (f"<fieldset class=\"seg\"><legend class=\"visually-hidden\">What the AI sees of {e(group.table)}."
            f"{e(column)}</legend>{choices}</fieldset>")


def _column(group: Group, column) -> str:
    kind = "not found here" if column.numeric is None else ("number" if column.numeric else "text or other")
    css = " changed" if column.chosen != column.saved else ""
    css += " is-hidden" if column.chosen == HIDDEN else ""
    why = ""
    if column.why:
        why = (f"<p class=\"why\"><span class=\"tag err\">Hidden here instead</span> "
               f"<span class=\"quiet\">{e(column.why)}.</span></p>")
    elif column.numeric is None:
        why = ("<p class=\"why\"><span class=\"tag\">Not in your data</span> <span class=\"quiet\">Granted by name. "
               "It lights up if this connection adds it.</span></p>")
    was = f"<span class=\"was\">was {e(LABELS[column.saved])}</span>" if column.chosen != column.saved else ""
    return (f"<div class=\"colrow{css}\"><div class=\"nm\"><span class=\"id\"><b>{e(column.column)}</b></span>"
            f"<span class=\"type\">{e(kind)}</span>{was}</div>{_radio(group, column.name, column.column, column.chosen)}"
            f"{why}</div>")


def _person(group: Group) -> str:
    """The person column only matters to totals, so it shows on tables with a totals column (or one already set)."""
    granted = [c.column for c in group.columns if c.chosen != HIDDEN]
    if group.missing or not (group.person or any(c.chosen == TOTALS for c in group.columns)):
        return ""
    choices = [("", "None: totals count rows")] + [(c, c) for c in granted]
    if group.person and group.person.lower() not in {c.lower() for c in granted}:
        choices.append((group.person, f"{group.person} (not granted)"))
    options = "".join(f"<option value=\"{e(v)}\"{' selected' if v.lower() == group.person.lower() else ''}>"
                      f"{e(text)}</option>" for v, text in choices)
    field_id = f"person-{e(group.table)}"
    return (f"<div class=\"person\"><label for=\"{field_id}\">Person column</label>"
            f"<select id=\"{field_id}\" name=\"{e(PERSON_FIELD + group.table)}\">{options}</select>"
            "<p class=\"quiet small\">The column that says whose row it is. Totals then count different people "
            "against the minimum group, not rows.</p></div>")


def _rows(group: Group) -> str:
    """The table's row rule (#132): fixed values only, checked when previewed or saved."""
    if group.missing:
        return ""
    field_id = f"rows-{e(group.table)}"
    hint_id = f"rows-hint-{e(group.table)}"
    return (f"<div class=\"person rows\"><label for=\"{field_id}\">Only rows where</label>"
            f"<input type=\"text\" id=\"{field_id}\" name=\"{e(ROWS_FIELD + group.table)}\" value=\"{e(group.rows)}\" "
            f"maxlength=\"2000\" placeholder=\"Every row\" aria-describedby=\"{hint_id}\">"
            f"<p id=\"{hint_id}\" class=\"quiet small\">Leave empty for every row. Fixed values only, for example "
            "<span class=\"id\">region IN ('EU', 'US') AND amount &gt;= 100</span>: a column IN a list, comparisons "
            "(=, &lt;&gt;, &lt;, &lt;=, &gt;, &gt;=), AND and OR. Rows outside it don't exist for the AI through this "
            "profile.</p></div>")


def _filter(group: Group) -> str:
    """The table's standing filter (#133): the rows every read of the table keeps, whichever profile reaches it."""
    if group.missing:
        return ""
    field_id = f"filter-{e(group.table)}"
    hint_id = f"filter-hint-{e(group.table)}"
    return (f"<div class=\"person rows\"><label for=\"{field_id}\">Standing filter: every read keeps only rows "
            f"where</label><input type=\"text\" id=\"{field_id}\" name=\"{e(FILTER_FIELD + group.table)}\" "
            f"value=\"{e(group.filter)}\" maxlength=\"2000\" placeholder=\"No filter\" "
            f"aria-describedby=\"{hint_id}\"><p id=\"{hint_id}\" class=\"quiet small\">For rows no answer should "
            "count, such as test accounts: <span class=\"id\">is_test = 0</span>. Same values-only form as above. "
            "Unlike the row rule, it applies to every read of this table, even through another profile.</p></div>")


def _group(view: EditorView, index: int, group: Group) -> str:
    head_id = f"tg-{index}"
    schema = f"<i>{e(group.schema)}.</i>" if group.schema and group.schema != "main" else ""
    meta = (f"{group.granted} of {len(group.columns)} columns granted" if not group.missing else
            f"Not in {view.source}")
    open_button = "" if group.missing else (
        f"<button type=\"submit\" name=\"open\" value=\"{e(group.table)}\" class=\"quiet\">"
        f"See what the AI gets from {e(group.table)}</button>")
    rows = "".join(_column(group, c) for c in group.columns)
    return (f"<div class=\"tbl-group\" role=\"group\" aria-labelledby=\"{head_id}\"><div class=\"tg-head\">"
            f"<h2 id=\"{head_id}\" class=\"id\">{schema}<b>{e(group.table)}</b></h2>"
            f"<span class=\"tg-meta\">{e(meta)}</span></div>{rows}{_rows(group)}{_filter(group)}{_person(group)}{open_button}</div>")


def _add_table(view: EditorView) -> str:
    if not view.addable:
        return ""
    options = "".join(f"<option value=\"{e(t)}\">{e(t)}</option>" for t in view.addable)
    return (f"<div class=\"add-table\"><label for=\"add\">Add a table from {e(view.source)}</label><div class=\"inline\">"
            f"<select id=\"add\" name=\"add\">{options}</select>"
            "<button type=\"submit\" name=\"action\" value=\"add\">Add table</button></div>"
            "<p class=\"quiet small\">Its columns start hidden: choose a level for each.</p></div>")


def _connection_picker(view: EditorView) -> str:
    if len(view.connections) < 2:
        return ""
    options = "".join(f"<option value=\"{e(c)}\"{' selected' if c == view.source else ''}>{e(c)}</option>"
                      for c in view.connections)
    return (f"<div class=\"inline picker\"><label for=\"source\" class=\"visually-hidden\">Preview on connection</label>"
            f"<select id=\"source\" name=\"source\">{options}</select>"
            "<button type=\"submit\" name=\"action\" value=\"preview\">Look</button></div>")


def _plist(items: Sequence[tuple[str, str]]) -> str:
    return "<ul class=\"plist\">" + "".join(
        f"<li><span class=\"id\">{e(name)}</span><span class=\"quiet\">{e(text)}</span></li>"
        for name, text in items) + "</ul>"


def _rail(view: EditorView) -> str:
    picker = _connection_picker(view)
    hidden = "" if picker else _hidden({"source": view.source})
    head = (f"<div class=\"rail-head\"><h2 id=\"prev-h\">On {e(view.source)}</h2>{picker}</div>{hidden}"
            "<p class=\"quiet small\">What the AI will get from this connection if you save.</p>")
    found = view.preview
    if found is None:
        body = (f"<p class=\"problem\">No preview: {e(view.catalog_problem or 'its tables cannot be listed')}. "
                "Saving still works: grants name columns, not connections.</p>")
        return f"<aside class=\"rail card\" aria-labelledby=\"prev-h\">{head}{body}</aside>"
    counts = {lv: sum(g.level == lv for g in found.matched) for lv in (FULL, ANONYMISED, TOTALS)}
    tally = (f"<div class=\"pe-tally\"><div><span class=\"n\">{len(found.matched)}</span>"
             f"<span class=\"w\">matched</span></div><div class=\"warn\"><span class=\"n\">{len(found.hidden_here)}</span>"
             f"<span class=\"w\">hidden here instead</span></div><div><span class=\"n\">"
             f"{len(found.not_in_your_data)}</span><span class=\"w\">not in your data</span></div></div>")
    levels = "<ul class=\"levels\" aria-label=\"Matched, by level\">" + "".join(
        f"<li><span class=\"lv\" data-lv=\"{lv}\" aria-hidden=\"true\"></span>{e(LABELS[lv])}"
        f"<span class=\"num\">{counts[lv]}</span></li>" for lv in counts) + "</ul>"
    parts = [tally, levels]
    if found.hidden_here:
        parts.append("<h3 class=\"small\">Hidden here instead</h3>" + _plist([(g.name, why) for g, why in found.hidden_here]))
    if found.not_in_your_data:
        parts.append("<h3 class=\"small\">Not in your data</h3>" + _plist(
            [(g.name, f"No such column here. If it's added later, it lights up as {LABELS[g.level].lower()}.")
             for g in found.not_in_your_data]))
    parts.append(_row_rules(view.rows))
    parts.append("<p class=\"later\"><span class=\"lv\" data-lv=\"hidden\" aria-hidden=\"true\"></span>Everything "
                 "else is hidden, including columns added later.</p>")
    if view.why is not None and view.why.on_profiles:
        parts.append("<details class=\"why-saved\"><summary>Why each thing is hidden here, as saved</summary>"
                     f"{why_hidden_html.reasons(view.why)}</details>")
    return f"<aside class=\"rail card\" aria-labelledby=\"prev-h\">{head}{''.join(parts)}</aside>"


def _row_rules(found: RowReasons | str) -> str:
    """Row rules in the preview (#132): which rows reach the AI, and where one profile lowers another."""
    if isinstance(found, str):
        return f"<h3 class=\"small\">Row rules</h3><p class=\"problem\">No row preview: {e(found)}.</p>"
    if not found:
        return ""
    items = [(r.table, r.text) for r in found.rows]
    items += [(f"{r.table}.{r.column}", r.text) for r in found.some_rows]
    parts = ["<h3 class=\"small\">Row rules</h3>", _plist(items)] if items else []
    if found.lowered:
        parts.append("<h3 class=\"small\">Lowered by another profile</h3>" + _plist(
            [(r.name, r.text) for r in found.lowered]))
    if found.linked:
        parts.append("<h3 class=\"small\">Anonymised with its link</h3>" + _plist(
            [(r.name, r.text + ".") for r in found.linked]))
    return "".join(parts)


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def _sample(view: EditorView) -> str:
    found = view.sample
    if found is None:
        return ""
    alone = (f" This profile isn't applied to {view.source}: shown as if it were the only profile applied there."
             if found.alone else "")
    parts = [f"<h2 id=\"sample-h\">What the AI gets from <span class=\"id\">{e(found.table)}</span></h2>",
             f"<p class=\"quiet small\">Rows read just now from {e(view.source)}, as the AI would get them with this "
             f"draft. Only you see this; nothing here goes to the AI.{e(alone)}</p>"]
    if found.problem:
        parts.append(f"<p class=\"problem\">No sample: {e(found.problem)}</p>")
    if found.columns:
        stand = {c.lower() for c in found.stand_ins}
        head = "".join(f"<th scope=\"col\"><span class=\"lv\" data-lv=\"{ANONYMISED if c.lower() in stand else FULL}\" "
                       f"aria-hidden=\"true\"></span>{e(c)}</th>" for c in found.columns)
        stand_cell = " class=\"stand\""
        body = "".join("<tr>" + "".join(
            f"<td{stand_cell if c.lower() in stand else ''}>{e(_cell(v))}</td>"
            for c, v in zip(found.columns, row)) + "</tr>" for row in found.rows)
        parts.append(f"<div class=\"ai-table\" role=\"region\" aria-label=\"Sample rows\" tabindex=\"0\"><table>"
                     f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
        if found.stand_ins:
            parts.append("<p class=\"quiet small\">Stand-ins stay the same for the same value, so the AI can still "
                         "count, group and join on them.</p>")
    elif not found.problem and not found.totals:
        parts.append(_empty(f"The AI gets nothing from {found.table} with this draft."))
    if found.totals:
        names = ", ".join(found.totals)
        if found.total_rows:
            values = "; ".join(f"{c}: {_cell(v)}" for c, v in zip(found.total_columns, found.total_rows[0]))
            line = f"{e(values)}. The AI gets sums, averages and counts in groups big enough, never one row."
        elif not found.problem:
            line = "No total: too few rows or people to pass the minimum group, so the AI gets none."
        else:
            line = ""
        parts.append(f"<p class=\"totline\"><span class=\"lv\" data-lv=\"totals\" aria-hidden=\"true\"></span>"
                     f"<span>Totals only: <span class=\"id\">{e(names)}</span>. {line}</span></p>")
    if found.hidden:
        parts.append(f"<p class=\"gone\"><span class=\"lv\" data-lv=\"hidden\" aria-hidden=\"true\"></span><span>Hidden: "
                     f"<span class=\"id\">{e(', '.join(found.hidden))}</span>. The AI is never told these exist.</span></p>")
    return f"<section id=\"sample\" class=\"sample card\" aria-labelledby=\"sample-h\">{''.join(parts)}</section>"


def _head(chrome: Chrome, view: EditorView) -> str:
    p = view.profile
    applied = (f"<span class=\"tag kept\">Applied to {e(', '.join(view.applied_to))}</span>" if view.applied_to else
               "<span class=\"tag\">Not applied anywhere</span>")
    return (f"<div class=\"pe-head\"><p class=\"crumbs\"><a href=\"{e(chrome.web.link(LIST_PATH))}\">Profiles</a> / "
            f"{e(p.name)} · <a href=\"{e(context_link(chrome, p))}\">Its context</a></p>"
            f"<div class=\"pe-title\"><h1>{e(p.name)}</h1>{applied}</div>"
            "<div class=\"pe-fields\">"
            f"<label class=\"field\"><span>Name</span><input type=\"text\" name=\"name\" value=\"{e(view.name)}\" "
            "maxlength=\"80\"></label>"
            f"<label class=\"field\"><span>What it's for</span><input type=\"text\" name=\"description\" "
            f"value=\"{e(view.description)}\" maxlength=\"200\"></label></div>"
            f"<p class=\"byline\">{e(p.signature)}.</p></div>")


def _savebar(chrome: Chrome, view: EditorView) -> str:
    v = view.profile.version
    n = view.changes
    text = f"No changes since v{v}." if not n else f"{n} change{'s' * (n != 1)} since v{v}."
    discard = edit_link(chrome, view.profile, source=view.source)
    save = (f"<button type=\"submit\" name=\"action\" value=\"save\" class=\"primary\">Save as v{v + 1}</button>"
            if view.editable else "")
    return (f"<div class=\"savebar\"><p class=\"changes\" role=\"status\">{e(text)}</p><div class=\"actions\">"
            "<button type=\"submit\" name=\"action\" value=\"preview\">Preview changes</button>"
            f"<a class=\"button-link\" href=\"{e(discard)}\">Discard changes</a>{save}</div></div>")


def editor(chrome: Chrome, view: EditorView) -> str:
    notes = said(view.said)
    if view.problem:
        notes += f"<p class=\"problem\" role=\"alert\">{e(view.problem)}</p>"
    if not view.editable:
        notes += ("<p class=\"problem\">This page was opened for the web routes: profiles can be read here but "
                  "changed only from the Portward page on this computer.</p>")
    groups = "".join(_group(view, i, g) for i, g in enumerate(view.groups)) or _empty(
        "Nothing granted yet: through this profile the AI sees nothing. Add a table, then choose a level for each "
        "column.")
    fields = {"id": view.profile.id, "version": str(view.profile.version), "table": view.table,
              "tables": "\n".join(view.extra)}
    inner = (f"{_head(chrome, view)}{_savebar(chrome, view)}{notes}<div class=\"pe-grid\">"
             f"<section class=\"grants\" aria-label=\"Grants\">{groups}{_add_table(view)}</section>{_rail(view)}</div>"
             f"{_sample(view)}")
    return layout(chrome, f"{view.profile.name}: profile", form(chrome.web, EDIT_PATH, fields, inner, "editor"),
                  home=False)


def confirm(chrome: Chrome, question: str, fields: Mapping[str, str]) -> str:
    """Ask before the AI sees more; both answers post the whole draft back, so "no" keeps editing it."""
    kept = {k: v for k, v in fields.items() if k not in ("action", "confirm", "open", "form_token")}
    no = "<button type=\"submit\" name=\"action\" value=\"preview\" class=\"primary\">No, keep editing</button>"
    yes = "<button type=\"submit\" name=\"action\" value=\"save\" class=\"danger\">Yes, save it</button>"
    answers = form(chrome.web, EDIT_PATH, kept, f"<div class=\"actions\">{no}</div>") + form(
        chrome.web, EDIT_PATH, {**kept, "confirm": "yes"}, f"<div class=\"actions\">{yes}</div>")
    main = (f"<section class=\"ask\" aria-labelledby=\"ask\"><p class=\"eyebrow\">Save profile</p>"
            f"<h1 id=\"ask\">{e(question)}</h1><p>The AI would see more through this profile. Only you can answer this: "
            f"the AI can't reach this page.</p><div class=\"actions\">{answers}</div></section>")
    return layout(chrome, "Save profile?", main, home=False)
