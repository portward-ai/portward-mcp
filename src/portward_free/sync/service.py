"""The sync service's API in the control app (/v1/sync/..., #55), over HTTP with this computer's device token. It only
ever carries wraps and sealed records; nothing here is readable without keys the service doesn't have."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from portward_free.account import AccountService, AccountServiceError

STATUS_ENDED = 402  # Personal has ended: sync stops (#54)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise AccountServiceError("the sync service sent something this Portward can't read")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        raise AccountServiceError("the sync service sent something this Portward can't read") from None


@dataclass(frozen=True)
class RemoteKeys:
    key_id: str
    recovery: bytes
    device: bytes | None
    previous_key_id: str | None = None  # after the recovery key was replaced elsewhere: the key this one held
    handover: bytes | None = None  # the current data key sealed under that previous one


@dataclass(frozen=True)
class NewKeys:
    """A replaced recovery key and the new data key: what `rotate` sends."""
    key_id: str
    recovery: bytes
    device: bytes
    handover: bytes
    verifier: str


@dataclass(frozen=True)
class Remote:
    id: str
    version: int
    seq: int
    ciphertext: bytes


@dataclass(frozen=True)
class Written:
    saved: dict[str, int]  # id -> its new version
    conflicts: tuple[Remote, ...]
    missing: frozenset[str] = frozenset()
    seqs: dict[str, int] = field(default_factory=dict)  # id -> the change counter its write got  # written on a version the service no longer has (restored from a backup)


def _int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AccountServiceError("the sync service sent something this Portward can't read")
    return value


def _remote(item: Any) -> Remote:
    if not isinstance(item, dict) or not isinstance(item.get("version"), int) or not isinstance(item.get("seq"), int):
        raise AccountServiceError("the sync service sent something this Portward can't read")
    return Remote(str(item.get("id", "")), item["version"], item["seq"], unb64(item.get("ciphertext")))


class SyncService:
    def __init__(self, account_service: AccountService) -> None:
        self._http = account_service

    def keys(self, token: str) -> RemoteKeys | None:
        got = self._http._call("GET", "/v1/sync/keys", token=token)
        if not got.get("set_up"):
            return None
        device, handover, previous = got.get("device"), got.get("handover"), got.get("previous_key_id")
        return RemoteKeys(str(got.get("key_id", "")), unb64(got.get("recovery")),
                          unb64(device) if device else None,
                          previous if isinstance(previous, str) else None, unb64(handover) if handover else None)

    def set_up(self, token: str, key_id: str, recovery: bytes, device: bytes, verifier: str) -> None:
        self._http._call("POST", "/v1/sync/keys", {"key_id": key_id, "recovery": b64(recovery),
                                                   "device": b64(device), "verifier": verifier}, token=token)

    def set_device(self, token: str, device: bytes, key_id: str, proof: str) -> None:
        self._http._call("PUT", "/v1/sync/keys/device", {"device": b64(device), "key_id": key_id, "proof": proof},
                         token=token)

    def rotate(self, token: str, proof: str, new: NewKeys) -> None:
        """A new recovery key and data key; `proof` is for the current data key. The service deletes the records
        under the old key: the computers send theirs again."""
        self._http._call("POST", "/v1/sync/keys/rotate", {
            "proof": proof, "key_id": new.key_id, "recovery": b64(new.recovery), "device": b64(new.device),
            "handover": b64(new.handover), "verifier": new.verifier}, token=token)

    def changes(self, token: str, since: int) -> tuple[list[Remote], bool, int]:
        """(records changed after `since`, whether there are more, the service's latest change counter)."""
        got = self._http._call("GET", f"/v1/sync/records?since={int(since)}", token=token)
        records = got.get("records")
        if not isinstance(records, list):
            raise AccountServiceError("the sync service sent something this Portward can't read")
        return [_remote(r) for r in records], got.get("more") is True, _int(got.get("head"))

    def write(self, token: str, writes: list[tuple[str, int, bytes]]) -> Written:
        got = self._http._call("POST", "/v1/sync/records", {"writes": [
            {"id": record_id, "base": base, "ciphertext": b64(sealed)} for record_id, base, sealed in writes]},
            token=token)
        saved = {str(s.get("id")): _int(s.get("version")) for s in got.get("saved") or [] if isinstance(s, dict)}
        seqs = {str(s.get("id")): _int(s.get("seq")) for s in got.get("saved") or [] if isinstance(s, dict)}
        missing = frozenset(str(m) for m in got.get("missing") or [])
        return Written(saved, tuple(_remote(c) for c in got.get("conflicts") or []), missing, seqs)

    # --- a browser asking to read the AI log for the proof report (#107) ----------------------------------------
    def browsers(self, token: str) -> list[dict[str, Any]]:
        """The browsers waiting for a computer's approval: [{id, public_key, created_at, label}]."""
        got = self._http._call("GET", "/v1/me/browsers", token=token)
        browsers = got.get("browsers")
        if not isinstance(browsers, list):
            raise AccountServiceError("the account service sent something this Portward can't read")
        return [b for b in browsers if isinstance(b, dict)]

    def approve_browser(self, token: str, request_id: str, sealed: bytes) -> None:
        self._http._call("POST", "/v1/me/browsers/approve", {"id": request_id, "sealed": b64(sealed)}, token=token)
