"""Where the hosted engine may connect (sweep open problem 1): database servers on the public internet only.

The computer decides whether a connection may go on the hosted route from its host name's text
(remote/routes.py laptop_only). A public name can still resolve to a private address, and the engine runs inside
Portward's host (Fly.io), where private addresses are Portward's own: the 6PN network (fdaa::/16), the metadata
address (169.254.169.254), 10/8, 172.16/12, 192.168/16, loopback. So the engine resolves the name itself and refuses
unless every address it gets is global. Then it connects to the address it checked, so the name can't answer
differently between the check and the connection (DNS rebinding). The engine opens every connection through
runtime.opener_for(route=HOSTED), so the check follows the route, never a connection's settings, and a file or
wrapped server never opens there at all.

There is no switch to allow private addresses on the engine. A database on a user's private network can't be
reached from Portward's host anyway, so the only private addresses there are Portward's. The direct route and Free
run on the user's computer and connect as they always have."""

from __future__ import annotations

import ipaddress
import socket

from portward_free.addresses import is_public
from portward_free.sources.base import SourceError

NOT_FOUND = "couldn't connect: the host name can't be found; check the host"


def public_address(host: str, port: int) -> str:
    """The address to connect to for `host`: refused (SourceError) unless every address it resolves to is public
    (addresses.is_public: not private, loopback, link-local or metadata, CGNAT, unique-local including Fly's
    fdaa::/16, multicast, or an IPv6 form wrapping one of those). Every network source on the engine uses this."""
    name = host.strip().strip("[]")
    try:
        found = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in found]
    except (OSError, UnicodeError, ValueError):
        raise SourceError(NOT_FOUND) from None
    if not addresses:
        raise SourceError(NOT_FOUND)
    if not all(is_public(a) for a in addresses):
        raise SourceError(f"{host} resolves to a private address, and Portward's hosted route only connects to "
                          "databases on the public internet; use the direct route for this connection")
    return str(addresses[0])
