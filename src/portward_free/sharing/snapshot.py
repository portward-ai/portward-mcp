"""A share's snapshot, sealed on the sender's machine (#50).

The snapshot is the pull's table as JSON, sealed with AES-256-GCM under a fresh random key for each share: the
sealed form is 0x01 | 12-byte nonce | ciphertext and tag, with SHARE_AAD as associated data, base64url without
padding. Only that goes to the share service; the key goes in the link's # fragment, which browsers never send. The
view page (the control app's share_static/view.js) opens it with WebCrypto the same way.

Version 2 adds `allow_download` (2026-11-04): whether the sender lets the teammate download the table as a CSV. It is
sealed with the rows, so the service never learns it and nobody can change it once the share is made. A version 1
snapshot has no such field and is view only, as is anything but a real `true` (allows_download)."""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from portward_free.pull import Pull

SHARE_AAD = b"portward-share-v1"
SNAPSHOT_VERSION = 2
MAX_SEALED_BYTES = 5 * 1024 * 1024  # the share service's cap (control shares.MAX_SNAPSHOT_BYTES); checked before upload
_FORMAT = 1
_NONCE_BYTES = 12


class SnapshotLocked(Exception):
    """The key doesn't open the sealed snapshot, or it isn't one."""


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as err:
        raise SnapshotLocked("not base64url") from err


def cell(value: Any) -> Any:
    """A value as the view page shows it: JSON numbers, text, booleans and nulls stay; anything else is text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def snapshot_of(pull: Pull, *, title: str, pulled_at: str, allow_download: bool = False) -> dict[str, Any]:
    """The table as shared. `pull` must come from run_pull under the source's current rules, so blocked columns
    are already gone (#39); #52's per-column options (hide, anonymise, totals) apply on top of this."""
    return {"v": SNAPSHOT_VERSION, "title": title, "source": pull.source, "pulled_at": pulled_at,
            "columns": list(pull.columns), "rows": [[cell(v) for v in row] for row in pull.rows],
            "row_count": len(pull.rows), "truncated": pull.truncated, "allow_download": allow_download is True}


def allows_download(snapshot: dict[str, Any]) -> bool:
    """Whether the teammate may download this snapshot: only when the sender said so (view.js decides the same)."""
    return snapshot.get("allow_download") is True


def seal_snapshot(snapshot: dict[str, Any]) -> tuple[str, str]:
    """(key, sealed), both base64url. A new key every time: one key never seals two shares."""
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(_NONCE_BYTES)
    plain = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False).encode()
    return b64(key), b64(bytes([_FORMAT]) + nonce + AESGCM(key).encrypt(nonce, plain, SHARE_AAD))


def sealed_size(sealed: str) -> int:
    """The bytes a base64url sealed snapshot stands for: what the service's size cap counts."""
    return len(sealed) * 3 // 4


def open_snapshot(sealed: str, key: str) -> dict[str, Any]:
    """What the view page does, for tests and tools: open a sealed snapshot with its key."""
    raw, secret = unb64(sealed), unb64(key)
    if len(raw) < 1 + _NONCE_BYTES + 16 or raw[0] != _FORMAT or len(secret) != 32:
        raise SnapshotLocked("not a sealed snapshot this Portward knows")
    try:
        plain = AESGCM(secret).decrypt(raw[1:1 + _NONCE_BYTES], raw[1 + _NONCE_BYTES:], SHARE_AAD)
    except InvalidTag as err:
        raise SnapshotLocked("the key doesn't open this snapshot") from err
    return json.loads(plain)
