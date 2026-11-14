"""Result values at rest: sealed with AES-256-GCM before they reach the local file, under a key held in the keychain.

Two things are sealed: kept result rows (one blob per pull) and the log of what the AI was sent (`ai_log`, one blob
per response). The key is made on first use and stored base64 in the keychain under KEY_ACCOUNT. Every blob has a
fresh 96-bit nonce and associated data naming what it belongs to (the pull id, or the log's conversation), so a
sealed blob can't be moved onto another entry.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from portward_free.keychain import Keychain, KeychainError
from portward_free.pull import typed_value

KEY_ACCOUNT = "kept-rows-key"
KEEP_DAYS = 30  # kept rows older than this are pruned
KEEP_ROWS_SETTING = "keep_rows"  # the setting that turns keeping on: "on" or "off" (the default)
_NONCE_BYTES = 12
_FORMAT = 1  # first byte of a sealed blob


class RowsLocked(Exception):
    """Kept rows can't be opened: the key is missing from the keychain, or doesn't match."""


@dataclass(frozen=True)
class KeptRows:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool


def key(keychain: Keychain, *, create: bool) -> bytes | None:
    """The key from the keychain; made and stored first if `create` and there is none. Raises KeychainError."""
    stored = keychain.get(KEY_ACCOUNT)
    if stored is not None:
        try:
            raw = base64.b64decode(stored, validate=True)
        except binascii.Error as err:
            raise RowsLocked("the key for kept rows in the keychain is damaged") from err
        if len(raw) != 32:
            raise RowsLocked("the key for kept rows in the keychain is damaged")
        return raw
    if not create:
        return None
    raw = AESGCM.generate_key(bit_length=256)
    keychain.set(KEY_ACCOUNT, base64.b64encode(raw).decode())
    return raw


def seal_json(secret: bytes, associated: str, data: Any) -> bytes:
    """`data` as JSON, sealed: 0x01 | nonce | ciphertext. `associated` must be given again to open it."""
    plain = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    nonce = os.urandom(_NONCE_BYTES)
    return bytes([_FORMAT]) + nonce + AESGCM(secret).encrypt(nonce, plain, associated.encode())


def open_json(secret: bytes, associated: str, sealed: bytes, what: str) -> Any:
    """Open a blob from `seal_json`. Raises RowsLocked (with `what` named in the message) if it can't be opened."""
    if not sealed or sealed[0] != _FORMAT:
        raise RowsLocked(f"{what} are in a format this Portward can't read")
    nonce, body = sealed[1:1 + _NONCE_BYTES], sealed[1 + _NONCE_BYTES:]
    try:
        return json.loads(AESGCM(secret).decrypt(nonce, body, associated.encode()))
    except InvalidTag as err:
        raise RowsLocked(f"the key in the keychain doesn't open {what} (it was replaced)") from err


def stored_key(keychain: Keychain, what: str) -> bytes:
    """The key, to open something sealed earlier. Raises RowsLocked if it's gone, or KeychainError."""
    secret = key(keychain, create=False)
    if secret is None:
        raise RowsLocked(f"the key for {what} isn't in the keychain any more, so they can't be opened")
    return secret


def seal(secret: bytes, pull_id: str, columns: Sequence[str], rows: Sequence[Sequence[Any]],
         truncated: bool) -> bytes:
    return seal_json(secret, pull_id, {"columns": list(columns), "rows": [[typed_value(v) for v in r] for r in rows],
                                       "truncated": truncated})


def open_sealed(keychain: Keychain, pull_id: str, sealed: bytes) -> KeptRows:
    """Decrypt one pull's kept rows. Raises RowsLocked or KeychainError."""
    data = open_json(stored_key(keychain, "kept rows"), pull_id, sealed, "these kept rows")
    return KeptRows(columns=tuple(data["columns"]), rows=tuple(tuple(untyped(v) for v in r) for r in data["rows"]),
                    truncated=bool(data["truncated"]))


def untyped(value: list[Any]) -> Any:
    """A value back from `typed_value`'s form (bytes as bytes; other non-JSON types as their text)."""
    kind, raw = value
    return bytes.fromhex(raw) if kind == "bytes" else raw


__all__ = ["KEEP_DAYS", "KEY_ACCOUNT", "KeptRows", "KeychainError", "RowsLocked", "key", "open_json", "open_sealed",
           "seal", "seal_json", "stored_key", "untyped"]
