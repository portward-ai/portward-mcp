"""What a source points at, so block rules follow the data and not the name it was added under.

A file is known by its real path (symlinks resolved) and by its
device and inode (hard links, a path spelled another way). A database is known by host, port and database (host in
lower case; localhost, 127.0.0.1 and ::1 are one host). A rule stores the keys of the source it was made on; any
source with a matching key gets the rule, whatever its name, and keeps getting it after the first is removed.
"""

from __future__ import annotations

import os
from typing import Any

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306}
FILE_KINDS = frozenset({"sqlite", "csv", "excel"})


def _file_keys(path: str) -> tuple[str, ...]:
    real = os.path.normcase(os.path.realpath(path))
    try:
        found = os.stat(real)
    except OSError:
        return (f"file:{real}",)
    return f"file:{real}", f"inode:{found.st_dev}:{found.st_ino}"


def _database_key(kind: str, settings: dict[str, Any]) -> str:
    host = str(settings.get("host") or "localhost").strip().lower()
    host = "localhost" if host in LOCAL_HOSTS else host
    port = settings.get("port") or DEFAULT_PORTS.get(kind, "")
    return f"db:{host}:{port}/{settings.get('database') or ''}"


def target_keys(kind: str, settings: dict[str, Any]) -> tuple[str, ...]:
    """The keys of what a connection points at: one for a database, up to two for a file; none if unknown."""
    if kind in FILE_KINDS:
        path = settings.get("path")
        return _file_keys(str(path)) if path else ()
    if kind in DEFAULT_PORTS:
        return (_database_key(kind, settings),)
    return ()
