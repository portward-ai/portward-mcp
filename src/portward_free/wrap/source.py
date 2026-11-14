"""A wrapped server as a Portward source: the catalog and every query go through the provider's SQL tool, after the
same guard and allowed-column rewrite as Portward's own connectors (pull.run_pull). So profiles apply to it exactly
as they do elsewhere, through `query` on the main server and through the wrap server's own SQL tool alike.

Stored like any connection: kind `wrap-<provider>`, settings (Postgres MCP: {"command": [...], "env": {...}};
Snowflake: {"url": ..., "database": ...}; never the login), and the login (Postgres MCP's DATABASE_URI, Snowflake's
access token) in the keychain under the connection's name, like a password.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from portward_free.sources.base import Fetched, SourceError, Table
from portward_free.wrap.child import ChildServer
from portward_free.wrap.providers import Provider, provider_for


class WrappedSource:
    warnings: tuple[str, ...] = ()

    def __init__(self, provider: Provider, child: ChildServer) -> None:
        self.provider, self.child = provider, child
        self.dialect = provider.dialect
        self.tools = child.tools()
        found = next((t for t in self.tools if provider.sql_argument(t)), None)
        if found is None:
            child.close()
            raise SourceError(f"the wrapped server has no {provider.label} SQL tool; is it the right server?")
        self.sql_tool, self.sql_argument = found.name, provider.sql_argument(found) or ""

    def _run(self, sql: str) -> Any:
        return self.child.call(self.sql_tool, {self.sql_argument: sql})

    def tables(self) -> tuple[Table, ...]:
        """Read from the server each time it's asked for (once per pull), so a column added later is seen (hidden
        unless granted)."""
        return tuple(replace(t, stand_ins=False) for t in self.provider.catalog(self._run(self.provider.catalog_sql)))

    def fetch(self, sql: str) -> Fetched:
        return self.provider.rows(self._run(sql))

    def _one(self, sql: str | None) -> Any:
        if not sql:
            return None
        found = self.fetch(sql).rows
        return found[0][0] if found and found[0] else None

    def current_database(self) -> str | None:
        """The session's database, for a matching login's SQL (#99), when the provider can say."""
        found = self._one(getattr(self.provider, "database_sql", None))
        return str(found) if found else None

    def readable_elsewhere(self) -> int:
        """Tables the login reads outside the catalog's schema, for the drift check (#99), when the provider can
        say."""
        found = self._one(getattr(self.provider, "elsewhere_sql", None))
        try:
            return int(found or 0)
        except (TypeError, ValueError):
            return 0

    def close(self) -> None:
        self.child.close()


def open_wrapped(kind: str, settings: Mapping[str, Any], secret: str | None) -> WrappedSource:
    provider = provider_for(kind)
    if not secret:
        raise SourceError(f"the wrapped server's login ({provider.secret_env}) isn't in the keychain; set it up "
                          "again with portward-free wrap-add")
    return WrappedSource(provider, provider.open_child(settings, secret))


def opener(kind: str) -> Any:
    return lambda settings, password=None: open_wrapped(kind, settings, password)
