"""The share service, over HTTP (#50). It is the control app's /v1/share... and /v1/shares routes; this package never
imports that code. The address is PORTWARD_SHARE_URL (default DEFAULT_SHARE_URL): https, or http only on this
machine (for tests and a local copy). Links are built here from that address, never from what the service returns,
so a service can't point a link (and its key) somewhere else."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from portward_free.http_json import JsonService, ServiceError

DEFAULT_SHARE_URL = "https://share.portward.ai"
SHARE_URL_ENV = "PORTWARD_SHARE_URL"
SHARE_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class ShareServiceError(ServiceError):
    """The share service refused or couldn't be reached; the message is safe to show. `status` is the HTTP status
    (0: not reached)."""


@dataclass(frozen=True)
class Created:
    id: str
    expires_at: str


def solve_altcha(challenge: dict[str, Any]) -> str:
    """The service's bot check (ALTCHA proof of work): about a second of hashing, done here."""
    salt, target = str(challenge["salt"]), str(challenge["challenge"])
    for number in range(int(challenge["maxnumber"]) + 1):
        if hashlib.sha256(f"{salt}{number}".encode()).hexdigest() == target:
            answer = {"algorithm": challenge["algorithm"], "challenge": target, "number": number, "salt": salt,
                      "signature": challenge["signature"]}
            return base64.b64encode(json.dumps(answer).encode()).decode()
    raise ShareServiceError("the share service's robot check couldn't be solved")


class ShareService(JsonService):
    WHAT = "share service"
    FAILURE = ShareServiceError

    @classmethod
    def from_env(cls) -> ShareService:
        return cls(os.environ.get(SHARE_URL_ENV, DEFAULT_SHARE_URL))

    def link(self, share_id: str, key: str) -> str:
        if not SHARE_ID.fullmatch(share_id):
            raise ShareServiceError("the share service gave an id that isn't one")
        return f"{self.base_url}/s/{quote(share_id)}#{key}"

    def start_sender(self, email: str, name: str, device: str) -> str:
        """Email a code to `email`; the verification id to confirm it with. `device` is a random secret this computer
        keeps: the code confirms only with it, so a code and verification id alone are no use elsewhere."""
        answer = solve_altcha(self._call("GET", "/v1/challenge"))
        started = self._call("POST", "/v1/share/senders",
                             {"email": email, "name": name, "device": device, "altcha": answer})
        return str(started["verification"])

    def confirm_sender(self, verification: str, code: str, device: str) -> dict[str, Any]:
        """{token, email, name, expires_at}: the device-bound share token for this machine."""
        return self._call("POST", "/v1/share/senders/confirm",
                          {"verification": verification, "code": code, "device": device})

    def rotate_sender(self, token: str) -> dict[str, Any]:
        """The same as confirm_sender, with a new token; `token` stops working on the service at once."""
        return self._call("POST", "/v1/share/senders/rotate", {}, token)

    def create(self, token: str, recipient: str, sealed: str, days: int) -> Created:
        made = self._call("POST", "/v1/shares", {"recipient": recipient, "ciphertext": sealed, "days": days}, token)
        return Created(str(made["id"]), str(made["expires_at"]))

