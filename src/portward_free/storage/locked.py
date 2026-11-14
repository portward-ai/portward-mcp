"""One SQLite connection used from several threads (the hosted engine's poll thread, MCP tools on worker threads, a
server mode's plan watcher): every statement runs under one re-entrant lock and hands back fully read rows, and a
transaction keeps the lock from its BEGIN to its COMMIT or ROLLBACK, so no other thread's statement lands inside it.

The control plane does the same with portward.db.LockedConnection; the free tool never imports it, so this is its
own. The stores keep writing `db.execute("BEGIN IMMEDIATE")` ... `db.execute("COMMIT")` as before."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from typing import Any

_OPENS = ("BEGIN",)
_ENDS = ("COMMIT", "ROLLBACK", "END")


class Rows:
    """What a statement gave back, read whole while the lock was held: fetchone, fetchall, iteration, rowcount."""

    def __init__(self, rows: list[Any], rowcount: int, lastrowid: int | None) -> None:
        self._rows, self._next = rows, 0
        self.rowcount, self.lastrowid = rowcount, lastrowid

    def fetchone(self) -> Any:
        if self._next >= len(self._rows):
            return None
        self._next += 1
        return self._rows[self._next - 1]

    def fetchall(self) -> list[Any]:
        rest, self._next = self._rows[self._next:], len(self._rows)
        return rest

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


def _verb(sql: str) -> str:
    return sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""


class LockedConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.lock = threading.RLock()
        self._mine = threading.local()  # per thread: whether it holds the lock for a transaction it began

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Rows:
        verb = _verb(sql)
        if verb in _OPENS:
            self.lock.acquire()  # held until this transaction ends (below), on this thread
            try:
                found = self._run(sql, params)
            except BaseException:
                self.lock.release()
                raise
            self._mine.open = True
            return found
        try:
            with self.lock:
                return self._run(sql, params)
        finally:
            if verb in _ENDS and self._holds_transaction():
                self._mine.open = False
                self.lock.release()

    def _run(self, sql: str, params: Sequence[Any] | dict[str, Any]) -> Rows:
        cursor = self._conn.execute(sql, params)
        return Rows(cursor.fetchall(), cursor.rowcount, cursor.lastrowid)

    def _holds_transaction(self) -> bool:
        """This thread holds the lock for an open BEGIN and the connection has left the transaction (COMMIT or
        ROLLBACK went through); a COMMIT that failed keeps the lock for the ROLLBACK that follows."""
        return getattr(self._mine, "open", False) and not self._conn.in_transaction

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> Rows:
        with self.lock:
            cursor = self._conn.executemany(sql, rows)
            return Rows([], cursor.rowcount, cursor.lastrowid)

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def close(self) -> None:
        with self.lock:
            self._conn.close()
