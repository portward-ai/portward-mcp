"""Which network addresses are on the public internet (#103, #104).

Used twice: to decide from a host's text whether a database may go on the hosted route (remote/routes.py), and by
the hosted engine to check every address a name resolves to before it connects (sources/egress.py), since the
engine runs inside Portward's own network (Fly's private 6PN, fdaa::/16, and the metadata address among it)."""

from __future__ import annotations

import ipaddress

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and other overlay networks
NAT64 = ipaddress.ip_network("64:ff9b::/96")  # an IPv4 address reached through a NAT64 gateway
IPV4_BITS = 0xFFFFFFFF


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in NAT64:
        return ipaddress.IPv4Address(int(address) & IPV4_BITS)
    return None


def is_public(address: Address) -> bool:
    """True only for an address on the public internet: not private, loopback, link-local (169.254.x, the cloud
    metadata address), CGNAT, unique-local (fd00::/8, Fly's fdaa::/16), multicast, or an IPv6 form wrapping one of
    those."""
    if address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        inner = _embedded_ipv4(address)
        if inner is not None:
            return is_public(inner)
        return address.is_global
    return address.is_global and address not in CGNAT
