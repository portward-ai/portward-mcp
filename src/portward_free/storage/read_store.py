"""The AI log's summary in the local file (#98): one row per read, never the rows the AI was sent.

Each read written here is chained to the one written here before it (#135): `record` reads this computer's chain
head, links the new entry after it and moves the head, in one transaction, so two processes on one file (the desktop
server and serve-http) can't both link after the same entry."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import replace

from portward_free.proof import COMPUTER_SETTING, AiRead

_COLUMNS = ("id, created_at, origin, app, route, connection, profiles, columns, row_count, result_hash, context, "
            "prev, hash")
CHAIN_HEAD_SETTING = "ai_log_chain_head"  # the hash of the last entry written here (#135)


def _read(row: sqlite3.Row) -> AiRead:
    return AiRead.from_dict({**dict(row), "profiles": json.loads(row["profiles"]),
                             "columns": json.loads(row["columns"]), "context": json.loads(row["context"])})


class ReadStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def origin(self) -> str:
        """This computer's id on the entries it writes, made on first use."""
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (COMPUTER_SETTING,)).fetchone()
        if row is not None:
            return str(row["value"])
        made = uuid.uuid4().hex
        self._db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (COMPUTER_SETTING, made))
        return str(self._db.execute("SELECT value FROM settings WHERE key = ?", (COMPUTER_SETTING,)).fetchone()[0])

    def chain_head(self) -> tuple[str, str]:
        """(hash, created_at) of the last entry written here, or ("", "") before the first."""
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (CHAIN_HEAD_SETTING,)).fetchone()
        try:
            head = json.loads(row["value"]) if row is not None else {}
            return str(head["hash"]), str(head["at"])
        except (ValueError, KeyError, TypeError):
            return "", ""

    def record(self, read: AiRead) -> AiRead:
        """Add one read written here: stamped with this computer's origin and chained after the last one."""
        origin = read.origin or self.origin()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            stamped = replace(read, origin=origin).chained(self.chain_head()[0])
            self._insert(stamped)
            self._db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = "
                             "excluded.value", (CHAIN_HEAD_SETTING,
                                                json.dumps({"hash": stamped.hash, "at": stamped.created_at})))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return stamped

    def add(self, reads: Iterable[AiRead]) -> None:
        """Add reads (from here or another computer) as they were written; one already here is left as it is."""
        for r in reads:
            self._insert(r)

    def _insert(self, r: AiRead) -> None:
        d = r.as_dict()
        self._db.execute(
            f"INSERT INTO ai_reads ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO NOTHING",
            (d["id"], d["created_at"], d["origin"], d["app"], d["route"], d["connection"],
             json.dumps(d["profiles"]), json.dumps(d["columns"]), d["row_count"], d["result_hash"],
             json.dumps(d["context"]), r.prev, r.hash))

    def between(self, start: str | None = None, end: str | None = None) -> tuple[AiRead, ...]:
        """Reads from day `start` to day `end` (ISO dates, both included; None: no bound), oldest first."""
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM ai_reads WHERE (? IS NULL OR substr(created_at, 1, 10) >= ?) "
            "AND (? IS NULL OR substr(created_at, 1, 10) <= ?) ORDER BY created_at, id",
            (start, start, end, end)).fetchall()
        return tuple(_read(r) for r in rows)

    def prune(self, first_day: str) -> int:
        """Drop reads from days before `first_day` (the summary is kept a year)."""
        return self._db.execute("DELETE FROM ai_reads WHERE substr(created_at, 1, 10) < ?", (first_day,)).rowcount
