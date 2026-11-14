"""The tools that connect, test and remove sources. A database password is never handled here: the user types it
out of band (secret_entry), and it goes straight to the keychain."""

from __future__ import annotations

import shlex
import urllib.parse
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free import file_sources, login_sql, passwords, setup, setup_html, weekly_facts
from portward_free.clients import client_support
from portward_free.confirm import confirm_in_client
from portward_free.keychain import Keychain, KeychainError, account_for
from portward_free.local_page import local_page
from portward_free.replies import error, reply
from portward_free.rules import Rules
from portward_free.runtime import PASSWORD_PANEL_URI, Runtime
from portward_free.secret_entry import Texts
from portward_free.sources import NEEDS_PASSWORD, WRAP_KINDS, Source, SourceError, server_settings
from portward_free.storage import Connection, LocalStore

ADD_SOURCE = (
    "Connect a source; adding a name again replaces it. The file kinds take `path`, the file's location, and are "
    "added only once the user says yes themselves (Portward asks them): "
    "'sqlite' a SQLite file, 'csv' a CSV file (one table named after the file), 'excel' an .xlsx workbook (one "
    "table per sheet). 'postgres' and 'mysql' take host, port, database and user, never a password: the user "
    "types the password themselves where you can't see it, and it goes straight to the computer's keychain. "
    "Never ask for a password or connection string in the chat.")


def remove_connection(store: LocalStore, keychain: Keychain, name: str) -> None:
    """Delete the connection and its saved login (KeychainError, and nothing removed, if the keychain refuses)."""
    keychain.delete(account_for(name))
    store.remove_connection(name)


def remove_on_the_computer(name: str) -> str:
    """Over the web, removing a connection (and its saved password) is the user's to do at the computer: the same
    rule as typing a secret (secret_entry: a terminal on this computer only). A web AI can't do it by itself."""
    return (f"Nothing was removed. Removing a connection and its saved password can't be done from the web. Ask the "
            f"user to run this in a terminal on this computer; it asks them to type the source's name to confirm:"
            f"\n\n    {passwords.command(f'remove-source {shlex.quote(name)}')}")


def replace_on_the_computer(was: Connection) -> str:
    """Over the web, changing where a saved connection points (which forgets its saved password) is the user's to
    do at the computer, like removing it: a web AI can't move a name the user trusts to another server."""
    where = (f"{was.settings.get('host')}:{was.settings.get('port')}" if "host" in was.settings
             else str(was.settings.get("path") or was.kind))
    return (f"Nothing was changed. {was.name!r} is already a source ({where}), and changing where a saved "
            "connection points can't be done from the web. Use another name for the new connection, or ask the "
            "user to remove this one first in a terminal on this computer:"
            f"\n\n    {passwords.command(f'remove-source {shlex.quote(was.name)}')}")


def file_settings(path: str | None) -> dict[str, Any]:
    """A file source's stored settings: its path, absolute."""
    return {"path": str(Path(path).expanduser().resolve())} if path else {}


def warnings(source: Source, rules: Rules) -> list[str]:
    """The source's own warnings, and how the user's rules hold on it (a parent withheld, a ruled column gone)."""
    return [*source.warnings, *rules.notices(source.tables())]


def login_of(connection: Connection) -> str:
    """The connection's login name; a wrapped Snowflake connection signs in with a token and stores none, so its
    SQL is for the default matching login."""
    return str(connection.settings.get("user") or login_sql.login_name(connection.name))


def login_drift(connection: Connection, source: Source, rules: Rules,
                store: LocalStore | None = None) -> dict[str, Any]:
    """On a connection running on profiles: when its login reads more than they grant, the warning and SQL that
    tightens the login (#99). It never blocks; Portward hides the rest either way. With `store`, the counts are kept
    for the account page's weekly facts (#105)."""
    found = login_sql.drift(source, rules, str(connection.settings.get("database")), login_of(connection))
    if store is not None:
        weekly_facts.remember_drift(store, connection.name, found)
    return {"warnings": [found.warning], "login_sql": found.sql} if found else {"warnings": []}


def described(source: Source, rules: Rules, connection: Connection,
              store: LocalStore | None = None) -> dict[str, Any]:
    found: dict[str, Any] = {"tables": [t.name for t in rules.visible(source.tables())]}
    drift = login_drift(connection, source, rules, store)
    if said := [*warnings(source, rules), *drift.pop("warnings")]:
        found["warnings"] = said
    return {**found, **drift}


def no_source(name: str) -> CallToolResult:
    return error(f"no source named {name!r}; call list_sources")


async def ask_for_password(rt: Runtime, ctx: Context, connection: Connection,
                           replaces: str | None = None) -> CallToolResult | InputRequiredResult:
    """Ask the user for `connection`'s password out of band; the reply tells the AI where it was asked. `replaces`
    (passwords.pointed_elsewhere): where the name pointed until now, shown to the user where they type."""
    name = connection.name
    payload = {"source": name, "kind": connection.kind, **connection.settings, "password_needed": True}
    lead = f"Saved the settings for {name!r}; the password isn't set yet."
    if replaces:
        payload["replaces"] = replaces
        lead += (f" {name!r} pointed at {replaces} until now and points at {connection.settings.get('host')}:"
                 f"{connection.settings.get('port')} instead; tell the user so.")
    texts = Texts(what=f"the password for {name!r}", then=f"call test_source with name {name!r}",
                  command=passwords.terminal_command(name),
                  does="tests the connection and saves the password in the keychain", lead=lead)
    return await rt.out_of_band.ask(ctx, passwords.password_ask(connection, rt.keychain, replaces), payload, texts,
                                    "password_entry")


async def _add_database(rt: Runtime, ctx: Context, name: str, kind: str,
                        fields: dict[str, Any]) -> CallToolResult | InputRequiredResult:
    try:
        settings = server_settings(kind, fields)
    except SourceError as err:
        return error(str(err))
    connection = Connection(name, kind, settings)
    was = rt.store.connection(name)
    if rt.local_only and was is not None and was != connection:  # over the web: as remove_source, at the computer
        return error(replace_on_the_computer(was))
    replaces = passwords.pointed_elsewhere(was, connection)
    if was != connection:  # the same settings again keep their saved password; only the ask is renewed
        try:
            rt.keychain.delete(account_for(name))  # never send an old password to new settings
        except KeychainError as err:
            return error(str(err))
        rt.out_of_band.close(account_for(name))
        rt.store.add_connection(name, kind, settings)
    return await ask_for_password(rt, ctx, connection, replaces)


async def _confirmed_in_client(rt: Runtime, ctx: Context, connection: Connection,
                               found: file_sources.FileAdded) -> CallToolResult | InputRequiredResult:
    answer = await confirm_in_client(ctx, file_sources.question(connection), file_sources.CONFIRM_KEY,
                                     file_sources.confirm_state(connection))
    if isinstance(answer, InputRequiredResult):
        return answer
    if not answer:
        return error(f"the user didn't let the AI read {found.path}; nothing was added")
    try:
        file_sources.save(rt.store, rt.keychain, connection)
    except KeychainError as err:
        return error(str(err))
    return reply(found.payload(), found.summary())


async def _add_file(rt: Runtime, ctx: Context, name: str, kind: str,
                    path: str | None) -> CallToolResult | InputRequiredResult:
    """Add a file only on the user's own yes (file_sources): the AI could otherwise point a name at any file."""
    connection = Connection(name, kind, file_settings(path))
    found = file_sources.open_problem(rt.opened, connection, rt.store.rules_for(name))
    if isinstance(found, str):
        return error(found)
    if rt.store.connection(name) == connection:  # already added, as it is: nothing new for the AI to read
        return reply(found.payload(), found.summary())
    if client_support(ctx).elicitation:
        return await _confirmed_in_client(rt, ctx, connection, found)
    texts = Texts(what=f"letting the AI read {found.path} as {name!r}", then="call list_sources",
                  command=file_sources.terminal_command(connection), does="adds the file as a source",
                  lead=f"{found.path} isn't added yet: the user says yes first.", button="Allow")
    payload = {"source": name, "kind": kind, "path": found.path, "confirmation_needed": True}
    ask = file_sources.file_ask(rt.store, rt.keychain, connection, found)
    return await rt.out_of_band.ask(ctx, ask, payload, texts, "confirm_entry")


ADDED_FROM_CHAT = ("sqlite", "csv", "excel", "postgres", "mysql")  # the kinds add_source itself can add
WRAP_PREFIX = "wrap-"
WRAPPED = {"snowflake": "Snowflake", "postgres": "a Postgres MCP server"}  # by provider (wrap/providers.py)


def not_added_from_chat(name: str, kind: str) -> str | None:
    """Why add_source can't add `kind`, and the next step, for the AI to pass on; None for a kind it can add.

    A wrapped server runs a command, so only the user sets one up, at a terminal: the reply gives that command with
    the source's name in it. Any other kind gets what Portward reads here and the way round (an export), never the
    bare list of kind names."""
    asked = kind.strip().lower()
    if asked in ADDED_FROM_CHAT:
        return None
    provider = asked.removeprefix(WRAP_PREFIX)
    if f"{WRAP_PREFIX}{provider}" in WRAP_KINDS and (asked.startswith(WRAP_PREFIX) or provider == "snowflake"):
        run = passwords.command(f"wrap-add {shlex.quote(name)} --provider {provider}")
        return (f"{WRAPPED.get(provider, provider)} isn't added from the chat: Portward goes in front of its MCP "
                "server (Portward Personal), and because that runs a command, the user sets it up themselves in a "
                f"terminal on this computer. Ask them to run:\n\n    {run} --help\n\nIt shows what to give "
                f"`{run}` (the server's URL, or the command that starts it); the login is asked for there, hidden, "
                "and kept in the keychain. Never ask for a token or password in the chat.")
    return (f"Portward on this computer can't connect to {kind.strip()!r} directly. It reads: "
            f"{', '.join(ADDED_FROM_CHAT)}. The way round: the user exports the table as a CSV or Excel file and "
            "adds that file (kind 'csv' or 'excel').")


def setup_on_page(rt: Runtime, asked: dict[str, Any]) -> CallToolResult | None:
    """Open the setup page (#118) in the user's browser on this computer, filled in with what the AI asked for; the
    user checks the login, chooses the mode and the columns to keep, and saves there. None when it can't open (no
    browser, or the AI reaches this server over the web): the other ways to ask then apply. Nothing is stored here."""
    if rt.local_only or asked["kind"] not in setup.KIND_NAMES:
        return None
    query = {k: "" if v is None else str(v) for k, v in asked.items()}
    if query["path"]:
        query["path"] = file_settings(query["path"])["path"]
    url = f"{local_page(rt).url(setup_html.PATH)}?{urllib.parse.urlencode(query)}"
    if not rt.browser(url):
        return None
    name = asked["name"]
    return reply({"source": name, "kind": asked["kind"], "setup_entry": "page"}, (
        f"{name!r} isn't connected yet. A Portward page opened in the user's browser on this computer, where they "
        "check where the data is and the login (any password is typed there; you never see it), then choose how "
        "Claude works with this source and which columns to keep from Claude. Nothing is connected until they "
        f"press Save there. When they say it's done, call list_sources."))


def register_source_panel_tools(rt: Runtime) -> None:
    """add_source and update_password, bound to the password panel (where the user types a password)."""
    rt.out_of_band.register(passwords.PASSWORD_ASK, passwords.password_ask_builder(rt.keychain))
    rt.out_of_band.register(file_sources.FILE_ASK, file_sources.file_ask_builder(rt.store, rt.keychain))

    @rt.apps.tool(resource_uri=PASSWORD_PANEL_URI, name="add_source", description=ADD_SOURCE)
    async def add_source(name: str, kind: str, ctx: Context, path: str | None = None, host: str | None = None,
                         port: int | str | None = None, database: str | None = None, user: str | None = None
                         ) -> CallToolResult | InputRequiredResult:
        if (not_here := not_added_from_chat(name, kind)) is not None:
            return error(not_here)
        asked = {"kind": kind, "name": name, "path": path, "host": host, "port": port, "database": database,
                 "user": user}
        if (on_page := setup_on_page(rt, asked)) is not None:
            return on_page
        if kind in NEEDS_PASSWORD:
            fields = {"host": host, "port": port, "database": database, "user": user}
            return await _add_database(rt, ctx, name, kind, fields)
        return await _add_file(rt, ctx, name, kind, path)

    @rt.apps.tool(resource_uri=PASSWORD_PANEL_URI, name="update_password", description=(
        "Have the user enter a new password for a database source (when it changed, or test_source says it's "
        "missing or wrong). Never takes the password: the user types it where you can't see it. The old password "
        "stays until the new one works."))
    async def update_password(name: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        connection = rt.store.connection(name)
        if connection is None:
            return no_source(name)
        try:
            passwords.needs_password(connection)
        except passwords.PasswordRefused as err:
            return error(str(err))
        return await ask_for_password(rt, ctx, connection)


def matching_login(rt: Runtime, source: str, login: str | None, current_login: bool) -> CallToolResult:
    """The matching_login_sql tool's answer: setup SQL for a new login, or to tighten the one in use."""
    store = rt.store
    connection = store.connection(source)
    if connection is None:
        return no_source(source)
    name = login_of(connection) if current_login else (login or login_sql.login_name(source))
    try:
        with rt.opened(connection) as opened:
            database = login_sql.database_of(opened, connection.settings.get("database"))
            sql = login_sql.matching_sql(opened.dialect, store.rules_for(source), opened.tables(), database,
                                         name, new=not current_login)
    except (SourceError, login_sql.NoMatchingLogin) as err:
        return error(f"{source}: {err}")
    which = "the login it uses now" if current_login else "a new login"
    return reply({"source": source, "login": name, "sql": sql},
                 f"Setup SQL for {which}, {name!r}, on {source}. Show it to the user to run, or to hand to "
                 f"their DBA:\n{sql}")


def register_source_tools(server: MCPServer, rt: Runtime) -> None:
    store = rt.store

    @server.tool(description="Test a connected source: says whether it opens, and if not, why.")
    def test_source(name: str) -> CallToolResult:
        connection = store.connection(name)
        if connection is None:
            return no_source(name)
        try:
            with rt.opened(connection) as source:
                found = described(source, store.rules_for(name), connection, store)
        except SourceError as err:
            return error(f"{name}: {err}")
        return reply({"name": name, "ok": True, **found})

    @server.tool(description=(
        "Write setup SQL for a read-only database login that can read exactly what a connection's applied profiles "
        "grant (Postgres or Snowflake), for the user or their DBA to run, so the database itself hides what isn't "
        "granted. `login` names a new login (default portward_<source>); `current_login` tightens the login the "
        "connection uses now instead. The SQL has a placeholder where the password or key goes; Portward doesn't "
        "keep it."))
    def matching_login_sql(source: str, login: str | None = None, current_login: bool = False) -> CallToolResult:
        return matching_login(rt, source, login, current_login)

    @server.tool(description=("Remove a connected source, and its password from the keychain. From claude.ai or "
                              "ChatGPT on the web it only gives the command the user runs on their computer."))
    def remove_source(name: str) -> CallToolResult:
        if store.connection(name) is None:
            return no_source(name)
        if rt.local_only:  # MCP over HTTP (#102): as with secrets, the user does it at the computer
            return error(remove_on_the_computer(name))
        try:
            remove_connection(store, rt.keychain, name)
        except KeychainError as err:
            return error(str(err))
        rt.out_of_band.close(account_for(name))
        return reply({"removed": name})
