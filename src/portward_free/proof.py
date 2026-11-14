"""Proof (#98): the AI log's summary of every read, and the proof report made from it.

Every read (a pull whose rows the AI is handed: query, rerun, run_saved, tweak, compare) writes one `AiRead`: when,
which AI app, the route it came in by, the connection, the profiles (and their versions) that allowed it, the tables
and columns it read with the level each was shown at, how many rows and a hash of the result, and the approved
context the answer relied on with the version of each (#125: names and versions only, never the text). Never the
rows: the values the AI was sent stay sealed in the per-conversation log (ai_log.py) for 30 days. The summary is kept
a year and syncs, end to end encrypted, between the user's computers (sync/records.py, kind "read").

Each entry a computer writes is chained (#135): it carries the hash of the entry that computer wrote before it
(`prev`) and its own hash over everything it says, `prev` included (`hash`). Changing an entry, or removing one
between two others, then shows when the chain is checked (proof_chain.py) before a report is signed. Entries written
before #135 have neither and are counted as not linked.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from portward_free.route_names import DESKTOP, DIRECT, HOSTED, ROUTES

__all__ = ["DESKTOP", "DIRECT", "HOSTED", "ROUTES", "AiRead", "ShownColumn", "UsedContext", "UsedProfile", "cutoff_day",
           "now", "valid_entry"]


def now() -> str:
    """Now, to the microsecond: entries sort by it (then id) the same way on every computer."""
    return datetime.now(UTC).isoformat(timespec="microseconds")

KEEP_DAYS = 365  # the AI log's summary is kept a year (spec #90)
COMPUTER_SETTING = "ai_log_origin"  # this computer's id on the entries it writes (sync groups them by it)


@dataclass(frozen=True)
class ShownColumn:
    table: str
    column: str
    level: str  # profiles.LEVELS: full, anonymised, totals
    # The rules applied: the applied profiles that grant it at `level` (a profile granting it lower isn't one). Empty
    # on block rules (nothing read was blocked), and on entries written before this was kept.
    allowed_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class UsedProfile:
    name: str
    version: int


KIND_WORDS = {"verified_answer": "verified answer", "row_rule": "row rule",  # report_build.js KIND_WORDS mirrors
              "standing_filter": "standing filter"}


@dataclass(frozen=True)
class UsedContext:
    """One item of the user's approved context an answer relied on (#125), and the version it came from.

    `kind` is "meaning", "definition", "metric" (#129), "verified_answer" (#130) or "row_rule" (#132: a row rule
    of an applied profile on a table the read went through, `about` = "<table>: <rule>"; "standing_filter" (#133)
    likewise for a standing filter). `followed` is set for metrics:
    True when Portward wrote the SQL from it or the AI's SQL applied its filters and grouping, False when it didn't;
    and for verified answers: True when the result matched the verified one, False when it changed (None for other
    kinds).
    `version` is the version that pins the item's text: the profile's version for meanings and definitions. An entry
    from a newer Portward may carry kinds (and extra keys) this one doesn't know: it still reads, shown as written."""

    kind: str
    about: str  # the table or table.column a meaning describes, a definition's term
    profile: str
    version: int
    followed: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "about": self.about, "profile": self.profile, "version": self.version} | (
            {} if self.followed is None else {"followed": self.followed})

    @property
    def said(self) -> str:
        """How the AI log and the proof report name it (report_build.js contextOf mirrors this)."""
        answer = self.kind == "verified_answer"  # #130; kinds this Portward doesn't know are shown as written
        missed = ", result changed" if answer else ", not followed"
        word = KIND_WORDS.get(self.kind, self.kind)
        return f"{word} {self.about} ({self.profile} v{self.version})" + (
            missed if self.followed is False else "")


@dataclass(frozen=True)
class AiRead:
    """One read as the AI log's summary keeps it. `profiles` is empty on a connection that runs on block rules."""

    app: str | None
    route: str
    connection: str
    profiles: tuple[UsedProfile, ...]
    columns: tuple[ShownColumn, ...]
    row_count: int
    result_hash: str
    origin: str = ""  # the computer (or gateway) that wrote it
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=now)
    context: tuple[UsedContext, ...] = ()  # what the answer relied on (empty on entries written before #125)
    prev: str = ""  # #135: the hash of the entry its computer wrote before it ("" for the first, or not chained)
    hash: str = ""  # #135: entry_hash() as written; "" on entries written before chaining

    def entry_hash(self) -> str:
        """SHA-256 over the entry as written, `prev` included and `hash` left out (canonical JSON)."""
        body = {k: v for k, v in self.as_dict().items() if k != "hash"} | {"prev": self.prev}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def chained(self, prev: str) -> AiRead:
        """This entry linked after the entry whose hash is `prev`, with its own hash."""
        linked = replace(self, prev=prev, hash="")
        return replace(linked, hash=linked.entry_hash())

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(c.table for c in self.columns))

    @property
    def day(self) -> str:
        return self.created_at[:10]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "created_at": self.created_at, "origin": self.origin, "app": self.app,
                "route": self.route, "connection": self.connection,
                "profiles": [{"name": p.name, "version": p.version} for p in self.profiles],
                "columns": [{"table": c.table, "column": c.column, "level": c.level,
                             "allowed_by": list(c.allowed_by)} for c in self.columns],
                "context": [u.as_dict() for u in self.context],
                "row_count": self.row_count, "result_hash": self.result_hash} | (
                    {"prev": self.prev, "hash": self.hash} if self.hash else {})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AiRead:
        return cls(app=data["app"], route=data["route"], connection=data["connection"],
                   profiles=tuple(UsedProfile(str(p["name"]), int(p["version"])) for p in data["profiles"]),
                   columns=tuple(ShownColumn(str(c["table"]), str(c["column"]), str(c["level"]),
                                             tuple(str(p) for p in c.get("allowed_by", ())))
                                 for c in data["columns"]),
                   context=tuple(UsedContext(str(u["kind"]), str(u["about"]), str(u["profile"]), int(u["version"]),
                                             u["followed"] if isinstance(u.get("followed"), bool) else None)
                                 for u in data.get("context", ())),
                   row_count=int(data["row_count"]), result_hash=data["result_hash"], origin=data["origin"],
                   id=data["id"], created_at=data["created_at"], prev=str(data.get("prev") or ""),
                   hash=str(data.get("hash") or ""))


def valid_entry(data: Any) -> bool:
    """An entry another computer sent has the shape this Portward writes."""
    if not isinstance(data, dict):
        return False
    text = lambda k: isinstance(data.get(k), str)  # noqa: E731
    profiles, columns = data.get("profiles"), data.get("columns")
    return (all(map(text, ("id", "created_at", "origin", "route", "connection", "result_hash")))
            and (data.get("app") is None or text("app")) and isinstance(data.get("row_count"), int)
            and isinstance(profiles, list) and isinstance(columns, list)
            and all(isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("version"), int)
                    for p in profiles)
            and all(isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ("table", "column", "level"))
                    and _names(c.get("allowed_by", [])) for c in columns)
            and _used(data.get("context", []))
            and all(isinstance(data.get(k, ""), str) for k in ("prev", "hash")))


def _used(value: Any) -> bool:
    """The context an answer relied on: a list of {kind, about, profile, version} (other keys are left alone)."""
    return isinstance(value, list) and all(
        isinstance(u, dict) and all(isinstance(u.get(k), str) for k in ("kind", "about", "profile"))
        and isinstance(u.get("version"), int) and not isinstance(u.get("version"), bool) for u in value)


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def cutoff_day(today: datetime | None = None) -> str:
    """The first day the AI log's summary still keeps."""
    return ((today or datetime.now(UTC)) - timedelta(days=KEEP_DAYS)).date().isoformat()
