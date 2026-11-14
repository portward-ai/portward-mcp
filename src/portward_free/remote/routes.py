"""Which web routes a connection is offered (#103; #90 story 31).

- direct: claude.ai and ChatGPT reach this computer through the relay; Portward never sees your data.
- hosted (#104): Portward runs a small gateway that holds the connection's login; it works with the computer off.

Files (CSV, Excel, SQLite) and databases only this computer can reach (localhost, a private or tailnet address, a
.local or single-label name) are offered the direct route only, so nothing is uploaded to reach them. A database
on the internet is offered both, hosted first (the default). Anything not known to be on the internet (a kind this
doesn't know, wrapped servers, a missing host) gets the direct route only: the safe answer.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any

from portward_free.addresses import is_public
from portward_free.route_names import DIRECT, WEB_ROUTES

HOSTED_LABEL = "Portward runs this"  # the hosted route's label, everywhere it appears (#90 trust line)
DIRECT_LINE = "Portward never sees your data"  # the direct route's trust line, everywhere it is described
HOSTED_LABEL_PATH = "/security"  # where the label links on the account service: what the engine holds and reads (#109)
INTERNET_KINDS = frozenset({"postgres", "mysql"})  # Portward's own connectors to database servers
LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".home.arpa", ".corp")


def laptop_only(host: str) -> bool:
    """True when `host` can only be reached from this computer or its local network."""
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return name == "localhost" or "." not in name or name.endswith(LOCAL_SUFFIXES)
    return not is_public(address)


def offered_routes(kind: str, settings: Mapping[str, Any]) -> tuple[str, ...]:
    """The routes this connection may use on the web, the default first."""
    if kind not in INTERNET_KINDS or laptop_only(str(settings.get("host") or "")):
        return (DIRECT,)
    return WEB_ROUTES
