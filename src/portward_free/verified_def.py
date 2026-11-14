"""A verified answer (#130, Personal): a saved pull the user has checked and promoted into a profile's context. It
keeps the question, the SQL exactly as saved, the connection and SQL dialect it was verified on, the hash and row
count of the result it gave then, when that was, and a version. The AI is offered the questions (describe_source) and
runs one as written (run_verified_answer); on connect each is re-run and compared with its verified result
(verified.py), and one whose result changed is held back until the user checks it again.

Nothing here imports context.py: context.Context holds these, as it holds metrics (metric_def.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

MAX_QUESTION, MAX_SQL = 300, 20_000
HASH = re.compile(r"[0-9a-f]{8,128}")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class VerifiedError(ValueError):
    """A verified answer that can't be made as written; the message is safe to show."""


def checked_question(question: str) -> str:
    cleaned = " ".join(str(question or "").split())
    if not cleaned or len(cleaned) > MAX_QUESTION:
        raise VerifiedError(f"write the question in 1 to {MAX_QUESTION} characters")
    return cleaned


def parsed(sql: str, dialect: str) -> exp.Expression:
    """The SQL's one statement, which must be a query; VerifiedError otherwise."""
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect or None) if t is not None]
    except (SqlglotError, ValueError):
        raise VerifiedError("its SQL isn't SQL Portward can read") from None
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise VerifiedError("its SQL must be one SELECT")
    return trees[0]


@dataclass(frozen=True)
class Reads:
    """What a verified answer's SQL reads: `tables` maps each name or alias used to its table's name (WITH names
    left out), `columns` the columns it names as (qualifier or None, column)."""

    tables: dict[str, str]
    columns: tuple[tuple[str | None, str], ...]

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.tables.values()))


def reads(sql: str, dialect: str) -> Reads:
    tree = parsed(sql, dialect)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables: dict[str, str] = {}
    for node in tree.find_all(exp.Table):
        if node.name and node.name.lower() not in ctes:
            tables[node.name.lower()] = node.name
            if node.alias:
                tables[node.alias.lower()] = node.name
    columns = tuple(dict.fromkeys((c.table or None, c.name) for c in tree.find_all(exp.Column) if c.name))
    return Reads(tables, columns)


@dataclass(frozen=True)
class VerifiedAnswer:
    question: str
    sql: str
    source: str  # the connection it was verified on; it's offered there only
    dialect: str
    result_hash: str
    row_count: int
    checked_at: str  # when the user's result was taken (ISO, UTC)
    version: int = 1

    @property
    def key(self) -> str:
        return self.question.lower()

    @property
    def about(self) -> str:
        return self.question

    @property
    def text(self) -> str:
        """What the user may edit when approving: the question's wording."""
        return self.question

    def same_answer(self, other: VerifiedAnswer) -> bool:
        """The same SQL giving the same result: a new check of it keeps the version."""
        return (self.sql, self.source.lower(), self.result_hash) == (other.sql, other.source.lower(),
                                                                     other.result_hash)

    @property
    def summary(self) -> str:
        """The whole answer, for the user's approval and for picking between two profiles."""
        rows = f"{self.row_count} row{'s' * (self.row_count != 1)}"
        return f"{self.question} (on {self.source}: {rows}, checked {self.checked_at[:10]})\n{self.sql}"

    def brief(self) -> dict[str, Any]:
        """What describe_source sends: the question and its version."""
        return {"question": self.question, "version": self.version}

    def payload(self) -> dict[str, Any]:
        return {"question": self.question, "sql": self.sql, "source": self.source, "row_count": self.row_count,
                "checked_at": self.checked_at, "version": self.version}

    def stored(self) -> dict[str, Any]:
        return {"question": self.question, "sql": self.sql, "source": self.source, "dialect": self.dialect,
                "result_hash": self.result_hash, "row_count": self.row_count, "checked_at": self.checked_at,
                "version": self.version}

    @classmethod
    def from_stored(cls, data: dict) -> VerifiedAnswer:
        return cls(data["question"], data["sql"], data["source"], data.get("dialect", ""), data["result_hash"],
                   int(data["row_count"]), data["checked_at"], int(data.get("version", 1)))


def new_verified(question: str, sql: str, source: str, dialect: str, result_hash: str, row_count: int,
                 checked_at: str) -> VerifiedAnswer:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL:
        raise VerifiedError(f"its SQL must be 1 to {MAX_SQL} characters")
    parsed(sql, dialect)
    if not str(source or "").strip() or not HASH.fullmatch(str(result_hash or "")):
        raise VerifiedError("it needs the connection and the result it was checked against")
    if not NAME.fullmatch(dialect or "x") or isinstance(row_count, bool) or not isinstance(row_count, int) \
            or row_count < 0 or not isinstance(checked_at, str) or not checked_at:
        raise VerifiedError("it needs the dialect, row count and time it was checked")
    return VerifiedAnswer(checked_question(question), sql.strip(), source.strip(), dialect, result_hash, row_count,
                          checked_at)


def with_version(answer: VerifiedAnswer, version: int) -> VerifiedAnswer:
    return replace(answer, version=version)
