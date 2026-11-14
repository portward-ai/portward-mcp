"""The direct route (#103): claude.ai and ChatGPT on the web reach this computer through Portward's relay.

The computer gets its own name under the personal relay domain from the account service, dials out to the relay
(the frp client, a fake here), and serves MCP with a certificate held only on this computer: the relay passes TLS
through by name, so it ends here. Against the real control app on 127.0.0.1."""

from __future__ import annotations

import datetime
import json
import socket
import ssl
import stat
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpcore2
import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from httpcore2._backends.auto import AutoBackend
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from test_direct_certificate import FakeCa
from test_remote_mcp import fake_authkit_token, free_port
from test_sync import Device, Service, device, service  # noqa: F401  (the control app, as a module fixture)

from portward_free.account import Accounts, AccountService
from portward_free.paths import database_path
from portward_free.remote import RemoteRefused
from portward_free.remote.relay import KEY_ENV, FrpcTunnel, RelayAccess, frpc_config, relay_access
from portward_free.storage import LocalStore

DOMAIN = "me.portward.test"  # tests/fixtures/share_service.py: PERSONAL_RELAY_DOMAIN


# --- relay access ---------------------------------------------------------------------------------------------------
async def test_a_personal_computer_gets_its_own_address_on_the_relay(service: Service, tmp_path: Path) -> None:
    async with device(service, tmp_path / "laptop", "sam@gmail.example") as laptop:
        access = relay_access(laptop.keychain, AccountService(service.url))
        assert access.address == f"https://{access.name}.{DOMAIN}" and access.relay_host == "relay.portward.test"
        assert access.key not in repr(access)
        again = relay_access(laptop.keychain, AccountService(service.url))
        assert again.name == access.name and again.key != access.key


async def test_a_lapsed_account_gets_no_relay_access(service: Service, tmp_path: Path) -> None:
    async with device(service, tmp_path / "laptop", "ended-sam@gmail.example") as laptop:
        with pytest.raises(RemoteRefused, match="needs Personal"):
            relay_access(laptop.keychain, AccountService(service.url))


def test_a_computer_that_isnt_signed_in_gets_no_relay_access(tmp_path: Path) -> None:
    from harness import MemoryKeychain

    with pytest.raises(RemoteRefused, match="isn't signed in"):
        relay_access(MemoryKeychain(), AccountService("http://127.0.0.1:9"))


def test_an_odd_answer_from_the_account_service_is_refused() -> None:
    good = {"name": "p-abcdefghij", "address": f"https://p-abcdefghij.{DOMAIN}", "relay_host": "relay.portward.test",
            "relay_domain": DOMAIN, "key": "pwrk_x"}
    assert RelayAccess.parse(good).host == f"p-abcdefghij.{DOMAIN}"
    for bad in ({"name": "arco"}, {"address": "https://elsewhere.example"}, {"relay_host": "relay\n.x"},
                {"key": ""}, {"name": 'p-abcdefghij"\nx = "y'}):
        with pytest.raises(ValueError):
            RelayAccess.parse(good | bad)


# --- the dial-out ---------------------------------------------------------------------------------------------------
ACCESS = RelayAccess("p-abcdefghij", f"https://p-abcdefghij.{DOMAIN}", "relay.portward.test", "pwrk_secret_marker")

FAKE_FRPC = """#!{python}
import json, os, sys, time
from pathlib import Path
config = Path(sys.argv[2])
with open(config.parent / "runs.jsonl", "a") as runs:
    runs.write(json.dumps({{"argv": sys.argv[1:], "key": os.environ.get("{key}"), "env": sorted(os.environ),
                           "config": config.read_text()}}) + "\\n")
if (config.parent / "crash").exists():
    (config.parent / "crash").unlink()
    sys.exit(1)
time.sleep(60)
"""


@pytest.fixture()
def fake_frpc(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "frpc"
    path.parent.mkdir()
    path.write_text(FAKE_FRPC.format(python=sys.executable, key=KEY_ENV))
    path.chmod(0o755)
    return path


def runs(folder: Path) -> list[dict]:
    file = folder / "runs.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []


def wait_for(condition, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_the_relay_client_asks_for_exactly_this_address_and_keeps_the_key_out_of_files(
        tmp_path: Path, fake_frpc: Path) -> None:
    folder = tmp_path / "direct"
    tunnel = FrpcTunnel(ACCESS, 8766, folder, fake_frpc)
    tunnel.start()
    try:
        wait_for(lambda: runs(folder))
        run = runs(folder)[0]
        assert run["key"] == "pwrk_secret_marker"
        assert set(run["env"]) - {"PATH", "PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING", "LC_CTYPE"} == {KEY_ENV}
        config = run["config"]
        assert "pwrk_secret_marker" not in config
        assert 'user = "p-abcdefghij"' in config and 'metadatas.route = "personal"' in config
        assert f'customDomains = ["p-abcdefghij.{DOMAIN}"]' in config and "localPort = 8766" in config
        assert 'localIP = "127.0.0.1"' in config and 'type = "https"' in config  # passed through, still encrypted
        assert "disableCustomTLSFirstByte = false" in config and "trustedCaFile" in config
        assert stat.S_IMODE((folder / "frpc.toml").stat().st_mode) == 0o600
    finally:
        tunnel.stop()
    assert not tunnel.running


def test_the_relay_client_is_started_again_when_it_stops(tmp_path: Path, fake_frpc: Path) -> None:
    folder = tmp_path / "direct"
    folder.mkdir()
    (folder / "crash").touch()
    tunnel = FrpcTunnel(ACCESS, 8766, folder, fake_frpc, restart_seconds=(0.05,))
    tunnel.start()
    try:
        wait_for(lambda: len(runs(folder)) == 2 and tunnel.running)
    finally:
        tunnel.stop()


def test_without_the_relay_client_the_direct_route_says_what_to_install(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("PORTWARD_FRPC", raising=False)
    with pytest.raises(RemoteRefused, match="frpc"):
        FrpcTunnel(ACCESS, 8766, tmp_path / "direct")


# --- end to end: a connector, the relay (passing TLS through), the computer ---------------------------------------
class PassthroughRelay:
    """The relay and the frp client in one: bytes from the public port are copied to the computer's local port
    untouched (no TLS here at all), as frp's https-by-name passthrough does."""

    def __init__(self, access: RelayAccess, local_port: int) -> None:
        self.access, self.local_port, self.started, self.stopped = access, local_port, False, False
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.first_bytes: list[bytes] = []

    def start(self) -> None:
        self.started = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                visitor, _ = self.listener.accept()
            except OSError:
                return
            try:
                computer = socket.create_connection(("127.0.0.1", self.local_port), timeout=10)
            except OSError:
                visitor.close()  # the computer isn't listening (yet): like a tunnel with nothing behind it
                continue
            threading.Thread(target=self._pump, args=(visitor, computer, True), daemon=True).start()
            threading.Thread(target=self._pump, args=(computer, visitor, False), daemon=True).start()

    def _pump(self, source: socket.socket, sink: socket.socket, record: bool) -> None:
        try:
            while chunk := source.recv(65536):
                if record and len(self.first_bytes) < 1000:
                    self.first_bytes.append(chunk)
                sink.sendall(chunk)
        except OSError:
            pass
        finally:
            for s in (source, sink):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def stop(self) -> None:
        self.stopped = True
        self.listener.close()


class LocalCa:
    """Stands in for Let's Encrypt: signs the computer's request. Connectors trust it (and only it) here."""

    def __init__(self, days: list[float] | None = None) -> None:
        self.ca, self.days, self.issued, self.hosts = FakeCa(), days or [90], 0, []

    def issue(self, host: str, key, local_port: int) -> bytes:
        days = self.days[min(self.issued, len(self.days) - 1)]
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
               .sign(key, hashes.SHA256()))
        self.issued += 1
        self.hosts.append(host)
        return self.ca.sign(csr, days).public_bytes(serialization.Encoding.PEM)

    def trust(self) -> ssl.SSLContext:
        now = datetime.datetime.now(datetime.UTC)
        root = (x509.CertificateBuilder().subject_name(self.ca.name).issuer_name(self.ca.name)
                .public_key(self.ca.key.public_key()).serial_number(1)
                .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=9))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(self.ca.key, hashes.SHA256()))
        context = ssl.create_default_context(cadata=root.public_bytes(serialization.Encoding.PEM).decode())
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT  # the stand-in CA's root is minimal
        return context


class Unsent:
    def send(self, payload: dict) -> bool:
        raise AssertionError("nothing is sent in these tests")


@contextmanager
def direct(laptop: Device, service: Service, tmp_path: Path, ca: LocalCa,
           **options) -> Iterator[tuple[object, PassthroughRelay]]:
    from portward_free.remote.direct import Direct
    from portward_free.usage import Usage

    relays: list[PassthroughRelay] = []

    def tunnel(access: RelayAccess, local_port: int) -> PassthroughRelay:
        relays.append(PassthroughRelay(access, local_port))
        return relays[-1]

    store = LocalStore.open(database_path(laptop.home))
    usage = Usage(store, Unsent())
    usage.seen("terminal")  # counts on, nothing sent for an hour
    route = Direct(store, laptop.keychain, Accounts(AccountService(service.url)), folder=tmp_path / "direct",
                   port=free_port(), tunnel=tunnel, issuer=ca, usage=usage, **options)
    try:
        route.start()
        yield route, relays[-1]
    finally:
        route.stop()
        store.close()


class ToRelay(AutoBackend):
    """The connector's DNS: every name leads to the (fake) relay's public port."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return await super().connect_tcp("127.0.0.1", self.port, timeout, local_address, socket_options)


def connect(address: str, token: str, trust: ssl.SSLContext, relay: PassthroughRelay):
    """claude.ai on the web: it connects to the address by name (SNI and Host), trusting only the stand-in CA."""
    transport = httpx2.AsyncHTTPTransport(verify=trust)
    transport._pool = httpcore2.AsyncConnectionPool(ssl_context=trust, network_backend=ToRelay(relay.port))
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, transport=transport, timeout=10)
    return http, Client(streamable_http_client(address + "/mcp", http_client=http))


async def test_claude_reaches_the_computer_through_the_relay_with_tls_ending_on_the_computer(
        service: Service, tmp_path: Path, shop_db: Path) -> None:
    ca = LocalCa()
    async with device(service, tmp_path / "laptop", "sam@gmail.example") as laptop:
        assert not (await laptop.h.add_file("shop", str(shop_db))).is_error
        with direct(laptop, service, tmp_path, ca) as (route, relay):
            address = route.access.address
            assert ca.hosts == [route.access.host]  # the certificate is for the computer's own relay name
            http, client = connect(address, fake_authkit_token("sam@gmail.example", address + "/mcp"), ca.trust(),
                                   relay)
            async with http, client:
                listed = await client.call_tool("list_sources", {})
                pulled = await client.call_tool("query", {"source": "shop", "question": "names",
                                                          "sql": "SELECT name FROM customers"})
            assert not listed.is_error and "shop" in json.dumps(listed.structured_content)
            assert not pulled.is_error
            logged = (await laptop.h.call("ai_log")).ai.structured["entries"]
            assert [e["route"] for e in logged] == ["direct"]  # the AI log says how the AI app reached it (#98)
            seen = b"".join(relay.first_bytes)
            assert seen[:1] == b"\x16"  # what the relay carried started with a TLS handshake...
            assert b"list_sources" not in seen and b"Bearer" not in seen  # ...and it never saw the request
            assert route.usage.reports()[0]["routes"] == ["direct"]  # usage counts: the route, nothing else


async def test_nobody_but_the_account_owner_gets_through(service: Service, tmp_path: Path) -> None:
    ca = LocalCa()
    async with device(service, tmp_path / "laptop", "sam@gmail.example") as laptop:
        async with device(service, tmp_path / "other", "dee@gmail.example"):
            with direct(laptop, service, tmp_path, ca) as (route, relay):
                address = route.access.address
                http, client = connect(address, fake_authkit_token("dee@gmail.example", address + "/mcp"),
                                       ca.trust(), relay)
                with pytest.raises(Exception):  # noqa: B017 - the SDK raises its own error for the 401
                    async with http, client:
                        await client.list_tools()


async def test_the_direct_route_stops_and_hangs_up_on_the_relay_when_personal_lapses(
        service: Service, tmp_path: Path) -> None:
    async with device(service, tmp_path / "laptop", "lapsing@gmail.example") as laptop:
        try:
            with direct(laptop, service, tmp_path, LocalCa(), plan_seconds=0.2) as (route, relay):
                service.move_clock(15 * 86_400)
                assert route.wait(10), "the direct route kept running after Personal ended"
                assert relay.stopped and "Personal" in (route.stopped_because or "")
        finally:
            service.move_clock(0)


async def test_the_certificate_is_renewed_while_serving(service: Service, tmp_path: Path) -> None:
    ca = LocalCa(days=[50 / 86_400, 50 / 86_400, 90])  # the first two are past half their life at once
    async with device(service, tmp_path / "laptop", "sam@gmail.example") as laptop:
        with direct(laptop, service, tmp_path, ca, renew_seconds=0.2) as (route, relay):
            wait_for(lambda: ca.issued >= 3 and route.serving)
            address = route.access.address
            http, client = connect(address, fake_authkit_token("sam@gmail.example", address + "/mcp"), ca.trust(),
                                   relay)
            async with http, client:
                assert (await client.list_tools()).tools


def test_the_command_says_to_sign_in_first(home: Path) -> None:
    import io

    from harness import MemoryKeychain

    from portward_free.cli import main

    out = io.StringIO()
    assert main(["serve-direct"], home=home, keychain=MemoryKeychain(), out=out, is_terminal=lambda: False) == 1
    assert "sign-in" in out.getvalue()


async def test_the_command_tells_a_lapsed_account_that_the_direct_route_needs_personal(service: Service,
                                                                                      tmp_path: Path) -> None:
    import io

    from portward_free.cli import main

    async with device(service, tmp_path / "laptop", "ended-sam@gmail.example") as laptop:
        out = io.StringIO()
        code = main(["serve-direct"], home=laptop.home, keychain=laptop.keychain, out=out, is_terminal=lambda: False,
                    account_service=AccountService(service.url))
    assert code == 1 and "needs Personal" in out.getvalue()


def test_the_config_quotes_what_it_is_given() -> None:
    assert 'trustedCaFile = "C:\\\\certs\\\\ca \\"x\\".pem"' in frpc_config(ACCESS, 1, 'C:\\certs\\ca "x".pem')
