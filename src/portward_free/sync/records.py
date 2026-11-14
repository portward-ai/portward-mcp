"""What sync carries (#55), as records: this computer's history, saved pulls, notes and rules, Personal's profiles and
where they're applied (#97, sync/profile_records.py), the AI log's summary (#98, sync/read_records.py), how two
versions of one record merge, and how a record from another computer lands here. A new kind of record is a `kinds.Kind` added to REGISTERED (see sync/kinds.py).

Kinds and their ids (the id is a keyed hash of the kind and its key fields, `DataKey.record_id`):
- pull: one history entry, by its id. Entries never change once made, so two copies are the same entry.
- saved: a saved name -> the pull it names, and when it was named. A name stands for one pull and a pull has one
  name (as on one computer: naming again moves the name). Two computers naming differently: the later naming wins;
  both pulls stay in history, so nothing is lost.
- note: a note on a table or column of a source, by (source, table, column). Edits on two computers at once are
  kept both: the note becomes both texts, one after the other. An edit beats a deletion.
- rule: a block on a table or column, by (source, table, column), case-insensitive like the rules themselves.
  Blocks merge as a union: a block from any computer is added here at once. A block removed on another computer is
  NOT removed here: removing a block needs the user's yes on each computer (#39), so it stays until they unblock it
  here ("held").

Not synced, and why: kept result rows and the values the AI was sent (#45, #47) are result values, sealed under
this computer's own key and kept 30 days; syncing them would send the data itself off the machine.
A source's mode (#117) syncs by its name (sync/mode_records.py). Connections stay: hosts and file paths are this computer's, and passwords live in its keychain. Drafts of context not yet approved and the record of the AI's assumptions stay on
this computer (#97)."""

from __future__ import annotations

import json
from typing import Any

from portward_free.rules import Rule
from portward_free.storage import PullRecord
from portward_free import stand_ins
from portward_free.hosted import inbox
from portward_free.sync import mode_records, profile_records, read_records
from portward_free.sync.kinds import Content, Kind, Landing, Place, secret_kind

__all__ = ["KINDS", "REGISTERED", "Content", "Kind", "Landing", "Place", "apply", "canonical", "deletable", "gone",
           "here", "key_of", "local_state", "merge", "order", "tombstone", "valid"]

KINDS = ("pull", "saved", "note", "rule")  # the first kinds, built in here; also the order records are applied in

# Kinds added through the seam (sync/kinds.py), applied after the first four in this order: profiles (with their
# approved context, #95) and where they're applied, the AI log's summary (#98) and the anonymising key (#93).
def _anon_key(place: Place) -> bytes | None:
    """This computer's anonymising key (#93), or None: none made yet, or the keychain can't give it now (then it
    isn't sent this run; nothing here changes)."""
    if place.keychain is None:
        return None
    try:
        return stand_ins.key_for_sync(place.keychain)
    except stand_ins.StandInsUnavailable:
        return None


def _adopt_anon_key(place: Place, key: bytes) -> None:
    if place.keychain is None:
        raise stand_ins.StandInsUnavailable("there's no keychain here to keep the anonymising key in")
    stand_ins.adopt(place.keychain, key)


# the anonymising key (#93): one per account, the copy already on the account wins, so stand-ins match everywhere
ANON_KEY = secret_kind("anon_key", _anon_key, _adopt_anon_key)

def _inbox_key(place: Place) -> bytes | None:
    return None if place.keychain is None else inbox.inbox_key(place.keychain, create=False)


def _adopt_inbox_key(place: Place, key: bytes) -> None:
    if place.keychain is None:
        raise ValueError("there's no keychain here to keep the inbox key in")
    inbox.adopt_inbox_key(place.keychain, key)


# the hosted engine's inbox key (#104): any of the account's computers can collect what the engine sealed to it
HOSTED_INBOX_KEY = secret_kind("hosted_inbox_key", _inbox_key, _adopt_inbox_key)

REGISTERED: dict[str, Kind] = {k.name: k for k in (*profile_records.KINDS, read_records.READ, ANON_KEY,
                                                   HOSTED_INBOX_KEY, mode_records.SEND_MODE)}


def order(kind: str) -> int:
    """Where records of `kind` come when a batch lands."""
    return KINDS.index(kind) if kind in KINDS else len(KINDS) + list(REGISTERED).index(kind)


def deletable(kind: str) -> bool:
    """Whether sync deletes a record of `kind` that's gone here (else the copy on the service is put back)."""
    if kind in REGISTERED:
        return REGISTERED[kind].tombstone is not None
    return kind != "pull"


def canonical(content: Content | None) -> str | None:
    return None if content is None else json.dumps(content, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=False)


def key_of(content: Content) -> tuple[str, ...]:
    """The kind and key fields the record id is made from."""
    kind = content["kind"]
    if kind in REGISTERED:
        return (kind, *REGISTERED[kind].key(content))
    if kind == "pull":
        return ("pull", content["id"])
    if kind == "saved":
        return ("saved", content["name"])
    if kind == "note":
        return ("note", content["source"], content["table"], content["column"])
    if kind == "rule":
        return ("rule", content["source"], content["table"].lower(), (content["column"] or "").lower())
    raise ValueError(f"unknown kind {kind!r}")


def valid(content: Any) -> bool:
    """A record another computer sent has the shape this Portward writes (a damaged or foreign one is skipped)."""
    if not isinstance(content, dict) or (content.get("kind") not in KINDS and content.get("kind") not in REGISTERED):
        return False
    if content["kind"] in REGISTERED:
        return REGISTERED[content["kind"]].valid(content)
    text = lambda k: isinstance(content.get(k), str)  # noqa: E731
    kind = content["kind"]
    if kind == "pull":
        return (all(map(text, ("id", "question", "sql", "source", "created_at", "result_hash")))
                and isinstance(content.get("tables"), list) and isinstance(content.get("columns"), list)
                and isinstance(content.get("row_count"), int)
                and (content.get("parent_id") is None or text("parent_id")))
    if kind == "saved":
        return text("name") and text("saved_at") and isinstance(content.get("deleted"), bool) and \
            (content.get("pull_id") is None or text("pull_id"))
    if kind == "note":
        return text("source") and text("table") and text("column") and \
            (content.get("note") is None or text("note"))
    return text("source") and text("table") and (content.get("column") is None or text("column")) and \
        isinstance(content.get("blocked"), bool)


def tombstone(remote: Content) -> Content:
    """The record saying `remote`'s thing is gone here (for a kind that can go)."""
    kind = remote["kind"]
    if kind in REGISTERED and REGISTERED[kind].tombstone is not None:
        return REGISTERED[kind].tombstone(remote)
    if kind == "saved":
        return {**remote, "deleted": True, "pull_id": None}
    if kind == "note":
        return {**remote, "note": None}
    if kind == "rule":
        return {**remote, "blocked": False}
    raise ValueError("history entries aren't deleted by sync")


def gone(content: Content) -> bool:
    if content["kind"] in REGISTERED:
        return REGISTERED[content["kind"]].gone(content)
    return (content["kind"] == "saved" and content["deleted"]) or (content["kind"] == "note" and
                                                                   content["note"] is None) or \
        (content["kind"] == "rule" and not content["blocked"])


# --- this computer's state ---------------------------------------------------------------------------------------------
def local_state(place: Place) -> list[Content]:
    """Every synced thing this computer holds now."""
    store = place.store
    out: list[Content] = []
    for p in store.all_pulls():
        out.append({"kind": "pull", "id": p.id, "parent_id": p.parent_id, "question": p.question, "sql": p.sql,
                    "source": p.source, "tables": list(p.tables), "columns": list(p.columns),
                    "created_at": p.created_at, "row_count": p.row_count, "result_hash": p.result_hash})
    for name, pull_id, saved_at in store.saved_names():
        out.append({"kind": "saved", "name": name, "pull_id": pull_id, "saved_at": saved_at, "deleted": False})
    for source, table, column, note in store.all_notes():
        out.append({"kind": "note", "source": source, "table": table, "column": column, "note": note})
    for rule in store.rules():
        out.append({"kind": "rule", "source": rule.source, "table": rule.table, "column": rule.column,
                    "blocked": True})
    for kind in REGISTERED.values():
        out.extend({**c, "kind": kind.name} for c in kind.local(place))
    return out


def here(place: Place, content: Content) -> bool:
    """Whether this computer can hold `content` now: a note needs its source here (notes go with a connection)."""
    if content["kind"] in REGISTERED:
        return REGISTERED[content["kind"]].here(place, content)
    return content["kind"] != "note" or place.store.connection(content["source"]) is not None


# --- merging two versions --------------------------------------------------------------------------------------------
def merge(local: Content | None, remote: Content) -> Content:
    """One record changed on this computer (`local`; None: gone here) and on another (`remote`) since they last
    agreed. Never drops what either side wrote (see the module's rules)."""
    kind = remote["kind"]
    if kind in REGISTERED:
        return REGISTERED[kind].merge(local, remote)
    if kind == "pull":
        return remote
    mine = local if local is not None else tombstone(remote)
    if kind == "saved":
        return max((remote, mine), key=lambda c: (c["saved_at"], c["pull_id"] or ""))  # a tie: remote
    if kind == "note":
        if gone(mine) or mine["note"] == remote["note"]:
            return remote
        if gone(remote):
            return mine
        if mine["note"] in remote["note"]:
            return remote
        if remote["note"] in mine["note"]:
            return mine
        return {**remote, "note": f"{remote['note']}\n\n{mine['note']}"}
    return {**remote, "blocked": remote["blocked"] or mine["blocked"]}


# --- landing here ----------------------------------------------------------------------------------------------------
def apply(place: Place, content: Content, current: Content | None, landing: Landing) -> Content | None:
    """Make this computer hold `content` (a record as agreed with the service). Returns what this computer then
    holds for it (None: nothing, e.g. a note waiting for its source)."""
    kind = content["kind"]
    if kind in REGISTERED:
        return REGISTERED[kind].apply(place, content, current, landing)
    store = place.store
    if kind == "pull":
        if current is None:
            store.add_synced_pulls([PullRecord(
                id=content["id"], parent_id=content["parent_id"], question=content["question"], sql=content["sql"],
                source=content["source"], tables=tuple(content["tables"]), columns=tuple(content["columns"]),
                created_at=content["created_at"], row_count=content["row_count"],
                result_hash=content["result_hash"])])
        return content
    if kind == "saved":
        if content["deleted"]:
            if current is not None:
                store.unsave_name(content["name"])
            return None
        if store.pull(content["pull_id"]) is None:
            return None
        store.save_name(content["pull_id"], content["name"], content["saved_at"])
        return content
    if kind == "note":
        if not here(place, content):
            return None
        store.set_note(content["source"], content["table"], content["column"], content["note"] or "")
        return None if content["note"] is None else content
    rule = Rule(content["source"], content["table"], content["column"])
    if content["blocked"]:
        store.add_rule(rule)
        return content
    if current is not None:  # removed elsewhere: stays blocked here until the user says so here (#39)
        landing.held_rules += 1
        return current
    return None
