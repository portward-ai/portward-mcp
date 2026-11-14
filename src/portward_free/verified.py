"""Checking verified answers (#130, Personal): whether one may be offered to the AI on a connection, and whether its
result still matches the one the user verified.

Held back, and the AI not told it exists, when:
- it was verified on another connection (its result belongs to that data);
- its SQL or its question names a table or column hidden here, or one its own profile doesn't grant
  (`hidden_names`; no sealed answers, as no sealed metrics);
- it names a column its table no longer has (context_drift, #123);
- its result changed: on connect each one offered is re-run through the normal guarded path (never to the AI) and
  its hash compared with the verified one (`recheck`); running it for the AI compares too. The last result seen is
  kept per connection on this computer (settings `verified_check:...`), so a mismatch holds it back here until the
  result matches again or the user verifies it afresh (a new version). The user sees it listed on the local page
  (why_hidden, `Kind.RESULT_CHANGED`), and the weekly facts count it with the other held-back items.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from portward_free.context import hides_something, mentions, ungranted
from portward_free.verified_def import VerifiedAnswer, VerifiedError, reads

if TYPE_CHECKING:
    from portward_free.profiles import Grant, Profile
    from portward_free.sources import Table
    from portward_free.storage import LocalStore

CHECK_KEY = "verified_check:"
VERIFIED_KIND = "verified_answer"


def on(answer: VerifiedAnswer, source: str) -> bool:
    """Whether `answer` belongs to connection `source`."""
    return answer.source.lower() == source.lower()


def _granted(grants: Sequence[Grant], table: str, column: str) -> bool:
    return any(g.table.lower() == table.lower() and g.column.lower() == column.lower() for g in grants)


def hidden_names(answer: VerifiedAnswer, grants: Sequence[Grant], visible: Sequence[Table],
                 catalog: Sequence[Table]) -> tuple[str, ...]:
    """The names that keep `answer` (of a profile granting `grants`) from the AI here: tables and columns its SQL
    reads that are hidden here or not granted by its own profile, and hidden names in its question. Empty when it may
    be offered. SQL that won't parse holds it back too."""
    try:
        read = reads(answer.sql, answer.dialect)
    except VerifiedError:
        return ("its SQL",)
    every = {t.name.lower(): t for t in catalog}
    shown = {t.name.lower(): {c.lower() for c in t.columns} for t in visible}
    found: list[str] = []
    for table in read.table_names:
        if table.lower() not in shown or table.lower() not in every:
            found.append(table)
    tables = [every[t.lower()] for t in read.table_names if t.lower() in every]
    for qualifier, column in read.columns:
        named = read.tables.get(qualifier.lower()) if qualifier else None
        owners = [t for t in tables if (named is None or t.name.lower() == named.lower())
                  and column.lower() in {c.lower() for c in t.columns}]
        if owners and not any(column.lower() in shown.get(t.name.lower(), set()) and _granted(grants, t.name, column)
                              for t in owners):
            found.append(f"{owners[0].name}.{column}")
    found += [n for n in mentions(answer.question) if hides_something(n, visible, catalog)]
    found += ungranted(answer.question, grants, [catalog])
    return tuple(dict.fromkeys(found))


# --- the result seen on this computer -------------------------------------------------------------------------------

def _key(source: str, profile_id: str, answer: VerifiedAnswer) -> str:
    return f"{CHECK_KEY}{source.lower()}|{profile_id}|{answer.key}"


@dataclass(frozen=True)
class Seen:
    """The last result of `answer` seen on a connection: its hash, or why it wouldn't run."""

    version: int
    verified: str
    seen: str
    at: str


def seen(store: LocalStore, source: str, profile_id: str, answer: VerifiedAnswer) -> Seen | None:
    raw = store.setting(_key(source, profile_id, answer))
    try:
        found = Seen(**json.loads(raw)) if raw else None
    except (TypeError, ValueError):
        return None
    return found if found is not None and found.version == answer.version \
        and found.verified == answer.result_hash else None


def remember(store: LocalStore, source: str, profile_id: str, answer: VerifiedAnswer, result: str) -> bool:
    """Keep the result just seen (a hash, or "error"); True when it matches the verified one."""
    store.set_setting(_key(source, profile_id, answer), json.dumps(
        {"version": answer.version, "verified": answer.result_hash, "seen": result, "at": store.profiles.seen_now()}))
    return result == answer.result_hash


def changed(store: LocalStore, source: str, profile_id: str, answer: VerifiedAnswer) -> Seen | None:
    """The last result seen here, when it differs from the verified one; None when it matches or wasn't checked."""
    found = seen(store, source, profile_id, answer)
    return found if found is not None and found.seen != found.verified else None


Run = Callable[[str, str, str], str]  # (source, question, sql) -> the result's hash, or "error"


def recheck(store: LocalStore, source: str, profiles: Sequence[Profile], visible: Sequence[Table],
            catalog: Sequence[Table], run: Run, done: set[tuple]) -> int:
    """On connect: re-run each verified answer offered on `source` that this server hasn't checked yet (`done`
    remembers them by connection, profile, question and version) and keep what it gave. Returns how many are held
    back here because their result changed."""
    held = 0
    for profile in profiles:
        for answer in profile.context.verified:
            if not on(answer, source) or hidden_names(answer, profile.grants, visible, catalog):
                continue
            mark = (source.lower(), profile.id, answer.key, answer.version, answer.result_hash)
            if mark not in done:
                done.add(mark)
                remember(store, source, profile.id, answer, run(source, answer.question, answer.sql))
            held += changed(store, source, profile.id, answer) is not None
    return held


def changed_on(store: LocalStore, source: str) -> tuple[tuple[Profile, VerifiedAnswer, Seen], ...]:
    """The verified answers of the profiles applied to `source` held back there because their result changed."""
    if not store.profiles.on_profiles(source):
        return ()
    return tuple((a.profile, answer, found) for a in store.profiles.applied(source)
                 for answer in a.profile.context.verified
                 if on(answer, source) and (found := changed(store, source, a.profile.id, answer)) is not None)
