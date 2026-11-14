"""The one local database file: connections (no secrets), settings and the history of pulls."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from portward_free.grants import Grants
from portward_free.rules import Policy, Rule, Rules
from portward_free.storage.locked import LockedConnection
from portward_free.storage.migrations import migrate
from portward_free.storage.profile_store import ProfileStore
from portward_free.storage.assumption_store import AssumptionStore
from portward_free.storage.read_store import ReadStore
from portward_free.storage.targets import target_keys


SEND_RESULTS, SEND_PICK = "results", "pick"  # 'Claude gets results' and 'I send rows myself' (#117)
SEND_MODES = (SEND_RESULTS, SEND_PICK)
SEND_MODE_KEY = "send_mode:"  # settings key prefix; the source's name follows


@dataclass(frozen=True)
class UsageWeek:
    """One ISO week's anonymous usage counts, as kept until they're sent."""

    week: str
    report: str
    pulls: int
    shares: int
    source_types: tuple[str, ...]
    ai_clients: tuple[str, ...]
    routes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Connection:
    name: str
    kind: str
    settings: dict[str, Any]


@dataclass(frozen=True)
class ShareRecord:
    """One share in the sender's local history (#50). The link's key is never kept."""
    id: str
    pull_id: str | None
    recipient: str
    created_at: str
    expires_at: str
    columns: tuple[str, ...]
    row_count: int


@dataclass(frozen=True)
class OpenAsk:
    """One open ask for a secret, as the file keeps it (asks.Requests): never the token itself."""

    token_hash: str
    id: str
    key: str
    kind: str
    params: dict[str, Any]
    expires_at: float
    attempts: int = 0


def _same_place(a: Rule, b: Rule) -> bool:
    """The same table and column, whatever source name each was made under (names match case-insensitively)."""
    return a.table.lower() == b.table.lower() and (a.column or "").lower() == (b.column or "").lower()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LocalStore:
    def __init__(self, db: LockedConnection, path: Path) -> None:
        self._db = db
        self.path = path
        self.profiles = ProfileStore(db, self._matching, self._target_keys, lambda: now())  # Personal's profiles (#91)
        self.reads = ReadStore(db)  # the AI log's summary (#98)
        self.assumptions = AssumptionStore(db, now)  # what the AI assumed, per answer (#96); never synced

    @classmethod
    def memory(cls) -> LocalStore:
        """A store in memory only (tests, and asks made outside a server)."""
        return cls.open(Path(":memory:"))

    @classmethod
    def open(cls, path: Path) -> LocalStore:
        """Open (or create) the file and migrate it to the current schema."""
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)  # transactions are explicit
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            migrate(db)
        except BaseException:
            db.close()
            raise
        store = cls(LockedConnection(db), path)  # used from several threads in server modes (storage/locked.py)
        for connection in store.connections():
            store._stamp_targets(connection.name)
        return store

    def close(self) -> None:
        self._db.close()

    # connections

    def add_connection(self, name: str, kind: str, settings: dict[str, Any]) -> Connection:
        before = self.connection(name)
        if before is not None and (before.kind, before.settings) != (kind, settings):
            self._forget_send_mode(name)  # the name now points at other data: back to 'I send rows myself'
        self._db.execute(
            "INSERT INTO connections (name, kind, settings, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET kind = excluded.kind, settings = excluded.settings",
            (name, kind, json.dumps(settings), now()),
        )
        self._stamp_targets(name)
        return Connection(name, kind, dict(settings))

    def connection(self, name: str) -> Connection | None:
        row = self._db.execute("SELECT name, kind, settings FROM connections WHERE name = ?", (name,)).fetchone()
        return Connection(row["name"], row["kind"], json.loads(row["settings"])) if row else None

    def remove_connection(self, name: str) -> bool:
        """Forget a connection (its history entries stay); False when there was none."""
        self._forget_send_mode(name)
        return self._db.execute("DELETE FROM connections WHERE name = ?", (name,)).rowcount > 0

    def connections(self) -> tuple[Connection, ...]:
        rows = self._db.execute("SELECT name, kind, settings FROM connections ORDER BY name").fetchall()
        return tuple(Connection(r["name"], r["kind"], json.loads(r["settings"])) for r in rows)

    # block rules: kept by the name they were made under and by what that source points at (targets.py), and
    # matched on either, so a second name or path for the same data, or the source added again, gets them too

    def _target_keys(self, name: str) -> tuple[str, ...]:
        connection = self.connection(name)
        return target_keys(connection.kind, connection.settings) if connection is not None else ()

    def _matching(self, name: str) -> tuple[str, tuple[str, ...]]:
        """A WHERE clause (and its parameters) for the rules that apply to the source `name`."""
        keys = self._target_keys(name)
        if not keys:
            return "source = ?", (name,)
        marks = ", ".join("?" * len(keys))
        return f"(source = ? OR target IN ({marks}) OR target_alt IN ({marks}))", (name, *keys, *keys)

    def _stamp_targets(self, name: str) -> None:
        """Give the rules made under `name` before its target was known (synced, or older files) its target."""
        keys = self._target_keys(name)
        if keys:
            self._db.execute("UPDATE rules SET target = ?, target_alt = ? WHERE source = ? AND target IS NULL",
                             (keys[0], keys[1] if len(keys) > 1 else None, name))
        self.profiles.stamp(name, keys)

    def add_rule(self, rule: Rule) -> bool:
        """Store a block rule; False if the same rule already applies to the source (under any name)."""
        if any(_same_place(r, rule) for r in self.rules(rule.source)):
            return False
        keys = self._target_keys(rule.source)
        cursor = self._db.execute(
            "INSERT INTO rules (source, table_name, column_name, created_at, target, target_alt) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (rule.source, rule.table, rule.column or "", now(), keys[0] if keys else None,
             keys[1] if len(keys) > 1 else None))
        return cursor.rowcount == 1

    def remove_rule(self, rule: Rule) -> bool:
        """Remove a block rule from the source `rule.source` names, under whatever name it was made; False if there
        was none."""
        where, params = self._matching(rule.source)
        cursor = self._db.execute(
            f"DELETE FROM rules WHERE {where} AND lower(table_name) = lower(?) AND lower(column_name) = lower(?)",
            (*params, rule.table, rule.column or ""))
        return cursor.rowcount > 0

    def rules(self, source: str | None = None) -> tuple[Rule, ...]:
        """Every rule, or the rules that apply to `source` (one per table and column, under the name it was made)."""
        where, params = self._matching(source) if source is not None else ("1", ())
        rows = self._db.execute(
            f"SELECT source, table_name, column_name FROM rules WHERE {where} "
            "ORDER BY source, lower(table_name), lower(column_name)", params).fetchall()
        found = tuple(Rule(r["source"], r["table_name"], r["column_name"] or None) for r in rows)
        if source is None:
            return found
        return tuple(r for i, r in enumerate(found) if not any(_same_place(r, o) for o in found[:i]))

    def rules_for(self, source: str) -> Policy:
        """What the AI may see of `source`: its block rules, or, on a connection that runs on profiles, the union of
        the applied profiles' grants (hide by default; with none applied, nothing), with their links, which scope stand-ins,
        and their row rules (#132).
        Never both."""
        if self.profiles.on_profiles(source):
            return Grants.applied((a.profile for a in self.profiles.applied(source)), self.profiles.applied_anywhere())
        return Rules(self.rules(source))

    def rules_added_since(self, source: str, moment: str) -> bool:
        """Whether a rule that applies to `source` was added at or after `moment` (an ISO time from `now()`); ties
        count, so a rule made in the same second as a pull is treated as newer."""
        where, params = self._matching(source)
        return self._db.execute(f"SELECT 1 FROM rules WHERE {where} AND created_at >= ? LIMIT 1",
                                (*params, moment)).fetchone() is not None

    def any_rule_added_since(self, moment: str) -> bool:
        """Whether a rule on any source was added at or after `moment` (ties count as newer)."""
        return self._db.execute("SELECT 1 FROM rules WHERE created_at >= ? LIMIT 1", (moment,)).fetchone() is not None

    # shares: the sender's own record of what they shared and with whom

    def record_share(self, entry: ShareRecord) -> ShareRecord:
        self._db.execute("INSERT INTO shares (id, pull_id, recipient, created_at, expires_at, columns, row_count) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (entry.id, entry.pull_id, entry.recipient, entry.created_at,
                                                          entry.expires_at, json.dumps(list(entry.columns)),
                                                          entry.row_count))
        return entry

    def set_share_expiry(self, share_id: str, expires_at: str) -> None:
        """A share's new expiry, as the service set it (#56: Personal keeps a share longer)."""
        self._db.execute("UPDATE shares SET expires_at = ? WHERE id = ?", (expires_at, share_id))

    def shares(self, limit: int) -> tuple[ShareRecord, ...]:
        """Newest first."""
        rows = self._db.execute("SELECT * FROM shares ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        return tuple(ShareRecord(r["id"], r["pull_id"], r["recipient"], r["created_at"], r["expires_at"],
                                 tuple(json.loads(r["columns"])), r["row_count"]) for r in rows)

    # settings

    def setting(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    # send modes (#117): 'results' (Claude gets results) or 'pick' (I send rows myself), per source. A source with
    # none set picks, so every source made before modes keeps picking: nothing loosens on upgrade

    def send_mode(self, source: str) -> str:
        return SEND_RESULTS if self.setting(SEND_MODE_KEY + source) == SEND_RESULTS else SEND_PICK

    def set_send_mode(self, source: str, mode: str) -> None:
        if mode not in SEND_MODES:
            raise ValueError(f"the mode must be one of: {', '.join(SEND_MODES)}")
        self.set_setting(SEND_MODE_KEY + source, mode)

    def send_modes(self) -> dict[str, str]:
        """Every source with a mode set, and its mode."""
        rows = self._db.execute("SELECT key, value FROM settings WHERE substr(key, 1, ?) = ?",
                                (len(SEND_MODE_KEY), SEND_MODE_KEY)).fetchall()
        return {r["key"][len(SEND_MODE_KEY):]: r["value"] for r in rows if r["value"] in SEND_MODES}

    def _forget_send_mode(self, source: str) -> None:
        self._db.execute("DELETE FROM settings WHERE key = ?", (SEND_MODE_KEY + source,))

    # usage counts, per ISO week (portward_free.usage)

    def usage_weeks(self) -> tuple[UsageWeek, ...]:
        rows = self._db.execute("SELECT * FROM usage_weeks ORDER BY week").fetchall()
        return tuple(UsageWeek(r["week"], r["report"], r["pulls"], r["shares"], tuple(json.loads(r["source_types"])),
                               tuple(json.loads(r["ai_clients"])), tuple(json.loads(r["routes"]))) for r in rows)

    def count_usage(self, week: str, report: str, *, pulls: int = 0, shares: int = 0, source_type: str | None = None,
                    ai_client: str | None = None, route: str | None = None) -> None:
        """Add to a week's counts (the week's row is created with `report` the first time)."""
        self._db.execute("INSERT INTO usage_weeks (week, report) VALUES (?, ?) ON CONFLICT (week) DO NOTHING",
                         (week, report))
        row = self._db.execute("SELECT source_types, ai_clients, routes FROM usage_weeks WHERE week = ?",
                               (week,)).fetchone()
        kinds = sorted(set(json.loads(row["source_types"])) | ({source_type} if source_type else set()))
        clients = sorted(set(json.loads(row["ai_clients"])) | ({ai_client} if ai_client else set()))
        routes = sorted(set(json.loads(row["routes"])) | ({route} if route else set()))
        self._db.execute("UPDATE usage_weeks SET pulls = pulls + ?, shares = shares + ?, source_types = ?, "
                         "ai_clients = ?, routes = ? WHERE week = ?",
                         (pulls, shares, json.dumps(kinds), json.dumps(clients), json.dumps(routes), week))

    def drop_usage_week(self, week: str) -> None:
        self._db.execute("DELETE FROM usage_weeks WHERE week = ?", (week,))

    def clear_usage(self) -> None:
        self._db.execute("DELETE FROM usage_weeks")

    # notes: what the user says tables and columns mean ('' as the column is the table's own note)

    def set_note(self, source: str, table: str, column: str, note: str) -> None:
        """Write or replace a note; an empty note removes it."""
        if not note.strip():
            self._db.execute("DELETE FROM notes WHERE source = ? AND table_name = ? AND column_name = ?",
                             (source, table, column))
            return
        self._db.execute(
            "INSERT INTO notes (source, table_name, column_name, note, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (source, table_name, column_name) DO UPDATE SET note = excluded.note, "
            "updated_at = excluded.updated_at",
            (source, table, column, note.strip(), now()),
        )

    def notes(self, source: str) -> dict[tuple[str, str], str]:
        rows = self._db.execute("SELECT table_name, column_name, note FROM notes WHERE source = ?", (source,))
        return {(r["table_name"], r["column_name"]): r["note"] for r in rows}

    # history

    def record_pull(self, entry: PullRecord, sealed_rows: bytes | None = None) -> PullRecord:
        """Add a history entry; `sealed_rows` (already encrypted) are kept with it when given."""
        if (sealed_rows is not None) != entry.keep_rows:
            raise ValueError("keep_rows must say whether sealed rows are given")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "INSERT INTO pulls (id, parent_id, question, sql, source, tables, columns, created_at, row_count, "
                "result_hash, saved_name, keep_rows) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.id, entry.parent_id, entry.question, entry.sql, entry.source, json.dumps(list(entry.tables)),
                 json.dumps(list(entry.columns)), entry.created_at, entry.row_count, entry.result_hash,
                 entry.saved_name, int(entry.keep_rows)),
            )
            if sealed_rows is not None:
                self._db.execute("INSERT INTO kept_rows (pull_id, sealed) VALUES (?, ?)", (entry.id, sealed_rows))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return entry

    def kept_rows(self, pull_id: str) -> bytes | None:
        row = self._db.execute("SELECT sealed FROM kept_rows WHERE pull_id = ?", (pull_id,)).fetchone()
        return bytes(row["sealed"]) if row else None

    def prune_kept_rows(self, older_than: datetime) -> int:
        """Delete the kept rows of pulls made before `older_than`; their entries stay, marked not kept."""
        cutoff = older_than.astimezone(UTC).isoformat(timespec="seconds")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            pruned = self._db.execute(
                "DELETE FROM kept_rows WHERE pull_id IN (SELECT id FROM pulls WHERE created_at < ?)", (cutoff,),
            ).rowcount
            self._db.execute("UPDATE pulls SET keep_rows = 0 WHERE keep_rows = 1 AND created_at < ?", (cutoff,))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return pruned

    # open asks for a secret (asks.Requests), shared by every server process on this file

    def put_ask(self, ask: OpenAsk) -> None:
        self._db.execute(
            "INSERT INTO open_asks (token_hash, id, ask_key, kind, params, expires_at, attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ask.token_hash, ask.id, ask.key, ask.kind, json.dumps(ask.params), ask.expires_at, ask.attempts),
        )

    def open_ask(self, token_hash: str | None = None, *, ask_id: str | None = None) -> OpenAsk | None:
        """The open ask with this token hash, or this public id."""
        column, value = ("token_hash", token_hash) if ask_id is None else ("id", ask_id)
        row = self._db.execute(f"SELECT * FROM open_asks WHERE {column} = ?", (value,)).fetchone()
        return OpenAsk(row["token_hash"], row["id"], row["ask_key"], row["kind"], json.loads(row["params"]),
                       row["expires_at"], row["attempts"]) if row else None

    def ask_attempted(self, token_hash: str) -> int | None:
        """Count one wrong answer; the attempts so far, or None when the ask is gone."""
        self._db.execute("BEGIN IMMEDIATE")  # one count per wrong answer, whichever process took it
        try:
            self._db.execute("UPDATE open_asks SET attempts = attempts + 1 WHERE token_hash = ?", (token_hash,))
            row = self._db.execute("SELECT attempts FROM open_asks WHERE token_hash = ?", (token_hash,)).fetchone()
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return int(row["attempts"]) if row else None

    def rotate_ask(self, token_hash: str, new_hash: str) -> bool:
        """Give an open ask a new token (the old one stops working); False when it is gone."""
        return self._db.execute("UPDATE open_asks SET token_hash = ? WHERE token_hash = ?",
                                (new_hash, token_hash)).rowcount > 0

    def close_ask(self, token_hash: str) -> bool:
        return self._db.execute("DELETE FROM open_asks WHERE token_hash = ?", (token_hash,)).rowcount > 0

    def close_asks(self, key: str | None = None, *, expired_by: float | None = None) -> None:
        """Close every ask for `key`, or every ask expired by `expired_by`."""
        if key is not None:
            self._db.execute("DELETE FROM open_asks WHERE ask_key = ?", (key,))
        if expired_by is not None:
            self._db.execute("DELETE FROM open_asks WHERE expires_at <= ?", (expired_by,))

    # the rows recent pulls lent the results panel (results_panel.HeldPulls), sealed

    def hold_panel_pull(self, pull_id: str, sealed: bytes, held_at: float, expires_at: float, limit: int) -> None:
        """Keep one pull's sealed panel rows; drop expired ones and all but the `limit` most recent."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("INSERT INTO panel_pulls (pull_id, held_at, expires_at, sealed) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT (pull_id) DO UPDATE SET held_at = excluded.held_at, "
                             "expires_at = excluded.expires_at, sealed = excluded.sealed",
                             (pull_id, held_at, expires_at, sealed))
            self._db.execute("DELETE FROM panel_pulls WHERE expires_at <= ?", (held_at,))
            self._db.execute("DELETE FROM panel_pulls WHERE pull_id NOT IN (SELECT pull_id FROM panel_pulls "
                             "ORDER BY held_at DESC, rowid DESC LIMIT ?)", (limit,))
            # a selection never outlives the pull it was picked from
            self._db.execute("DELETE FROM panel_selections WHERE pull_id NOT IN (SELECT pull_id FROM panel_pulls)")
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def panel_pull(self, pull_id: str, now: float) -> tuple[bytes, float] | None:
        """A pull's sealed panel rows and when they expire, unless they expired by `now` (or were never held)."""
        row = self._db.execute("SELECT sealed, expires_at FROM panel_pulls WHERE pull_id = ? AND expires_at > ?",
                               (pull_id, now)).fetchone()
        return (bytes(row["sealed"]), float(row["expires_at"])) if row else None

    # what the user put in the chat from the panel (selections.Selections), sealed, keyed by its id's hash

    def hold_selection(self, id_hash: str, pull_id: str, sealed: bytes, held_at: float, expires_at: float,
                       limit: int) -> None:
        """Keep one sealed selection; drop expired ones and all but the `limit` most recent."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("INSERT INTO panel_selections (id_hash, pull_id, held_at, expires_at, sealed) "
                             "VALUES (?, ?, ?, ?, ?)", (id_hash, pull_id, held_at, expires_at, sealed))
            self._db.execute("DELETE FROM panel_selections WHERE expires_at <= ?", (held_at,))
            self._db.execute("DELETE FROM panel_selections WHERE id_hash NOT IN (SELECT id_hash FROM "
                             "panel_selections ORDER BY held_at DESC, rowid DESC LIMIT ?)", (limit,))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def selection(self, id_hash: str, now: float) -> tuple[bytes, float] | None:
        """A selection's sealed rows and when it expires, unless it expired by `now` (or was never made)."""
        row = self._db.execute("SELECT sealed, expires_at FROM panel_selections WHERE id_hash = ? AND expires_at > ?",
                               (id_hash, now)).fetchone()
        return (bytes(row["sealed"]), float(row["expires_at"])) if row else None

    # a new share's link for the share panel (sharing.links.ShareLinks), sealed, under the share's public id

    def hold_share_link(self, share_id: str, sealed: bytes, held_at: float, expires_at: float, limit: int) -> None:
        """Keep one share's sealed link; drop expired ones and all but the `limit` most recent."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("INSERT INTO share_links (share_id, held_at, expires_at, sealed) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT (share_id) DO UPDATE SET held_at = excluded.held_at, "
                             "expires_at = excluded.expires_at, sealed = excluded.sealed",
                             (share_id, held_at, expires_at, sealed))
            self._db.execute("DELETE FROM share_links WHERE expires_at <= ?", (held_at,))
            self._db.execute("DELETE FROM share_links WHERE share_id NOT IN (SELECT share_id FROM share_links "
                             "ORDER BY held_at DESC, rowid DESC LIMIT ?)", (limit,))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def share_link(self, share_id: str, now: float) -> bytes | None:
        """A share's sealed link, unless it expired by `now` (or was never held)."""
        row = self._db.execute("SELECT sealed FROM share_links WHERE share_id = ? AND expires_at > ?",
                               (share_id, now)).fetchone()
        return bytes(row["sealed"]) if row else None

    def pull(self, pull_id: str) -> PullRecord | None:
        row = self._db.execute(f"SELECT {_PULL_COLUMNS} FROM pulls WHERE id = ?", (pull_id,)).fetchone()
        return _pull_record(row) if row else None

    def recent_pulls(self, limit: int) -> tuple[PullRecord, ...]:
        """The latest `limit` pulls, newest first (pulls in the same second keep the order they were recorded)."""
        rows = self._db.execute(
            f"SELECT {_PULL_COLUMNS} FROM pulls ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,),
        ).fetchall()
        return tuple(_pull_record(r) for r in rows)

    # saved pulls

    def save_name(self, pull_id: str, name: str, saved_at: str | None = None) -> str | None:
        """Give the pull `name`, taking it from any pull that had it; returns that pull's id, if any. `saved_at`
        (default now) is when it was named: sync (#55) settles two computers' names by it."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute("SELECT id FROM pulls WHERE saved_name = ?", (name,)).fetchone()
            previous = row["id"] if row and row["id"] != pull_id else None
            self._db.execute("UPDATE pulls SET saved_name = NULL, saved_at = NULL WHERE saved_name = ?", (name,))
            self._db.execute("UPDATE pulls SET saved_name = ?, saved_at = ? WHERE id = ?",
                             (name, saved_at or now(), pull_id))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return previous

    def saved_pull(self, name: str) -> PullRecord | None:
        row = self._db.execute(f"SELECT {_PULL_COLUMNS} FROM pulls WHERE saved_name = ?", (name,)).fetchone()
        return _pull_record(row) if row else None

    def saved_pulls(self) -> tuple[PullRecord, ...]:
        rows = self._db.execute(
            f"SELECT {_PULL_COLUMNS} FROM pulls WHERE saved_name IS NOT NULL ORDER BY saved_name").fetchall()
        return tuple(_pull_record(r) for r in rows)

    def unsave_name(self, name: str) -> None:
        """Take `name` off whichever pull has it (the pull stays in history)."""
        self._db.execute("UPDATE pulls SET saved_name = NULL, saved_at = NULL WHERE saved_name = ?", (name,))

    def saved_names(self) -> tuple[tuple[str, str, str], ...]:
        """(name, pull id, when it was named) for every saved pull."""
        rows = self._db.execute("SELECT saved_name, id, coalesce(saved_at, created_at) AS saved_at FROM pulls "
                                "WHERE saved_name IS NOT NULL ORDER BY saved_name").fetchall()
        return tuple((r["saved_name"], r["id"], r["saved_at"]) for r in rows)

    def all_pulls(self) -> tuple[PullRecord, ...]:
        """Every history entry, oldest first."""
        rows = self._db.execute(f"SELECT {_PULL_COLUMNS} FROM pulls ORDER BY created_at, rowid").fetchall()
        return tuple(_pull_record(r) for r in rows)

    def add_synced_pulls(self, entries: list[PullRecord]) -> None:
        """History entries from another computer (no rows are ever kept for them); one already here is left as it
        is. A tweak may arrive before the pull it tweaks, so the parent check waits for the end."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("PRAGMA defer_foreign_keys = ON")
            for entry in entries:
                self._db.execute(
                    "INSERT INTO pulls (id, parent_id, question, sql, source, tables, columns, created_at, row_count, "
                    "result_hash, saved_name, keep_rows) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0) "
                    "ON CONFLICT (id) DO NOTHING",
                    (entry.id, entry.parent_id, entry.question, entry.sql, entry.source,
                     json.dumps(list(entry.tables)), json.dumps(list(entry.columns)), entry.created_at,
                     entry.row_count, entry.result_hash))
            missing = self._db.execute("SELECT p.id FROM pulls p WHERE p.parent_id IS NOT NULL AND NOT EXISTS "
                                       "(SELECT 1 FROM pulls q WHERE q.id = p.parent_id)").fetchall()
            for row in missing:  # the pull it tweaked never came: keep the tweak, drop the dangling link
                self._db.execute("UPDATE pulls SET parent_id = NULL WHERE id = ?", (row["id"],))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def all_notes(self) -> tuple[tuple[str, str, str, str], ...]:
        """(source, table, column or '', note) for every note."""
        rows = self._db.execute("SELECT source, table_name, column_name, note FROM notes "
                                "ORDER BY source, table_name, column_name").fetchall()
        return tuple((r["source"], r["table_name"], r["column_name"], r["note"]) for r in rows)

    # sync (#55): what this computer and the sync service last agreed on

    def sync_rows(self) -> dict[str, tuple[str, int, str, str | None]]:
        """record id -> (kind, version, remote content JSON, local content JSON or None)."""
        rows = self._db.execute("SELECT record_id, kind, version, remote, local FROM sync_records").fetchall()
        return {r["record_id"]: (r["kind"], r["version"], r["remote"], r["local"]) for r in rows}

    def set_sync_row(self, record_id: str, kind: str, version: int, remote: str, local: str | None) -> None:
        self._db.execute("INSERT INTO sync_records (record_id, kind, version, remote, local) VALUES (?, ?, ?, ?, ?) "
                         "ON CONFLICT (record_id) DO UPDATE SET kind = excluded.kind, version = excluded.version, "
                         "remote = excluded.remote, local = excluded.local", (record_id, kind, version, remote, local))

    def clear_sync_rows(self) -> None:
        self._db.execute("DELETE FROM sync_records")

    # lineage

    def children(self, pull_id: str) -> tuple[PullRecord, ...]:
        """The pulls that tweak `pull_id`, oldest first."""
        rows = self._db.execute(
            f"SELECT {_PULL_COLUMNS} FROM pulls WHERE parent_id = ? ORDER BY created_at, rowid", (pull_id,),
        ).fetchall()
        return tuple(_pull_record(r) for r in rows)

    def ancestors(self, pull_id: str) -> tuple[PullRecord, ...]:
        """The parent of `pull_id`, its parent and so on up to the first pull (nearest first)."""
        found: list[PullRecord] = []
        seen = {pull_id}
        current = self.pull(pull_id)
        while current is not None and current.parent_id is not None and current.parent_id not in seen:
            seen.add(current.parent_id)
            current = self.pull(current.parent_id)
            if current is not None:
                found.append(current)
        return tuple(found)


    # what the AI was sent

    def start_conversation(self, client: str | None) -> Conversation:
        conversation = Conversation(id=uuid.uuid4().hex, started_at=now(), client=client)
        self._db.execute("INSERT INTO conversations (id, started_at, client) VALUES (?, ?, ?)",
                         (conversation.id, conversation.started_at, client))
        return conversation

    def log_response(self, entry: LoggedResponse) -> LoggedResponse:
        self._db.execute(
            "INSERT INTO ai_responses (conversation_id, created_at, tool, is_error, sealed, withheld) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entry.conversation_id, entry.created_at, entry.tool, int(entry.is_error), entry.sealed, entry.withheld),
        )
        return entry

    def conversations(self, limit: int) -> tuple[tuple[Conversation, int], ...]:
        """The latest `limit` conversations, newest first, each with how many responses the AI got in it."""
        rows = self._db.execute(
            "SELECT c.id, c.started_at, c.client, count(r.id) AS responses FROM conversations c "
            "LEFT JOIN ai_responses r ON r.conversation_id = c.id GROUP BY c.id "
            "ORDER BY c.started_at DESC, c.rowid DESC LIMIT ?", (limit,),
        ).fetchall()
        return tuple((Conversation(r["id"], r["started_at"], r["client"]), r["responses"]) for r in rows)

    def prune_conversations(self, started_before: datetime) -> None:
        """Delete conversations (and every response in them) that started before `started_before`."""
        cutoff = started_before.astimezone(UTC).isoformat(timespec="seconds")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("DELETE FROM ai_responses WHERE conversation_id IN "
                             "(SELECT id FROM conversations WHERE started_at < ?)", (cutoff,))
            self._db.execute("DELETE FROM conversations WHERE started_at < ?", (cutoff,))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def conversation(self, conversation_id: str) -> Conversation | None:
        row = self._db.execute("SELECT id, started_at, client FROM conversations WHERE id = ?",
                               (conversation_id,)).fetchone()
        return Conversation(row["id"], row["started_at"], row["client"]) if row else None

    def responses(self, conversation_id: str) -> tuple[LoggedResponse, ...]:
        """Every response the AI got in one conversation, in the order it got them, still sealed."""
        rows = self._db.execute(
            "SELECT conversation_id, created_at, tool, is_error, sealed, withheld "
            "FROM ai_responses WHERE conversation_id = ? ORDER BY id", (conversation_id,),
        ).fetchall()
        return tuple(LoggedResponse(
            conversation_id=r["conversation_id"], created_at=r["created_at"], tool=r["tool"],
            is_error=bool(r["is_error"]), sealed=r["sealed"], withheld=r["withheld"],
        ) for r in rows)


@dataclass(frozen=True)
class Conversation:
    """One AI client connection: every response the server sends on it is logged under this id."""

    id: str
    started_at: str
    client: str | None


@dataclass(frozen=True)
class LoggedResponse:
    """One logged tool response as the file holds it: what was asked and sent is in `sealed` (see `ai_log`), or
    `sealed` is None and `withheld` says why it wasn't stored."""

    conversation_id: str
    tool: str
    is_error: bool
    sealed: bytes | None
    withheld: str | None = None
    created_at: str = field(default_factory=lambda: now())


_PULL_COLUMNS = ("id, parent_id, question, sql, source, tables, columns, created_at, row_count, result_hash, "
                 "saved_name, keep_rows")


def _pull_record(row: sqlite3.Row) -> PullRecord:
    return PullRecord(
        id=row["id"], parent_id=row["parent_id"], question=row["question"], sql=row["sql"], source=row["source"],
        tables=tuple(json.loads(row["tables"])), columns=tuple(json.loads(row["columns"])),
        created_at=row["created_at"], row_count=row["row_count"], result_hash=row["result_hash"],
        saved_name=row["saved_name"], keep_rows=bool(row["keep_rows"]),
    )


@dataclass(frozen=True)
class PullRecord:
    """One history entry: what was asked, the SQL that ran, and a fingerprint of what came back (no rows)."""

    question: str
    sql: str
    source: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]  # the columns the SQL read
    row_count: int
    result_hash: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=lambda: now())
    parent_id: str | None = None  # the pull this one tweaks
    saved_name: str | None = None
    keep_rows: bool = False
