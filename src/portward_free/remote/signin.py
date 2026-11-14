"""Who may use this computer's MCP over HTTP (#102): only the owner of the Portward account it is signed in to.

The tool is an OAuth resource server (MCP authorization, RFC 9728). claude.ai and ChatGPT read its protected-resource
metadata, sign the user in at the authorization server it names (Portward's sign-in, WorkOS AuthKit) and send an
access token for this computer's address on every request. This computer can't judge that token on its own, so it
asks the account service (`AccountService.mcp_check`) with its device token: is it real, for this address, the same
account, and is Personal on? A good answer is kept for CHECK_SECONDS so a conversation isn't a round trip per call;
a refusal is never kept. A "Personal is off" answer (402) sets `lapsed`: from then on every token is refused, and
RemoteMcp stops serving.

Tokens never reach a log or the AI.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import anyio.to_thread
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from portward_free.account import AccountService, AccountServiceError, account_token
from portward_free.keychain import Keychain, KeychainError
from portward_free.web_app import CLIENT_ID

log = logging.getLogger("portward.remote")

CHECK_SECONDS = 60.0
CONNECTOR = "connector"  # the client id we report when the account service didn't name the token's OAuth client


@dataclass
class ConnectorCheck:
    """The MCP SDK's TokenVerifier for this computer: each token is checked by the account service."""

    keychain: Keychain
    service: AccountService
    resource: str
    remember: float = CHECK_SECONDS
    clock: Callable[[], float] = time.monotonic
    lapsed: threading.Event = field(default_factory=threading.Event)
    # a good answer, by token hash: (kept until, the OAuth client the token was issued to, CONNECTOR when unnamed)
    _good: MappingProxyType[str, tuple[float, str]] = field(default_factory=lambda: MappingProxyType({}), repr=False)

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.lapsed.is_set() or not token:
            return None
        key, now = hashlib.sha256(token.encode()).hexdigest(), self.clock()
        until, client = self._good.get(key, (0.0, CONNECTOR))
        if until > now:
            return self._access(token, client)
        answer = await anyio.to_thread.run_sync(self._ask, token)
        if answer is None:
            return None
        client = _client(answer)
        kept = {k: good for k, good in self._good.items() if good[0] > now}
        self._good = MappingProxyType({**kept, key: (now + self.remember, client)})
        return self._access(token, client)

    def _ask(self, token: str) -> dict[str, Any] | None:
        try:
            device = account_token(self.keychain)
        except KeychainError as err:
            log.warning("the account token couldn't be read from the keychain: %s", err)
            return None
        if not device:
            log.info("connector refused: this computer isn't signed in")
            return None
        try:
            return self.service.mcp_check(device, token, self.resource)
        except AccountServiceError as err:
            if err.status == 402:
                self.lapsed.set()
            log.info("connector refused (%s)", err.status or "account service not reached")
            return None

    def _access(self, token: str, client: str) -> AccessToken:
        return AccessToken(token=token, client_id=client, scopes=[], resource=self.resource)


def _client(answer: dict[str, Any]) -> str:
    """The OAuth client the account service says the token was issued to (#134), else CONNECTOR."""
    found = answer.get("client_id") if isinstance(answer, dict) else None
    return found if isinstance(found, str) and CLIENT_ID.fullmatch(found) and found != CONNECTOR else CONNECTOR


def client_of(ctx: Any) -> str | None:
    """The OAuth client behind an MCP request on the web routes (web_app.WebAppScope): None when unnamed."""
    scope = getattr(getattr(ctx, "request", None), "scope", None)
    user = scope.get("user") if isinstance(scope, dict) else None
    access = getattr(user, "access_token", None)
    client = getattr(access, "client_id", None)
    return client if isinstance(client, str) and client != CONNECTOR else None


@dataclass(frozen=True)
class HttpSignIn:
    """What build_server needs to require sign-in: where connectors sign in, this server's address, the check."""

    authorization_server: str
    resource: str  # this server's MCP address, e.g. https://<name>.<relay domain>/mcp
    verifier: TokenVerifier

    def settings(self) -> AuthSettings:
        """The SDK serves the protected-resource metadata and answers 401 with WWW-Authenticate from these. The
        audience is checked by the account service (`resource` goes with every check)."""
        return AuthSettings(issuer_url=self.authorization_server, resource_server_url=self.resource,  # type: ignore[arg-type]
                            validate_token_resource=False)


def authorization_server(answer: dict[str, Any]) -> str:
    """The authorization server from the account service's /v1/me/mcp (ValueError unless it's an https address, or
    http on this machine for tests)."""
    found = answer.get("authorization_server")
    if not isinstance(found, str) or not (found.startswith("https://") or found.startswith("http://127.0.0.1:")):
        raise ValueError("the account service didn't say where connectors sign in")
    return found
