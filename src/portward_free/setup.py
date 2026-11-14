"""Connecting a source on the local page, in three steps (#118): where the data is, check the login, then choose the
mode and the columns to keep from Claude. setup_page serves it; setup_html draws it.

Nothing is stored, kept in the keychain or sent before the user presses Save on the third step:

- Checking opens the source with what the user typed (and the password, for a database) and closes it again. The
  check, its catalog and the password wait in `Pending`, in this page server's memory only, under a single-use
  unguessable token that the form carries. The password is never written back into the page.
- Save checks the token, the mode and every ticked column first; only then does it test the password again and save
  it to the keychain (passwords.check_and_save), store the connection, add the block rules ("Keep from Claude") and
  set the mode (store.set_send_mode). A refused save stores nothing and keeps the setup open to fix.

Suggested keeps (suggested_keeps) arrive pre-ticked; columns already kept under this name show as kept and can't be
unticked here (unblocking asks first, on the overview).
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from portward_free import modes, passwords
from portward_free.keychain import Keychain, KeychainError, account_for
from portward_free.rule_tools import named_rule
from portward_free.rules import Rule
from portward_free.sources import NEEDS_PASSWORD, SourceError, Table, open_source, server_settings
from portward_free.storage import Connection, LocalStore
from portward_free.suggested_keeps import suggested

KINDS = (("postgres", "Postgres"), ("mysql", "MySQL"), ("sqlite", "SQLite file"), ("csv", "CSV"),
         ("excel", "Excel"))
KIND_NAMES = dict(KINDS)
WHERE_FIELDS = ("kind", "name", "host", "port", "database", "user", "path")
PENDING_SECONDS = 10 * 60  # how long a checked setup waits for Save
STALE = "This setup has expired or was already used: start again from the first step."


class SetupProblem(Exception):
    """Nothing was saved. The message is safe to show: it never holds the password."""


@dataclass(frozen=True)
class Where:
    """Step 1, as typed (never the password)."""
    kind: str = "postgres"
    name: str = ""
    host: str = ""
    port: str = ""
    database: str = ""
    user: str = ""
    path: str = ""

    @classmethod
    def from_fields(cls, fields: Mapping[str, str]) -> Where:
        values = {k: str(fields.get(k, "")).strip() for k in WHERE_FIELDS}
        if values["kind"] not in KIND_NAMES:
            values["kind"] = cls.kind
        return cls(**values)

    @property
    def is_database(self) -> bool:
        return self.kind in NEEDS_PASSWORD

    def fields(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in WHERE_FIELDS}

    def connection(self) -> Connection:
        """The connection these settings make; SetupProblem when they can't."""
        if not self.name:
            raise SetupProblem("give it a name, like Shop database")
        if self.is_database:
            try:
                settings = server_settings(self.kind, {"host": self.host, "port": self.port,
                                                       "database": self.database, "user": self.user})
            except SourceError as err:
                raise SetupProblem(str(err)) from None
            return Connection(self.name, self.kind, settings)
        if not self.path:
            raise SetupProblem("say where the file is")
        return Connection(self.name, self.kind, {"path": str(Path(self.path).expanduser().resolve())})


@dataclass(frozen=True)
class Line:
    ok: bool
    text: str


@dataclass(frozen=True)
class Checked:
    """A setup that passed step 2: what it connects, what the check found, and the columns to offer."""
    where: Where
    connection: Connection
    tables: tuple[Table, ...]
    lines: tuple[Line, ...]
    suggested: frozenset[tuple[str, str]]
    kept: tuple[Rule, ...] = ()  # rules already on this name
    replaces: bool = False  # a source by this name exists, pointing elsewhere
    password: str = field(default="", repr=False)

    def kept_already(self, table: str, column: str) -> bool:
        return any(r.table.lower() == table.lower() and (r.column is None or r.column.lower() == column.lower())
                   for r in self.kept)


def check(where: Where, password: str, store: LocalStore) -> Checked:
    """Open the source with what was typed and close it again; SetupProblem (scrubbed of the password) if it won't."""
    connection = where.connection()
    if where.is_database and not password:
        raise SetupProblem("type the login's password")
    try:
        source = open_source(connection.kind, dict(connection.settings), password if where.is_database else None)
        try:
            tables, warnings = tuple(source.tables()), tuple(source.warnings)
        finally:
            source.close()
    except SourceError as err:
        raise SetupProblem(passwords.scrub(str(err), password)) from None
    before = store.connection(where.name)
    return Checked(where, connection, tables, _lines(connection, tables, warnings), suggested(tables),
                   tuple(store.rules(where.name)) if before is not None else (),
                   before is not None and before != connection, password)


def _lines(connection: Connection, tables: Sequence[Table], warnings: Sequence[str]) -> tuple[Line, ...]:
    s = connection.settings
    found = Line(True, f"Found {len(tables)} table{'' if len(tables) == 1 else 's'}.")
    if connection.kind not in NEEDS_PASSWORD:
        name = PurePath(str(s.get("path"))).name
        return Line(True, f"Opened {name}. Portward only reads it."), found
    first = (Line(True, f"Reached {s.get('host')}:{s.get('port')} and signed in to {s.get('database')} as "
                        f"{s.get('user')}."),)
    if not warnings:
        return (*first, Line(True, "This login can only read. Portward also opens every session read-only."), found)
    return (*first, *(Line(False, f"{w[:1].upper()}{w[1:]}.") for w in warnings),
            Line(True, "Portward still only reads: every query runs in a read-only session."), found)


class Pending:
    """Checked setups waiting for Save, by single-use token, in this page server's memory only."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._held: dict[str, tuple[Checked, float]] = {}
        self._clock = clock

    def hold(self, checked: Checked) -> str:
        now = self._clock()
        self._held = {t: h for t, h in self._held.items() if h[1] > now}
        token = secrets.token_urlsafe(24)
        self._held[token] = (checked, now + PENDING_SECONDS)
        return token

    def get(self, token: str) -> Checked | None:
        held = self._held.get(token)
        if held is None or held[1] <= self._clock():
            self._held.pop(token, None)
            return None
        return held[0]

    def drop(self, token: str) -> None:
        self._held.pop(token, None)


@dataclass(frozen=True)
class Saved:
    name: str
    mode: str
    kept: tuple[Rule, ...]


def keeps_of(checked: Checked, ticked: Sequence[str]) -> tuple[Rule, ...]:
    """The block rules for the ticked columns; SetupProblem for one the source doesn't have."""
    rules: list[Rule] = []
    for value in ticked:
        try:
            table, column = json.loads(value)
            rule = named_rule(checked.where.name, checked.tables, str(table), str(column))
        except (ValueError, TypeError) as err:
            raise SetupProblem(str(err) if isinstance(err, ValueError) and str(err) else
                               "a ticked column couldn't be read") from None
        if rule not in rules and not checked.kept_already(rule.table, rule.column or ""):
            rules.append(rule)
    return tuple(rules)


def save(store: LocalStore, keychain: Keychain, checked: Checked, mode: str, ticked: Sequence[str]) -> Saved:
    """Store the source, its kept columns and its mode; nothing at all when anything is refused."""
    if mode not in modes.MODES:
        raise SetupProblem("choose how Claude works with this source")
    rules = keeps_of(checked, ticked)
    connection = checked.connection
    try:
        if connection.kind in NEEDS_PASSWORD:
            passwords.check_and_save(connection, checked.password, keychain)
        else:
            keychain.delete(account_for(connection.name))  # a name that was a database before loses its password
    except (passwords.PasswordRefused, KeychainError) as err:
        raise SetupProblem(str(err)) from None
    store.add_connection(connection.name, connection.kind, dict(connection.settings))
    for rule in rules:
        store.add_rule(rule)
    store.set_send_mode(connection.name, mode)
    return Saved(connection.name, mode, (*checked.kept, *rules))
