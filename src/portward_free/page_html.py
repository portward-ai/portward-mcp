"""The Portward page's HTML: pure functions from what the store holds to markup, every value escaped.

The page reads like a ledger of what Portward did on this computer: a tally that doubles as the menu, then pulls,
saved pulls, rules, what the AI was sent, and settings. No scripts: expanding a pull's SQL is a <details>, and every
change is a small form.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from portward_free import __version__
from portward_free.ai_log import AiResponse
from portward_free.localweb import FORM_TOKEN_FIELD, LocalWeb
from portward_free.rules import Rule
from portward_free.sources import Table
from portward_free.storage import Connection, Conversation, PullRecord, ShareRecord
from portward_free.usage import NEVER, QUESTION


@dataclass(frozen=True)
class Chrome:
    """What every page shares: the page server (for links and forms) and the team link (usage.team_link: counted
    through the usage service only while usage counts are on)."""

    web: LocalWeb
    team_link: str

TITLE = "Portward"


def e(value: object) -> str:
    return escape(str(value), quote=True)


@dataclass(frozen=True)
class SourceRules:
    connection: Connection
    rules: tuple[Rule, ...]
    catalog: tuple[Table, ...] | str  # its tables, or why they can't be listed
    on_profiles: bool = False  # runs on profiles (Personal): the card links to "Why is this hidden?"
    held_back: int = 0  # context items naming a column that's gone (#123) or with a changed result (#130)
    notes: int = 0  # the user's notes on its tables and columns (#119: written on the notes page)


@dataclass(frozen=True)
class Shared:
    share: ShareRecord
    pull: PullRecord | None  # the pull it was made from, if it's still in history


@dataclass(frozen=True)
class Overview:
    pulls: tuple[PullRecord, ...]
    saved: tuple[PullRecord, ...]
    sources: tuple[SourceRules, ...]
    conversations: tuple[tuple[Conversation, int], ...]
    keep_rows: bool
    keep_days: int
    data_folder: str
    usage: str = "disabled"  # usage.Usage.state
    usage_off_why: str | None = None
    usage_asked_now: bool = False  # this view is the first time the user sees the usage question
    said: str = ""
    shares: tuple[Shared, ...] = ()
    now: str = ""  # ISO time, to tell expired shares apart


# --- the whole page ----------------------------------------------------------------------------------------------

def layout(chrome: Chrome, title: str, main: str, *, home: bool = True) -> str:
    web = chrome.web
    back = "" if home else f"<a class=\"back\" href=\"{e(web.link('/'))}\">Back to the overview</a>"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"same-origin\"><meta name=\"color-scheme\" content=\"light dark\">"
        f"<title>{e(title)}</title><link rel=\"stylesheet\" href=\"{e(web.link('/page.css'))}\"></head>"
        "<body><a class=\"skip\" href=\"#main\">Skip to the content</a>"
        "<header class=\"masthead\"><div class=\"wrap\"><p class=\"mark\">Portward</p>"
        "<p class=\"where\"><span class=\"dot\" aria-hidden=\"true\"></span>On this computer only</p>"
        f"{back}</div></header>"
        f"<main id=\"main\" class=\"wrap\" tabindex=\"-1\">{main}</main>"
        "<footer class=\"foot\"><div class=\"wrap\"><p>This page is served from this computer and only opens "
        "from Portward. "
        "Close the tab when you're done.</p>"
        f"<p class=\"team\"><a href=\"{e(chrome.team_link)}\" rel=\"noreferrer\">Need this for your team?</a></p>"
        f"<p class=\"version\">Portward {e(__version__)}</p></div></footer></body></html>")


def overview(chrome: Chrome, view: Overview) -> str:
    web = chrome.web
    blocked = sum(len(s.rules) for s in view.sources)
    tally = [("pulls", "Pulls", len(view.pulls)), ("saved", "Saved", len(view.saved)),
             ("rules", "Blocked", blocked), ("shares", "Shared", len(view.shares)),
             ("sent", "Conversations", len(view.conversations)),
             ("settings", "Keeping rows", "On" if view.keep_rows else "Off")]
    items = "".join(f"<li><a href=\"#{key}\"><span class=\"n\">{n}</span>"
                    f"<span class=\"label\">{label}</span></a></li>" for key, label, n in tally)
    intro = ("<div class=\"intro\"><h1>What Portward did here</h1>"
             "<p class=\"lede\">Every pull, the rules that keep tables and columns from the AI, who you shared "
             "with, and exactly what the AI was sent.</p></div>"
             f"<nav aria-label=\"Sections\" class=\"tally\"><ul>{items}</ul></nav>")
    return layout(chrome, TITLE, intro + said(view.said) + "".join((
        history(view.pulls), saved(view.saved), rules(web, view.sources), shares(view.shares, view.now),
        conversations(web, view.conversations), settings(web, view))))


def said(text: str) -> str:
    return f"<p class=\"said\" role=\"status\">{e(text)}</p>" if text else ""


def _section(key: str, title: str, lede: str, body: str) -> str:
    return (f"<section class=\"part\" aria-labelledby=\"{key}\"><div class=\"part-head\">"
            f"<h2 id=\"{key}\">{e(title)}</h2><p class=\"lede\">{e(lede)}</p></div>"
            f"<div class=\"part-body\">{body}</div></section>")


def _empty(text: str) -> str:
    return f"<p class=\"empty\">{e(text)}</p>"


def when(iso: str) -> str:
    """2026-11-03T14:02:11.123+00:00 -> 2026-11-03 14:02 UTC."""
    return f"{iso[:10]} {iso[11:16]} UTC" if len(iso) >= 16 else iso


def _time(iso: str) -> str:
    return f"<time datetime=\"{e(iso)}\">{e(when(iso))}</time>"


# --- pulls --------------------------------------------------------------------------------------------------------

def _pull(p: PullRecord) -> str:
    tags = []
    if p.saved_name:
        tags.append(f"<span class=\"tag saved\">Saved as {e(p.saved_name)}</span>")
    if p.parent_id:
        tags.append(f"<span class=\"tag\">Tweak of <span class=\"id\">{e(p.parent_id[:8])}</span></span>")
    if p.keep_rows:
        tags.append("<span class=\"tag kept\">Rows kept</span>")
    rows = "row" if p.row_count == 1 else "rows"
    return (f"<li class=\"entry\"><p class=\"when\">{_time(p.created_at)}</p>"
            f"<div class=\"what\"><p class=\"question\">{e(p.question)}</p>"
            f"<p class=\"meta\"><span class=\"id\">{e(p.source)}</span><span>{', '.join(map(e, p.tables))}</span>"
            f"<span class=\"id\" title=\"Pull id\">{e(p.id[:8])}</span></p>{_tags(tags)}"
            f"<details><summary>SQL</summary><pre class=\"sql\"><code>{e(p.sql)}</code></pre></details></div>"
            f"<p class=\"count\"><span class=\"n\">{p.row_count}</span> {rows}</p></li>")


def _tags(tags: Sequence[str]) -> str:
    return f"<p class=\"tags\">{''.join(tags)}</p>" if tags else ""


def history(pulls: Sequence[PullRecord]) -> str:
    body = (f"<ol class=\"ledger\">{''.join(map(_pull, pulls))}</ol>" if pulls else
            _empty("No pulls yet. Ask your AI a question about your data and each pull shows up here."))
    return _section("pulls", "Pulls", "Newest first: the question, the SQL that ran and how many rows came back. "
                    "Rows themselves aren't stored unless you keep them.", body)


def saved(entries: Sequence[PullRecord]) -> str:
    items = "".join(f"<li class=\"entry\"><p class=\"when\">{_time(p.created_at)}</p><div class=\"what\">"
                    f"<p class=\"question\">{e(p.saved_name)}</p><p class=\"meta\"><span>{e(p.question)}</span>"
                    f"<span class=\"id\">{e(p.source)}</span></p></div>"
                    f"<p class=\"count\"><span class=\"n\">{p.row_count}</span> rows</p></li>" for p in entries)
    body = (f"<ol class=\"ledger\">{items}</ol>" if entries else
            _empty("Nothing saved yet. Ask your AI to save a pull under a name, and run it again by that name."))
    return _section("saved", "Saved pulls", "Pulls you named. Ask your AI to run one by its name.", body)


# --- rules --------------------------------------------------------------------------------------------------------

def _hidden(fields: dict[str, str | None]) -> str:
    return "".join(f"<input type=\"hidden\" name=\"{e(k)}\" value=\"{e(v)}\">" for k, v in fields.items()
                   if v is not None)


def form(web: LocalWeb, action: str, fields: dict[str, str | None], inner: str, css: str = "") -> str:
    kind = f" class=\"{css}\"" if css else ""
    return (f"<form method=\"post\" action=\"{e(web.link(action))}\"{kind}>"
            f"{_hidden({FORM_TOKEN_FIELD: web.form_token, **fields})}{inner}</form>")


def rule_fields(rule: Rule) -> dict[str, str | None]:
    return {"source": rule.source, "table": rule.table, "column": rule.column}


def _rule(web: LocalWeb, rule: Rule) -> str:
    what = (f"<span class=\"id\">{e(rule.table)}.<strong>{e(rule.column)}</strong></span>" if rule.column else
            f"<span class=\"id\"><strong>{e(rule.table)}</strong></span> <span class=\"quiet\">whole table</span>")
    button = (f"<button type=\"submit\" class=\"quiet\" aria-label=\"Unblock {e(rule.describe())} in "
              f"{e(rule.source)}\">Unblock</button>")
    return f"<li><span class=\"lock\" aria-hidden=\"true\"></span>{what}" \
           f"{form(web, '/rules/unblock', rule_fields(rule), button)}</li>"


def _block_form(web: LocalWeb, source: SourceRules, place: int) -> str:
    if isinstance(source.catalog, str):
        return f"<p class=\"problem\">Its tables can't be listed right now: {e(source.catalog)}</p>"
    taken = {(r.table.lower(), (r.column or "").lower()) for r in source.rules}
    groups = []
    for table in source.catalog:
        choices = [(None, f"Whole table {table.name}")] + [(c, f"{table.name}.{c}") for c in table.columns]
        shown = "".join(f"<option value=\"{e(json.dumps([table.name, c]))}\">{e(text)}</option>"
                        for c, text in choices if (table.name.lower(), (c or "").lower()) not in taken)
        if shown:
            groups.append(f"<optgroup label=\"{e(table.name)}\">{shown}</optgroup>")
    if not groups:
        return ""
    field = f"block-{place}"
    inner = (f"<label for=\"{field}\">Block a table or column</label><div class=\"inline\">"
             f"<select id=\"{field}\" name=\"target\">{''.join(groups)}</select>"
             f"<button type=\"submit\" class=\"primary\">Block</button></div>")
    return form(web, "/rules/block", {"source": source.connection.name}, inner, "block")


def rules(web: LocalWeb, sources: Sequence[SourceRules]) -> str:
    cards = []
    for place, source in enumerate(sources):
        if source.on_profiles:
            cards.append(_profile_card(web, source))
            continue
        listed = (f"<ul class=\"rules\">{''.join(_rule(web, r) for r in source.rules)}</ul>" if source.rules else
                  "<p class=\"quiet\">Nothing blocked: the AI may read every table here.</p>")
        cards.append(f"<article class=\"card\"><h3><span class=\"id\">{e(source.connection.name)}</span> "
                     f"<span class=\"kind\">{e(source.connection.kind)}</span></h3>{listed}"
                     f"{_block_form(web, source, place)}{_notes_link(web, source)}</article>")
    connect = f"<p><a class=\"connect\" href=\"{e(web.link('/connect'))}\">Connect a database or file</a></p>"
    body = connect + ("".join(cards) or _empty("No sources are connected yet. Connect one here, or ask your AI to "
                                               "connect a database or a file."))
    return _section("rules", "Rules", "Blocked tables and columns are never sent to the AI, whatever it asks. "
                    "Blocking works at once; unblocking asks you first.", body)


def _notes_link(web: LocalWeb, source: SourceRules) -> str:
    href = f"{web.link('/notes')}?{urllib.parse.urlencode({'source': source.connection.name})}"
    count = (f" ({source.notes} note{'' if source.notes == 1 else 's'})" if source.notes else "")
    return f"<p><a href=\"{e(href)}\">Notes: what its tables and columns mean</a>{count}</p>"


def _profile_card(web: LocalWeb, source: SourceRules) -> str:
    href = f"{web.link('/hidden')}?{urllib.parse.urlencode({'source': source.connection.name})}"
    return (f"<article class=\"card\"><h3><span class=\"id\">{e(source.connection.name)}</span> "
            f"<span class=\"kind\">{e(source.connection.kind)}</span></h3>"
            "<p class=\"quiet\">Runs on profiles: the AI sees only what the applied profiles grant.</p>"
            f"{_held_back(source.held_back)}"
            f"<p><a href=\"{e(href)}\">Why is something hidden from the AI?</a></p>"
            f"<p><a href=\"{e(web.link('/profiles'))}\">Edit profiles</a></p>"
            f"<p><a href=\"{e(web.link('/context'))}\">Context: what the data means</a></p></article>")


def _held_back(count: int) -> str:
    if not count:
        return ""
    return (f"<p class=\"problem\">{count} context item{'' if count == 1 else 's'} held back: a column "
            f"{'it names' if count == 1 else 'they name'} is gone, so the AI isn't told. "
            f"Follow the link below to see what to reword.</p>")


def confirm_unblock(chrome: Chrome, rule: Rule) -> str:
    web = chrome.web
    question = f"Let the AI read {rule.describe()} in {rule.source} again?"
    keep = form(web, "/rules/unblock", {**rule_fields(rule), "confirm": "no"},
                "<button type=\"submit\" class=\"primary\">Keep it blocked</button>")
    yes = form(web, "/rules/unblock", {**rule_fields(rule), "confirm": "yes"},
               "<button type=\"submit\" class=\"danger\">Yes, unblock it</button>")
    main = (f"<section class=\"ask\" aria-labelledby=\"ask\"><p class=\"eyebrow\">Unblock</p>"
            f"<h1 id=\"ask\">{e(question)}</h1>"
            f"<p>It is blocked now. Once unblocked, the AI can read it in any query it writes. Only you can answer "
            f"this: the AI can't reach this page.</p><div class=\"actions\">{keep}{yes}</div></section>")
    return layout(chrome, "Unblock?", main, home=False)


# --- shares -------------------------------------------------------------------------------------------------------

def _share(entry: Shared, now: str) -> str:
    share, pull = entry.share, entry.pull
    expired = bool(now) and share.expires_at <= now
    ends = (f"<span class=\"tag\">Expired {_time(share.expires_at)}</span>" if expired else
            f"<span class=\"tag saved\">Expires {_time(share.expires_at)}</span>")
    what = (f"<span>{e(pull.saved_name or pull.question)}</span>" if pull is not None else
            "<span>A pull no longer in history</span>")
    pull_id = f"<span class=\"id\" title=\"Pull id\">{e(share.pull_id[:8])}</span>" if share.pull_id else ""
    rows = "row" if share.row_count == 1 else "rows"
    return (f"<li class=\"entry\"><p class=\"when\">{_time(share.created_at)}</p>"
            f"<div class=\"what\"><p class=\"question\">{e(share.recipient)}</p>"
            f"<p class=\"meta\">{what}{pull_id}<span>{', '.join(map(e, share.columns))}</span></p>"
            f"{_tags([ends])}</div>"
            f"<p class=\"count\"><span class=\"n\">{share.row_count}</span> {rows}</p></li>")


def shares(entries: Sequence[Shared], now: str) -> str:
    body = (f"<ol class=\"ledger\">{''.join(_share(x, now) for x in entries)}</ol>" if entries else
            _empty("Nothing shared yet. Ask your AI to share a pull with a teammate by email."))
    return _section("shares", "Shared", "Newest first: who you shared a pull with, when, and when their link stops "
                    "working. The link itself isn't kept here, so it can't be shown.", body)


# --- what the AI was sent -------------------------------------------------------------------------------------------

def conversations(web: LocalWeb, listed: Sequence[tuple[Conversation, int]]) -> str:
    items = "".join(
        f"<li class=\"entry\"><p class=\"when\">{_time(c.started_at)}</p><div class=\"what\">"
        f"<p class=\"question\"><a href=\"{e(web.link(f'/sent/{c.id}'))}\">{e(c.client or 'An AI app')}</a></p>"
        f"<p class=\"meta\"><span class=\"id\">{e(c.id[:8])}</span></p></div>"
        f"<p class=\"count\"><span class=\"n\">{n}</span> {'reply' if n == 1 else 'replies'}</p></li>"
        for c, n in listed)
    body = (f"<ol class=\"ledger\">{items}</ol>" if listed else
            _empty("Nothing has been sent to an AI yet."))
    return _section("sent", "Sent to the AI", "One entry per run of your AI app. Open one to read every reply "
                    "Portward gave the AI, word for word. Kept for 30 days.", body)


def _response(r: AiResponse) -> str:
    error = " <span class=\"tag err\">Error</span>" if r.is_error else ""
    head = f"<h2><span class=\"id\">{e(r.tool)}</span>{error} {_time(r.created_at)}</h2>"
    if r.withheld:
        return f"<li class=\"reply\">{head}<p class=\"quiet\">What was sent {e(r.withheld)}.</p></li>"
    arguments = (f"<details><summary>What the AI asked</summary><pre><code>{e(json.dumps(r.arguments, indent=2))}"
                 f"</code></pre></details>" if r.arguments else "")
    return f"<li class=\"reply\">{head}{arguments}<pre class=\"sent\"><code>{e(r.text)}</code></pre></li>"


def conversation(chrome: Chrome, chat: Conversation, responses: Sequence[AiResponse]) -> str:
    body = (f"<ol class=\"replies\">{''.join(map(_response, responses))}</ol>" if responses else
            _empty("Nothing was sent to the AI in this conversation."))
    main = (f"<div class=\"intro\"><p class=\"eyebrow\">Sent to the AI</p><h1>{e(chat.client or 'An AI app')}, "
            f"{e(when(chat.started_at))}</h1><p class=\"lede\">Every reply Portward gave the AI in this "
            f"conversation, in order, exactly as it was sent.</p></div>{body}")
    return layout(chrome, f"Sent to the AI, {when(chat.started_at)}", main, home=False)


# --- settings -----------------------------------------------------------------------------------------------------

def _source_place(connection: Connection) -> str:
    settings: dict[str, Any] = connection.settings
    if "path" in settings:
        return str(settings["path"])
    host = f"{settings.get('host', '')}:{settings.get('port', '')}".strip(":")
    return " ".join(x for x in (f"{settings.get('user', '')}@{host}".strip("@"), settings.get("database", "")) if x)


def _usage_card(web: LocalWeb, view: Overview) -> str:
    if view.usage == "disabled":
        state = f"Anonymous usage counts are off on this computer ({view.usage_off_why or 'turned off'})."
        switch = ""
    elif view.usage == "on":
        state = (QUESTION if view.usage_asked_now else
                 "Anonymous usage counts are on: once a week, how many pulls and shares, which kinds of source "
                 "and AI app.")
        switch = form(web, "/settings/usage-counts", {"usage_counts": "off"},
                      "<button type=\"submit\">Turn usage counts off</button>")
    else:
        state = ("Anonymous usage counts are off." if view.usage == "off" else
                 "Anonymous usage counts aren't on yet.")
        switch = form(web, "/settings/usage-counts", {"usage_counts": "on"},
                      "<button type=\"submit\">Turn usage counts on</button>")
    return (f"<article class=\"card\"><h3>Usage counts</h3><p class=\"state\" "
            f"data-on=\"{str(view.usage == 'on').lower()}\">{e(state)}</p><p class=\"quiet\">{e(NEVER)}</p>"
            f"{switch}</article>")


def settings(web: LocalWeb, view: Overview) -> str:
    if view.keep_rows:
        state = f"Rows of new pulls are kept, encrypted on this computer, for {view.keep_days} days."
        toggle = form(web, "/settings/keep-rows", {"keep_rows": "off"},
                      "<button type=\"submit\">Stop keeping rows</button>")
    else:
        state = "Rows aren't kept: each pull stores its question, SQL and a fingerprint, never the rows."
        toggle = form(web, "/settings/keep-rows", {"keep_rows": "on"},
                      "<button type=\"submit\">Keep rows</button>")
    sources = "".join(f"<li><span class=\"id\">{e(c.name)}</span> <span class=\"kind\">{e(c.kind)}</span>"
                      f"<span class=\"place\">{e(_source_place(c))}</span></li>"
                      for c in (s.connection for s in view.sources))
    listed = f"<ul class=\"sources\">{sources}</ul>" if sources else _empty("None connected.")
    usage = _usage_card(web, view)
    body = (f"<article class=\"card\"><h3>Result rows</h3><p class=\"state\" data-on=\"{str(view.keep_rows).lower()}\">"
            f"{e(state)}</p>{toggle}</article>"
            f"<article class=\"card\"><h3>Sources</h3>"
            f"{listed}"
            f"<p class=\"quiet\">Database passwords are in this computer's keychain, never on this page.</p>"
            f"</article><article class=\"card\"><h3>Local file</h3><p class=\"id place\">{e(view.data_folder)}</p>"
            f"<p class=\"quiet\">History, rules, notes and settings live here. Nothing on this page leaves this "
            f"computer.</p></article>{usage}")
    return _section("settings", "Settings", "What Portward keeps, and where.", body)
