"""The Portward page: the user's own view of history, saved pulls, rules, shares, settings and what the AI was
sent.

Served by `localweb.LocalWeb` on this computer only; see there for what keeps other apps and websites out. The page
is rendered on the server with every value escaped (page_html), uses no scripts, and changes things only through
its own forms (POST, then a redirect back). It opens its own connection to the local database file, so it can run
on the page server's thread inside the MCP server, or alone from the `portward-free page` command.

Rules from the page keep #39's promise: blocking runs at once (it only narrows what the AI sees); unblocking asks
first, on a page of its own, and only a yes there removes the rule. Only the person at this browser can answer,
because the AI is never given the page's address. (An AI that can run commands on this computer, like Claude Code
with a shell, can do whatever the user can; no local page changes that.)
"""

from __future__ import annotations

import json
from dataclasses import replace
import urllib.parse
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from portward_free import kept, page_html, why_hidden_html
from portward_free.account import Accounts
from portward_free.ai_log import AiLog
from portward_free.context_drift import drifted_on
from portward_free.verified import changed_on
from portward_free.keychain import Keychain, KeychainError
from portward_free.localweb import LocalWeb, Request, Response
from portward_free.context_editor_page import ContextEditorPage
from portward_free.notes_page import NotesPage
from portward_free.profile_editor_page import ProfileEditorPage
from portward_free.rule_tools import Opener, named_rule
from portward_free.setup_page import SetupPage
from portward_free.rules import Rule
from portward_free.sources import SourceError, Table
from portward_free.storage import Connection, LocalStore
from portward_free.storage.store import now
from portward_free.usage import Usage
from portward_free.why_hidden import WhyHidden, why_hidden

HISTORY_SHOWN = 100
CONVERSATIONS_SHOWN = 100
SHARES_SHOWN = 100
STYLES = ("panel/tokens.css", "page.css", "profile_editor.css", "context_editor.css", "setup.css", "notes.css")
NOT_FOUND = Response.text(404, "Not found.")
SENT_PREFIX = "/sent/"


class PortwardPage:
    """The page's routes (a `localweb.App`)."""

    def __init__(self, database: Path, keychain: Keychain, opened: Opener,
                 usage: Callable[[LocalStore], Usage], *, accounts: Accounts | None = None,
                 profiles_editable: bool = True) -> None:
        """`usage` gives the usage counts on the page's own connection to the file (Usage.on_store, or
        Usage.from_env when the page runs alone). `accounts` checks the plan before a profile is saved (#126);
        `profiles_editable` False when the page is opened for the web routes, which can't change profiles."""
        self._store = LocalStore.open(database)
        self._keychain = keychain
        self._opened = opened
        self._ai_log = AiLog(self._store, keychain)
        self._usage = usage(self._store)
        self._profiles = ProfileEditorPage(self._store, keychain, opened, accounts, profiles_editable,
                                           self._catalog, self._catalog_of)
        self._setup = SetupPage(self._store, keychain)
        self._context = ContextEditorPage(self._store, keychain, opened, accounts, profiles_editable,
                                          self._catalog, self._catalog_of)
        self._notes = NotesPage(self._store, self._catalog, profiles_editable)  # #119; set_note is computer-only too

    def close(self) -> None:
        self._store.close()

    def __call__(self, request: Request, web: LocalWeb) -> Response:
        if self._setup.handles(request.path):
            chrome = page_html.Chrome(web, self._usage.team_link())
            if request.method == "GET":
                return self._setup.get(chrome, request.query)
            return self._setup.post(chrome, request.form, request.every("keep"))
        if self._profiles.handles(request.path):
            chrome = page_html.Chrome(web, self._usage.team_link())
            if request.method == "GET":
                return self._profiles.get(chrome, request.path, request.query)
            return self._profiles.post(chrome, request.path, request.form)
        if self._context.handles(request.path):
            chrome = page_html.Chrome(web, self._usage.team_link())
            if request.method == "GET":
                return self._context.get(chrome, request.query)
            return self._context.post(chrome, request.form)
        if self._notes.handles(request.path):
            chrome = page_html.Chrome(web, self._usage.team_link())
            if request.method == "GET":
                return self._notes.get(chrome, request.query)
            return self._notes.post(chrome, request.form)
        if request.method == "GET":
            return self._get(request, web)
        changes = {"/rules/block": self._block, "/rules/unblock": self._unblock,
                   "/settings/keep-rows": self._keep_rows, "/settings/usage-counts": self._usage_counts}
        change = changes.get(request.path)
        return change(web, request.form) if change is not None else NOT_FOUND

    def _get(self, request: Request, web: LocalWeb) -> Response:
        web_page = page_html.Chrome(web, self._usage.team_link())
        if request.path == "/":
            asked_now = self._usage.seen("page")  # the page shows the question: the user sees it here
            view = self._overview(request.query.get("said", ""))
            return Response(body=page_html.overview(web_page, replace(view, usage_asked_now=asked_now)))
        if request.path == "/page.css":
            return Response(body=_styles(), content_type="text/css; charset=utf-8")
        if request.path == why_hidden_html.PATH:
            return Response(body=self._why_hidden(web_page, request.query.get("source", ""),
                                                  request.query.get("about", "")))
        if request.path.startswith(SENT_PREFIX):
            chat = self._store.conversation(request.path.removeprefix(SENT_PREFIX))
            if chat is None:
                return NOT_FOUND
            return Response(body=page_html.conversation(web_page, chat, self._ai_log.responses(chat.id)))
        return NOT_FOUND

    # changes

    def _block(self, web: LocalWeb, form: dict[str, str]) -> Response:
        connection = self._store.connection(form.get("source", ""))
        if connection is None:
            return _back(web, "That source isn't connected any more.", "rules")
        try:
            table, column = json.loads(form.get("target", ""))
        except (ValueError, TypeError):
            return _back(web, "Choose a table or column to block.", "rules")
        try:
            with self._opened(connection) as opened:
                rule = named_rule(connection.name, opened.tables(), str(table), column and str(column))
        except (SourceError, ValueError) as err:
            return _back(web, f"Nothing was blocked: {err}.", "rules")
        added = self._store.add_rule(rule)
        said = (f"Blocked {rule.describe()} in {rule.source}: it is never sent to the AI." if added else
                f"{rule.describe()} in {rule.source} was already blocked.")
        return _back(web, said, "rules")

    def _unblock(self, web: LocalWeb, form: dict[str, str]) -> Response:
        wanted = Rule(form.get("source", ""), form.get("table", ""), form.get("column") or None)
        rule = next((r for r in self._store.rules(wanted.source) if _same(r, wanted)), None)
        if rule is None:
            return _back(web, "That rule isn't there any more.", "rules")
        answer = form.get("confirm")
        if answer is None:
            return Response(body=page_html.confirm_unblock(page_html.Chrome(web, self._usage.team_link()), rule))
        if answer != "yes":
            return _back(web, f"{rule.describe()} in {rule.source} stays blocked.", "rules")
        self._store.remove_rule(rule)
        return _back(web, f"{rule.describe()} in {rule.source} is no longer blocked: the AI can read it again.",
                     "rules")

    def _keep_rows(self, web: LocalWeb, form: dict[str, str]) -> Response:
        on = form.get("keep_rows") == "on"
        if on:
            try:
                kept.key(self._keychain, create=True)  # make the key first, so a keychain problem shows here
            except (KeychainError, kept.RowsLocked) as err:
                return _back(web, f"Rows can't be kept: {err}.", "settings")
        self._store.set_setting(kept.KEEP_ROWS_SETTING, "on" if on else "off")
        said = (f"Rows of new pulls are kept, encrypted, for {kept.KEEP_DAYS} days." if on else
                f"Rows of new pulls won't be kept. Rows kept already are removed when {kept.KEEP_DAYS} days old.")
        return _back(web, said, "settings")

    def _usage_counts(self, web: LocalWeb, form: dict[str, str]) -> Response:
        try:
            self._usage.set(form.get("usage_counts") == "on", by_user=True)
        except ValueError as err:
            return _back(web, f"Nothing changed: {err}.", "settings")
        said = ("Anonymous usage counts are on." if self._usage.on else
                "Anonymous usage counts are off, and the counts kept on this computer are deleted.")
        return _back(web, said, "settings")

    # reading

    def _overview(self, said: str) -> page_html.Overview:
        sources = tuple(self._source_rules(c) for c in self._store.connections())
        return page_html.Overview(
            pulls=self._store.recent_pulls(HISTORY_SHOWN), saved=self._store.saved_pulls(), sources=sources,
            conversations=self._store.conversations(CONVERSATIONS_SHOWN),
            keep_rows=self._store.setting(kept.KEEP_ROWS_SETTING) == "on", keep_days=kept.KEEP_DAYS,
            data_folder=str(self._store.path), usage=self._usage.state, usage_off_why=self._usage.disabled,
            said=said, shares=tuple(page_html.Shared(s, self._store.pull(s.pull_id) if s.pull_id else None)
                                    for s in self._store.shares(SHARES_SHOWN)), now=now())

    def _source_rules(self, connection: Connection) -> page_html.SourceRules:
        catalog = self._catalog(connection)
        held_back = len(drifted_on(self._store, connection.name, catalog)) + len(
            changed_on(self._store, connection.name)) if isinstance(catalog, tuple) else 0
        return page_html.SourceRules(connection, self._store.rules(connection.name), catalog,
                                     self._store.profiles.on_profiles(connection.name), held_back,
                                     len(self._store.notes(connection.name)))

    def _why_hidden(self, chrome: page_html.Chrome, source: str, about: str) -> str:
        """Why things are hidden on `source` (every connection on profiles when it names none); the user's only."""
        wanted = [c for c in self._store.connections() if self._store.profiles.on_profiles(c.name)
                  and (not source or c.name == source)]
        found: list[WhyHidden] = []
        problems = []
        for connection in wanted:
            catalog = self._catalog(connection)
            if isinstance(catalog, str):
                problems.append(f"{connection.name}: its tables can't be listed right now: {catalog}")
                continue
            found.append(why_hidden(self._store, connection.name, catalog, self._catalog_of))
        if source and not wanted:
            problems.append(f"{source} isn't a connection on profiles.")
        return why_hidden_html.page(chrome, found, about.strip(), " ".join(problems))

    def _catalog_of(self, name: str) -> tuple[Table, ...] | None:
        connection = self._store.connection(name)
        catalog = self._catalog(connection) if connection is not None else None
        return catalog if isinstance(catalog, tuple) else None

    def _catalog(self, connection: Connection) -> tuple[Table, ...] | str:
        try:
            with self._opened(connection) as opened:
                return opened.tables()
        except SourceError as err:
            return str(err)


def _same(a: Rule, b: Rule) -> bool:
    return (a.source == b.source and a.table.lower() == b.table.lower()
            and (a.column or "").lower() == (b.column or "").lower())


def _back(web: LocalWeb, said: str, section: str) -> Response:
    return Response.redirect(f"{web.link('/')}?{urllib.parse.urlencode({'said': said})}#{section}")


def _styles() -> str:
    root = files("portward_free")
    return "\n".join(root.joinpath(name).read_text(encoding="utf-8") for name in STYLES)
