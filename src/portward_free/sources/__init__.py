"""Source connectors, by kind. A new kind is one opener in `OPENERS`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from portward_free.sources import egress
from portward_free.sources.base import Fetched, Source, SourceError, SourceOpener, Table
from portward_free.sources.files import open_csv, open_excel
from portward_free.sources import mysql, postgres
from portward_free.sources.mysql import open_mysql
from portward_free.sources.network import ServerSettings
from portward_free.sources.postgres import open_postgres
from portward_free.sources.sqlite import open_sqlite
from portward_free.wrap import providers as wrap_providers
from portward_free.wrap.source import opener as wrap_opener

OPENERS: Mapping[str, SourceOpener] = {"sqlite": open_sqlite, "csv": open_csv, "excel": open_excel,
                                       "postgres": open_postgres, "mysql": open_mysql,
                                       **{kind: wrap_opener(kind) for kind in wrap_providers.KINDS}}
SERVERS = {"postgres": ("Postgres", postgres.DEFAULT_PORT), "mysql": ("MySQL", mysql.DEFAULT_PORT)}
NEEDS_PASSWORD = frozenset(SERVERS)  # kinds whose password is kept in the keychain
WRAP_KINDS = wrap_providers.KINDS  # wrapped MCP servers (#100): set up only from a terminal, login in the keychain
KEYCHAIN_KINDS = NEEDS_PASSWORD | WRAP_KINDS  # kinds opened with a secret read from the keychain


def server_settings(kind: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    """A database server's settings checked and filled in (the default port), without connecting to it."""
    label, default_port = SERVERS[kind]
    return ServerSettings.parse(label, settings, default_port).as_dict()


SERVER_SOURCES = {"postgres": postgres.PostgresSource, "mysql": mysql.MySqlSource}


def open_source(kind: str, settings: Mapping[str, Any], password: str | None = None, *,
                public_only: bool = False) -> Source:
    """Open a source. `public_only` (the hosted engine): only a database server whose host resolves to public
    addresses, connected to by the address checked (egress.py)."""
    opener = OPENERS.get(kind)
    if opener is None:
        raise SourceError(f"unknown source kind {kind!r}; use one of: {', '.join(sorted(OPENERS))}")
    if not public_only:
        return opener(settings, password)
    if kind not in SERVER_SOURCES:
        raise SourceError(f"a {kind} connection can't run on the hosted route, only a database on the internet")
    label, default_port = SERVERS[kind]
    server = ServerSettings.parse(label, settings, default_port)
    return SERVER_SOURCES[kind](replace(server, address=egress.public_address(server.host, server.port)),
                                password or "")


__all__ = ["KEYCHAIN_KINDS", "NEEDS_PASSWORD", "WRAP_KINDS", "OPENERS", "SERVERS", "server_settings", "Fetched", "Source", "SourceError", "Table", "open_source"]
