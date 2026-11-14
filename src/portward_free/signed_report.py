"""The signed proof report (#135): this computer's signing key, the signature file, and checking one.

Each computer makes an Ed25519 key on first use and keeps it in the OS keychain (KEY_ACCOUNT); the private half never
leaves it. Its public half is registered to the user's Portward account (account.register_signing_key, at sign-in
and before each signed export). A signed export writes the PDF and CSV as before, plus a signature file beside them
(SIGNATURE_SUFFIX): JSON naming the key, the date range, the AI log chain as this computer found it (proof_chain),
the hash of each entry reported and the SHA-256 of both files, with an Ed25519 signature over all of that.

`verify` checks a report: the signature, the files beside it against their hashes, and that the key is registered
to the account (or is the public key the person checking was given). What that proves is PROVES, word for word, and
the report says so: that the report came from that computer and wasn't edited after it was made. Not that the
computer itself wasn't tampered with, nor that the AI log it was made from is complete (the chain line says what the
computer found)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from portward_free.keychain import Keychain
from portward_free.proof import AiRead
from portward_free.proof_chain import ChainCheck
from portward_free.proof_report import ProofReport, export_report

KEY_ACCOUNT = "proof-signing-key"  # the keychain entry holding this computer's private key (base64url, 32 bytes)
SIGNATURE_SUFFIX = ".signature.json"
FORMAT = "portward-proof-report-signature-v1"
SIGNED_PREFIX = b"portward proof report signature v1\n"  # so this key's signatures can't be reused for anything else
PROVES = "The signature proves only that this report came from that computer and was not edited after it was made."
FILES = ("csv", "pdf")
MAX_SIGNATURE_FILE = 16 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def key_id(public_key: bytes) -> str:
    """How a report names its key: the first 16 hex digits of the public key's SHA-256 (the control plane's too)."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def signing_key(keychain: Keychain) -> Ed25519PrivateKey:
    """This computer's signing key, made and kept in the keychain on first use. Raises KeychainError."""
    stored = keychain.get(KEY_ACCOUNT)
    if stored:
        try:
            return Ed25519PrivateKey.from_private_bytes(_unb64(stored))
        except (ValueError, binascii.Error):
            pass  # not a key this code wrote: replace it (reports it signed, if any, name the old key)
    key = Ed25519PrivateKey.generate()
    keychain.set(KEY_ACCOUNT, _b64(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                     serialization.NoEncryption())))
    return key


def public_raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_key_text(key: Ed25519PrivateKey) -> str:
    """The public half as it is registered and given to others (base64url)."""
    return _b64(public_raw(key))


def signed_note(kid: str, chain: ChainCheck) -> tuple[str, ...]:
    """What the PDF says about its signature, in place of "It is not signed"."""
    return (f"Signed by the key of the computer that made it ({kid}).", PROVES,
            "Check it with: portward-free verify <this file>, beside its signature file.", chain.said)


def export_signed(folder: Path, report: ProofReport, reads: Sequence[AiRead], chain: ChainCheck,
                  key: Ed25519PrivateKey, account: str) -> tuple[Path, Path, Path]:
    """Write the report (CSV and PDF) and its signature file into `folder`; returns the three paths."""
    public = public_raw(key)
    kid = key_id(public)
    csv_path, pdf_path = export_report(folder, report, signed=signed_note(kid, chain))
    manifest: dict[str, Any] = {
        "format": FORMAT, "proves": PROVES, "key_id": kid, "public_key": _b64(public), "account": account,
        "signed_at": datetime.now(UTC).isoformat(timespec="seconds"), "start": report.start, "end": report.end,
        "reads": report.reads, "rows": report.rows, "chain": chain.as_dict(),
        "entries": [r.hash for r in reads],  # "" for an entry written before chaining
        "files": {"csv": {"name": csv_path.name, "sha256": _sha256(csv_path)},
                  "pdf": {"name": pdf_path.name, "sha256": _sha256(pdf_path)}}}
    manifest["signature"] = _b64(key.sign(SIGNED_PREFIX + _canonical(manifest)))
    signature_path = csv_path.with_name(csv_path.stem + SIGNATURE_SUFFIX)
    with signature_path.open("x", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    return csv_path, pdf_path, signature_path


# --- checking a report -----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    ok: bool
    text: str


class Unverified(Exception):
    """Why a report doesn't verify (the message is the verdict's text)."""


def signature_file(path: Path) -> Path:
    """The signature file for a report's CSV or PDF (or the signature file itself)."""
    return path if path.name.endswith(SIGNATURE_SUFFIX) else path.with_name(path.stem + SIGNATURE_SUFFIX)


def _manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Unverified(f"There is no signature file beside it ({path.name}): an unsigned report can't be verified.")
    try:
        if path.stat().st_size > MAX_SIGNATURE_FILE:
            raise ValueError("too big")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise Unverified(f"{path.name} isn't a Portward signature file.") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise Unverified(f"{path.name} isn't a Portward signature file.")
    return manifest


def _signed_by(manifest: dict[str, Any]) -> tuple[str, bytes]:
    """(key id, public key) after checking the signature over everything else in the file."""
    try:
        public = _unb64(str(manifest["public_key"]))
        signature = _unb64(str(manifest["signature"]))
        body = {k: v for k, v in manifest.items() if k != "signature"}
        Ed25519PublicKey.from_public_bytes(public).verify(signature, SIGNED_PREFIX + _canonical(body))
    except (KeyError, ValueError, binascii.Error, InvalidSignature):
        raise Unverified("The signature doesn't match the signature file: it was edited after it was signed, or "
                         "wasn't made by Portward.") from None
    kid = key_id(public)
    if manifest.get("key_id") != kid:
        raise Unverified("The signature file names a different key from the one that signed it.")
    return kid, public


def _checked_files(manifest: dict[str, Any], folder: Path, asked: Path) -> list[str]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise Unverified("The signature file lists no report files.")
    checked = []
    for kind in FILES:
        entry = files.get(kind)
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or Path(name).name != name or not name:
            raise Unverified("The signature file lists no report files.")
        path = folder / name
        if not path.is_file():
            continue
        if _sha256(path) != entry.get("sha256"):
            raise Unverified(f"{name} was edited after it was signed: it isn't the report that computer made.")
        checked.append(name)
    if asked.name not in (*checked, signature_file(asked).name) or not checked:
        raise Unverified(f"{asked.name} isn't one of the files this signature covers, or they aren't beside it.")
    return checked


def _trusted(kid: str, public: bytes, given: str | None,
             registered: Callable[[], dict[str, str] | None]) -> str:
    """How the key is known, or Unverified."""
    if given is not None:
        try:
            matches = _unb64(given.strip()) == public
        except (ValueError, binascii.Error):
            matches = False
        if not matches:
            raise Unverified(f"It was signed by key {kid}, not the key you gave.")
        return "the public key you gave"
    try:
        keys = registered()
    except Exception as err:  # noqa: BLE001 - the account service or keychain; said, never swallowed
        raise Unverified(f"Portward couldn't ask the account for its registered keys ({err}).") from None
    if keys is None:
        raise Unverified(f"It was signed by key {kid}, but Portward can't check whose key that is: sign this "
                         "computer in to the account, or pass --public-key with the key the account owner gave you.")
    known = keys.get(kid)
    try:
        registered_here = known is not None and _unb64(known) == public
    except (ValueError, binascii.Error):
        registered_here = False
    if not registered_here:
        raise Unverified(f"It was signed by key {kid}, which is not registered to this account.")
    return "registered to this account"


def verify(path: Path, registered: Callable[[], dict[str, str] | None], given: str | None = None) -> Verdict:
    """Check the report at `path` (its CSV, PDF or signature file). `registered` gives the account's keys (key id ->
    public key), or None when this computer can't ask; `given` is a public key the person checking was handed."""
    try:
        signature = signature_file(path)
        manifest = _manifest(signature)
        kid, public = _signed_by(manifest)
        checked = _checked_files(manifest, signature.parent, path)
        known = _trusted(kid, public, given, registered)
    except Unverified as err:
        return Verdict(False, f"Not verified. {err}")
    chain = manifest.get("chain")
    intact = isinstance(chain, dict) and chain.get("intact") is True
    return Verdict(True, (
        f"Verified: {' and '.join(checked)} came from the computer with key {kid} ({known}), signed on "
        f"{manifest.get('signed_at')}, and haven't been edited since. {PROVES} When it was made, that computer found "
        f"the AI log chain {'intact' if intact else 'broken (the report says where)'}."))
