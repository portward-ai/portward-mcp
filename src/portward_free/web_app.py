"""Which AI app is asking, on the web routes only (#134).

On the web routes (serve-http, the relay, the hosted engine) every request carries an access token the account
service checked, and with it the OAuth client the user signed in to Portward from (claude.ai, ChatGPT...). That
client id is trustworthy, so a profile can be applied for some AI apps only. On desktop (stdio) the AI app names
itself and could claim to be anything, so there is no app here: `current()` is None, and an application limited to
some apps never applies (it only ever narrows).

`WebAppScope` (MCP middleware, web routes only) sets the app for each request; storage reads it with `current()`.
This module imports nothing from the MCP SDK, so storage can use it.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Callable
from typing import Any

UNNAMED = ""  # a web request whose OAuth client the account service didn't name: no per-app application matches it
CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,199}$")

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("portward_web_app", default=None)


def current() -> str | None:
    """The OAuth client id of the AI app behind this web request; UNNAMED when the web client wasn't named; None on
    desktop or outside a request."""
    return _current.get()


class WebAppScope:
    """MCP middleware for the web routes: each request runs with its OAuth client as `current()`.
    `client_of(ctx)` reads it from the request (remote.signin.client_of)."""

    def __init__(self, client_of: Callable[[Any], str | None],
                 seen: Callable[[str, str | None], None] | None = None) -> None:
        self._client_of, self._seen = client_of, seen

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        client = self._client_of(ctx)
        token = _current.set(client if client and CLIENT_ID.fullmatch(client) else UNNAMED)
        try:
            if client and self._seen is not None and CLIENT_ID.fullmatch(client):
                self._seen(client, _client_name(ctx))
            return await call_next(ctx)
        finally:
            _current.reset(token)


def _client_name(ctx: Any) -> str | None:
    """The name the AI app gave itself (only a label next to its OAuth client id: never what decides)."""
    params = getattr(getattr(ctx, "session", None), "client_params", None)
    name = getattr(getattr(params, "client_info", None), "name", None)
    return name[:80] if isinstance(name, str) and name.strip() else None
