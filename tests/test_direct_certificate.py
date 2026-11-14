"""The direct route's certificate (#103): made on this computer, so TLS ends here and the relay can't read it.

The computer makes its own key and gets a certificate for its relay name over ACME (RFC 8555) with TLS-ALPN-01
(RFC 8737): the certificate authority connects to the name on 443, the relay passes that through by name to this
computer, and the computer answers the challenge. Here the authority is a fake that checks the requests' signatures
and really connects to the computer's challenge answer."""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import socket
import ssl
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID
from test_remote_mcp import free_port

from portward_free.remote.certificate import AcmeIssuer, CertificateError, Certificates

HOST = "p-abcdefghij.me.portward.test"
ACME_IDENTIFIER = x509.ObjectIdentifier("1.3.6.1.5.5.7.1.31")


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeCa:
    """A certificate authority's key, and certificates signed with it for `days`."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake ACME CA")])

    def sign(self, csr: x509.CertificateSigningRequest, days: float = 90) -> x509.Certificate:
        now = datetime.datetime.now(datetime.UTC)
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self.name)
                .public_key(csr.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(days=days))
                .add_extension(san, critical=False).sign(self.key, hashes.SHA256()))


class FakeAcme:
    """Just enough of an ACME server: nonces, signed requests, one order, a TLS-ALPN-01 check made for real."""

    def __init__(self, challenge_port: int, *, pass_challenge: bool = True) -> None:
        self.ca, self.challenge_port, self.pass_challenge = FakeCa(), challenge_port, pass_challenge
        self.nonces: set[str] = set()
        self.accounts: dict[str, ec.EllipticCurvePublicKey] = {}
        self.order: dict[str, Any] = {}
        self.authz_status = "pending"
        self.token = b64(b"t" * 24)
        self.certificate: x509.Certificate | None = None
        self.seen_alpn: list[str | None] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def nonce(self) -> str:
        value = b64(hashlib.sha256(str(len(self.nonces)).encode() + b"n").digest())
        self.nonces.add(value)
        return value

    def verify(self, path: str, body: bytes) -> tuple[dict, Any, ec.EllipticCurvePublicKey]:
        jws = json.loads(body)
        protected = json.loads(unb64(jws["protected"]))
        assert protected["alg"] == "ES256" and protected["url"] == self.url + path
        self.nonces.remove(protected["nonce"])  # each nonce once
        if "jwk" in protected:
            jwk = protected["jwk"]
            key = ec.EllipticCurvePublicNumbers(int.from_bytes(unb64(jwk["x"])), int.from_bytes(unb64(jwk["y"])),
                                                ec.SECP256R1()).public_key()
        else:
            key = self.accounts[protected["kid"]]
        raw = unb64(jws["signature"])
        signature = encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
        key.verify(signature, f"{jws['protected']}.{jws['payload']}".encode(), ec.ECDSA(hashes.SHA256()))
        payload = json.loads(unb64(jws["payload"])) if jws["payload"] else None
        return protected, payload, key

    def check_challenge(self, account_key: ec.EllipticCurvePublicKey) -> bool:
        """Connect to the computer as the CA would (through the relay, by name) and read its challenge answer."""
        numbers = account_key.public_numbers()
        jwk = {"crv": "P-256", "kty": "EC", "x": b64(numbers.x.to_bytes(32)), "y": b64(numbers.y.to_bytes(32))}
        thumb = b64(hashlib.sha256(json.dumps(jwk, sort_keys=True, separators=(",", ":")).encode()).digest())
        expected = hashlib.sha256(f"{self.token}.{thumb}".encode()).digest()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        context.set_alpn_protocols(["acme-tls/1"])
        with socket.create_connection(("127.0.0.1", self.challenge_port), timeout=5) as raw, \
                context.wrap_socket(raw, server_hostname=HOST) as tls:
            self.seen_alpn.append(tls.selected_alpn_protocol())
            cert = x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))
        extension = cert.extensions.get_extension_for_oid(ACME_IDENTIFIER)
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.DNSName)
        return extension.critical and extension.value.value == b"\x04\x20" + expected and names == [HOST]

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        acme = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                return

            def _send(self, status: int, body: Any = None, headers: dict[str, str] | None = None,
                      kind: str = "application/json") -> None:
                data = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Replay-Nonce", acme.nonce())
                self.send_header("Content-Type", kind)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/directory":
                    u = acme.url
                    self._send(200, {"newNonce": u + "/new-nonce", "newAccount": u + "/new-account",
                                     "newOrder": u + "/new-order"})
                else:
                    self._send(405)

            def do_HEAD(self) -> None:  # noqa: N802
                self._send(200)

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    protected, payload, key = acme.verify(self.path, body)
                except (KeyError, AssertionError, InvalidSignature) as err:
                    self._send(400, {"type": "urn:ietf:params:acme:error:malformed", "detail": repr(err)})
                    return
                u = acme.url
                if self.path == "/new-account":
                    assert payload["termsOfServiceAgreed"] is True
                    acme.accounts[u + "/acct/1"] = key
                    self._send(201, {"status": "valid"}, {"Location": u + "/acct/1"})
                elif self.path == "/new-order":
                    assert payload["identifiers"] == [{"type": "dns", "value": HOST}]
                    acme.order = {"status": "pending", "identifiers": payload["identifiers"],
                                  "authorizations": [u + "/authz/1"], "finalize": u + "/order/1/finalize"}
                    self._send(201, acme.order, {"Location": u + "/order/1"})
                elif self.path == "/authz/1":
                    self._send(200, {"status": acme.authz_status, "identifier": {"type": "dns", "value": HOST},
                                     "challenges": [{"type": "http-01", "url": u + "/chall/0", "token": "x"},
                                                    {"type": "tls-alpn-01", "url": u + "/chall/1",
                                                     "token": acme.token, "status": "pending"}]})
                elif self.path == "/chall/1":
                    ok = acme.pass_challenge and acme.check_challenge(key)
                    acme.authz_status = "valid" if ok else "invalid"
                    if ok:
                        acme.order["status"] = "ready"
                    self._send(200, {"type": "tls-alpn-01", "status": "processing"})
                elif self.path == "/order/1/finalize":
                    csr = x509.load_der_x509_csr(unb64(payload["csr"]))
                    assert csr.is_signature_valid
                    acme.certificate = acme.ca.sign(csr)
                    acme.order = {**acme.order, "status": "valid", "certificate": u + "/cert/1"}
                    self._send(200, acme.order, {"Location": u + "/order/1"})
                elif self.path == "/order/1":
                    self._send(200, acme.order)
                elif self.path == "/cert/1":
                    assert acme.certificate is not None
                    chain = acme.certificate.public_bytes(serialization.Encoding.PEM)
                    self._send(200, chain, kind="application/pem-certificate-chain")
                else:
                    self._send(404, {"type": "urn:ietf:params:acme:error:malformed"})

        return Handler


@pytest.fixture()
def port() -> int:
    return free_port()


@pytest.fixture()
def acme(port: int):
    server = FakeAcme(port)
    yield server
    server.close()


def test_the_computer_gets_a_certificate_for_its_name_with_a_key_made_here(tmp_path: Path, acme: FakeAcme,
                                                                         port: int) -> None:
    folder = tmp_path / "direct"
    certificates = Certificates(folder, AcmeIssuer(folder, acme.url + "/directory", poll_seconds=0.01))
    got = certificates.ensure(HOST, port)
    cert = x509.load_pem_x509_certificate(got.certfile.read_bytes())
    key = serialization.load_pem_private_key(got.keyfile.read_bytes(), None)
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName) == [HOST]
    assert acme.seen_alpn == ["acme-tls/1"]  # the authority reached the computer's own challenge answer
    assert stat.S_IMODE(got.keyfile.stat().st_mode) == 0o600


def test_the_certificate_is_kept_until_half_its_life_then_renewed(tmp_path: Path, acme: FakeAcme, port: int) -> None:
    clock = [datetime.datetime.now(datetime.UTC).timestamp()]
    folder = tmp_path / "direct"
    certificates = Certificates(folder, AcmeIssuer(folder, acme.url + "/directory", poll_seconds=0.01),
                                clock=lambda: clock[0])
    first = certificates.ensure(HOST, port).certfile.read_bytes()
    assert not certificates.due(HOST)
    assert certificates.ensure(HOST, port).certfile.read_bytes() == first
    clock[0] += 46 * 86_400
    assert certificates.due(HOST)
    assert certificates.ensure(HOST, port).certfile.read_bytes() != first
    assert certificates.due("p-zzzzzzzzzz.me.portward.test")  # a certificate for another name doesn't count


def test_a_failed_challenge_says_so_plainly_and_retries_back_off(tmp_path: Path, port: int) -> None:
    acme = FakeAcme(port, pass_challenge=False)
    clock = [1_800_000_000.0]
    folder = tmp_path / "direct"
    certificates = Certificates(folder, AcmeIssuer(folder, acme.url + "/directory", poll_seconds=0.01),
                                clock=lambda: clock[0])
    try:
        with pytest.raises(CertificateError, match="couldn't get a certificate"):
            certificates.ensure(HOST, port)
        for _ in range(2):
            with pytest.raises(CertificateError):
                certificates.ensure(HOST, port)
        with pytest.raises(CertificateError, match="try again after"):  # no more tries this hour
            certificates.ensure(HOST, port)
        acme.pass_challenge = True
        again = Certificates(folder, AcmeIssuer(folder, acme.url + "/directory", poll_seconds=0.01),
                             clock=lambda: clock[0])
        with pytest.raises(CertificateError, match="try again after"):  # remembered across restarts
            again.ensure(HOST, port)
        clock[0] += 3601
        assert again.ensure(HOST, port).certfile.exists()
    finally:
        acme.close()
