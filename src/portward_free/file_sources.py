"""Adding a file (SQLite, CSV, Excel) as a source: only once the user has said yes themselves.

The AI writes add_source's arguments, so on its own it could point a new name at any file on the computer, or at a
file the user already connected and blocked parts of (rules follow the file, storage/targets.py, but a new file is
new data). The user confirms in the first way the client has: its own prompt (elicitation, `confirm`), the panel's
form, a one-time local page, or a terminal command (`portward-free add-file`) that needs a person at a terminal.
The panel and the page show the file's name and one Allow button: their ask's token reaches only them, never the AI
(asks.py). The terminal command has the user type the source's name: an AI that can run commands could otherwise
answer a yes/no prompt itself.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from portward_free.asks import ALLOW, Ask, Refused
from portward_free.keychain import Keychain, KeychainError, account_for
from portward_free.passwords import command
from portward_free.rules import Rules
from portward_free.runtime import Opener
from portward_free.sources import SourceError
from portward_free.storage import Connection, LocalStore

CONFIRM_KEY = "confirm_add_source"  # the prompt's key in an input_required round
CONFIRM_STATE = "add-source:"  # request_state prefix: the source's name and path follow


@dataclass(frozen=True)
class FileAdded:
    name: str
    path: str
    tables: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        text = f"Added {self.path} as {self.name!r}. Tables: {', '.join(self.tables) or 'none'}."
        return text + "".join(f"\nWarning: {w}." for w in self.warnings)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"added": self.name, "tables": list(self.tables)}
        return {**payload, "warnings": list(self.warnings)} if self.warnings else payload


def checked(opened: Opener, connection: Connection, rules: Rules) -> FileAdded:
    """Open the file to check it is what `kind` says (SourceError if not), and what the AI would see of it."""
    with opened(connection) as source:
        tables = tuple(t.name for t in rules.visible(source.tables()))
        said = (*source.warnings, *rules.notices(source.tables()))
    return FileAdded(connection.name, str(connection.settings.get("path")), tables, tuple(said))


def save(store: LocalStore, keychain: Keychain, connection: Connection) -> None:
    """Store the connection (a name that was a database before loses its password). KeychainError if it can't."""
    keychain.delete(account_for(connection.name))
    store.add_connection(connection.name, connection.kind, connection.settings)


def question(connection: Connection) -> str:
    return (f"Add the file {connection.settings.get('path')} as the {connection.kind} source {connection.name!r}? "
            "The AI can then read it; your block rules still apply.")


def confirm_state(connection: Connection) -> str:
    return f"{CONFIRM_STATE}{connection.name}\n{connection.kind}\n{connection.settings.get('path')}"


def terminal_command(connection: Connection) -> str:
    path = str(connection.settings.get("path"))
    return command(f"add-file {shlex.quote(connection.name)} {connection.kind} {shlex.quote(path)}")


def typed_name(connection: Connection, typed: str) -> None:
    if typed.strip() != connection.name:
        raise Refused(f"type the source's name, {connection.name}, to let the AI read the file")


FILE_ASK = "file"  # the ask's kind (asks.Requests.kinds)


def file_name(found: FileAdded) -> str:
    return PurePath(found.path).name or found.path


def file_ask(store: LocalStore, keychain: Keychain, connection: Connection, found: FileAdded) -> Ask:
    """The out-of-band ask: the user presses Allow, under the file's name, to add the file."""

    def confirmed(sent: str) -> FileAdded:
        if sent != ALLOW:
            raise Refused("press Allow to let the AI read the file")
        try:
            save(store, keychain, connection)
        except KeychainError as err:
            raise Refused(str(err)) from None
        return found

    name = file_name(found)
    return Ask(key=f"add-source:{connection.name}", title=f"Let the AI read {name}", label="Allow", allow=True,
               details=(("File", name), ("Where", found.path), ("Source", connection.name)),
               done=f"I've allowed {name}.", save=confirmed, kind=FILE_ASK,
               params={"connection": {"name": connection.name, "kind": connection.kind,
                                      "settings": dict(connection.settings)},
                       "found": {"name": found.name, "path": found.path, "tables": list(found.tables),
                                 "warnings": list(found.warnings)}})


def file_ask_builder(store: LocalStore, keychain: Keychain) -> Callable[[Mapping[str, Any]], Ask]:
    """Rebuilds a file ask from its params, for the server process that didn't open it."""

    def rebuilt(params: Mapping[str, Any]) -> Ask:
        c, f = params["connection"], params["found"]
        return file_ask(store, keychain, Connection(c["name"], c["kind"], dict(c["settings"])),
                        FileAdded(f["name"], f["path"], tuple(f["tables"]), tuple(f["warnings"])))

    return rebuilt


def open_problem(opened: Opener, connection: Connection, rules: Rules) -> FileAdded | str:
    try:
        return checked(opened, connection, rules)
    except SourceError as err:
        return str(err)
