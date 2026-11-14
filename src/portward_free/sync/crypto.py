"""The keys of encrypted sync (#55), all made and used on the user's computers.

- The data key: 32 random bytes per account, made by the first computer that turns sync on. Records are sealed
  under keys derived from it (HKDF-SHA256): one for AES-256-GCM, one for keyed record ids (HMAC-SHA256), so the
  service sees neither what a record is nor which local thing it stands for. Its public id (`key_id`) is a keyed
  hash of it.
- The recovery key: 160 random bits shown to the user once, as 32 base32 letters and digits in groups of four. It
  never leaves the computer it is shown or typed on. Argon2id (RFC 9106's second recommended setting: 64 MiB, 3
  passes, 4 lanes; random 16-byte salt) turns it into the key that wraps the data key for the service to keep.
- A device key: 32 random bytes in each computer's keychain (DEVICE_KEY_ACCOUNT) that wraps the data key for the
  service to keep, per device session. Computers unwrap the data key at each sync and never store it.
- A proof (`DataKey.proof`): 32 bytes derived from the data key that a computer shows the service to file its wrap
  or replace the recovery key; the service keeps only its SHA-256 (`DataKey.verifier`), so a device token alone can
  do neither. Replacing the recovery key makes a new data key, sealed under the old one for the account's other
  computers to move over with (`seal_handover`).

- The AI log's key (`DataKey.ai_log_key`): the AI log's summary (records of kind "read", #98) is sealed under its own
  key derived from the data key, so a browser can be handed the key to read it for the proof report (#107) without
  being able to open profiles, notes, rules or the other kinds. Records sealed before it are still read.

Every wrap and record is AES-256-GCM with a fresh 96-bit nonce and associated data naming the account (and, for a
record, its id and version), so a blob only opens where it was sealed for. That is also why the service can't slip
in a key of its own: a wrap it made would not open under the recovery key or a device key, and a record it made would
not open under the data key."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import struct
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from portward_free.keychain import Keychain

DEVICE_KEY_ACCOUNT = "sync-device-key"
RECOVERY_BYTES = 20  # 160 bits
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_LOOKALIKES = str.maketrans({"0": "O", "1": "I", "8": "B"})
_FORMAT = 1
_NONCE = 12
AI_LOG_KINDS = frozenset({"read"})  # the kinds sealed under the AI log's key (#107)
# Argon2id: RFC 9106 §4, second recommended option. Kept in each wrap so it can be raised later.
ARGON_MEMORY_KIB, ARGON_PASSES, ARGON_LANES = 64 * 1024, 3, 4
# What a wrap may ask for: a hostile service can't make a computer burn a gigabyte before the tag check fails.
_MIN_MEMORY_KIB, _MAX_MEMORY_KIB, _MAX_PASSES, _MAX_LANES = 19 * 1024, 256 * 1024, 8, 8


class Unopenable(Exception):
    """A key or record didn't open: the wrong recovery key, a key that was replaced, or bytes that were changed."""


@dataclass(frozen=True)
class DataKey:
    raw: bytes

    def _derive(self, info: str) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info.encode()).derive(self.raw)

    @property
    def key_id(self) -> str:
        return hmac.new(self._derive("portward-sync-key-id-v1"), b"key-id", hashlib.sha256).hexdigest()[:32]

    @property
    def proof(self) -> str:
        """Hex: what proves to the service that this computer holds the data key."""
        return self._derive("portward-sync-proof-v1").hex()

    @property
    def verifier(self) -> str:
        """Hex: what the service keeps to check a proof (its SHA-256); it can't make the proof from it."""
        return hashlib.sha256(bytes.fromhex(self.proof)).hexdigest()

    def record_id(self, *parts: str) -> str:
        """The id the service files a record under: a keyed hash of what it is (kind and local key)."""
        message = json.dumps(list(parts), separators=(",", ":"), ensure_ascii=False).encode()
        return hmac.new(self._derive("portward-sync-record-ids-v1"), message, hashlib.sha256).hexdigest()

    @property
    def ai_log_key(self) -> bytes:
        """The AES-256-GCM key the AI log's records are sealed under: all a browser gets for the report (#107)."""
        return self._derive("portward-sync-ai-log-v1")

    def seal(self, account_id: str, record_id: str, version: int, content: dict[str, Any]) -> bytes:
        plain = json.dumps(content, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode()
        nonce = os.urandom(_NONCE)
        key = self.ai_log_key if content.get("kind") in AI_LOG_KINDS else self._derive("portward-sync-records-v1")
        sealed = AESGCM(key).encrypt(nonce, plain, _record_aad(account_id, record_id, version))
        return bytes([_FORMAT]) + nonce + sealed

    def open(self, account_id: str, record_id: str, version: int, blob: bytes) -> dict[str, Any]:
        if len(blob) < 1 + _NONCE + 16 or blob[0] != _FORMAT:
            raise Unopenable("a synced record is in a format this Portward can't read")
        aad = _record_aad(account_id, record_id, version)
        plain, under_ai_log_key = None, False
        for key in (self._derive("portward-sync-records-v1"), self.ai_log_key):
            try:
                plain = AESGCM(key).decrypt(blob[1:1 + _NONCE], blob[1 + _NONCE:], aad)
                break
            except InvalidTag:
                under_ai_log_key = True
        if plain is None:
            raise Unopenable("a synced record doesn't open with this account's key")
        content = json.loads(plain)
        # the AI log's key only ever seals the AI log: a record of another kind under it (made by whoever holds
        # only that key, such as an approved browser) is refused
        if not isinstance(content, dict) or (under_ai_log_key and content.get("kind") not in AI_LOG_KINDS):
            raise Unopenable("a synced record isn't what this Portward wrote")
        return content


def _record_aad(account_id: str, record_id: str, version: int) -> bytes:
    return f"portward-sync-record-v1\n{account_id}\n{record_id}\n{version}".encode()


def new_data_key() -> DataKey:
    return DataKey(AESGCM.generate_key(bit_length=256))


# --- moving to a new data key --------------------------------------------------------------------------------------
def _handover_aad(account_id: str, old_id: str, new_id: str) -> bytes:
    return f"portward-sync-handover-v1\n{account_id}\n{old_id}\n{new_id}".encode()


def seal_handover(old: DataKey, new: DataKey, account_id: str) -> bytes:
    """The new data key sealed under the old one, for computers that hold the old one to move over."""
    nonce = os.urandom(_NONCE)
    key = old._derive("portward-sync-handover-v1")
    return bytes([_FORMAT]) + nonce + AESGCM(key).encrypt(nonce, new.raw, _handover_aad(account_id, old.key_id,
                                                                                        _key_id_of(new.raw)))


def open_handover(blob: bytes, old: DataKey, account_id: str, new_id: str) -> DataKey:
    if len(blob) < 1 + _NONCE + 16 or blob[0] != _FORMAT:
        raise Unopenable("the account's key handover is in a format this Portward can't read")
    try:
        raw = AESGCM(old._derive("portward-sync-handover-v1")).decrypt(
            blob[1:1 + _NONCE], blob[1 + _NONCE:], _handover_aad(account_id, old.key_id, new_id))
    except InvalidTag:
        raise Unopenable("the account's key handover doesn't open with this computer's key") from None
    key = _data_key(raw)
    if key.key_id != new_id:
        raise Unopenable("the account's key handover holds another key")
    return key


def _key_id_of(raw: bytes) -> str:
    return DataKey(raw).key_id


# --- the recovery key -------------------------------------------------------------------------------------------------
def new_recovery_key() -> str:
    raw = base64.b32encode(os.urandom(RECOVERY_BYTES)).decode()
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def parse_recovery_key(typed: str) -> bytes:
    """The key's bytes from what the user typed: spaces, dashes and case don't matter, nor 0/1/8 for O/I/B."""
    cleaned = "".join(ch for ch in typed.upper() if ch.isalnum()).translate(_LOOKALIKES)
    if len(cleaned) != RECOVERY_BYTES * 8 // 5 or any(ch not in _ALPHABET for ch in cleaned):
        raise Unopenable("that isn't a recovery key: it is 32 letters and digits, in groups of four")
    try:
        return base64.b32decode(cleaned)
    except binascii.Error:
        raise Unopenable("that isn't a recovery key: it is 32 letters and digits, in groups of four") from None


def _recovery_aad(account_id: str, header: bytes) -> bytes:
    return f"portward-sync-recovery-v1\n{account_id}\n".encode() + header


def wrap_for_recovery(data_key: DataKey, recovery_key: str, account_id: str) -> bytes:
    """format | memory KiB, passes, lanes | salt | nonce | the data key sealed under Argon2id(recovery key)."""
    salt = os.urandom(16)
    header = bytes([_FORMAT]) + struct.pack(">IBB", ARGON_MEMORY_KIB, ARGON_PASSES, ARGON_LANES) + salt
    kek = _argon(parse_recovery_key(recovery_key), salt, ARGON_MEMORY_KIB, ARGON_PASSES, ARGON_LANES)
    nonce = os.urandom(_NONCE)
    return header + nonce + AESGCM(kek).encrypt(nonce, data_key.raw, _recovery_aad(account_id, header))


def open_with_recovery_key(wrapped: bytes, recovery_key: str, account_id: str) -> DataKey:
    secret = parse_recovery_key(recovery_key)
    header_size = 1 + 6 + 16
    if len(wrapped) < header_size + _NONCE + 16 or wrapped[0] != _FORMAT:
        raise Unopenable("the account's recovery wrap is in a format this Portward can't read")
    header = wrapped[:header_size]
    memory, passes, lanes = struct.unpack(">IBB", header[1:7])
    if not (_MIN_MEMORY_KIB <= memory <= _MAX_MEMORY_KIB and 1 <= passes <= _MAX_PASSES and 1 <= lanes <= _MAX_LANES):
        raise Unopenable("the account's recovery wrap asks for key settings this Portward won't use")
    kek = _argon(secret, header[7:], memory, passes, lanes)
    nonce, body = wrapped[header_size:header_size + _NONCE], wrapped[header_size + _NONCE:]
    try:
        raw = AESGCM(kek).decrypt(nonce, body, _recovery_aad(account_id, header))
    except InvalidTag:
        raise Unopenable("that recovery key doesn't open this account's synced data") from None
    return _data_key(raw)


def _argon(secret: bytes, salt: bytes, memory: int, passes: int, lanes: int) -> bytes:
    return Argon2id(salt=salt, length=32, iterations=passes, lanes=lanes, memory_cost=memory).derive(secret)


def _data_key(raw: bytes) -> DataKey:
    if len(raw) != 32:
        raise Unopenable("the account's data key isn't one this Portward made")
    return DataKey(raw)


# --- this computer's device key -----------------------------------------------------------------------------------------
def device_key(keychain: Keychain, *, create: bool) -> bytes | None:
    """This computer's own key from the keychain; made first if `create`. Raises KeychainError."""
    stored = keychain.get(DEVICE_KEY_ACCOUNT)
    if stored:
        try:
            raw = base64.b64decode(stored, validate=True)
        except binascii.Error:
            raw = b""
        if len(raw) == 32:
            return raw
        if not create:
            return None
    if not create:
        return None
    raw = AESGCM.generate_key(bit_length=256)
    keychain.set(DEVICE_KEY_ACCOUNT, base64.b64encode(raw).decode())
    return raw


def _device_aad(account_id: str) -> bytes:
    return f"portward-sync-device-v1\n{account_id}".encode()


def wrap_for_device(data_key: DataKey, key: bytes, account_id: str) -> bytes:
    nonce = os.urandom(_NONCE)
    return bytes([_FORMAT]) + nonce + AESGCM(key).encrypt(nonce, data_key.raw, _device_aad(account_id))


def open_with_device_key(wrapped: bytes, key: bytes, account_id: str) -> DataKey:
    if len(wrapped) < 1 + _NONCE + 16 or wrapped[0] != _FORMAT:
        raise Unopenable("this computer's key wrap is in a format this Portward can't read")
    try:
        raw = AESGCM(key).decrypt(wrapped[1:1 + _NONCE], wrapped[1 + _NONCE:], _device_aad(account_id))
    except InvalidTag:
        raise Unopenable("this computer's key doesn't open the account's data key") from None
    return _data_key(raw)
