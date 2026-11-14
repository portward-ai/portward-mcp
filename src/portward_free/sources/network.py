"""Settings shared by the database servers (Postgres, MySQL): where the server is and who logs in.

The password is never one of these settings: it is kept in the keychain and handed to the opener separately.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from portward_free.sources.base import LOCAL_HOSTS, SourceError

CONNECT_TIMEOUT_SECONDS = 10
FIELDS = ("host", "port", "database", "user")


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    database: str
    user: str
    address: str | None = None  # the checked address to connect to (the hosted engine, egress.py); None: resolve host

    @property
    def is_local(self) -> bool:
        return self.host.lower() in LOCAL_HOSTS

    @classmethod
    def parse(cls, kind: str, settings: Mapping[str, Any], default_port: int) -> ServerSettings:
        missing = [f for f in ("host", "database", "user") if not str(settings.get(f) or "").strip()]
        if missing:
            raise SourceError(f"a {kind} connection needs {', '.join(missing)}")
        port = settings.get("port") or default_port
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise SourceError(f"the port must be a number, like {default_port}") from None
        if not 0 < port < 65536:
            raise SourceError(f"the port must be between 1 and 65535, like {default_port}")
        return cls(str(settings["host"]).strip(), port, str(settings["database"]).strip(), str(settings["user"]).strip())

    def as_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "database": self.database, "user": self.user}


def writable_warning(tables: list[tuple[str, str]]) -> str:
    """The warning for a login that can change some tables: (table or "*", privilege)."""
    named = sorted({"every table" if t == "*" else t for t, _ in tables})
    return (f"this login can change data ({', '.join(named)}). Portward only ever runs read-only queries through it, "
            "but a login that can only read is safer")
