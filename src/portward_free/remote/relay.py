"""The direct route's dial-out (#103): this computer connects out to Portward's relay, which passes claude.ai's and
ChatGPT's TLS through to it by name. Nothing on this computer listens to the internet and no router port is opened.

- `relay_access` asks the account service (with this computer's device token) for this account's name under the
  personal relay domain and this computer's relay key. Personal must be on.
- `FrpcTunnel` runs the frp client (`frpc`, the same client Team gateways use) with a config made here: it logs in
  as the account's name with the relay key, says it is a Personal computer, and asks for exactly the account's
  address, forwarded still encrypted to RemoteMcp on 127.0.0.1. The key goes to frpc in its environment, never in
  the config file. If frpc exits it is started again, backing off, until stopped.

The relay never holds this computer's certificate key (certificate.py makes it here), so it can't read what passes.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import certifi

from portward_free.account import AccountService, AccountServiceError, account_token
from portward_free.keychain import Keychain, KeychainError
from portward_free.remote.serve import RemoteRefused

log = logging.getLogger("portward.remote")

NAME = re.compile(r"^p-[a-z2-7]{10}$")
HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
KEY_ENV = "PORTWARD_RELAY_KEY"
FRPC_ENV = "PORTWARD_FRPC"
ROUTE = "personal"  # the relay's gatekeeper asks the control plane's Personal check for logins that say this
RESTART_SECONDS: tuple[float, ...] = (1, 2, 5, 10, 30, 60)
STOP_SECONDS = 5.0
NO_FRPC = ("the direct route needs the frp client (frpc) on this computer: install it (https://github.com/fatedier/"
           f"frp/releases, v0.71.1) and put it on PATH, or set {FRPC_ENV} to where it is")


@dataclass(frozen=True)
class RelayAccess:
    """This account's address on the direct route, and this computer's key for the relay."""

    name: str
    address: str  # https://<name>.<personal relay domain>
    relay_host: str
    key: str = field(repr=False)

    @property
    def host(self) -> str:
        return urlsplit(self.address).netloc

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> RelayAccess:
        """The account service's answer, checked: a wrong shape is refused, never passed on to frpc."""
        name, address, relay, key = (raw.get(k) for k in ("name", "address", "relay_host", "key"))
        domain = raw.get("relay_domain")
        if not (isinstance(name, str) and NAME.fullmatch(name) and isinstance(domain, str) and HOST.fullmatch(domain)
                and address == f"https://{name}.{domain}" and isinstance(relay, str) and HOST.fullmatch(relay)
                and isinstance(key, str) and 0 < len(key) <= 512 and key.isprintable()):
            raise ValueError("the account service's relay access isn't in the expected form")
        return cls(name, address, relay, key)


def relay_access(keychain: Keychain, service: AccountService) -> RelayAccess:
    """This computer's relay access, or RemoteRefused (not signed in, Personal off, no direct route yet)."""
    try:
        token = account_token(keychain)
    except KeychainError as err:
        raise RemoteRefused(f"the account token couldn't be read from the keychain: {err}") from None
    if not token:
        raise RemoteRefused("this computer isn't signed in to Portward; run `portward-free sign-in` first",
                            free=True)
    try:
        return RelayAccess.parse(service.relay_access(token))
    except AccountServiceError as err:
        if err.status == 402:
            raise RemoteRefused("the direct route needs Personal, and this account doesn't have it now",
                                free=True) from None
        raise RemoteRefused(f"couldn't get relay access: {err}") from None
    except ValueError as err:
        raise RemoteRefused(str(err)) from None


def frpc_config(access: RelayAccess, local_port: int, ca_file: str | None = None) -> str:
    """frpc's config for this computer. It holds no secret: the key is read from KEY_ENV."""
    ca = ca_file or certifi.where()
    return "\n".join([
        "# Portward's direct route: connects out to the relay, which passes connectors' TLS through to this computer.",
        f'serverAddr = "{access.relay_host}"',
        "serverPort = 443",
        "loginFailExit = false",
        f'user = "{access.name}"',
        f'metadatas.token = "{{{{ .Envs.{KEY_ENV} }}}}"',
        f'metadatas.route = "{ROUTE}"',
        "transport.tls.enable = true",
        f'transport.tls.serverName = "{access.relay_host}"',
        f"transport.tls.trustedCaFile = {_toml_string(ca)}",
        "transport.tls.disableCustomTLSFirstByte = false",
        'log.to = "console"',
        'log.level = "warn"',
        "",
        "[[proxies]]",
        'name = "mcp"',
        'type = "https"',
        f'customDomains = ["{access.host}"]',
        'localIP = "127.0.0.1"',
        f"localPort = {int(local_port)}",
        "",
    ])


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find_frpc(env: dict[str, str] | None = None) -> Path | None:
    env = dict(os.environ) if env is None else env
    configured = env.get(FRPC_ENV, "").strip()
    if configured:
        return Path(configured) if Path(configured).is_file() else None
    found = shutil.which("frpc")
    return Path(found) if found else None


class Tunnel(Protocol):
    """The dial-out to the relay (FrpcTunnel; a fake in tests)."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


class FrpcTunnel:
    def __init__(self, access: RelayAccess, local_port: int, folder: Path, frpc: Path | None = None, *,
                 restart_seconds: Sequence[float] = RESTART_SECONDS,
                 spawn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen) -> None:
        found = frpc or find_frpc()
        if found is None:
            raise RemoteRefused(NO_FRPC)
        self.access, self.local_port, self.folder, self.frpc = access, local_port, folder, found
        self.restart_seconds, self._spawn = tuple(restart_seconds), spawn
        self.config = folder / "frpc.toml"
        self.starts = 0
        self._stopping = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.config.write_text(frpc_config(self.access, self.local_port))
        self.config.chmod(0o600)
        self._launch()
        threading.Thread(target=self._supervise, name="portward-relay", daemon=True).start()

    def _launch(self) -> None:
        env = {KEY_ENV: self.access.key, "PATH": os.environ.get("PATH", "")}  # nothing else of ours reaches frpc
        with self._lock:
            self._process = self._spawn([str(self.frpc), "-c", str(self.config)], env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.starts += 1

    def _supervise(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            process = self._process
            assert process is not None
            process.wait()
            if self._stopping.is_set():
                return
            delay = self.restart_seconds[min(failures, len(self.restart_seconds) - 1)]
            failures += 1
            log.info("the relay client stopped (exit %s); starting it again in %ss", process.returncode, delay)
            if self._stopping.wait(delay):
                return
            self._launch()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
