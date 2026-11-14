"""Database passwords, typed by the user where the AI can't see them, and saved straight to the keychain.

No AI-facing tool takes a password. `add_source` saves a database's settings, then the password is asked for out of
band, in the first of these the client has:

1. the panel: a small form (`panel/password.html`) that sends it through the app-only `save_secret` tool;
2. URL-mode elicitation: a one-time page on this machine (`password_page`);
3. otherwise a terminal command (`portward-free password <name>`) that prompts with hidden input.

(`secret_entry` picks the way; `asks` holds the open asks. The sharing code, #50, goes the same ways.)
Every way ends in `check_and_save`: the connection is tested with the password, and only a password that works is
saved. A password is never in a tool result, the AI log or a log line; error texts are scrubbed of it.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from portward_free import __version__
from portward_free.asks import Ask, Refused
from portward_free.keychain import Keychain, KeychainError, account_for
from portward_free.paths import HOME_ENV
from portward_free.sources import NEEDS_PASSWORD, SourceError, open_source
from portward_free.storage import Connection

PACKAGE = "portward-free"
MASK = "(password)"
PasswordRefused = Refused  # the password wasn't saved; the message never contains it


@dataclass(frozen=True)
class Saved:
    name: str
    tables: tuple[str, ...]
    warnings: tuple[str, ...]

    def summary(self) -> str:
        text = f"Saved the password for {self.name!r} in the keychain. Tables: {', '.join(self.tables) or 'none'}."
        return text + "".join(f"\nWarning: {w}." for w in self.warnings)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"saved": self.name, "tables": list(self.tables)}
        return {**payload, "warnings": list(self.warnings)} if self.warnings else payload


def terminal_command(name: str) -> str:
    """The command the user runs to type `name`'s password (quoted for a POSIX shell)."""
    return command(f"password {shlex.quote(name)}")


def command(arguments: str) -> str:
    """This program run with `arguments`, as the user types it in a terminal (same app-data folder as now)."""
    home = os.environ.get(HOME_ENV)
    prefix = f"{HOME_ENV}={shlex.quote(home)} " if home else ""
    return f"{prefix}{_program()} {arguments}"


def _program() -> str:
    """How the user runs this program: the compiled one itself (the desktop extension, #60; Nuitka marks compiled
    modules with `__compiled__`), else the pinned package through uvx."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return shlex.quote(sys.argv[0])
    return f"uvx {PACKAGE}=={__version__}"


def missing_text(name: str) -> str:
    """Why a database connection can't open yet, and how the user fixes it."""
    return (f"the password for {name!r} isn't in the keychain yet. Call update_password to have the user enter it, "
            f"or ask them to run this in a terminal: {terminal_command(name)}")


def needs_password(connection: Connection) -> None:
    if connection.kind not in NEEDS_PASSWORD:
        raise PasswordRefused(f"{connection.name!r} is a {connection.kind} source; it doesn't use a password")


def scrub(text: str, password: str) -> str:
    return text.replace(password, MASK) if password else text


def check_and_save(connection: Connection, password: str, keychain: Keychain) -> Saved:
    """Test the connection with `password` and, only if it works, save the password in the keychain."""
    needs_password(connection)
    if not password:
        raise PasswordRefused("type the password")
    try:
        source = open_source(connection.kind, connection.settings, password)
        try:
            saved = Saved(connection.name, tuple(t.name for t in source.tables()), tuple(source.warnings))
        finally:
            source.close()
    except SourceError as err:
        raise PasswordRefused(scrub(str(err), password)) from None
    try:
        keychain.set(account_for(connection.name), password)
    except KeychainError as err:
        raise PasswordRefused(str(err)) from None
    return saved


PASSWORD_ASK = "database_password"  # the ask's kind (asks.Requests.kinds)


def password_ask(connection: Connection, keychain: Keychain, replaces: str | None = None) -> Ask:
    """The out-of-band ask for `connection`'s password: where it goes, and how it is tested and saved. `replaces`:
    where this source's name pointed until now, when that was somewhere else (`pointed_elsewhere`), so a name the
    user trusts can't be moved to another server without them seeing it where they type the password."""
    settings = connection.settings
    moved = ((("Replaces", f"{replaces} (where {connection.name!r} pointed until now; check the server above is "
                           "one you meant)"),) if replaces else ())
    return Ask(key=account_for(connection.name), title="Database password", label="Password",
               details=(("Source", connection.name), ("Server", f"{settings.get('host')}:{settings.get('port')}"),
                        *moved,
                        ("Database", str(settings.get("database"))), ("Login", str(settings.get("user")))),
               done=f"I've entered the password for {connection.name}.",
               save=lambda password: check_and_save(connection, password, keychain), kind=PASSWORD_ASK,
               params={"name": connection.name, "kind": connection.kind, "settings": dict(settings),
                       "replaces": replaces})


def pointed_elsewhere(previous: Connection | None, connection: Connection) -> str | None:
    """Where `previous` (the source this name was until now) pointed, when `connection` goes to another server or
    replaces a file; None when there was none or it is the same server (host names compare without case)."""
    if previous is None:
        return None
    was = previous.settings
    if "host" not in was:
        return str(was.get("path") or previous.kind)
    now = connection.settings
    same = (str(was.get("host")).lower() == str(now.get("host")).lower()
            and str(was.get("port")) == str(now.get("port")))
    return None if same else f"{was.get('host')}:{was.get('port')}"


def password_ask_builder(keychain: Keychain) -> Callable[[Mapping[str, Any]], Ask]:
    """Rebuilds a password ask (add_source, update_password) from its params, for the other server process."""
    return lambda p: password_ask(Connection(p["name"], p["kind"], dict(p["settings"])), keychain,
                                   replaces=p.get("replaces"))
