"""Stand-ins (#93, Personal): what the AI sees in place of each value in a column granted anonymised.

A stand-in is `anon_` plus the first 16 hex digits of HMAC-SHA256(key, "anon|" + group + "|" + the value as text).
The group is the column's link group (#121): the columns the profiles' links join into one connected group, or the
column alone when no link names it (`Scopes`). So the same value gets the same stand-in, for one person, within one
column and across columns a link joins: in every query and every connection the profile is applied to (so the AI can
filter, group and join on them), and on every computer and the hosted engine holding the same key and profiles.
Unrelated columns with equal values (customer 42, product 42) get different stand-ins. Values are turned to text by
the database (`CAST(... AS TEXT)`), so text and whole numbers match across connections; how other types print can
differ between databases.

Each source computes stand-ins inside the database (sources/*: `stand_in_expr`), so a WHERE, GROUP BY or JOIN works
on them; `stand_in_for` is the reference every source must reproduce exactly.

The key is the account's anonymising key: 32 random bytes made on first use and kept in the keychain under
KEY_ACCOUNT. It is its own key, never derived from sync's data key, so a new recovery key doesn't change any
stand-in. Sync (#97) carries it to the account's other computers through `key_for_sync` and `adopt`; resetting it
changes every stand-in.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from portward_free.keychain import Keychain, KeychainError

KEY_ACCOUNT = "anonymising-key"
KEY_BYTES = 32
PREFIX = "anon"
HEX_CHARS = 16
GROUP_CHARS = 32
HMAC_BLOCK = 64
GROUP_PATTERN = re.compile(rf"[0-9a-f]{{{GROUP_CHARS}}}")
PATTERN = re.compile(rf"{PREFIX}_[0-9a-f]{{{HEX_CHARS}}}")


class StandInsUnavailable(Exception):
    """The anonymising key can't be read or made. The message is safe to show."""


def stand_in_for(key: bytes, value: Any, group: str) -> str | None:
    """The stand-in for one value already turned to text (None stays None), in link group `group`
    (`Scopes.group`)."""
    if value is None:
        return None
    digest = hmac.new(key, f"{PREFIX}|{group}|{value}".encode(), hashlib.sha256).hexdigest()
    return f"{PREFIX}_{digest[:HEX_CHARS]}"


Pair = tuple[tuple[str, str], tuple[str, str]]  # a link's two ends, each (table, column) in lower case


@dataclass(frozen=True)
class Scopes:
    """The link groups of one connection's applied profiles: which columns share stand-ins.

    Each link joins its two columns; joined columns form groups (connected components). A column's group is named
    after the group's first column in sorted order, so every computer and the hosted engine, holding the same
    profiles, name it the same; a column no link names is a group of its own. Columns are matched by table and
    column name: the guard reads one schema per connection, and a link's schema, if any, doesn't split a group
    (a link names the same column on every connection the profile is applied to)."""

    links: frozenset[Pair] = frozenset()

    @classmethod
    def of(cls, links: Iterable[Pair]) -> Scopes:
        return cls(frozenset(tuple(sorted(pair)) for pair in links))  # type: ignore[misc]

    def group(self, table: str, column: str) -> str:
        """The group `table.column` belongs to, as hex safe to put in SQL text (it names no column there)."""
        here = (table.lower(), column.lower())
        found = self._components().get(here)
        named = f"link:{found[0]}.{found[1]}" if found else f"column:{here[0]}.{here[1]}"
        return hashlib.sha256(named.encode()).hexdigest()[:GROUP_CHARS]

    def groups(self) -> tuple[frozenset[tuple[str, str]], ...]:
        """Every link group: the columns that share stand-ins (link_caps)."""
        found: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for node, first in self._components().items():
            found.setdefault(first, set()).add(node)
        return tuple(frozenset(members) for _, members in sorted(found.items()))

    def _components(self) -> dict[tuple[str, str], tuple[str, str]]:
        """Each linked column -> the first column of its group."""
        parent: dict[tuple[str, str], tuple[str, str]] = {}

        def root(node: tuple[str, str]) -> tuple[str, str]:
            while parent.setdefault(node, node) != node:
                node = parent[node]
            return node

        for one, other in sorted(self.links):
            a, b = root(one), root(other)
            if a != b:
                parent[max(a, b)] = min(a, b)
        return {node: root(node) for node in list(parent)}


def checked_group(group: str) -> str:
    """`group` as it goes into a stand-in's SQL text: only a group `Scopes.group` names (hex) is ever accepted."""
    if not GROUP_PATTERN.fullmatch(group):
        raise ValueError("not a stand-in group")
    return group


def hmac_pads(key: bytes) -> tuple[bytes, bytes]:
    """RFC 2104 inner and outer pads, for databases with SHA-256 but no HMAC function:
    HMAC(K, m) = SHA256(opad || SHA256(ipad || m))."""
    if len(key) > HMAC_BLOCK:
        key = hashlib.sha256(key).digest()
    key = key.ljust(HMAC_BLOCK, b"\0")
    return bytes(b ^ 0x36 for b in key), bytes(b ^ 0x5C for b in key)


def account_key(keychain: Keychain) -> bytes:
    """The account's anonymising key, made and kept on first use. Raises StandInsUnavailable."""
    try:
        stored = keychain.get(KEY_ACCOUNT)
        if stored is None:
            raw = os.urandom(KEY_BYTES)
            keychain.set(KEY_ACCOUNT, base64.b64encode(raw).decode())
            return raw
    except KeychainError as err:
        raise StandInsUnavailable(f"the anonymising key can't be read ({err})") from None
    return _decoded(stored)


def key_or_none(keychain: Keychain) -> bytes | None:
    """The account's anonymising key, or None when the keychain can't give it (anonymised columns are then hidden,
    never shown in full)."""
    try:
        return account_key(keychain)
    except StandInsUnavailable:
        return None


def key_for_sync(keychain: Keychain) -> bytes | None:
    """The key as it is now, for sync to seal and send to the account's other computers; None if none was made."""
    try:
        stored = keychain.get(KEY_ACCOUNT)
    except KeychainError as err:
        raise StandInsUnavailable(f"the anonymising key can't be read ({err})") from None
    return None if stored is None else _decoded(stored)


def adopt(keychain: Keychain, key: bytes) -> None:
    """Keep `key` (from another of the account's computers) as this computer's anonymising key, so both give the
    same stand-ins."""
    if len(key) != KEY_BYTES:
        raise StandInsUnavailable("that anonymising key isn't the right length")
    try:
        keychain.set(KEY_ACCOUNT, base64.b64encode(key).decode())
    except KeychainError as err:
        raise StandInsUnavailable(f"the anonymising key can't be saved ({err})") from None


def _decoded(stored: str) -> bytes:
    try:
        raw = base64.b64decode(stored, validate=True)
    except binascii.Error:
        raw = b""
    if len(raw) != KEY_BYTES:
        raise StandInsUnavailable("the anonymising key in the keychain is damaged")
    return raw
