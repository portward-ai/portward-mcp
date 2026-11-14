"""The direct route (#103): claude.ai and ChatGPT on the web reach this computer through Portward's relay, and
Portward never sees your data.

`Direct` puts the pieces in order:
1. relay access from the account service: this account's own name under the personal relay domain, and a relay
   key for this computer (Personal must be on);
2. the dial-out (relay.FrpcTunnel): this computer connects out to the relay, which passes connections for that name
   through, still encrypted, to 127.0.0.1:`port`;
3. this computer's certificate for the name (certificate.Certificates), made here; getting it the first time goes
   through the relay too (TLS-ALPN-01 on `port`);
4. RemoteMcp on 127.0.0.1:`port` with that certificate, open only to the account owner (#102).

It renews the certificate when due (RemoteMcp is restarted on the new one: a few seconds offline), and when
RemoteMcp stops for good (Personal lapsed, signed out, stop()) it hangs up on the relay too.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from portward_free import proof
from portward_free.account import Accounts
from portward_free.keychain import Keychain
from portward_free.remote.certificate import AcmeIssuer, CertificateError, Certificates, Issuer
from portward_free.remote.relay import FrpcTunnel, RelayAccess, Tunnel, relay_access
from portward_free.remote.serve import PLAN_SECONDS, Certificate, RemoteMcp, RemoteRefused
from portward_free.storage import LocalStore
from portward_free.usage import Usage

log = logging.getLogger("portward.remote")

DIRECT_PORT = 8766
RENEW_SECONDS = 12 * 3600.0
ROUTE = proof.DIRECT  # "direct": the usage counts' route and the AI log's


class Direct:
    def __init__(self, store: LocalStore, keychain: Keychain, accounts: Accounts, *, folder: Path,
                 port: int = DIRECT_PORT, tunnel: Callable[[RelayAccess, int], Tunnel] | None = None,
                 issuer: Issuer | None = None, plan_seconds: float = PLAN_SECONDS,
                 renew_seconds: float = RENEW_SECONDS, usage: Usage | None = None) -> None:
        self.store, self.keychain, self.accounts, self.folder, self.port = store, keychain, accounts, folder, port
        self._make_tunnel = tunnel or (lambda access, local: FrpcTunnel(access, local, folder))
        self.certificates = Certificates(folder, issuer if issuer is not None else AcmeIssuer(folder))
        self.plan_seconds, self.renew_seconds = plan_seconds, renew_seconds
        self.usage = (usage if usage is not None else Usage.from_env(store)).routed(ROUTE)
        self.access: RelayAccess | None = None
        self.remote: RemoteMcp | None = None
        self.stopped_because: str | None = None
        self._tunnel: Tunnel | None = None
        self._stopping = threading.Event()
        self._stopped = threading.Event()

    @property
    def serving(self) -> bool:
        return self.remote is not None and self.remote.stopped_because is None and not self._stopped.is_set()

    def start(self) -> None:
        """Dial out, get the certificate if needed, and serve. RemoteRefused (a plain message) when it can't."""
        self.access = relay_access(self.keychain, self.accounts.service)
        self._tunnel = self._make_tunnel(self.access, self.port)
        self._tunnel.start()
        try:
            self.remote = self._serve(self._certificate())
        except RemoteRefused:
            self._tunnel.stop()
            raise
        threading.Thread(target=self._watch, name="portward-direct", daemon=True).start()

    def _certificate(self) -> Certificate:
        assert self.access is not None
        try:
            return self.certificates.ensure(self.access.host, self.port)
        except CertificateError as err:
            raise RemoteRefused(f"the direct route couldn't start: {err}") from None

    def _serve(self, certificate: Certificate) -> RemoteMcp:
        assert self.access is not None
        remote = RemoteMcp(self.store, self.keychain, self.accounts, public_url=self.access.address,
                           host="127.0.0.1", port=self.port, plan_seconds=self.plan_seconds,
                           certificate=certificate, usage=self.usage, route=ROUTE)
        remote.start()
        return remote

    def _watch(self) -> None:
        """Until stopped: renew the certificate when due; when RemoteMcp stops on its own, stop everything."""
        assert self.remote is not None and self.access is not None
        while True:
            if self.remote.wait(self.renew_seconds):
                if self._stopping.is_set() or not self._renewing():
                    break
                continue
            if self.certificates.due(self.access.host):
                self.remote.stop("renewing the certificate")
        self.stopped_because = self.stopped_because or self.remote.stopped_because
        if self._tunnel is not None:
            self._tunnel.stop()
        self._stopped.set()

    def _renewing(self) -> bool:
        """After RemoteMcp stopped: True if it was for a renewal and serving started again."""
        assert self.remote is not None
        if self.remote.stopped_because != "renewing the certificate":
            return False
        try:
            certificate = self._certificate()
        except RemoteRefused as err:  # keep serving on the certificate there is, while it lasts
            log.warning("%s", err)
            certificate = Certificate(self.certificates.certfile, self.certificates.keyfile)
        try:
            self.remote = self._serve(certificate)
        except RemoteRefused as err:
            self.stopped_because = str(err)
            return False
        return True

    def stop(self, reason: str | None = None) -> None:
        self.stopped_because = self.stopped_because or reason or "stopped"
        self._stopping.set()
        if self.remote is not None:
            self.remote.stop(reason)
        if self._tunnel is not None:
            self._tunnel.stop()

    def wait(self, timeout: float | None = None) -> bool:
        """True once it has stopped serving."""
        return self._stopped.wait(timeout)
