"""Route switches asked for on the account page (#106), made here.

The account page can only ask: a connection's login is on this computer, so the switch is made here. After a tool
call (at most every CHECK_INTERVAL, only when signed in) this asks the control plane for the waiting switches
(GET /v1/me/routes), puts each connection this computer has on the hosted route or takes it off (HostedRoute), and
says which it made and which it couldn't (POST /v1/me/routes/done). A connection this computer doesn't have is left
for the computer that does. After a change the weekly facts go at the next chance, so the page shows the route the
connection is really on. Nothing but connection names and "hosted" / "direct" goes either way.

The page's ask is not the user's yes on this computer: a switch to hosted for a connection the user hasn't allowed
here (HostedRoute.allow) is refused by the route and settled as refused, with nothing sent."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import anyio.to_thread

from portward_free import weekly_facts
from portward_free.account import AccountServiceError, account_token
from portward_free.hosted.route import RouteRefused, Sent, hosted_connections
from portward_free.keychain import Keychain, KeychainError
from portward_free.route_names import DIRECT, HOSTED
from portward_free.storage import LocalStore

log = logging.getLogger("portward.hosted")

CHECK_INTERVAL = timedelta(minutes=5)
CHECKED = "routes_last_check"
TOOL_CALL = "tools/call"


class SwitchService(Protocol):
    def wanted_routes(self, token: str) -> dict[str, Any]: ...

    def settle_routes(self, token: str, done: list[str], refused: list[str]) -> None: ...


class Switcher(Protocol):
    def put_on(self, name: str) -> Sent: ...

    def take_off(self, name: str) -> Sent: ...


class WantedRoutes:
    """MCP middleware (and `apply` for a direct call): makes the account page's waiting route switches."""

    def __init__(self, store: LocalStore, keychain: Keychain, service: SwitchService, route: Switcher, *,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.store, self.keychain, self.service, self.route, self.now = store, keychain, service, route, now

    def _token(self) -> str | None:
        try:
            return account_token(self.keychain)
        except KeychainError as err:
            log.info("route switches not checked: %s", err)
            return None

    def _due(self) -> bool:
        last = self.store.setting(CHECKED)
        try:
            return last is None or self.now() - datetime.fromisoformat(last) >= CHECK_INTERVAL
        except ValueError:
            return True

    def check(self) -> None:
        """`apply` if signed in and the last check was over CHECK_INTERVAL ago. Never raises."""
        if not self._due() or not self._token():
            return
        self.store.set_setting(CHECKED, self.now().isoformat())
        self.apply()

    def _make(self, name: str, route: str) -> bool:
        """Whether `name` is on `route` now; RouteRefused when it couldn't be put there."""
        on_hosted = name in hosted_connections(self.store)
        if route == HOSTED and not on_hosted:
            self.route.put_on(name)
            return True
        if route == DIRECT and on_hosted:
            self.route.take_off(name)
            return True
        return False

    def apply(self) -> None:
        token = self._token()
        if not token:
            return
        try:
            wanted = self.service.wanted_routes(token).get("routes", {})
        except AccountServiceError as err:  # signed out, Personal ended, or not reached: next time
            log.info("route switches not fetched: %s", err)
            return
        mine = {c.name for c in self.store.connections()}
        done, refused, changed = [], [], False
        for name, route in sorted(wanted.items()):
            if name not in mine or route not in (HOSTED, DIRECT):
                continue
            try:
                changed = self._make(name, route) or changed
                done.append(name)
            except RouteRefused as err:
                log.info("the route switch for a connection wasn't made: %s", err)
                refused.append(name)
        if changed:
            self.store.set_setting(weekly_facts.LAST_SENT, (self.now() - weekly_facts.SEND_INTERVAL).isoformat())
        if not done and not refused:
            return
        try:
            self.service.settle_routes(token, done, refused)
        except AccountServiceError as err:
            log.info("route switches not settled: %s", err)

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        result = await call_next(ctx)
        if ctx.method == TOOL_CALL:
            await anyio.to_thread.run_sync(self.check)
        return result
