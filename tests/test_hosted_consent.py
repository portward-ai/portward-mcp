"""A connection's login goes to Portward's hosted engine only after the user said yes on this computer.

The account page can ask for the hosted route, and the AI can call web_route; neither is the user at this computer.
Before this, a signed-in computer obeyed either: whoever controlled the account service (or a browser session on the
account page), or text the AI had read, could have a database login sealed and uploaded. Now the route itself refuses
a connection the user hasn't allowed here: in the AI app's own prompt, or with `hosted-allow` at a terminal.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from harness import MemoryKeychain

from portward_free import cli
from portward_free.account import ACCOUNT_SETTING, TOKEN_ACCOUNT
from portward_free.hosted import seal
from portward_free.hosted.route import ROUTE_SETTING, HostedRoute, RouteRefused, hosted_connections
from portward_free.hosted.service import EngineInfo
from portward_free.hosted.wanted import WantedRoutes
from portward_free.keychain import account_for
from portward_free.paths import database_path
from portward_free.storage import LocalStore
from portward_free.usage import Usage

WAREHOUSE = {"host": "db.example.com", "port": 5432, "database": "d", "user": "u"}


class FakeEngines:
    """The account service's hosted-engine calls: one engine with a key, recording every call made to it."""

    def __init__(self) -> None:
        self.public = seal.public_key(seal.new_private_key())
        self.calls: list[str] = []
        self.bundles: list[bytes] = []

    def _info(self) -> EngineInfo:
        return EngineInfo("https://engine.example", "Portward runs this", "ready", self.public,
                          seal.key_id(self.public), len(self.bundles))

    def ensure(self, token: str) -> EngineInfo:
        self.calls.append("ensure")
        return self._info()

    def engine(self, token: str) -> EngineInfo | None:
        self.calls.append("engine")
        return self._info()

    def put_bundle(self, token: str, version: int, key_id: str, sealed: Any) -> None:
        self.calls.append("put_bundle")
        self.bundles.append(sealed)

    def remove(self, token: str) -> None:
        self.calls.append("remove")


class Asked:
    """The account page's waiting switches, and what this computer settled."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes, self.settled = routes, []

    def wanted_routes(self, token: str) -> dict[str, Any]:
        return {"routes": self.routes}

    def settle_routes(self, token: str, done: list[str], refused: list[str]) -> None:
        self.settled.append({"done": done, "refused": refused})


@pytest.fixture()
def keychain() -> MemoryKeychain:
    held = MemoryKeychain()
    held.set(TOKEN_ACCOUNT, "device-token")
    held.set(account_for("warehouse"), "the-database-password")
    return held


@pytest.fixture()
def store(home: Path) -> Any:
    opened = LocalStore.open(database_path(home))
    opened.add_connection("warehouse", "postgres", WAREHOUSE)
    opened.set_setting(ACCOUNT_SETTING, json.dumps({"email": "u@example.com", "account_id": "acct_1"}))
    yield opened
    opened.close()


def test_a_connection_the_user_hasnt_allowed_here_is_refused_and_nothing_is_asked_of_the_service(
        store: LocalStore, keychain: MemoryKeychain) -> None:
    engines = FakeEngines()
    with pytest.raises(RouteRefused) as refused:
        HostedRoute(store, keychain, engines).put_on("warehouse")
    assert "hosted-allow warehouse" in str(refused.value) and "on this computer" in str(refused.value)
    assert engines.calls == []  # no engine made, no bundle sent
    assert hosted_connections(store) == ()


def test_once_allowed_here_the_connection_goes_on_the_route(store: LocalStore, keychain: MemoryKeychain) -> None:
    engines = FakeEngines()
    route = HostedRoute(store, keychain, engines)
    route.allow("warehouse")
    sent = route.put_on("warehouse")
    assert sent.delivered and len(engines.bundles) == 1
    assert hosted_connections(store) == ("warehouse",)


def test_the_account_pages_ask_alone_never_uploads_a_login(store: LocalStore, keychain: MemoryKeychain) -> None:
    """H1: the service says 'warehouse: hosted' and offers a key. Without the user's yes here, nothing is sealed."""
    engines, asked = FakeEngines(), Asked({"warehouse": "hosted"})
    WantedRoutes(store, keychain, asked, HostedRoute(store, keychain, engines)).apply()
    assert engines.bundles == [] and "put_bundle" not in engines.calls
    assert hosted_connections(store) == ()
    assert asked.settled == [{"done": [], "refused": ["warehouse"]}]


def test_the_account_pages_ask_is_made_once_the_user_allowed_it_here(store: LocalStore,
                                                                    keychain: MemoryKeychain) -> None:
    engines, asked = FakeEngines(), Asked({"warehouse": "hosted"})
    route = HostedRoute(store, keychain, engines)
    route.allow("warehouse")
    WantedRoutes(store, keychain, asked, route).apply()
    assert len(engines.bundles) == 1
    assert asked.settled == [{"done": ["warehouse"], "refused": []}]


def test_taking_a_connection_off_forgets_the_yes(store: LocalStore, keychain: MemoryKeychain) -> None:
    route = HostedRoute(store, keychain, FakeEngines())
    route.allow("warehouse")
    route.put_on("warehouse")
    route.take_off("warehouse")
    with pytest.raises(RouteRefused):
        route.put_on("warehouse")


def test_an_allowed_connection_is_sent_again_after_a_change_without_asking_again(
        store: LocalStore, keychain: MemoryKeychain) -> None:
    engines = FakeEngines()
    route = HostedRoute(store, keychain, engines)
    route.allow("warehouse")
    route.put_on("warehouse")
    keychain.set(account_for("warehouse"), "a-new-password")
    route.refresh()
    assert len(engines.bundles) == 2


def test_a_name_slipped_into_the_list_without_a_yes_is_never_sent(store: LocalStore,
                                                                  keychain: MemoryKeychain) -> None:
    """The list is a setting in the local file; the yes is checked at the moment of sending, not only at put_on."""
    engines = FakeEngines()
    route = HostedRoute(store, keychain, engines)
    store.set_setting(ROUTE_SETTING, json.dumps({"connections": ["warehouse"], "allowed": []}))
    with pytest.raises(RouteRefused):
        route.send()
    assert engines.bundles == []


def test_connections_already_on_the_route_before_this_check_existed_keep_working(
        store: LocalStore, keychain: MemoryKeychain) -> None:
    """Their login is with the engine already; refusing to refresh it would only leave the engine stale."""
    engines = FakeEngines()
    store.set_setting(ROUTE_SETTING, json.dumps({"connections": ["warehouse"], "version": 0}))  # no "allowed" key
    assert HostedRoute(store, keychain, engines).send().delivered


def _hosted_allow(home: Path, keychain: MemoryKeychain, typed: str, at_terminal: bool = True) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(["hosted-allow", "warehouse"], home=home, keychain=keychain, prompt=lambda _: typed, out=out,
                    is_terminal=lambda: at_terminal,
                    usage=lambda s: Usage(s, None, disabled="usage counts are off in tests"))
    return code, out.getvalue()


def test_the_terminal_command_says_what_goes_where_and_takes_the_sources_name_as_the_yes(
        store: LocalStore, keychain: MemoryKeychain, home: Path) -> None:
    code, said = _hosted_allow(home, keychain, "warehouse")
    assert "db.example.com" in said and "password" in said and "Portward's hosted engine" in said
    allowed = json.loads(store.setting(ROUTE_SETTING))["allowed"]
    assert allowed == ["warehouse"], (code, said)


def test_the_terminal_command_refuses_a_wrong_name_and_a_redirected_run(
        store: LocalStore, keychain: MemoryKeychain, home: Path) -> None:
    wrong, _ = _hosted_allow(home, keychain, "yes")
    piped, said = _hosted_allow(home, keychain, "warehouse", at_terminal=False)
    assert wrong != 0 and piped == 2 and "yourself in a terminal" in said
    assert store.setting(ROUTE_SETTING) is None
