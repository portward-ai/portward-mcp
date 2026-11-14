"""The direct route's certificate (#103), made and kept on this computer: TLS ends here, never at the relay.

`Certificates` keeps one certificate for this computer's relay name and its key, in this tool's folder (the key
file readable by this user only, and never sent anywhere). It renews once a certificate is past half its life, so a
computer that is often asleep still renews in time as Let's Encrypt's certificates get shorter. After repeated
failures it stops trying for an hour (and remembers that across restarts): Let's Encrypt locks a name out after five
failed validations in an hour, and a sleeping laptop retrying in a loop would do exactly that.

`AcmeIssuer` gets the certificate over ACME (RFC 8555) with the TLS-ALPN-01 challenge (RFC 8737): the authority
connects to the name on port 443, the relay passes that connection through by name to this computer like any
other, and this computer answers it on the port RemoteMcp is about to use (`local_port`) with a one-off
certificate that proves it holds the ACME account key. No DNS change is needed, and nothing but this computer
can answer for its name.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import logging
import os
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

from portward_free.remote.serve import Certificate

log = logging.getLogger("portward.remote")

LETS_ENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
ACME_TLS_ALPN = "acme-tls/1"
ACME_IDENTIFIER = x509.ObjectIdentifier("1.3.6.1.5.5.7.1.31")  # id-pe-acmeIdentifier (RFC 8737)
MAX_FAILURES = 3  # per hour: under Let's Encrypt's five failed validations per name per hour
FAILURE_WINDOW = 3600.0
HTTP_SECONDS = 15
ISSUE_SECONDS = 120.0
POLL_SECONDS = 2.0


class CertificateError(Exception):
    """The certificate couldn't be had; the message is safe to show."""


class Issuer(Protocol):
    def issue(self, host: str, key: ec.EllipticCurvePrivateKey, local_port: int) -> bytes:
        """A PEM certificate chain for `host` and `key`'s public key."""
        ...


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
    path.chmod(0o600)


def _load_or_make_key(path: Path) -> ec.EllipticCurvePrivateKey:
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), None)
        if isinstance(key, ec.EllipticCurvePrivateKey):
            return key
    key = ec.generate_private_key(ec.SECP256R1())
    _write_private(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return key


@dataclass
class Certificates:
    folder: Path
    issuer: Issuer
    clock: Callable[[], float] = time.time

    @property
    def certfile(self) -> Path:
        return self.folder / "certificate.pem"

    @property
    def keyfile(self) -> Path:
        return self.folder / "certificate-key.pem"

    @property
    def _failures_file(self) -> Path:
        return self.folder / "certificate-failures.json"

    def due(self, host: str) -> bool:
        """True when there is no certificate for `host`, or it is past half its life."""
        try:
            cert = x509.load_pem_x509_certificate(self.certfile.read_bytes())
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except (OSError, ValueError, x509.ExtensionNotFound):
            return True
        if names.get_values_for_type(x509.DNSName) != [host]:
            return True
        start, end = cert.not_valid_before_utc.timestamp(), cert.not_valid_after_utc.timestamp()
        return self.clock() >= start + (end - start) / 2

    def ensure(self, host: str, local_port: int) -> Certificate:
        """A good certificate for `host`, getting a new one if it is due. CertificateError if that fails."""
        if not self.due(host):
            return Certificate(self.certfile, self.keyfile)
        failures = self._recent_failures()
        if len(failures) >= MAX_FAILURES:
            wait = datetime.datetime.fromtimestamp(min(failures) + FAILURE_WINDOW, datetime.UTC)
            raise CertificateError(f"the last {len(failures)} tries to get this computer's certificate failed; "
                                   f"Portward will try again after {wait:%H:%M} UTC")
        key = _load_or_make_key(self.keyfile)
        try:
            chain = self.issuer.issue(host, key, local_port)
            x509.load_pem_x509_certificate(chain)
        except (CertificateError, OSError, ValueError) as err:
            self._failed(failures)
            raise CertificateError(f"couldn't get a certificate for {host}: {err}") from None
        self.certfile.write_bytes(chain)
        self._failures_file.unlink(missing_ok=True)
        return Certificate(self.certfile, self.keyfile)

    def _recent_failures(self) -> list[float]:
        try:
            times = json.loads(self._failures_file.read_text())
        except (OSError, ValueError):
            return []
        now = self.clock()
        return [t for t in times if isinstance(t, (int, float)) and now - t < FAILURE_WINDOW]

    def _failed(self, earlier: list[float]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._failures_file.write_text(json.dumps([*earlier, self.clock()]))


# --- ACME ------------------------------------------------------------------------------------------------------------
class _AlpnAnswer:
    """The TLS-ALPN-01 answer on 127.0.0.1:port while the authority checks: a certificate for `host` carrying the
    digest of the key authorization in a critical acmeIdentifier extension, offered only for acme-tls/1."""

    def __init__(self, host: str, key_authorization: str, port: int) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        digest = hashlib.sha256(key_authorization.encode()).digest()
        now = datetime.datetime.now(datetime.UTC)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(x509.UnrecognizedExtension(ACME_IDENTIFIER, b"\x04\x20" + digest), critical=True)
                .sign(key, hashes.SHA256()))
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.set_alpn_protocols([ACME_TLS_ALPN])
        with tempfile.TemporaryDirectory() as folder:  # load_cert_chain wants files; they last only this long
            certfile, keyfile = Path(folder) / "c.pem", Path(folder) / "k.pem"
            certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            _write_private(keyfile, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()))
            self.context.load_cert_chain(certfile, keyfile)
        self.listener = socket.create_server(("127.0.0.1", port), reuse_port=False)
        self.listener.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="portward-acme-answer", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        try:
            with self.context.wrap_socket(conn, server_side=True):
                pass  # the handshake is the answer
        except (OSError, ssl.SSLError):
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.listener.close()


@dataclass
class AcmeIssuer:
    """Certificates from an ACME authority (Let's Encrypt by default) with TLS-ALPN-01."""

    folder: Path
    directory: str = LETS_ENCRYPT
    poll_seconds: float = POLL_SECONDS
    issue_seconds: float = ISSUE_SECONDS
    _nonce: str | None = field(default=None, repr=False)
    _kid: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._tls = ssl.create_default_context(cafile=certifi.where())
        self._account_key = _load_or_make_key(self.folder / "acme-account-key.pem")

    def issue(self, host: str, key: ec.EllipticCurvePrivateKey, local_port: int) -> bytes:
        directory = self._get_json(self.directory)
        self._kid = None
        _, _, headers = self._post(directory["newAccount"], {"termsOfServiceAgreed": True}, directory)
        self._kid = headers.get("Location")
        if not self._kid:
            raise CertificateError("the certificate authority didn't name the account")
        order, _, headers = self._post(directory["newOrder"], {"identifiers": [{"type": "dns", "value": host}]},
                                       directory)
        order_url = headers.get("Location", "")
        for authorization in order.get("authorizations", []):
            self._authorize(authorization, host, local_port, directory)
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
               .sign(key, hashes.SHA256()))
        self._post(order["finalize"], {"csr": _b64(csr.public_bytes(serialization.Encoding.DER))}, directory)
        order = self._wait(order_url, directory, done="valid")
        _, chain, _ = self._post(order["certificate"], None, directory, accept="application/pem-certificate-chain")
        return chain

    def _authorize(self, url: str, host: str, local_port: int, directory: dict[str, Any]) -> None:
        authz, _, _ = self._post(url, None, directory)
        if authz.get("status") == "valid":
            return
        challenge = next((c for c in authz.get("challenges", []) if c.get("type") == "tls-alpn-01"), None)
        if challenge is None:
            raise CertificateError("the certificate authority didn't offer the TLS-ALPN-01 challenge")
        answer = _AlpnAnswer(host, f"{challenge['token']}.{self._thumbprint()}", local_port)
        try:
            self._post(challenge["url"], {}, directory)
            self._wait(url, directory, done="valid")
        finally:
            answer.close()

    def _wait(self, url: str, directory: dict[str, Any], done: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.issue_seconds
        while True:
            body, _, _ = self._post(url, None, directory)
            status = body.get("status")
            if status == done:
                return body
            if status == "invalid":
                raise CertificateError("the certificate authority couldn't reach this computer through the relay "
                                       "to check it holds the name")
            if time.monotonic() > deadline:
                raise CertificateError("the certificate authority took too long")
            time.sleep(self.poll_seconds)

    # --- the wire ------------------------------------------------------------------------------------------------
    def _jwk(self) -> dict[str, str]:
        numbers = self._account_key.public_key().public_numbers()
        return {"crv": "P-256", "kty": "EC", "x": _b64(numbers.x.to_bytes(32)), "y": _b64(numbers.y.to_bytes(32))}

    def _thumbprint(self) -> str:
        return _b64(hashlib.sha256(json.dumps(self._jwk(), sort_keys=True, separators=(",", ":")).encode()).digest())

    def _signed(self, url: str, payload: dict[str, Any] | None, nonce: str) -> bytes:
        protected: dict[str, Any] = {"alg": "ES256", "nonce": nonce, "url": url}
        if self._kid:
            protected["kid"] = self._kid
        else:
            protected["jwk"] = self._jwk()
        head = _b64(json.dumps(protected).encode())
        body = "" if payload is None else _b64(json.dumps(payload).encode())
        r, s = decode_dss_signature(self._account_key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        return json.dumps({"protected": head, "payload": body,
                           "signature": _b64(r.to_bytes(32) + s.to_bytes(32))}).encode()

    def _new_nonce(self, directory: dict[str, Any]) -> str:
        if self._nonce:
            nonce, self._nonce = self._nonce, None
            return nonce
        request = urllib.request.Request(directory["newNonce"], method="HEAD")
        with urllib.request.urlopen(request, timeout=HTTP_SECONDS, context=self._tls_for(request)) as response:
            return response.headers["Replay-Nonce"]

    def _tls_for(self, request: urllib.request.Request) -> ssl.SSLContext | None:
        return self._tls if request.full_url.startswith("https://") else None

    def _post(self, url: str, payload: dict[str, Any] | None, directory: dict[str, Any], *,
              accept: str = "application/json") -> tuple[Any, bytes, dict[str, str]]:
        """(JSON body or {}, raw body, headers) of a signed POST (payload None: POST-as-GET); one retry on a stale
        nonce, as RFC 8555 asks."""
        for attempt in (1, 2):
            request = urllib.request.Request(url, self._signed(url, payload, self._new_nonce(directory)), method="POST",
                                             headers={"Content-Type": "application/jose+json", "Accept": accept,
                                                      "User-Agent": "portward-free"})
            try:
                with urllib.request.urlopen(request, timeout=HTTP_SECONDS, context=self._tls_for(request)) as response:
                    raw, headers = response.read(), dict(response.headers)
            except urllib.error.HTTPError as err:
                self._nonce = err.headers.get("Replay-Nonce")
                problem = self._problem(err.read())
                if attempt == 1 and problem.get("type", "").endswith(":badNonce"):
                    continue
                raise CertificateError(f"the certificate authority refused ({problem.get('detail') or err.code})") \
                    from None
            except (urllib.error.URLError, OSError) as err:
                raise CertificateError(f"couldn't reach the certificate authority ({err})") from None
            self._nonce = headers.get("Replay-Nonce")
            body = json.loads(raw) if raw and "json" in headers.get("Content-Type", "") else {}
            return body, raw, headers
        raise CertificateError("the certificate authority kept refusing the request's nonce")

    @staticmethod
    def _problem(raw: bytes) -> dict[str, Any]:
        try:
            problem = json.loads(raw)
        except ValueError:
            return {}
        return problem if isinstance(problem, dict) else {}

    def _get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"User-Agent": "portward-free"})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_SECONDS, context=self._tls_for(request)) as response:
                data = json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise CertificateError(f"couldn't reach the certificate authority ({err})") from None
        if not isinstance(data, dict):
            raise CertificateError("the certificate authority's directory isn't in the expected form")
        return data
