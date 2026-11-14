"""The provider's MCP server, called from ordinary (sync) code: run by Portward as a child process over stdio
(Postgres MCP), or reached at its URL over streamable HTTP (Snowflake's MCP server, #101).

The MCP client runs on its own event loop in a background thread (an anyio blocking portal), so the guard, the
pull path and the catalog tools use it like any other source. Only the command the user set up is ever run, with
the login passed in its environment, never on its command line; over HTTP the login goes in a header, never in
the URL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx2
from anyio.from_thread import start_blocking_portal
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Tool

from portward_free.sources.base import QUERY_TIMEOUT_SECONDS, SourceError

CALL_TIMEOUT_SECONDS = QUERY_TIMEOUT_SECONDS + 15  # the child's own timeout, plus its start-up


@asynccontextmanager
async def _over_http(url: str, headers: Mapping[str, str]) -> AsyncIterator[Client]:
    # no redirects: one would carry the token elsewhere. The initialize handshake: Snowflake speaks MCP 2025-11-25.
    async with (httpx2.AsyncClient(headers=dict(headers), timeout=CALL_TIMEOUT_SECONDS, follow_redirects=False) as http,
                Client(streamable_http_client(url, http_client=http), read_timeout_seconds=CALL_TIMEOUT_SECONDS,
                       mode="legacy") as client):
        yield client


class ChildServer:
    def __init__(self, command: Sequence[str], env: Mapping[str, str]) -> None:
        if not command:
            raise SourceError("no command is set up for this wrapped server")
        params = StdioServerParameters(command=command[0], args=list(command[1:]), env=dict(env))
        self._connect(lambda: Client(params, read_timeout_seconds=CALL_TIMEOUT_SECONDS),
                      f"couldn't start the wrapped server ({command[0]}): check it runs by itself")

    @classmethod
    def over_http(cls, url: str, headers: Mapping[str, str]) -> ChildServer:
        """The server at `url` (streamable HTTP), sent `headers` (the login) with every request."""
        found = cls.__new__(cls)
        found._connect(lambda: _over_http(url, headers),
                       f"couldn't reach the wrapped server at {urlsplit(url).hostname}: check its URL, that the "
                       "token is right and not expired, and that its role may use the MCP server")
        return found

    def _connect(self, client: Callable[[], AbstractAsyncContextManager[Client]], failed: str) -> None:
        self._stack = ExitStack()
        try:
            self._portal = self._stack.enter_context(start_blocking_portal())
            self._client: Client = self._stack.enter_context(self._portal.wrap_async_context_manager(client()))
        except BaseException as err:
            self._stack.close()
            if isinstance(err, Exception):  # never the server's own words: they may hold the login
                raise SourceError(failed) from None
            raise

    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._portal.call(self._client.list_tools).tools)

    def tool_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tools())

    def tool(self, name: str) -> Any:
        return next((t for t in self.tools() if t.name == name), None)

    def call(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            return self._portal.call(self._client.call_tool, name, arguments)
        except Exception:  # noqa: BLE001 - a dead or stuck child: say so in Portward's words
            raise SourceError("the wrapped server didn't answer") from None

    def on_close(self, callback: Callable[[], object]) -> None:
        """Run `callback` when the server is closed (first, before the child is stopped)."""
        self._stack.callback(callback)

    def close(self) -> None:
        self._stack.close()
