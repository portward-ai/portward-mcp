"""The one HTTP client the free tool uses for Portward's services (the account service, the share service and sync
through the account service): JSON in and out, TLS against certifi's roots, and every failure turned into the
service's own exception with a message that is safe to show. Addresses are https, or http only on this machine
(tests and a local copy)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any, ClassVar
from urllib.parse import urlsplit

import certifi

from portward_free import __version__

TIMEOUT_SECONDS = 20
_LOCAL = ("127.0.0.1", "localhost", "::1")


class ServiceError(Exception):
    """A service refused or couldn't be reached; the message is safe to show. `status` is the HTTP status (0: not
    reached) and `body` the service's JSON answer, if any."""

    def __init__(self, message: str, status: int = 0, body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.body = status, body or {}


def base_url(address: str, what: str) -> str:
    """`scheme://host[:port]` of a service address; ValueError unless it is https (or http on this machine)."""
    parts = urlsplit(address.rstrip("/"))
    local = parts.scheme == "http" and parts.hostname in _LOCAL
    if not (parts.scheme == "https" or local) or not parts.netloc or parts.path or parts.query:
        raise ValueError(f"the {what} address must be https://host (http only on this machine)")
    return f"{parts.scheme}://{parts.netloc}"


def json_object(raw: bytes) -> dict[str, Any]:
    """A JSON object from a response body; {} for anything else."""
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class JsonService:
    """A Portward service over HTTP. Subclasses name themselves (`WHAT`) and their exception (`FAILURE`)."""

    WHAT: ClassVar[str] = "service"
    FAILURE: ClassVar[type[ServiceError]] = ServiceError

    def __init__(self, address: str) -> None:
        self.base_url = base_url(address, self.WHAT)
        self._tls = ssl.create_default_context(cafile=certifi.where())

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
              token: str | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": f"portward/{__version__}"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base_url + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=self._tls) as response:
                return json_object(response.read())
        except urllib.error.HTTPError as err:
            answer = json_object(err.read())
            raise self.FAILURE(str(answer.get("error") or f"the {self.WHAT} said {err.code}"), err.code,
                               answer) from None
        except (urllib.error.URLError, OSError) as err:  # incl. a 413 sent before the body was read (broken pipe)
            raise self.FAILURE(f"couldn't reach the {self.WHAT} at {self.base_url} ({err})") from None
