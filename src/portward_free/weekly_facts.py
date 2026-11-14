"""Weekly facts for the Personal account page (#105): the only facts this computer sends the control plane about
the user's work, because the account page shows them (POST /v1/me/facts, with the device token).

What is sent, and nothing else (the control plane refuses a report with any other key):
- this week's counts (the week starts Monday, UTC): rows the AI read and asks per AI app, from this computer's own
  AI log entries (synced entries from other computers are theirs to report), and how many asks named a column no
  applied profile grants (`count_hidden_ask`, counted in pull.run_pull; the ask itself isn't kept);
- each profile's name, version, grant counts by level, the connection names it is applied to and the AI apps that
  read through it this week;
- each connection's name and route (hosted when it is on the hosted route, #104), the web routes it may use
  (remote.routes.offered_routes, so the account page offers the switch only where it can work, #106), with the last
  drift check's counts (login_sql.drift via
  source_tools.login_drift, `remember_drift`): never which tables or columns, never the SQL; and how many context
  items are held back there because a column they name is gone (context_drift, #123, `remember_held_back`), never
  which items;
- this computer's id (the AI log's origin, so linking it again is the same computer) and Portward's version.
Table and column names, context, questions, SQL, rows and history never go this way.

When: signed in to a Personal account, after a tool call, at most once an hour (SEND_INTERVAL, the usage counts'
cadence). The hosted engine (#104) runs the same code with route "hosted" and reports as the hosted engine."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import anyio
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

from portward_free import __version__, proof
from portward_free.account import AccountServiceError, Accounts, account_token
from portward_free.hosted.route import hosted_connections
from portward_free.keychain import Keychain, KeychainError
from portward_free.login_sql import Drift
from portward_free.profiles import LEVELS
from portward_free.remote.routes import offered_routes
from portward_free.storage import LocalStore

logger = logging.getLogger(__name__)

SEND_INTERVAL = timedelta(hours=1)
LAST_SENT = "facts_last_attempt"
HIDDEN_ASKS = "facts_hidden_asks"  # {"YYYY-MM-DD" (a Monday): count}, this week and last only
LOGIN_DRIFT = "facts_login_drift"  # {connection name: {columns, tables, elsewhere}}
HELD_BACK = "facts_held_back"  # {connection name: count of context items naming a column that's gone}
GRANT_LEVELS = (*LEVELS, "whole")  # "whole": non-table items, not built yet (always 0 here)
MAX_NAME = 80
MAX_ITEMS = 200
MAX_APPS = 50
TOOL_CALL = "tools/call"


def week_of(at: datetime) -> str:
    """The Monday (UTC) of the week `at` falls in, as the control plane counts weeks."""
    day = at.astimezone(UTC).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def _name(raw: object) -> str:
    """A name as the control plane takes it: printable, single-spaced, at most MAX_NAME characters."""
    text = "".join(ch for ch in str(raw or "") if ch.isprintable())
    return " ".join(text.split())[:MAX_NAME].strip()


def _json_setting(store: LocalStore, key: str) -> dict[str, Any]:
    try:
        found = json.loads(store.setting(key) or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


# --- what's counted along the way -----------------------------------------------------------------------------------
def count_hidden_ask(store: LocalStore, now: datetime | None = None) -> None:
    """One more ask this week that named a column (or table) no applied profile grants."""
    this_week = week_of(now or datetime.now(UTC))
    last_week = (date.fromisoformat(this_week) - timedelta(days=7)).isoformat()
    kept = {w: n for w, n in _json_setting(store, HIDDEN_ASKS).items() if w >= last_week and isinstance(n, int)}
    store.set_setting(HIDDEN_ASKS, json.dumps({**kept, this_week: kept.get(this_week, 0) + 1}))


def remember_drift(store: LocalStore, connection: str, found: Drift | None) -> None:
    """The last drift check on `connection`, as counts only (the SQL stays with the tool's reply)."""
    kept = {k: v for k, v in _json_setting(store, LOGIN_DRIFT).items() if k != connection}
    if found is not None:
        kept = {**kept, connection: {"columns": found.columns, "tables": found.tables, "elsewhere": found.elsewhere}}
    store.set_setting(LOGIN_DRIFT, json.dumps(kept))


def remember_held_back(store: LocalStore, connection: str, count: int) -> None:
    """How many context items the last check on `connection` held back for naming a column that's gone (#123)."""
    kept = {k: v for k, v in _json_setting(store, HELD_BACK).items() if k != connection}
    store.set_setting(HELD_BACK, json.dumps({**kept, connection: count}))


# --- the report -----------------------------------------------------------------------------------------------------
def _this_weeks_reads(store: LocalStore, week: str, today: str) -> tuple[proof.AiRead, ...]:
    mine = store.reads.origin()
    return tuple(r for r in store.reads.between(week, today) if r.origin == mine)


def _asks(reads: tuple[proof.AiRead, ...]) -> dict[str, int]:
    asks: dict[str, int] = {}
    for read in reads:
        app = _name(read.app) or "other"
        asks = {**asks, app: asks.get(app, 0) + 1}
    return dict(sorted(asks.items(), key=lambda kv: -kv[1])[:MAX_APPS])


def _profiles(store: LocalStore, reads: tuple[proof.AiRead, ...]) -> list[dict[str, Any]]:
    applied: dict[str, set[str]] = {}
    for connection, profile_id in store.profiles.applications():
        applied = {**applied, profile_id: applied.get(profile_id, set()) | {_name(connection)}}
    found = []
    for profile in store.profiles.all()[:MAX_ITEMS]:
        grants = {level: sum(1 for g in profile.grants if g.level == level) for level in GRANT_LEVELS}
        apps = {_name(r.app) or "other" for r in reads if any(p.name == profile.name for p in r.profiles)}
        found.append({"name": _name(profile.name), "version": profile.version, "grants": grants,
                      "applied_to": sorted(n for n in applied.get(profile.id, set()) if n)[:MAX_ITEMS],
                      "apps": sorted(apps)[:MAX_APPS]})
    return [p for p in found if p["name"]]


def _connections(store: LocalStore, route: str) -> list[dict[str, Any]]:
    drift = _json_setting(store, LOGIN_DRIFT)
    held_back = {k: v for k, v in _json_setting(store, HELD_BACK).items() if isinstance(v, int)}
    hosted = set(hosted_connections(store))

    def on_route(name: str) -> str:
        return proof.HOSTED if route == proof.HOSTED or name in hosted else proof.DESKTOP

    found = [{"name": _name(c.name), "route": on_route(c.name), "drift": drift.get(c.name),
              "offered": list(offered_routes(c.kind, c.settings)),
              **({"held_back": held_back[c.name]} if c.name in held_back else {})}
             for c in store.connections()[:MAX_ITEMS]]
    return [c for c in found if c["name"]]


def facts(store: LocalStore, route: str = proof.DESKTOP, now: datetime | None = None) -> dict[str, Any]:
    """Exactly what the next report posts."""
    at = now or datetime.now(UTC)
    week, today = week_of(at), at.astimezone(UTC).date().isoformat()
    reads = _this_weeks_reads(store, week, today)
    hidden = _json_setting(store, HIDDEN_ASKS).get(week, 0)
    report: dict[str, Any] = {
        "source": proof.HOSTED if route == proof.HOSTED else "computer", "week": week,
        "rows": sum(r.row_count for r in reads), "hidden_asks": hidden if isinstance(hidden, int) else 0,
        "asks": _asks(reads), "profiles": _profiles(store, reads), "connections": _connections(store, route)}
    if route == proof.HOSTED:
        return report
    return {**report, "computer": store.reads.origin(), "version": _version()}


def _version() -> str:
    allowed = "".join(ch for ch in __version__ if ch.isalnum() or ch in ".+-")
    return allowed[:32] or "0"


# --- sending --------------------------------------------------------------------------------------------------------
class WeeklyFacts:
    """Server middleware: after a tool call, the week's facts go to the account at most once an hour, when signed in."""

    def __init__(self, store: LocalStore, keychain: Keychain, accounts: Accounts, route: str = proof.DESKTOP, *,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._store, self._keychain, self._accounts, self._route, self._now = store, keychain, accounts, route, now

    def _due(self) -> bool:
        last = self._store.setting(LAST_SENT)
        try:
            return last is None or self._now() - datetime.fromisoformat(last) >= SEND_INTERVAL
        except ValueError:
            return True

    def flush(self) -> None:
        """Post this week's facts if signed in and the last attempt was over SEND_INTERVAL ago."""
        if not self._due():
            return
        try:
            token = account_token(self._keychain)
        except KeychainError as err:
            logger.info("weekly facts not sent: %s", err)
            return
        if not token:
            return
        self._store.set_setting(LAST_SENT, self._now().isoformat())
        try:
            self._accounts.service.report_facts(token, facts(self._store, self._route, self._now()))
        except AccountServiceError as err:  # signed out, Personal ended, or not reached: next hour
            logger.info("weekly facts not sent this time: %s", err)

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method == TOOL_CALL:
            await anyio.to_thread.run_sync(self.flush)
        return result
