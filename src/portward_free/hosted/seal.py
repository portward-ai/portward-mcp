"""Sealing to a public key (#104): what the computer sends the hosted engine, and what the engine sends back.

A sealed box: a fresh X25519 key pair per message, its shared secret with the recipient's public key through
HKDF-SHA256 (salt: both public keys; info: the purpose) into an AES-256-GCM key, and associated data naming the
purpose and the account. Only the holder of the recipient's private key opens it; the control plane, which carries
it, can't. Format: 1 byte version, 32 bytes ephemeral public key, 12 bytes nonce, ciphertext and tag.

Two uses, each with its own purpose string, so a blob made for one never opens as the other:
- BUNDLE: the computer to the engine, sealed to the engine's key (made on the engine's volume, hosted/engine.py).
- READS: the engine to the user's computers, sealed to the account's inbox key (hosted/inbox.py)."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

FORMAT = 1
KEY_BYTES = 32
_NONCE = 12
BUNDLE = "portward-hosted-bundle-v1"
READS = "portward-hosted-reads-v1"


class Unopenable(Exception):
    """It didn't open: another key, another account or purpose, or changed bytes."""


def new_private_key() -> bytes:
    return X25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def public_key(private: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def key_id(public: bytes) -> str:
    """The public key's short id, as the control plane names it (personal_hosted.key_id_of)."""
    return hashlib.sha256(public).hexdigest()[:16]


def _key(shared: bytes, ephemeral: bytes, recipient: bytes, purpose: str) -> bytes:
    return HKDF(hashes.SHA256(), 32, salt=ephemeral + recipient, info=purpose.encode()).derive(shared)


def _aad(purpose: str, account_id: str) -> bytes:
    return f"{purpose}\n{account_id}".encode()


def seal(recipient: bytes, purpose: str, account_id: str, content: dict[str, Any]) -> bytes:
    if len(recipient) != KEY_BYTES:
        raise ValueError("a recipient key is 32 bytes")
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient))
    nonce = os.urandom(_NONCE)
    body = AESGCM(_key(shared, ephemeral_public, recipient, purpose)).encrypt(
        nonce, json.dumps(content, separators=(",", ":")).encode(), _aad(purpose, account_id))
    return bytes([FORMAT]) + ephemeral_public + nonce + body


def open_sealed(private: bytes, purpose: str, account_id: str, blob: bytes) -> dict[str, Any]:
    if len(blob) < 1 + KEY_BYTES + _NONCE + 16 or blob[0] != FORMAT:
        raise Unopenable("not a sealed box this Portward made")
    ephemeral_public, nonce = blob[1:1 + KEY_BYTES], blob[1 + KEY_BYTES:1 + KEY_BYTES + _NONCE]
    mine = X25519PrivateKey.from_private_bytes(private)
    recipient = mine.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    try:
        shared = mine.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
        raw = AESGCM(_key(shared, ephemeral_public, recipient, purpose)).decrypt(
            nonce, blob[1 + KEY_BYTES + _NONCE:], _aad(purpose, account_id))
    except (InvalidTag, ValueError):
        raise Unopenable("it doesn't open with this key") from None
    content = json.loads(raw)
    if not isinstance(content, dict):
        raise Unopenable("not a sealed object")
    return content
