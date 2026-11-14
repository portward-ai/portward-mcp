"""The AI's assumptions in the local file (#96): one row per assumption, with the pull (answer) it was made for.
Nothing here is synced (sync/records.py never reads this table)."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable

from portward_free.assumptions import Assumption

Clock = Callable[[], str]
_COLUMNS = "id, pull_id, source, question, text, created_at, defined_as"


def _assumption(row: sqlite3.Row) -> Assumption:
    return Assumption(row["id"], row["pull_id"], row["source"], row["question"], row["text"], row["created_at"],
                      row["defined_as"])


class AssumptionStore:
    def __init__(self, db: sqlite3.Connection, clock: Clock) -> None:
        self._db, self._now = db, clock

    def record(self, pull_id: str, source: str, question: str, texts: tuple[str, ...]) -> tuple[Assumption, ...]:
        made = tuple(Assumption(uuid.uuid4().hex[:12], pull_id, source, question, text, self._now())
                     for text in texts)
        for a in made:
            self._db.execute(f"INSERT INTO assumptions ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                             (a.id, a.pull_id, a.source, a.question, a.text, a.created_at))
        return made

    def recent(self, limit: int, source: str | None = None) -> tuple[Assumption, ...]:
        """The latest `limit` assumptions, newest first: every source's, or one's."""
        rows = self._db.execute(f"SELECT {_COLUMNS} FROM assumptions WHERE (? IS NULL OR source = ?) "
                                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (source, source, limit)).fetchall()
        return tuple(_assumption(r) for r in rows)

    def get(self, assumption_id: str) -> Assumption | None:
        row = self._db.execute(f"SELECT {_COLUMNS} FROM assumptions WHERE id = ?",
                               (str(assumption_id).strip(),)).fetchone()
        return _assumption(row) if row else None

    def defined(self, assumption_id: str, term: str) -> None:
        self._db.execute("UPDATE assumptions SET defined_as = ? WHERE id = ?", (term, assumption_id))
