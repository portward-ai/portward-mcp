"""The computer's side of the hosted route (#104): which connections are on it, and handing them to the engine.

`put_on` / `take_off` change the list (kept in the setting ROUTE_SETTING with the last version sent) and `send`
hands the engine the current bundle: it asks the control plane for the account's engine (made the first time,
Personal only), waits for the engine to register its public key, seals the bundle to that key and sends the
ciphertext with the next version. Nothing is sent when the bundle is the same as the last one sent to the same key.
With no connection left on the route, the engine is deleted. `refresh` re-sends after a change on the computer
(a profile, a rule, a password) and never raises.

A connection's login leaves this computer only after the user said yes here (`allow`: the AI app's own prompt in
web_route, or `portward-free hosted-allow` at a terminal). The account page and the AI can ask for the hosted route;
neither is the user at this computer, so `put_on` and every send refuse a name that isn't allowed, before anything
is asked of the service. Taking a connection off forgets the yes."""

from __future__ import annotations

import json
import logging
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from portward_free.account import ACCOUNT_SETTING, AccountServiceError, account_token
from portward_free.hosted import bundle, inbox, seal
from portward_free.hosted.service import EngineInfo, HostedService
from portward_free.keychain import Keychain, KeychainError
from portward_free.passwords import command
from portward_free.storage import LocalStore

log = logging.getLogger("portward.hosted")

ROUTE_SETTING = "hosted_route"  # {connections: [...], allowed: [...], version, sent: fingerprint, key_id}
WAIT_SECONDS = 20.0  # for a new engine to start and register its key


class RouteRefused(Exception):
    """The hosted route can't be used now; the message is safe to show."""


@dataclass(frozen=True)
class Sent:
    """What `send` did: the engine's address, whether the bundle reached it, and what to tell the user."""
    address: str | None
    delivered: bool
    note: str


def _state(store: LocalStore) -> dict[str, Any]:
    raw = store.setting(ROUTE_SETTING)
    state = json.loads(raw) if raw else {}
    connections = list(state.get("connections", []))
    # a list written before the yes was kept: those logins are with the engine already, so they stay allowed
    allowed = state.get("allowed")
    return {"connections": connections, "allowed": list(connections if allowed is None else allowed),
            "version": int(state.get("version", 0)),
            "sent": state.get("sent"), "key_id": state.get("key_id"), "address": state.get("address")}


def hosted_connections(store: LocalStore) -> tuple[str, ...]:
    return tuple(_state(store)["connections"])


def allow_command(name: str) -> str:
    """What the user runs at a terminal on this computer to say yes for `name` (cli.hosted_allow)."""
    return command(f"hosted-allow {shlex.quote(name)}")


def needs_yes(name: str) -> str:
    return (f"{name} isn't on the hosted route: that sends its login and password to Portward's hosted engine, so "
            "the user says yes on this computer first, and nothing was sent. In an AI app that can ask them, call "
            f"web_route for it; otherwise ask them to run this in a terminal on this computer:\n\n    "
            f"{allow_command(name)}")


class HostedRoute:
    def __init__(self, store: LocalStore, keychain: Keychain, service: HostedService,
                 wait: float = WAIT_SECONDS, sleep: Callable[[float], None] = time.sleep) -> None:
        self.store, self.keychain, self.service, self.wait, self.sleep = store, keychain, service, wait, sleep

    def _save(self, **changes: Any) -> dict[str, Any]:
        state = {**_state(self.store), **changes}
        self.store.set_setting(ROUTE_SETTING, json.dumps(state))
        return state

    def _signed(self) -> tuple[str, str]:
        try:
            token = account_token(self.keychain)
        except KeychainError as err:
            raise RouteRefused(f"the account token couldn't be read from the keychain: {err}") from None
        stored = self.store.setting(ACCOUNT_SETTING)
        account_id = json.loads(stored).get("account_id", "") if stored else ""
        if not token or not account_id:
            raise RouteRefused("this computer isn't signed in to a Portward account: call account_sign_in first")
        return token, account_id

    @staticmethod
    def _failed(err: AccountServiceError) -> RouteRefused:
        if err.status == 402:
            return RouteRefused("Personal has ended, so the hosted route is off. Everything on this computer stays.")
        if err.status == 401:
            return RouteRefused("the account no longer knows this computer: call account_sign_in again")
        return RouteRefused(f"the hosted route couldn't be used just now: {err}")

    # --- the user's yes, on this computer ----------------------------------------------------------------------------
    def is_allowed(self, name: str) -> bool:
        return name in _state(self.store)["allowed"]

    def allow(self, name: str) -> None:
        """The user said yes here for `name`. Only a path the user answers themselves calls this: never the account
        page's ask, never a tool argument."""
        try:
            bundle.hostable(self.store, name)
        except bundle.NotHostable as err:
            raise RouteRefused(str(err)) from None
        self._save(allowed=sorted({*_state(self.store)["allowed"], name}))

    def _require_allowed(self, names: list[str]) -> None:
        allowed = _state(self.store)["allowed"]
        for name in names:
            if name not in allowed:
                raise RouteRefused(needs_yes(name))

    # --- the list ---------------------------------------------------------------------------------------------------
    def put_on(self, name: str) -> Sent:
        try:
            bundle.hostable(self.store, name)
        except bundle.NotHostable as err:
            raise RouteRefused(str(err)) from None
        self._require_allowed([name])
        names = _state(self.store)["connections"]
        self._signed()
        sent = self._send(sorted({*names, name}))
        self._save(connections=sorted({*names, name}))
        return sent

    def take_off(self, name: str) -> Sent:
        state = _state(self.store)
        names = [n for n in state["connections"] if n != name]
        self._save(connections=names, allowed=[n for n in state["allowed"] if n != name])
        return self.send()

    # --- sending ----------------------------------------------------------------------------------------------------
    def send(self) -> Sent:
        """Hand the engine the current bundle. A connection removed from this computer leaves the route too."""
        names = [n for n in _state(self.store)["connections"] if self.store.connection(n) is not None]
        self._save(connections=names)
        return self._send(names)

    def _send(self, names: list[str]) -> Sent:
        self._require_allowed(names)  # before anything is asked of the service: no engine made, no key fetched
        token, account_id = self._signed()
        try:
            if not names:
                if _state(self.store)["address"] is not None or self.service.engine(token) is not None:
                    self.service.remove(token)
                self._save(sent=None, key_id=None, address=None)
                return Sent(None, True, "Nothing is on the hosted route now; Portward deleted its hosted engine "
                                        "and what it held.")
            info = self._engine_with_key(token)
        except AccountServiceError as err:
            raise self._failed(err) from None
        self._save(address=info.address)
        if info.public_key is None or info.key_id is None:
            return Sent(info.address, False, "The hosted engine is still starting; Portward sends the connections "
                                             "when it's ready (call web_routes to try again).")
        try:
            made = bundle.build(self.store, self.keychain, names,
                                seal.public_key(inbox.inbox_key(self.keychain, create=True) or b""))
        except bundle.NotHostable as err:
            raise RouteRefused(str(err)) from None
        state = _state(self.store)
        mark = bundle.fingerprint(made)
        if state["sent"] == mark and state["key_id"] == info.key_id and info.version >= state["version"] > 0:
            return Sent(info.address, True, "The hosted engine already has these connections.")
        sealed = seal.seal(info.public_key, seal.BUNDLE, account_id, made)
        version = max(state["version"], info.version) + 1
        try:
            self.service.put_bundle(token, version, info.key_id, sealed)
        except AccountServiceError as err:
            if err.status != 409:
                raise self._failed(err) from None
            return Sent(info.address, False, "The hosted engine changed its key just now; call web_routes to send "
                                             "the connections again.")
        self._save(version=version, sent=mark, key_id=info.key_id)
        return Sent(info.address, True, "Sent, sealed so only the hosted engine opens it.")

    def _engine_with_key(self, token: str) -> EngineInfo:
        info = self.service.ensure(token)
        waited = 0.0
        while info.public_key is None and waited < self.wait:
            self.sleep(0.25)
            waited += 0.25
            found = self.service.engine(token)
            info = found if found is not None else info
        return info

    def refresh(self) -> None:
        """After a change on this computer: send again if anything the engine holds changed. Never raises."""
        if not _state(self.store)["connections"]:
            return
        try:
            self.send()
        except RouteRefused as err:
            log.info("the hosted engine wasn't updated: %s", err)

    def collect(self) -> int:
        """The engine's AI log summary into this computer's (inbox.collect); 0 when there's nothing or it can't."""
        try:
            token, account_id = self._signed()
            return inbox.collect(self.store, self.keychain, self.service, token, account_id)
        except (RouteRefused, AccountServiceError, KeychainError) as err:
            log.info("the hosted engine's AI log wasn't collected: %s", err)
            return 0
