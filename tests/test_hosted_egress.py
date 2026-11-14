"""The hosted engine never connects into a private network (sweep open problem 1).

Whether a database may go on the hosted route is decided on the computer from the host name's text
(remote/routes.py laptop_only). A public name can still resolve to a private address: Fly's own private network
(fdaa::/16), the cloud metadata address (169.254.169.254), 10/8 and the rest. So the engine resolves the name itself,
refuses unless every address is on the public internet, and then connects to the address it checked (never resolving
again, so a name can't change its answer between the check and the connection).

DNS is played by a stand-in getaddrinfo; nothing here reaches the network."""

from __future__ import annotations

import base64
import socket
from typing import Any

import pytest

from portward_free import proof, stand_ins
from portward_free.hosted import bundle
from portward_free.keychain import account_for
from portward_free.runtime import opener_for
from portward_free.sources import SourceError, open_source
from portward_free.storage import Connection, LocalStore

from harness import MemoryKeychain

SETTINGS = {"host": "db.customer.example", "port": 5432, "database": "shop", "user": "reader"}
PRIVATE = ["169.254.169.254", "fdaa:0:1:a7b:7d:1:2:3", "fdaa::3", "10.1.2.3", "172.16.0.9", "192.168.1.20", "127.0.0.1",
           "::1", "100.64.0.1", "0.0.0.0", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a00:5",
           "2002:a00:5::1", "fe80::1", "224.0.0.1"]
PUBLIC = "93.184.215.14"


def answer(address: str, port: Any) -> tuple[Any, ...]:
    return (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "",
            (address, int(port), 0, 0) if ":" in address else (address, int(port)))


def resolving_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> list[tuple[str, int]]:
    asked: list[tuple[str, int]] = []

    def getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        asked.append((host, port))
        return [answer(a, port) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return asked


def rebinding(monkeypatch: pytest.MonkeyPatch, first: str, then: str) -> list[str]:
    """A name that answers `first` once and `then` after that (DNS rebinding); returns the names looked up."""
    asked: list[str] = []

    def getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        asked.append(host)
        return [answer(first if len(asked) == 1 else then, port)]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return asked


@pytest.mark.parametrize("kind", ["postgres", "mysql"])
@pytest.mark.parametrize("address", PRIVATE)
def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch, kind: str,
                                                               address: str) -> None:
    resolving_to(monkeypatch, address)
    with pytest.raises(SourceError, match="private") as caught:
        open_source(kind, SETTINGS, "pw", public_only=True)
    assert "db.customer.example" in str(caught.value)


def test_one_private_address_among_public_ones_is_enough_to_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    resolving_to(monkeypatch, PUBLIC, "fdaa::3")
    with pytest.raises(SourceError, match="private"):
        open_source("postgres", SETTINGS, "pw", public_only=True)


@pytest.mark.parametrize("host", ["169.254.169.254", "localhost", "[fdaa::2]", "10.0.0.5"])
def test_a_private_address_typed_as_the_host_is_refused(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)  # real resolution: these never leave the machine
    with pytest.raises(SourceError, match="private"):
        open_source("postgres", {**SETTINGS, "host": host}, "pw", public_only=True)


def test_files_and_wrapped_servers_never_open_on_the_hosted_engine(tmp_path: Any) -> None:
    db = tmp_path / "x.sqlite"
    db.write_bytes(b"")
    with pytest.raises(SourceError, match="hosted"):
        open_source("sqlite", {"path": str(db)}, None, public_only=True)


def test_postgres_connects_to_the_address_it_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    resolving_to(monkeypatch, PUBLIC)
    seen: dict[str, Any] = {}

    def connect(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", connect)
    asked = rebinding(monkeypatch, PUBLIC, "10.0.0.5")  # a second answer would point inside the network
    with pytest.raises(SourceError, match="didn't answer"):
        open_source("postgres", SETTINGS, "pw", public_only=True)
    assert asked == ["db.customer.example"]  # resolved once
    assert seen["hostaddr"] == PUBLIC  # the checked address, so nothing resolves the name again
    assert seen["host"] == "db.customer.example" and seen["sslmode"] == "verify-full"  # TLS still checks the name


def test_mysql_connects_to_the_address_it_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    resolving_to(monkeypatch, PUBLIC)
    dialled: list[tuple[str, int]] = []

    def create_connection(address: tuple[str, int], *args: Any, **kwargs: Any) -> None:
        dialled.append(address)
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(socket, "create_connection", create_connection)
    asked = rebinding(monkeypatch, PUBLIC, "10.0.0.5")
    with pytest.raises(SourceError, match="didn't answer"):
        open_source("mysql", {**SETTINGS, "port": 3306}, "pw", public_only=True)
    assert asked == ["db.customer.example"]
    assert dialled == [(PUBLIC, 3306)]  # the checked address; pymysql never resolves the name itself


def test_the_computer_still_connects_to_its_own_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Free and the direct route run on the user's computer, where a database on 10.x is theirs to reach."""
    import psycopg

    asked = resolving_to(monkeypatch, "10.1.2.3")
    seen: dict[str, Any] = {}

    def connect(**kwargs: Any) -> None:
        seen.update(kwargs)
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(SourceError, match="didn't answer"):
        open_source("postgres", SETTINGS, "pw")
    assert "hostaddr" not in seen and asked == []


def test_the_hosted_route_opens_sources_public_only(monkeypatch: pytest.MonkeyPatch) -> None:
    resolving_to(monkeypatch, "fdaa::9")
    keychain = MemoryKeychain()
    keychain.set(account_for("shop"), "pw")
    connection = Connection("shop", "postgres", SETTINGS)
    with pytest.raises(SourceError, match="private"):
        with opener_for(keychain, route=proof.HOSTED)(connection):
            pass


def test_a_landed_bundle_cant_switch_the_check_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check follows the route, not anything in a connection's settings, so a bundle can't turn it off."""
    keychain, store = MemoryKeychain(), LocalStore.memory()
    bundle.land(store, keychain, {
        "format": bundle.FORMAT, "rules": [], "notes": [], "profiles": [], "on_profiles": [], "applied": [],
        "connections": [{"name": "shop", "kind": "postgres", "login": "pw",
                         "settings": {**SETTINGS, "public_only": False}}],
        "anon_key": base64.b64encode(bytes(range(stand_ins.KEY_BYTES))).decode(),
        "inbox_key": base64.b64encode(bytes(range(32))).decode()})
    (connection,) = store.connections()
    resolving_to(monkeypatch, "fdaa::3")
    with pytest.raises(SourceError, match="private"):
        with opener_for(keychain, route=proof.HOSTED)(connection):
            pass
