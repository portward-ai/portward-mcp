"""The seam for a new kind of synced record (#97): one `Kind` says how its records are keyed, checked, read from
this computer, merged and landed. Kinds listed in records.REGISTERED sync with no change to the engine; the four
first kinds (pull, saved, note, rule) predate this and live in records.py itself.

A record's content is a JSON object whose "kind" is the Kind's name. The engine seals it under the account's data
key, so whatever a kind puts in its content (a profile's grants, a secret key) only ever leaves the computer
encrypted. Replacing the recovery key re-sends every kind's records under the new data key unchanged: anything kept
in a record, a key included, survives it.

`secret_kind` makes the kind for one secret per account (the anonymising key, #93): the copy already on the
account wins, so every computer converges on one key and tokens made from it match across computers."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from portward_free.keychain import Keychain
from portward_free.storage import LocalStore

Content = dict[str, Any]


@dataclass(frozen=True)
class Place:
    """This computer, as a kind sees it: the local file and the keychain (None where a caller has none)."""
    store: LocalStore
    keychain: Keychain | None = None


@dataclass
class Landing:
    """What applying a batch of records did."""
    held_rules: int = 0  # blocks removed elsewhere that stay here until the user unblocks them here
    held_switches: int = 0  # connections switched back to block rules elsewhere that stay on profiles here
    held_modes: int = 0  # sources switched to 'Claude gets results' elsewhere that keep picking here (#117)
    profile_copies: int = 0  # profiles saved on two computers at once: this computer's version kept as a copy


def take_remote(local: Content | None, remote: Content) -> Content:
    return remote


def never(content: Content) -> bool:
    return False


def everywhere(place: Place, content: Content) -> bool:
    return True


@dataclass(frozen=True)
class Kind:
    """How one kind of record syncs.

    - key: the key fields after the kind (the record id is a keyed hash of the kind and these); stable for the
      record's life.
    - valid: whether a record from another computer has the shape this Portward writes (else it's skipped).
    - local: every record of this kind this computer holds now.
    - apply: make this computer hold `content` (with what it held before, or None); return what it then holds, in
      the form `local` would give it (None: nothing, e.g. waiting for something it needs). Return what this computer
      reads back, not the incoming content, so fields this version doesn't keep aren't sent back as a change.
    - merge: the record changed here (`local`, None if gone here) and on another computer since they agreed.
    - tombstone: the record saying it's gone here; None when sync never deletes this kind (a copy gone here is put
      back from the service).
    - gone: whether content is a tombstone.
    - here: whether this computer can hold it now (else it waits, and lands when that changes)."""

    name: str
    key: Callable[[Content], tuple[str, ...]]
    valid: Callable[[Content], bool]
    local: Callable[[Place], Iterable[Content]]
    apply: Callable[[Place, Content, Content | None, Landing], Content | None]
    merge: Callable[[Content | None, Content], Content] = take_remote
    tombstone: Callable[[Content], Content] | None = None
    gone: Callable[[Content], bool] = never
    here: Callable[[Place, Content], bool] = everywhere


# --- one secret per account ------------------------------------------------------------------------------------------
MIN_SECRET, MAX_SECRET = 16, 64  # bytes


def _secret_of(content: Content) -> bytes | None:
    value = content.get("secret")
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except binascii.Error:
        return None
    return raw if MIN_SECRET <= len(raw) <= MAX_SECRET else None


def secret_kind(name: str, read: Callable[[Place], bytes | None], write: Callable[[Place, bytes], None]) -> Kind:
    """A kind for one secret per account. `read` gives this computer's copy (None: none yet); `write` replaces it.
    The secret syncs sealed under the data key and is never deleted by sync. When two computers each made one before
    they first synced, the one already on the account wins and the other computer takes it."""

    def content(raw: bytes) -> Content:
        return {"kind": name, "secret": base64.b64encode(raw).decode()}

    def local(place: Place) -> list[Content]:
        raw = read(place)
        return [] if raw is None else [content(raw)]

    def apply(place: Place, incoming: Content, current: Content | None, landing: Landing) -> Content | None:
        raw = _secret_of(incoming)
        assert raw is not None  # checked by valid
        if read(place) != raw:
            write(place, raw)
        return content(raw)

    return Kind(name=name, key=lambda c: (), valid=lambda c: _secret_of(c) is not None, local=local, apply=apply)
