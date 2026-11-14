"""The MCP server the AI app runs in place of the provider's own (`portward-free wrap <name>`).

It starts the provider's server as a child process (wrap.child) and shows the AI only:
- the provider's SQL tools, under their own names: each query goes through the same guard, allowed-column rewrite
  and result re-check as Portward's own connectors (pulls.Pulls -> pull.run_pull), under the connection's applied
  profiles, and lands in history and the AI log like any pull;
- `list_tables`, the catalog as the profiles let the AI see it (the provider's schema tools stay hidden);
- `more_rows`, which asks the user before sending more than the preview.
Every other tool of the child is never listed and is refused if called by name: the MCP server only knows the tools
registered here. The child keeps running under frozen profiles after a lapse: nothing here checks the plan, and an
unknown plan never loosens anything.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult

from portward_free import __version__
from portward_free.keychain import Keychain, KeychainError, account_for
from portward_free.pull_tools import register_more_rows
from portward_free.pulls import Pulls
from portward_free.replies import error, reply
from portward_free.runtime import Runtime
from portward_free.server import _add_middleware
from portward_free.sources import SourceError
from portward_free.storage import Connection, LocalStore
from portward_free.tool_marks import READ, Mark, marked
from portward_free.usage import Usage
from portward_free.wrap.providers import KINDS, classify
from portward_free.wrap.source import WrappedSource, open_wrapped

GOVERNED_NOTE = (" Portward applies the user's profiles: send one read-only SELECT over the tables list_tables "
                 "shows; nothing else exists for you, and the rows come back as a preview.")
MAIN_CATALOG_TOOLS = ("list_sources", "describe_source")  # named in refusals; the wrap server has neither


class WrapRefused(Exception):
    """The wrap server can't start; the message is for the user at the terminal."""


def instructions(name: str, label: str, hidden: tuple[tuple[str, str], ...]) -> str:
    lines = [f"Portward runs the user's {label} server ({name!r}) and applies their profiles to it: you see only "
             "the tables and columns their applied profiles grant. Call list_tables first, then query with one "
             "SELECT."]
    if hidden:
        lines.append("These tools of the server are hidden because Portward can't govern them: "
                     + "; ".join(f"{tool} ({why})" for tool, why in hidden) + ".")
    return " ".join(lines)


def open_connection(store: LocalStore, keychain: Keychain, name: str) -> tuple[Connection, WrappedSource]:
    connection = store.connection(name)
    if connection is None or connection.kind not in KINDS:
        raise WrapRefused(f"no wrapped server named {name!r}; set one up with portward-free wrap-add")
    try:
        secret = keychain.get(account_for(name))
        return connection, open_wrapped(connection.kind, connection.settings, secret)
    except (KeychainError, SourceError) as err:
        raise WrapRefused(f"{name}: {err}") from None


def _kept_open(source: WrappedSource) -> Callable[..., Any]:
    """An opener that hands every pull the one running child, instead of starting it again per call."""

    @contextmanager
    def opened(connection: Connection, password: str | None = None) -> Iterator[WrappedSource]:
        yield source

    return opened


def _sql_tool(pulls: Pulls, name: str, tool: str, argument: str) -> Callable[..., CallToolResult]:
    async def run(ctx: Context, **arguments: Any) -> CallToolResult:
        sql = arguments.get(argument)
        if not isinstance(sql, str) or not sql.strip():
            return error(f"pass the SQL in `{argument}`")
        result = await pulls.record(ctx, name, f"(through {tool})", sql)
        if not isinstance(result, CallToolResult):  # a prompt round: never on profiles, which wrapped sources run on
            return error("this result needs the user's yes, which this server can't ask for")
        if result.is_error:  # the guard's words name the main server's catalog tools; this server's is list_tables
            said = " ".join(getattr(c, "text", "") for c in result.content)
            for other in MAIN_CATALOG_TOOLS:
                said = said.replace(other, "list_tables")
            return error(said)
        return result

    run.__signature__ = inspect.Signature([  # type: ignore[attr-defined]
        inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context),
        inspect.Parameter(argument, inspect.Parameter.KEYWORD_ONLY, annotation=str),
    ])
    run.__annotations__ = {"ctx": Context, argument: str, "return": CallToolResult}
    return run


def build_wrap_server(store: LocalStore, keychain: Keychain, name: str, usage: Usage | None = None) -> MCPServer:
    """The wrap server for the wrapped connection `name`, its child already running (stopped when the server
    stops). WrapRefused when it isn't set up or won't start."""
    connection, source = open_connection(store, keychain, name)
    provider = source.provider
    rt = Runtime(store=store, keychain=keychain, usage=usage if usage is not None else Usage.from_env(store),
                 browser=lambda url: False, opened=_kept_open(source))
    pulls = Pulls(rt, panel=False)
    found = classify(provider, source.tools)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            source.close()

    server = MCPServer(f"portward-wrap-{provider.name}", version=__version__,
                       instructions=instructions(name, provider.label, found.hidden), lifespan=lifespan)
    _add_middleware(server, rt)
    sql_tools: dict[str, Mark] = {}  # #147: Portward lets only a read-only SELECT through
    for listed in source.tools:
        argument = provider.sql_argument(listed)
        if not argument:
            continue
        said = (listed.description or f"Run SQL on {name}.").strip()
        server.add_tool(_sql_tool(pulls, name, listed.name, argument), name=listed.name,
                        description=said + GOVERNED_NOTE)
        sql_tools[listed.name] = Mark(f"Run SQL on {name}", READ)

    @server.tool(description=f"The tables and columns of {name} that the user's profiles let you see.")
    def list_tables() -> CallToolResult:
        policy = store.rules_for(name)
        try:
            visible = policy.visible(source.tables())
        except SourceError as err:
            return error(str(err))
        tables = [{"name": t.name, "columns": list(t.columns)} for t in visible]
        text = "\n".join(f"- {t['name']}: {', '.join(t['columns'])}" for t in tables)
        return reply({"source": name, "tables": tables},
                     text or f"Nothing in {name} is granted yet: the user applies a profile to it in Portward.")

    register_more_rows(server, rt)
    return marked(server, sql_tools)
