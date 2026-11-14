"""The context editor's markup (#127): design "Personal: context editor A" (a review queue, one draft at a time with
what it's based on beside it, and tabs for definitions, links, a dictionary import and what the AI assumed) with B as
the read view of what the AI is told (context_told_html). Pure functions to markup, every value escaped, no scripts:
the editor is one form, its tabs are buttons that post it back, and the keyboard works through access keys and the
focused Approve button.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from portward_free import context_told_html as told_html
from portward_free.assumptions import Assumption
from portward_free.context import Context, Definition, Draft, Link, Meaning, mentions
from portward_free.context_editor import APPROVE, DISCARD, SKIP, Sitting, link_id
from portward_free.page_html import Chrome, _empty, e, form, layout, said, when
from portward_free.profiles import Profile

PATH = "/context"
QUEUE, DEFINITIONS, LINKS, IMPORT, ASSUMED, TOLD = "queue", "definitions", "links", "import", "assumed", "told"
# The tabs in order. #129 (metrics) adds its own entry here, before TOLD, and a panel in PANELS.
TABS = ((QUEUE, "To approve"), (DEFINITIONS, "Definitions"), (LINKS, "Links"), (IMPORT, "Import a dictionary"),
        (ASSUMED, "What the AI assumed"), (TOLD, "What the AI is told"))
TAB_KEYS = tuple(k for k, _ in TABS)
INPUTS = ("def_term", "def_text", "def_from", "link_one", "link_other", "link_text", "dict_path")
PROMPT = "Draft what each column of the tables in my profile means"
STATUS = {APPROVE: "approved", SKIP: "skipped", DISCARD: "to discard"}


@dataclass(frozen=True)
class Evidence:
    source: str  # where the sample values were read
    values: tuple[str, ...] = ()
    problem: str = ""
    elsewhere: tuple[tuple[str, int, str], ...] = ()  # (profile, version, its meaning of the same column)


@dataclass(frozen=True)
class ImportRow:
    key: str  # m<i> (matched) or u<i> (matched nothing)
    name: str  # as the file names it
    about: str  # what it maps to, "" when nothing
    text: str
    problem: str = ""  # why it can't be taken
    mapped: str = ""  # the user's choice for an unmatched row


@dataclass(frozen=True)
class Reading:
    path: str
    file: str
    rows: tuple[ImportRow, ...]
    missed: tuple[ImportRow, ...]
    columns: tuple[str, ...]  # what an unmatched row can be mapped onto


@dataclass(frozen=True)
class ContextView:
    profile: Profile
    profiles: tuple[Profile, ...]
    applied_to: tuple[str, ...]
    tab: str
    sitting: Sitting
    drafts: tuple[Draft, ...]
    current: Draft | None
    text: str
    evidence: Evidence | None
    context: Context  # as it will be saved
    saved_context: Context
    held: Mapping[str, str]  # why_hidden name (lower case) -> why it isn't sent, with the fix
    assumptions: tuple[Assumption, ...]
    told: tuple = ()
    inputs: Mapping[str, str] = field(default_factory=dict)
    changes: int = 0
    discards: int = 0
    problem: str = ""
    said: str = ""
    editable: bool = True
    reading: Reading | None = None


def link_to(chrome: Chrome, profile: Profile, **more: str) -> str:
    return f"{chrome.web.link(PATH)}?{urllib.parse.urlencode({'id': profile.id, **more})}"


def no_profile(chrome: Chrome, problem: str = "") -> str:
    note = f"<p class=\"problem\" role=\"alert\">{e(problem)}</p>" if problem else ""
    main = ("<div class=\"intro\"><p class=\"eyebrow\">Personal</p><h1>Context</h1><p class=\"lede\">Context belongs "
            "to a profile. Make a profile first, then come back to say what its data means.</p></div>"
            f"{note}<p><a href=\"{e(chrome.web.link('/profiles'))}\">Profiles</a></p>")
    return layout(chrome, "Context", main, home=False)


def _id(about: str) -> str:
    parts = about.split(".")
    return (f"<code class=\"id\">{''.join(f'<i>{e(p)}.</i>' for p in parts[:-1])}<b>{e(parts[-1])}</b></code>")


def _chip(text: str, kind: str = "") -> str:
    return f"<span class=\"tag{' ' + kind if kind else ''}\">{e(text)}</span>"


# --- the queue ------------------------------------------------------------------------------------------------------

def _queue_list(view: ContextView) -> str:
    items = []
    for draft in view.drafts:
        decision = view.sitting.decisions.get(draft.id)
        now = view.current is not None and draft.id == view.current.id
        status = "now" if now else STATUS.get(decision or "", "")
        if decision == APPROVE and draft.id in view.sitting.texts:
            status = "edited" if not now else status
        css = " done" if decision and not now else ""
        current = " aria-current=\"true\"" if now else ""
        items.append(f"<li class=\"q-item{css}\"{current}><button type=\"submit\" name=\"show\" value=\"{e(draft.id)}\" "
                     f"class=\"q-open\"><span class=\"q-kind\">{e(draft.kind)}</span>{_id(draft.about)}"
                     f"<span class=\"st\">{e(status)}</span></button></li>")
    done = sum(d.id in view.sitting.decisions for d in view.drafts)
    return (f"<nav class=\"q-nav\" aria-label=\"Drafts waiting\"><p class=\"small quiet\">{done} of {len(view.drafts)} "
            f"decided</p><progress max=\"{len(view.drafts)}\" value=\"{done}\" aria-hidden=\"true\"></progress>"
            f"<ul class=\"q-list\">{''.join(items)}</ul></nav>")


def _evidence(view: ContextView) -> str:
    found, draft = view.evidence, view.current
    if found is None or draft is None:
        return ""
    parts = []
    if isinstance(draft.item, Meaning) and draft.item.column:
        if found.values:
            values = "".join(f"<li>{e(v)}</li>" for v in found.values)
            parts.append(f"<div><h3>Values as the AI gets them</h3><ul class=\"values\">{values}</ul>"
                         f"<p class=\"small quiet\">Read just now from {e(found.source)}, for you only.</p></div>")
        elif found.problem:
            parts.append(f"<div><h3>Values as the AI gets them</h3><p class=\"small quiet\">None to show: "
                         f"{e(found.problem)}.</p></div>")
    else:
        names = ", ".join(mentions(draft.text)) or "none"
        parts.append(f"<div><h3>Names it uses</h3><p class=\"small\">{e(names)}</p></div>")
    if isinstance(draft.item, Meaning):
        if found.elsewhere:
            said_there = "".join(f"<li><b>{e(p)}</b> v{v}: {e(t)}</li>" for p, v, t in found.elsewhere)
            parts.append(f"<div><h3>Already said elsewhere</h3><ul class=\"elsewhere\">{said_there}</ul>"
                         "<p class=\"small quiet\">If both profiles apply to one connection and the texts differ, "
                         "the AI gets neither until you pick one.</p></div>")
        else:
            parts.append("<div><h3>Already said elsewhere</h3><p class=\"small quiet\">No other profile describes "
                         "this. No clash.</p></div>")
    return f"<aside class=\"evidence\" aria-label=\"What the draft is based on\">{''.join(parts)}</aside>"


def _draft_card(view: ContextView) -> str:
    draft = view.current
    assert draft is not None
    label = {"meaning": "What it means", "definition": f"What {draft.about} means", "link": "How they match"}[
        draft.kind]
    by = draft.drafted_by or "an AI app"
    on = f" on {when(draft.drafted_at)[:10]}" if draft.drafted_at else ""
    person = getattr(draft.item, "person", None)
    person_note = (f"<p class=\"small\">Person column: <code class=\"id\">{e(person)}</code>. Totals of "
                   f"{e(draft.about)} will count different people by it, not rows.</p>" if person else "")
    if isinstance(draft.item, Meaning):
        person_note += _extras(draft.item)
    decision = view.sitting.decisions.get(draft.id)
    decided = (f"<p class=\"small\">{_chip(STATUS[decision].capitalize(), 'kept' if decision == APPROVE else '')} "
               "You can change your mind until you save.</p>" if decision else "")
    acts = ("<div class=\"actions\">"
            "<button type=\"submit\" name=\"action\" value=\"approve\" class=\"primary\" accesskey=\"a\" autofocus>"
            f"Approve {e(draft.kind)} <kbd>A</kbd></button>"
            "<button type=\"submit\" name=\"action\" value=\"skip\" accesskey=\"s\">Skip for now <kbd>S</kbd></button>"
            "<button type=\"submit\" name=\"action\" value=\"discard\" class=\"quiet\" accesskey=\"d\">"
            "Discard draft</button></div>"
            "<p class=\"small quiet keys\">Keys: Enter approves (the Approve button has focus); with your browser's "
            "access key, A approves, S skips and D discards. Shift+Tab goes back to edit the text.</p>")
    return (f"<article class=\"card draft\" aria-labelledby=\"d-h\"><div class=\"draft-what\"><h2 id=\"d-h\">"
            f"<span class=\"visually-hidden\">Draft {e(draft.kind)} of </span>{_id(draft.about)}</h2>"
            f"{_chip(draft.kind.capitalize())}</div><div class=\"draft-grid\"><div class=\"draft-main\">"
            f"<label class=\"field\" for=\"text\"><span>{e(label)}</span></label>"
            f"<textarea id=\"text\" name=\"text\" rows=\"4\" maxlength=\"1000\">{e(view.text)}</textarea>"
            f"{person_note}<p class=\"small quiet\">Drafted by {e(by)}{e(on)}. It isn't used until you approve it "
            f"and save.</p>{decided}{acts}</div>{_evidence(view)}</div></article>")


def _extras(meaning: Meaning) -> str:
    """A drafted meaning's value meanings, samples and synonyms (#131), approved with it."""
    rows = []
    if meaning.values:
        said = "; ".join(f"<code>{e(v)}</code> means {e(m)}" for v, m in meaning.values)
        rows.append(f"<dt>Values</dt><dd>{said}</dd>")
    if meaning.samples:
        samples = ", ".join(f"<code>{e(s)}</code>" for s in meaning.samples)
        rows.append(f"<dt>Sample values</dt><dd>{samples} <span class=\"quiet\">(given to the AI only where it gets "
                    "this column in full)</span></dd>")
    if meaning.synonyms:
        rows.append(f"<dt>Also called</dt><dd>{e(', '.join(meaning.synonyms))}</dd>")
    return f"<dl class=\"small extras\">{''.join(rows)}</dl>" if rows else ""


def _queue(view: ContextView) -> str:
    if not view.drafts:
        return (f"<div class=\"empty-state\"><h2>No drafts waiting</h2><p>Ask your AI to draft meanings, for example "
                f"<q>{e(PROMPT)}</q>. Drafts land here, and nothing reaches the AI until you approve it and save."
                "</p></div>")
    body = _draft_card(view) if view.current else (
        f"<div class=\"empty-state\"><h2>Every draft has a decision</h2><p>Save as v{view.profile.version + 1} to "
        "publish your approvals in one version. Skipped drafts stay here for later.</p></div>")
    return f"<div class=\"q\">{_queue_list(view)}{body}</div>"


# --- definitions and links ------------------------------------------------------------------------------------------

def _status(view: ContextView, key: str, change: object, gone: bool) -> str:
    held = view.held.get(key.lower())
    parts = []
    if gone:
        parts.append(_chip("Will be removed", "err"))
    elif change is not None:
        parts.append(_chip("In this sitting", "saved"))
    if held and not gone:
        parts.append(f"{_chip('Held back', 'err')} <span class=\"small\">{e(held)}.</span>")
    return f"<p class=\"uses\">{' '.join(parts)}</p>" if parts else ""


def _definition_row(view: ContextView, d: Definition, gone: bool) -> str:
    change = view.sitting.definitions.get(d.key, None) if d.key in view.sitting.definitions else None
    if gone:
        buttons = (f"<button type=\"submit\" name=\"keep_def\" value=\"{e(d.key)}\" class=\"quiet\">"
                   f"Keep {e(d.term)}</button>")
    else:
        buttons = (f"<button type=\"submit\" name=\"edit_def\" value=\"{e(d.key)}\" class=\"quiet\">"
                   f"Edit<span class=\"visually-hidden\"> {e(d.term)}</span></button>"
                   f"<button type=\"submit\" name=\"remove_def\" value=\"{e(d.key)}\" class=\"quiet\">"
                   f"Remove<span class=\"visually-hidden\"> {e(d.term)}</span></button>")
    uses = ", ".join(mentions(d.text))
    return (f"<li class=\"def{' gone' if gone else ''}\"><h3>{e(d.term)}</h3><div><p>{e(d.text)}</p>"
            f"{_status(view, f'definition {d.term}', change, gone)}"
            f"<p class=\"small quiet\">Names: {e(uses or 'none')}</p></div><div class=\"row-acts\">{buttons}</div></li>")


def _definitions(view: ContextView) -> str:
    removed = [d for d in view.saved_context.definitions if view.sitting.definitions.get(d.key, 0) is None]
    rows = [_definition_row(view, d, False) for d in view.context.definitions]
    rows += [_definition_row(view, d, True) for d in removed]
    listed = f"<ul class=\"deflist\">{''.join(rows)}</ul>" if rows else _empty("No definitions yet.")
    inputs = view.inputs
    origin = (f"<input type=\"hidden\" name=\"def_from\" value=\"{e(inputs.get('def_from', ''))}\">"
              if inputs.get("def_from") else "")
    add = (f"<fieldset class=\"card add\"><legend>Add or change a definition</legend>{origin}"
           f"<label class=\"field\" for=\"def_term\"><span>Term</span></label>"
           f"<input type=\"text\" id=\"def_term\" name=\"def_term\" maxlength=\"80\" value=\"{e(inputs.get('def_term', ''))}\" "
           "placeholder=\"For example: Active customer\">"
           "<label class=\"field\" for=\"def_text\"><span>What it means, naming the tables and columns it uses</span>"
           f"</label><textarea id=\"def_text\" name=\"def_text\" rows=\"3\" maxlength=\"1000\">"
           f"{e(inputs.get('def_text', ''))}</textarea><div class=\"actions\"><button type=\"submit\" name=\"action\" "
           "value=\"define\">Add to this sitting</button></div><p class=\"small quiet\">A term that's already defined "
           "gets the new text.</p></fieldset>")
    return (f"<p class=\"muted lede\">Named terms the AI must use when a question mentions them.</p>{listed}{add}")


def _link_row(view: ContextView, k: Link, gone: bool) -> str:
    key = link_id(k)
    change = view.sitting.links.get(key) if key in view.sitting.links else None
    button = (f"<button type=\"submit\" name=\"keep_link\" value=\"{e(key)}\" class=\"quiet\">Keep it</button>" if gone
              else f"<button type=\"submit\" name=\"remove_link\" value=\"{e(key)}\" class=\"quiet\">Remove"
                   f"<span class=\"visually-hidden\"> {e(k.about)}</span></button>")
    return (f"<li class=\"def{' gone' if gone else ''}\"><h3>{_id(k.one.name)} joins {_id(k.other.name)}</h3><div>"
            f"<p>{e(k.text)}</p>{_join_path(k)}{_status(view, f'link {k.one.name} = {k.other.name}', change, gone)}"
            f"</div><div class=\"row-acts\">{button}</div></li>")


def _join_path(k: Link) -> str:
    """The link's cardinality and match counts (#131), as measured when it was saved."""
    if k.cardinality is None:
        return ("<p class=\"small quiet\">One-to-one or one-to-many is measured when you save, on what the AI gets."
                "</p>" if k.counts is None else "")
    parts = [f"{_chip(k.cardinality)}"]
    if k.counts:
        c = k.counts
        parts.append(f"{e(k.one.name)}: {c.one.rows} rows, {c.one.values} values · {e(k.other.name)}: "
                     f"{c.other.rows} rows, {c.other.values} values")
        if c.matching is not None:
            parts.append(f"{c.matching} value{'s' * (c.matching != 1)} match")
    else:
        parts.append("as drafted; it couldn't be measured")
    return f"<p class=\"small\">{' · '.join(parts)}</p>"


def _options(columns: Sequence[str], chosen: str, blank: str) -> str:
    return f"<option value=\"\">{e(blank)}</option>" + "".join(
        f"<option value=\"{e(c)}\"{' selected' if c == chosen else ''}>{e(c)}</option>" for c in columns)


def _links(view: ContextView) -> str:
    removed = [k for k in view.saved_context.links if view.sitting.links.get(link_id(k), 0) is None]
    rows = [_link_row(view, k, False) for k in view.context.links] + [_link_row(view, k, True) for k in removed]
    listed = f"<ul class=\"deflist\">{''.join(rows)}</ul>" if rows else _empty("No links yet.")
    columns = tuple(dict.fromkeys(g.name for g in view.profile.grants))
    i = view.inputs
    add = ("<fieldset class=\"card add\"><legend>Add a link</legend><div class=\"pair\">"
           f"<label class=\"field\"><span>This column</span><select name=\"link_one\">"
           f"{_options(columns, i.get('link_one', ''), 'Choose a column')}</select></label>"
           f"<label class=\"field\"><span>holds the same values as</span><select name=\"link_other\">"
           f"{_options(columns, i.get('link_other', ''), 'Choose a column')}</select></label></div>"
           "<label class=\"field\" for=\"link_text\"><span>How they match (optional)</span></label>"
           f"<input type=\"text\" id=\"link_text\" name=\"link_text\" maxlength=\"1000\" value=\"{e(i.get('link_text', ''))}\">"
           "<div class=\"actions\"><button type=\"submit\" name=\"action\" value=\"add_link\">Add to this sitting"
           "</button></div><p class=\"small quiet\">Both columns must be granted by the profile. Links to a column in "
           "another connection can be drafted by your AI (draft_link).</p></fieldset>")
    return (f"<p class=\"muted lede\">Columns of two tables that hold the same values, so the AI joins on exactly "
            f"those.</p>{listed}{add}")


# --- import and assumptions -----------------------------------------------------------------------------------------

def _reading(view: ContextView) -> str:
    found = view.reading
    if found is None:
        return ""
    rows = []
    for row in found.rows:
        keep = (f"<input type=\"checkbox\" name=\"keep:{e(row.key)}\" value=\"on\" checked "
                f"aria-label=\"Take {e(row.name)}\">" if not row.problem else
                f"<span class=\"small\">{_chip('Can not be taken', 'err')} {e(row.problem)}.</span>")
        rows.append(f"<tr><td>{e(row.name)}</td><td>{_id(row.about)}</td><td>{e(row.text)}</td><td>{keep}</td></tr>")
    for row in found.missed:
        pick = (f"<select name=\"map:{e(row.key)}\" aria-label=\"Map {e(row.name)}\">"
                f"{_options(found.columns, row.mapped, 'Leave out')}</select>")
        rows.append(f"<tr><td>{e(row.name)}</td><td>{pick}</td><td>{e(row.text)}</td>"
                    "<td><span class=\"small quiet\">Matched nothing granted</span></td></tr>")
    takeable = sum(not r.problem for r in found.rows)
    return (f"<p>From <b>{e(found.file)}</b>, {len(found.rows) + len(found.missed)} entries. Check how each one maps "
            "before anything joins the sitting.</p><div class=\"scroll-x\" role=\"region\" aria-label=\"How the file "
            "maps\" tabindex=\"0\"><table class=\"map\"><thead><tr><th scope=\"col\">In the file</th>"
            "<th scope=\"col\">Maps to</th><th scope=\"col\">Meaning</th><th scope=\"col\">Take</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div><div class=\"actions\">"
            f"<button type=\"submit\" name=\"action\" value=\"import\" class=\"primary\">Take the ticked rows "
            f"({takeable})</button><button type=\"submit\" name=\"action\" value=\"read_file\">Look again with my "
            "mapping</button></div><p class=\"small quiet\">Entries naming a column this profile doesn't grant are "
            "never matched to it: the AI isn't told those columns exist.</p>")


def _import(view: ContextView) -> str:
    taken = "".join(f"<li>{_id(Meaning.from_stored(m).about)}: {e(m['meaning'])}</li>" for m in view.sitting.meanings)
    taken_html = (f"<div class=\"card\"><h3>Taken into this sitting</h3><ul class=\"taken\">{taken}</ul></div>"
                  if taken else "")
    path = e(view.reading.path if view.reading else view.inputs.get("dict_path", ""))
    ask = ("<div class=\"card add\"><label class=\"field\" for=\"dict_path\"><span>A data dictionary on this computer "
           "(.csv, .tsv, .xlsx, .docx, .md or .txt)</span></label><div class=\"inline\">"
           f"<input type=\"text\" id=\"dict_path\" name=\"dict_path\" value=\"{path}\" "
           "placeholder=\"~/Documents/Data dictionary.xlsx\"><button type=\"submit\" name=\"action\" "
           "value=\"read_file\">Read the file</button></div><p class=\"small quiet\">Portward reads it here; nothing "
           "from it leaves this computer, and nothing is used until you save.</p></div>")
    return f"{ask}{_reading(view)}{taken_html}"


def _assumed(view: ContextView) -> str:
    if not view.assumptions:
        return _empty("Nothing assumed yet. When no definition covers a question, the AI says what it assumed, "
                      "and it shows up here to turn into a definition.")
    rows = []
    for a in view.assumptions:
        becoming = view.sitting.assumptions.get(a.id)
        if a.defined_as:
            act = _chip(f"Became {a.defined_as}", "kept")
        elif becoming:
            act = _chip(f"Becomes {becoming} when you save", "saved")
        else:
            act = (f"<button type=\"submit\" name=\"assume\" value=\"{e(a.id)}\">Make it a definition"
                   f"<span class=\"visually-hidden\">: {e(a.text)}</span></button>")
        rows.append(f"<li class=\"def\"><h3>{e(a.text)}</h3><div><p class=\"small quiet\">{e(a.source)}, "
                    f"{e(when(a.created_at))}, <q>{e(a.question)}</q></p></div><div class=\"row-acts\">{act}</div></li>")
    return ("<p class=\"muted lede\">What the AI had to assume because no definition covered the question.</p>"
            f"<ul class=\"deflist\">{''.join(rows)}</ul>")


PANELS = {QUEUE: _queue, DEFINITIONS: _definitions, LINKS: _links, IMPORT: _import, ASSUMED: _assumed,
          TOLD: lambda view: told_html.told(view.told, view.profile)}


# --- the page -------------------------------------------------------------------------------------------------------

def _counts(view: ContextView) -> dict[str, int]:
    waiting = sum(d.id not in view.sitting.decisions for d in view.drafts)
    return {QUEUE: waiting, DEFINITIONS: len(view.context.definitions), LINKS: len(view.context.links),
            ASSUMED: sum(not a.defined_as for a in view.assumptions)}


def _tabs(view: ContextView) -> str:
    counts = _counts(view)
    buttons = "".join(
        f"<li><button type=\"submit\" name=\"to_tab\" value=\"{key}\" class=\"tab\""
        f"{' aria-current=\"page\"' if key == view.tab else ''}>{e(label)}"
        f"{f' <span class=\"n\">{counts[key]}</span>' if counts.get(key) else ''}</button></li>"
        for key, label in TABS)
    return f"<nav class=\"tabs\" aria-label=\"Kinds of context\"><ul>{buttons}</ul></nav>"


def _picker(chrome: Chrome, view: ContextView) -> str:
    if len(view.profiles) < 2:
        return ""
    options = "".join(f"<option value=\"{e(p.id)}\"{' selected' if p.id == view.profile.id else ''}>{e(p.name)} "
                      f"(v{p.version})</option>" for p in view.profiles)
    return (f"<form method=\"get\" action=\"{e(chrome.web.link(PATH))}\" class=\"inline picker\">"
            f"<label for=\"pick-profile\">Profile</label><select id=\"pick-profile\" name=\"id\">{options}</select>"
            "<button type=\"submit\">Open</button></form>")


def _savebar(chrome: Chrome, view: ContextView) -> str:
    v, n = view.profile.version, view.changes
    text = f"No changes since v{v}." if not n else f"{n} change{'s' * (n != 1)} since v{v}."
    if view.discards:
        text += f" {view.discards} draft{'s' * (view.discards != 1)} to discard."
    save = (f"<button type=\"submit\" name=\"action\" value=\"save\" class=\"primary\">Save as v{v + 1}</button>"
            if view.editable else "")
    return (f"<div class=\"savebar\"><p class=\"changes\" role=\"status\">{e(text)}</p><div class=\"actions\">"
            f"<a class=\"button-link\" href=\"{e(link_to(chrome, view.profile))}\">Discard changes</a>{save}</div></div>")


def editor(chrome: Chrome, view: ContextView) -> str:
    p = view.profile
    applied = (f"Applied to {', '.join(view.applied_to)}." if view.applied_to else
               "Not applied to any connection yet, so the AI is told none of this.")
    head = (f"<div class=\"ce-head\"><p class=\"crumbs\"><a href=\"{e(chrome.web.link('/profiles'))}\">Profiles</a> / "
            f"{e(p.name)}</p><div class=\"ce-top\"><div><h1>Context</h1><p class=\"lede\">What {e(p.name)}'s data "
            f"means, told to the AI with every answer. It only ever speaks about what the profile grants. "
            f"{e(applied)}</p></div>{_picker(chrome, view)}</div></div>")
    notes = said(view.said)
    if view.problem:
        notes += f"<p class=\"problem\" role=\"alert\">{e(view.problem)}</p>"
    if not view.editable:
        notes += ("<p class=\"problem\">This page was opened for the web routes: context can be read here but "
                  "changed only from the Portward page on this computer.</p>")
    fields = {"id": p.id, "version": str(p.version), "tab": view.tab, "sitting": view.sitting.dumps(),
              "draft": view.current.id if view.current and view.tab == QUEUE else ""}
    default = ("<button type=\"submit\" name=\"action\" value=\"stay\" class=\"visually-hidden\" tabindex=\"-1\">"
               "Keep editing</button>")
    panel = PANELS[view.tab](view)
    label = dict(TABS)[view.tab]
    inner = (f"{default}{_savebar(chrome, view)}{notes}{_tabs(view)}<section class=\"panel\" aria-labelledby=\"panel-h\">"
             f"<h2 id=\"panel-h\" class=\"visually-hidden\">{e(label)}</h2>{panel}</section>")
    return layout(chrome, f"{p.name}: context", head + form(chrome.web, PATH, fields, inner, "ce"), home=False)
