"""The hosted engine (#104): the free tool in server mode, one per Personal user, in a container Portward runs.

`portward-free serve-hosted` (the container's command, free/Dockerfile) reads its settings from the environment
the control plane gave the container (personal_hosted: PORTWARD_ACCOUNT_URL, PORTWARD_PUBLIC_URL,
PORTWARD_ENGINE_TOKEN) and keeps its own state under PORTWARD_HOME (the container's volume, /data):
- engine.key: its X25519 private key, made on first start (mode 600). The public half is registered with the
  control plane; the computer seals the bundle to it. The engine's other keys (the one that seals its AI log and
  kept rows) are derived from it, so nothing else secret is written down.
- portward.db: the same local file as on a computer: the connections and governance of the latest bundle, the
  history and the AI log of what it served.
Logins live only in memory (HeldKeychain), from the bundle, opened again at every start.

Then it serves the same MCP as a computer (remote.RemoteMcp: OAuth-protected, the account owner only, stops when
Personal lapses), with route "hosted" in the AI log, and only the tools that read (TOOLS): editing rules, profiles,
context or connections happens in Portward on the computer, never on the web (#90 story 42). Every `poll_seconds`
it fetches a newer bundle if there is one and hands its new AI log entries on (hosted/inbox.py). Sleeping and waking
are the host's (a suspended machine resumes with all of this in memory)."""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from portward_free import kept, proof
from portward_free.account import ACCOUNT_SETTING, TOKEN_ACCOUNT, AccountServiceError, Accounts
from portward_free.hosted import bundle, inbox, seal
from portward_free.hosted.service import HostedService
from portward_free.remote.serve import PLAN_SECONDS, RemoteMcp, RemoteRefused
from portward_free.storage import LocalStore
from portward_free.usage import Usage

log = logging.getLogger("portward.hosted")

PORT = 8080
POLL_SECONDS = 30.0
KEY_FILE = "engine.key"
DATABASE = "portward.db"
LANDED_SETTING = "hosted_bundle"  # {version, key_id}: the bundle the engine holds now
# What the AI may call on the hosted route: reading, and the results panel's own tools. Anything else, including
# tools added later, isn't there.
TOOLS = frozenset({
    "list_sources", "describe_source", "query", "rerun", "tweak", "compare", "more_rows", "get_selection",
    "history", "lineage", "kept_rows", "ai_log", "sent_to_ai",
    "panel_rows", "put_in_chat", "value_source",
})
ENV = ("PORTWARD_ACCOUNT_URL", "PORTWARD_PUBLIC_URL", "PORTWARD_ENGINE_TOKEN")


class EngineRefused(Exception):
    """The engine can't start; the message is safe to log."""


@dataclass
class HeldKeychain:
    """The engine's keychain: in memory only, filled at every start."""
    entries: dict[str, str] = field(default_factory=dict)

    def set(self, account: str, secret: str) -> None:
        self.entries[account] = secret

    def get(self, account: str) -> str | None:
        return self.entries.get(account)

    def delete(self, account: str) -> None:
        self.entries.pop(account, None)


def engine_key(home: Path) -> bytes:
    """The engine's private key from its volume, made the first time."""
    path = home / KEY_FILE
    if path.exists():
        raw = path.read_bytes()
        if len(raw) == seal.KEY_BYTES:
            return raw
        raise EngineRefused(f"{path} isn't an engine key; move it away to start with a new one")
    home.mkdir(parents=True, exist_ok=True)
    raw = seal.new_private_key()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(raw)
    return raw


def _derived(key: bytes, purpose: str) -> str:
    return base64.b64encode(HKDF(hashes.SHA256(), 32, salt=None, info=purpose.encode()).derive(key)).decode()


class Engine:
    def __init__(self, home: Path, account_url: str, token: str, public_url: str, *, host: str = "0.0.0.0",
                 port: int = PORT, poll_seconds: float = POLL_SECONDS, plan_seconds: float = PLAN_SECONDS,
                 usage: Usage | None = None) -> None:
        self.home, self.token, self.public_url = home, token, public_url
        self.host, self.port, self.poll_seconds, self.plan_seconds = host, port, poll_seconds, plan_seconds
        self.service = HostedService(account_url)
        self.accounts = Accounts(self.service)
        self.keychain = HeldKeychain()
        self.store = LocalStore.open(home / DATABASE)
        self.usage = usage
        self.remote: RemoteMcp | None = None
        self.account_id = ""
        self._key = b""
        self._inbox: bytes | None = None
        self._stopped = threading.Event()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Engine:
        env = dict(os.environ if env is None else env)
        missing = [name for name in ENV if not env.get(name)]
        if missing:
            raise EngineRefused(f"the engine needs {', '.join(missing)} in its environment")
        return cls(Path(env.get("PORTWARD_HOME", "/data")), env["PORTWARD_ACCOUNT_URL"],
                   env["PORTWARD_ENGINE_TOKEN"], env["PORTWARD_PUBLIC_URL"], port=int(env.get("PORT", PORT)))

    @property
    def resource(self) -> str:
        return self.public_url.rstrip("/") + "/mcp"

    # --- starting ---------------------------------------------------------------------------------------------------
    def prepare(self) -> None:
        """Keys, account, registration and the latest bundle; EngineRefused when it can't serve."""
        self._key = engine_key(self.home)
        self.keychain.set(TOKEN_ACCOUNT, self.token)
        self.keychain.set(kept.KEY_ACCOUNT, _derived(self._key, "portward-engine-kept-v1"))
        try:
            me = self.service.me(self.token)
            self.account_id = str(me.get("id") or "")
            self.store.set_setting(ACCOUNT_SETTING, json_dumps({"account_id": self.account_id,
                                                                "email": me.get("email")}))
            self.service.register_key(self.token, seal.public_key(self._key))
        except AccountServiceError as err:
            raise EngineRefused(f"the account service refused the engine ({err.status or 'not reached'})") from None
        self.refresh()

    def refresh(self) -> bool:
        """Land a newer bundle if the control plane holds one; True when it did. Never raises."""
        try:
            sealed = self.service.bundle(self.token)
        except AccountServiceError as err:
            log.info("couldn't fetch the bundle (%s)", err.status or "not reached")
            return False
        if sealed is None:
            return False
        held = json_loads(self.store.setting(LANDED_SETTING))
        if held.get("version") == sealed.version and held.get("key_id") == sealed.key_id and self._inbox:
            return False
        try:
            content = seal.open_sealed(self._key, seal.BUNDLE, self.account_id, sealed.sealed)
            self._inbox = bundle.land(self.store, self.keychain, content)
        except (seal.Unopenable, bundle.BadBundle) as err:
            log.warning("the bundle wasn't landed: %s", err)
            return False
        self.store.set_setting(LANDED_SETTING, json_dumps({"version": sealed.version, "key_id": sealed.key_id}))
        log.info("landed bundle version %s", sealed.version)
        return True

    def forward(self) -> int:
        if self._inbox is None:
            return 0
        return inbox.forward(self.store, self.service, self.token, self.account_id, self._inbox)

    def start(self) -> None:
        """Serve in the background; returns once listening. EngineRefused when it can't."""
        self.prepare()
        usage = self.usage if self.usage is not None else Usage.from_env(self.store)
        self.remote = RemoteMcp(self.store, self.keychain, self.accounts, public_url=self.public_url,
                                host=self.host, port=self.port, plan_seconds=self.plan_seconds,
                                usage=usage.routed(proof.HOSTED), route=proof.HOSTED, tools=TOOLS)
        try:
            self.remote.start()
        except RemoteRefused as err:
            raise EngineRefused(str(err)) from None
        threading.Thread(target=self._poll, name="portward-engine", daemon=True).start()

    def _poll(self) -> None:
        assert self.remote is not None
        while not self.remote.wait(self.poll_seconds):
            self.refresh()
            self.forward()
        self.forward()
        self._stopped.set()

    def stop(self, reason: str | None = None) -> None:
        if self.remote is not None:
            self.remote.stop(reason)

    def wait(self, timeout: float | None = None) -> bool:
        return self._stopped.wait(timeout)

    def close(self) -> None:
        self.stop()
        self.wait(5)
        self.store.close()


def json_dumps(value: object) -> str:
    return json.dumps(value)


def json_loads(raw: str | None) -> dict:
    value = json.loads(raw) if raw else {}
    return value if isinstance(value, dict) else {}
