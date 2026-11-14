"""The setup page's HTML (#118): three steps down one column, in ONE form with no scripts. The kind picker shows the
database or file fields with CSS alone (:has), and every step's button posts the same form (setup_page reads
`action`). Every value is escaped; the password field is always drawn empty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from portward_free import modes
from portward_free.localweb import FORM_TOKEN_FIELD
from portward_free.page_html import Chrome, e, layout
from portward_free.setup import KIND_NAMES, KINDS, Checked, Saved, Where

PATH = "/connect"
MODE_HELP = {
    modes.RESULTS: (f"Claude runs its query and gets the rows straight away, up to {modes.AI_ROWS}, minus the "
                    "columns you keep from it. Under each answer you see what it got."),
    modes.PICK: ("Claude sees only column names. It gets rows when you tick them and put them in the chat "
                 "yourself."),
}
FIELD_LABELS = {"name": "Name", "host": "Host", "port": "Port", "database": "Database", "user": "Login",
                "path": "Where the file is"}
PLACEHOLDERS = {"name": "What you call it, like Shop database", "host": "db.example.internal", "port": "5432",
                "database": "shop", "user": "a login that can only read", "path": "/Users/you/Documents/shop.sqlite"}


@dataclass(frozen=True)
class SetupView:
    where: Where
    checked: Checked | None = None
    token: str = ""  # the pending setup, once checked
    problem: str = ""
    ticked: tuple[str, ...] | None = None  # the user's ticks after a refused save (None: the suggestions)
    mode: str = modes.RESULTS


def page(chrome: Chrome, view: SetupView) -> str:
    where = view.where
    title = f"Connect {where.name}" if where.name else "Connect your first data"
    problem = (f"<p class=\"problem setup-problem\" role=\"alert\">{e(view.problem)}</p>" if view.problem else "")
    steps = _where_step(view) + (_check_step(view.checked) + _keep_step(view) if view.checked else "")
    main = (f"<div class=\"setup\"><div class=\"setup-head\"><p class=\"eyebrow\">Set up a source</p>"
            f"<h1>{e(title)}</h1><p class=\"lede\">Portward reads your data on this computer and hands Claude only "
            "what you allow. It never writes to it.</p></div>"
            f"{problem}<form method=\"post\" action=\"{e(chrome.web.link(PATH))}\" autocomplete=\"off\">"
            f"<input type=\"hidden\" name=\"{FORM_TOKEN_FIELD}\" value=\"{e(chrome.web.form_token)}\">"
            f"<ol class=\"steps\">{steps}</ol></form></div>")
    return layout(chrome, f"{title} - Portward", main, home=False)


# --- step 1 -------------------------------------------------------------------------------------------------------

def _where_step(view: SetupView) -> str:
    where = view.where
    if view.checked is not None:  # done: what was typed, read only, and a way back
        hidden = "".join(f"<input type=\"hidden\" name=\"{k}\" value=\"{e(v)}\">" for k, v in where.fields().items())
        shown = [("Kind", KIND_NAMES[where.kind]), ("Name", where.name)]
        shown += ([("Server", f"{view.checked.connection.settings.get('host')}:"
                              f"{view.checked.connection.settings.get('port')}"),
                   ("Database", where.database), ("Login", where.user)] if where.is_database else
                  [("File", str(view.checked.connection.settings.get("path")))])
        rows = "".join(f"<div><dt>{e(k)}</dt><dd class=\"id\">{e(v)}</dd></div>" for k, v in shown)
        return (f"<li class=\"step done\"><h2><span class=\"num\" aria-hidden=\"true\">1</span>Where is it?</h2>"
                f"{hidden}<dl class=\"summary\">{rows}</dl>"
                "<button type=\"submit\" name=\"action\" value=\"edit\" class=\"quiet\">Change these</button></li>")
    kinds = "".join(f"<label><input type=\"radio\" name=\"kind\" value=\"{k}\"{' checked' if k == where.kind else ''}>"
                    f"{e(label)}</label>" for k, label in KINDS)
    db = "".join(_field(where, k) for k in ("host", "port", "database", "user"))
    return (f"<li class=\"step\"><h2><span class=\"num\" aria-hidden=\"true\">1</span>Where is it?</h2>"
            "<div class=\"locate\"><fieldset class=\"kinds\"><legend>Kind</legend>"
            f"<div class=\"seg\">{kinds}</div></fieldset>"
            f"<div class=\"grid2\">{_field(where, 'name', wide=True)}"
            f"<div class=\"db grid2 wide\">{db}"
            "<label class=\"field wide\"><span>Password</span>"
            "<input type=\"password\" name=\"password\" value=\"\" autocomplete=\"off\" "
            "aria-describedby=\"pw-help\">"
            "<span id=\"pw-help\" class=\"hint\">Kept in this computer's keychain once you save, not in Portward's "
            "files. Claude never sees it.</span></label></div>"
            f"<div class=\"file grid2 wide\">{_field(where, 'path', wide=True)}</div></div></div>"
            "<div class=\"finish\"><button type=\"submit\" name=\"action\" value=\"check\" class=\"primary\">"
            "Check the login</button><span class=\"hint\">Nothing is saved yet.</span></div></li>")


def _field(where: Where, key: str, *, wide: bool = False) -> str:
    kind = "number" if key == "port" else "text"
    extra = " inputmode=\"numeric\"" if key == "port" else ""
    return (f"<label class=\"field{' wide' if wide else ''}\"><span>{e(FIELD_LABELS[key])}</span>"
            f"<input type=\"{kind}\" name=\"{key}\" value=\"{e(getattr(where, key))}\" "
            f"placeholder=\"{e(PLACEHOLDERS[key])}\" autocomplete=\"off\" spellcheck=\"false\"{extra}></label>")


# --- step 2 -------------------------------------------------------------------------------------------------------

def _check_step(checked: Checked) -> str:
    lines = "".join(f"<li class=\"{'ok' if line.ok else 'warn'}\"><span class=\"mark-{'y' if line.ok else 'w'}\" "
                    f"aria-hidden=\"true\">{'✓' if line.ok else '!'}</span><span>{e(line.text)}</span></li>"
                    for line in checked.lines)
    replaces = (f"<p class=\"problem\">There is already a source named {e(checked.where.name)}. Saving points that "
                "name at this data instead.</p>" if checked.replaces else "")
    return (f"<li class=\"step done\"><h2><span class=\"num\" aria-hidden=\"true\">2</span>Check the login</h2>"
            f"<ul class=\"checks\">{lines}</ul>{replaces}</li>")


# --- step 3 -------------------------------------------------------------------------------------------------------

def _keep_step(view: SetupView) -> str:
    checked = view.checked
    assert checked is not None
    choices = "".join(
        f"<label class=\"mode\"><input type=\"radio\" name=\"mode\" value=\"{m}\"{' checked' if m == view.mode else ''}>"
        f"<span><strong>{e(modes.LABELS[m])}</strong><span class=\"hint\">{e(MODE_HELP[m])}</span></span></label>"
        for m in (modes.RESULTS, modes.PICK))
    ticked = set(view.ticked) if view.ticked is not None else {_value(t, c) for t, c in checked.suggested}
    tables = "".join(_table(checked, t.name, t.columns, ticked) for t in checked.tables) or (
        "<p class=\"empty\">No tables were found.</p>")
    suggested = len(checked.suggested)
    lead = (f"{suggested} column{' looks' if suggested == 1 else 's look'} personal, so "
            f"{'it is' if suggested == 1 else 'they are'} ticked for you. Check them before you save."
            if suggested else "No column names look personal. Tick any you want to keep from Claude.")
    return (f"<li class=\"step\"><h2><span class=\"num\" aria-hidden=\"true\">3</span>Choose how Claude works and "
            "what to keep from it</h2>"
            f"<input type=\"hidden\" name=\"setup\" value=\"{e(view.token)}\">"
            f"<fieldset class=\"modes\"><legend>How Claude works with {e(checked.where.name)}</legend>{choices}"
            "</fieldset>"
            "<fieldset class=\"keeps\"><legend>Keep from Claude</legend>"
            f"<p class=\"hint\">Claude gets every column you don't keep, and never a kept column's values. You still "
            f"see kept columns yourself. {e(lead)}</p><div class=\"tables\">{tables}</div></fieldset>"
            "<div class=\"finish\"><button type=\"submit\" name=\"action\" value=\"save\" class=\"primary\">"
            "Save and go back to the chat</button>"
            "<span class=\"hint\">Nothing is kept, saved or sent to Claude before you press this. You can change "
            "it any time.</span></div></li>")


def _value(table: str, column: str) -> str:
    return json.dumps([table, column])


def _table(checked: Checked, table: str, columns: tuple[str, ...], ticked: set[str]) -> str:
    rows, count = [], 0
    whole = any(r.table.lower() == table.lower() and r.column is None for r in checked.kept)
    for column in columns:
        value = _value(table, column)
        already = checked.kept_already(table, column)
        on = already or value in ticked
        count += on
        tag = ("<span class=\"tag kept-tag\">kept already</span>" if already else
               "<span class=\"tag look\">looks personal</span>" if (table, column) in checked.suggested else "")
        rows.append(f"<label class=\"check\"><input type=\"checkbox\" name=\"keep\" value=\"{e(value)}\""
                    f"{' checked' if on else ''}{' disabled' if already else ''}>"
                    f"<code class=\"id\">{e(column)}</code>{tag}</label>")
    status = ("whole table kept already" if whole else
              f"{count} of {len(columns)} kept" if count else "none kept")
    opened = " open" if count or len(checked.tables) <= 3 else ""
    return (f"<details{opened}><summary><code class=\"id\">{e(table)}</code><span class=\"ct\">{e(status)}</span>"
            f"</summary><div class=\"cols\">{''.join(rows)}</div></details>")


# --- saved ----------------------------------------------------------------------------------------------------------

def saved(chrome: Chrome, done: Saved) -> str:
    kept = (", ".join(f"{r.table}.{r.column}" if r.column else f"{r.table} (whole table)" for r in done.kept)
            if done.kept else "nothing")
    main = (f"<section class=\"setup done-page\" aria-labelledby=\"done\"><p class=\"eyebrow\">Saved</p>"
            f"<h1 id=\"done\">{e(done.name)} is connected</h1>"
            f"<dl class=\"summary\"><div><dt>How Claude works</dt><dd>{e(modes.LABELS[done.mode])}</dd></div>"
            f"<div><dt>Kept from Claude</dt><dd class=\"id\">{e(kept)}</dd></div></dl>"
            "<p>Go back to the chat and ask Claude something, like \"how many orders this month?\". You can change "
            f"what's kept any time on <a href=\"{e(chrome.web.link('/'))}#rules\">the overview</a>.</p></section>")
    return layout(chrome, f"{done.name} is connected - Portward", main, home=False)
