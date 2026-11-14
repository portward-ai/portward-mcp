"""Route switches asked for on the account page (#106), made on the computer.

The account page can only ask: a connection's login is on the computer, so the computer picks the waiting switches
up after a tool call (at most every CHECK_INTERVAL), puts a connection on the hosted route or takes it off, and tells
the control plane which it made and which it couldn't. Its weekly facts then report the route each connection is
on and which routes it may use, so the page shows the truth rather than the wish."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from harness import MemoryKeychain
from test_sync import Service, device, service  # noqa: F401 (fixtures)

from portward_free import weekly_facts
from portward_free.account import TOKEN_ACCOUNT, AccountService, account_token
from portward_free.hosted.route import ROUTE_SETTING, RouteRefused, Sent, hosted_connections
from portward_free.hosted.wanted import CHECKED, CHECK_INTERVAL, WantedRoutes
from portward_free.paths import database_path
from portward_free.storage import LocalStore


class FakeService:
    """The two account-service calls WantedRoutes makes, answered from memory."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.settled: list[dict[str, list[str]]] = []
        self.asked = 0

    def wanted_routes(self, token: str) -> dict[str, Any]:
        assert token == "device-token"
        self.asked += 1
        return {"routes": dict(self.routes)}

    def settle_routes(self, token: str, done: list[str], refused: list[str]) -> None:
        self.settled.append({"done": done, "refused": refused})


class FakeRoute:
    """HostedRoute's list, without an engine: put_on refuses names in `refuse`."""

    def __init__(self, store: LocalStore, refuse: set[str] | None = None) -> None:
        self.store, self.refuse = store, refuse or set()

    def _set(self, names: list[str]) -> Sent:
        self.store.set_setting(ROUTE_SETTING, json.dumps({"connections": sorted(names)}))
        return Sent(None, True, "")

    def put_on(self, name: str) -> Sent:
        if name in self.refuse:
            raise RouteRefused("not now")
        return self._set([*hosted_connections(self.store), name])

    def take_off(self, name: str) -> Sent:
        return self._set([n for n in hosted_connections(self.store) if n != name])


@pytest.fixture()
def store(home: Path) -> Any:
    opened = LocalStore.open(database_path(home))
    opened.add_connection("warehouse", "postgres", {"host": "db.example.com", "port": 5432, "database": "d",
                                                    "user": "u"})
    opened.add_connection("books", "sqlite", {"path": "/tmp/books.sqlite"})
    yield opened
    opened.close()


def signed_in() -> MemoryKeychain:
    keychain = MemoryKeychain()
    keychain.set(TOKEN_ACCOUNT, "device-token")
    return keychain


def test_a_waiting_switch_to_hosted_is_made_and_settled(store: LocalStore) -> None:
    service = FakeService({"warehouse": "hosted"})
    WantedRoutes(store, signed_in(), service, FakeRoute(store)).apply()
    assert hosted_connections(store) == ("warehouse",)
    assert service.settled == [{"done": ["warehouse"], "refused": []}]


def test_a_switch_back_to_direct_takes_the_connection_off_the_hosted_route(store: LocalStore) -> None:
    route = FakeRoute(store)
    route.put_on("warehouse")
    service = FakeService({"warehouse": "direct"})
    WantedRoutes(store, signed_in(), service, route).apply()
    assert hosted_connections(store) == ()
    assert service.settled == [{"done": ["warehouse"], "refused": []}]


def test_a_switch_this_computer_cannot_make_is_reported_refused(store: LocalStore) -> None:
    service = FakeService({"warehouse": "hosted"})
    WantedRoutes(store, signed_in(), service, FakeRoute(store, refuse={"warehouse"})).apply()
    assert hosted_connections(store) == ()
    assert service.settled == [{"done": [], "refused": ["warehouse"]}]


def test_a_connection_this_computer_does_not_have_is_left_for_another_computer(store: LocalStore) -> None:
    service = FakeService({"elsewhere": "hosted"})
    WantedRoutes(store, signed_in(), service, FakeRoute(store)).apply()
    assert service.settled == []


def test_a_switch_already_made_is_settled_without_touching_the_route(store: LocalStore) -> None:
    service = FakeService({"books": "direct"})
    WantedRoutes(store, signed_in(), service, FakeRoute(store, refuse={"books"})).apply()
    assert service.settled == [{"done": ["books"], "refused": []}]


def test_after_a_switch_the_weekly_facts_go_at_the_next_chance(store: LocalStore) -> None:
    store.set_setting(weekly_facts.LAST_SENT, datetime.now(UTC).isoformat())
    WantedRoutes(store, signed_in(), FakeService({"warehouse": "hosted"}), FakeRoute(store)).apply()
    last = datetime.fromisoformat(store.setting(weekly_facts.LAST_SENT) or "")
    assert datetime.now(UTC) - last >= weekly_facts.SEND_INTERVAL


def test_switches_are_checked_at_most_every_interval_and_only_when_signed_in(store: LocalStore) -> None:
    service = FakeService({})
    clock = [datetime(2027, 1, 15, 9, tzinfo=UTC)]
    wanted = WantedRoutes(store, signed_in(), service, FakeRoute(store), now=lambda: clock[0])
    wanted.check()
    wanted.check()
    assert service.asked == 1 and store.setting(CHECKED)
    clock[0] += CHECK_INTERVAL + timedelta(seconds=1)
    wanted.check()
    assert service.asked == 2
    WantedRoutes(store, MemoryKeychain(), service, FakeRoute(store), now=lambda: clock[0] + CHECK_INTERVAL * 2).check()
    assert service.asked == 2


def test_the_facts_report_each_connections_route_and_the_routes_it_may_use(store: LocalStore) -> None:
    FakeRoute(store).put_on("warehouse")
    connections = {c["name"]: c for c in weekly_facts.facts(store)["connections"]}
    assert connections["warehouse"] == {"name": "warehouse", "route": "hosted", "drift": None,
                                        "offered": ["hosted", "direct"]}
    assert connections["books"] == {"name": "books", "route": "desktop", "drift": None, "offered": ["direct"]}


async def test_the_account_service_answers_with_the_waiting_switches(service: Service,  # noqa: F811
                                                                     tmp_path: Path) -> None:
    async with device(service, tmp_path / "laptop", "wren@gmail.example") as laptop:
        token = account_token(laptop.keychain)
        assert token
        accounts = AccountService(service.url)
        assert accounts.wanted_routes(token) == {"routes": {}}
        accounts.settle_routes(token, ["warehouse"], [])
