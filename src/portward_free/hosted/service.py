"""The control app's hosted-route API (#104), over HTTP: the computer's side and the engine's side.

Every call carries a device token: a computer's, or the engine's own (given to it in its env when it was made).
What goes to the service is ciphertext (hosted/seal.py) and the engine's public key, never a login or a profile."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from portward_free.account import AccountService, AccountServiceError


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def unb64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise AccountServiceError("the account service sent something that isn't base64")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise AccountServiceError("the account service sent something that isn't base64") from None


@dataclass(frozen=True)
class EngineInfo:
    """What the service says about the account's engine."""
    address: str
    label: str
    state: str
    public_key: bytes | None
    key_id: str | None
    version: int

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> EngineInfo:
        address, label = raw.get("address"), raw.get("label")
        version = raw.get("version")
        if not isinstance(address, str) or not address.startswith("https://") or not isinstance(label, str) \
                or not isinstance(version, int):
            raise AccountServiceError("the account service's answer about the hosted engine didn't make sense")
        key = raw.get("public_key")
        return cls(address, label, str(raw.get("state") or ""), unb64(key) if key is not None else None,
                   raw.get("key_id") if isinstance(raw.get("key_id"), str) else None, version)


@dataclass(frozen=True)
class SealedBundle:
    version: int
    key_id: str
    sealed: bytes


class HostedService(AccountService):
    # --- a computer -------------------------------------------------------------------------------------------------
    def ensure(self, token: str) -> EngineInfo:
        """The account's engine, made (and started) if needed. 402 Personal is off, 503 no hosted route here."""
        return EngineInfo.parse(self._call("POST", "/v1/me/hosted", {}, token=token))

    def engine(self, token: str) -> EngineInfo | None:
        try:
            return EngineInfo.parse(self._call("GET", "/v1/me/hosted", token=token))
        except AccountServiceError as err:
            if err.status == 404:
                return None
            raise

    def put_bundle(self, token: str, version: int, key_id: str, sealed: bytes) -> None:
        """409 (body: version, key_id) when the service holds a newer version or the engine has another key."""
        self._call("PUT", "/v1/me/hosted/bundle", {"version": version, "key_id": key_id, "sealed": b64(sealed)},
                   token=token)

    def remove(self, token: str) -> None:
        self._call("DELETE", "/v1/me/hosted", token=token)

    def reads(self, token: str) -> list[tuple[str, bytes]]:
        answer = self._call("GET", "/v1/me/hosted/reads", token=token).get("reads")
        if not isinstance(answer, list):
            raise AccountServiceError("the account service's answer about hosted reads didn't make sense")
        return [(str(r.get("id")), unb64(r.get("sealed"))) for r in answer if isinstance(r, dict)]

    def ack(self, token: str, ids: list[str]) -> None:
        self._call("POST", "/v1/me/hosted/reads/ack", {"ids": ids}, token=token)

    # --- the engine -------------------------------------------------------------------------------------------------
    def register_key(self, token: str, public_key: bytes) -> str:
        return str(self._call("PUT", "/v1/engine/key", {"public_key": b64(public_key)}, token=token)["key_id"])

    def bundle(self, token: str) -> SealedBundle | None:
        try:
            raw = self._call("GET", "/v1/engine/bundle", token=token)
        except AccountServiceError as err:
            if err.status == 404:
                return None
            raise
        if not isinstance(raw.get("version"), int) or not isinstance(raw.get("key_id"), str):
            raise AccountServiceError("the account service's bundle didn't make sense")
        return SealedBundle(raw["version"], raw["key_id"], unb64(raw.get("sealed")))

    def send_reads(self, token: str, sealed: bytes) -> None:
        self._call("POST", "/v1/engine/reads", {"sealed": b64(sealed)}, token=token)
