"""The hosted engine's AI log summary, on its way into the user's synced history (#104, #98).

The engine writes an `AiRead` for every read it serves (route "hosted"), as a computer does. It can't put them in
sync itself: sync's records are sealed under the account's data key, and the engine never gets that key (it would
open everything the account syncs). Instead the engine seals batches of its entries to the account's inbox key and
leaves them with the control plane (`forward`); the next computer of the account that looks (`collect`: sync, the AI
log, the proof report, web_routes) opens them, adds them to its AI log summary and lets sync carry them everywhere.

The inbox key is an X25519 key pair per account, kept in the computer's keychain (INBOX_ACCOUNT) and synced
between the account's computers as its own secret record (sync/records.py), so any of them can collect. Its public
half goes to the engine inside the sealed bundle. An entry that doesn't say route "hosted" is dropped: the engine
can't write a computer's history."""

from __future__ import annotations

import base64
import binascii
import json
import logging

from portward_free import proof
from portward_free.account import AccountServiceError
from portward_free.hosted import seal
from portward_free.hosted.service import HostedService
from portward_free.keychain import Keychain
from portward_free.storage import LocalStore

log = logging.getLogger("portward.hosted")

INBOX_ACCOUNT = "hosted-inbox-key"
FORWARDED_SETTING = "hosted_forwarded"  # the engine: the last entry it handed on, as "created_at id"
BATCH = 200
BATCH_BYTES = 200 * 1024


def inbox_key(keychain: Keychain, *, create: bool) -> bytes | None:
    """The account's inbox private key from this computer's keychain; made first if `create`."""
    stored = keychain.get(INBOX_ACCOUNT)
    if stored:
        try:
            raw = base64.b64decode(stored, validate=True)
        except (binascii.Error, ValueError):
            raw = b""
        if len(raw) == seal.KEY_BYTES:
            return raw
    if not create:
        return None
    raw = seal.new_private_key()
    keychain.set(INBOX_ACCOUNT, base64.b64encode(raw).decode())
    return raw


def adopt_inbox_key(keychain: Keychain, raw: bytes) -> None:
    """The account's inbox key from another computer (sync): every computer can then collect."""
    if len(raw) != seal.KEY_BYTES:
        raise ValueError("an inbox key is 32 bytes")
    keychain.set(INBOX_ACCOUNT, base64.b64encode(raw).decode())


def collect(store: LocalStore, keychain: Keychain, service: HostedService, token: str, account_id: str) -> int:
    """Take the engine's waiting entries into this computer's AI log summary; how many were added. Batches that
    don't open here are left for a computer that can open them. Raises AccountServiceError."""
    private = inbox_key(keychain, create=False)
    if private is None:
        return 0
    added, taken = 0, []
    for read_id, sealed in service.reads(token):
        try:
            content = seal.open_sealed(private, seal.READS, account_id, sealed)
        except seal.Unopenable:
            continue
        entries = content.get("reads")
        good = [proof.AiRead.from_dict(e) for e in entries if proof.valid_entry(e) and e["route"] == proof.HOSTED] \
            if isinstance(entries, list) else []
        store.reads.add(good)
        added += len(good)
        taken.append(read_id)
    if taken:
        service.ack(token, taken)
    return added


def _mark(read: proof.AiRead) -> str:
    return f"{read.created_at} {read.id}"


def forward(store: LocalStore, service: HostedService, token: str, account_id: str, inbox_public: bytes) -> int:
    """The engine hands on the entries written since the last time, sealed to the inbox key; how many. It keeps
    its own copy (and its place) whatever happens, so a refusal is tried again next time."""
    after = store.setting(FORWARDED_SETTING) or ""
    pending = [r for r in store.reads.between(after[:10] or None) if _mark(r) > after]
    sent = 0
    while pending:
        batch, size = [], 0
        while pending and len(batch) < BATCH and size < BATCH_BYTES:
            entry = pending.pop(0).as_dict()
            batch.append(entry)
            size += len(json.dumps(entry))
        try:
            service.send_reads(token, seal.seal(inbox_public, seal.READS, account_id, {"reads": batch}))
        except AccountServiceError as err:
            log.info("couldn't hand on the AI log summary just now (%s); trying again later", err.status or "-")
            return sent
        last = batch[-1]
        store.set_setting(FORWARDED_SETTING, f"{last['created_at']} {last['id']}")
        sent += len(batch)
    return sent
