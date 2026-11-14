"""Anonymous usage counts: asked once at first run, on by default, off with one word.

What is counted, per ISO week the tool is used, and nothing else (the README publishes the same list):

- that this install was used that week (one report per week: installs, active weeks, retention come from these);
- the week this install first ran (its install week, so 4-week retention can be counted per install week);
- the number of pulls and of shares;
- the kinds of source pulled from (sqlite, csv, excel, postgres, mysql, or a wrapped Postgres MCP or Snowflake MCP
  server: wrap-postgres, wrap-snowflake, which is how wrap use is counted);
- the kinds of AI client (claude-desktop, claude-code, chatgpt, other);
- with Personal's profiles, how many profiles there are and how many grants at each level (full, anonymised,
  totals);
- the web routes that reached this computer (direct, hosted);
- team-link clicks, which the collection service counts when the "need this for your team?" link is opened (the link
  goes through it only while counts are on).

Never data, queries, questions, source or table names, paths, hosts, or identifiers. There is no install id: each
week's report carries a random `report` id, new every week, only so a resend of the same week replaces itself; two
weeks' reports can't be linked to each other or to a person, a machine or an address (the service keeps no IP).

Nothing is counted or sent until the user has seen the question (QUESTION) somewhere they see directly: the client's
own prompt (elicitation, on handshake connections), the results panel (which calls the app-only `usage_seen` once it
shows it), the local page, or a command they run at a terminal. A question in the AI's reply doesn't count: the AI
might not pass it on. Once seen, counts are on unless the user says no, and nothing is sent for an hour
(SEND_INTERVAL), so there is time to; after that, at most one post an hour, from a tool call. The AI can turn counts
off, never on before the user was asked. Off: nothing is counted, the counts kept so far are deleted, nothing is
sent. PORTWARD_USAGE=off or DO_NOT_TRACK=1 in the environment turns them off for good and the question is never
asked.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import anyio
from mcp.server.apps import client_supports_apps
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.elicitation import render_elicitation_schema
from pydantic import BaseModel, Field

from portward_free.storage import LocalStore, UsageWeek

logger = logging.getLogger(__name__)

USAGE_URL = "https://signup.portward.ai/v1/usage"
TEAM_URL = "https://portward.ai/teams"  # the "need this for your team?" page
TEAM_URL_COUNTED = "https://signup.portward.ai/v1/usage/team"  # counts the click, then redirects to TEAM_URL
SEND_INTERVAL = timedelta(hours=1)
SEND_TIMEOUT_SECONDS = 5
FIELDS = ("report", "week", "installed_week", "pulls", "shares", "source_types", "ai_clients", "profiles",
          "grants_full", "grants_anonymised", "grants_totals", "routes")
COUNTED = (
    "installs and active weeks (one report for each week Portward is used, with the week it was installed)",
    "number of pulls",
    "number of shares",
    "source type (sqlite, csv, excel, postgres, mysql or a wrapped Postgres MCP server)",
    "AI client (Claude Desktop, Claude Code, ChatGPT or other)",
    "number of profiles, and of their grants at each level (full, anonymised, totals)",
    "web route used (direct or hosted)",
    "team-link clicks",
)
NEVER = "Never your data, queries, questions, names, paths or any identifier."
SETTING = "usage_counts"  # "on" or "off"; absent until the first-run question
ASKED_AT, LAST_SENT, INSTALLED_WEEK = "usage_asked_at", "usage_last_attempt", "installed_week"
SEEN_IN, AI_TOLD = "usage_seen_in", "usage_ai_told"  # where the user saw the question; the AI was told once
NOT_ASKED = "not asked yet"
USAGE_META = "portward/usage_question"  # the panel's question, in a panel tool result's _meta (never the AI's)
TOOL_CALL = "tools/call"
OFF_VALUES = frozenset({"off", "0", "no", "false"})
QUESTION = (
    "Portward shares anonymous usage counts to learn whether it's useful. Counted: " + "; ".join(COUNTED) + ". "
    + NEVER + " They're on unless you turn them off; nothing is sent for an hour, and you can turn them off any "
    "time (on your Portward page, or ask your AI app)."
)
FIRST_RUN_NOTICE = (
    "First run of Portward. Portward asks the user itself, where they see it, whether to share anonymous usage "
    "counts; nothing is counted or sent until they have seen that. If they say they don't want them, call "
    "usage_counts with on=false. New here? get_started walks them through connecting a source and a first pull."
)


class KeepOn(BaseModel):
    keep_on: bool = Field(default=True, title="Keep anonymous usage counts on")


class Sender(Protocol):
    def send(self, payload: dict[str, Any]) -> bool:
        """Post one week's report; True when the service took it."""
        ...


class HttpSender:
    """Posts a report as JSON over HTTPS to the collection endpoint (the control app's POST /v1/usage)."""

    def __init__(self, url: str = USAGE_URL, timeout: float = SEND_TIMEOUT_SECONDS) -> None:
        self.url, self.timeout = url, timeout

    def send(self, payload: dict[str, Any]) -> bool:
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 (fixed https URL)
                return 200 <= response.status < 300
        except (OSError, ValueError) as err:
            logger.info("usage counts not sent this time: %s", err)
            return False


def iso_week(at: datetime) -> str:
    year, week, _ = at.isocalendar()
    return f"{year}-W{week:02d}"


def client_kind(name: str | None) -> str:
    """The AI client's kind from the name it gave when it connected (never the name itself)."""
    lowered = (name or "").lower()
    if "claude-code" in lowered or "claude code" in lowered:
        return "claude-code"
    if "claude" in lowered:
        return "claude-desktop"
    if any(word in lowered for word in ("chatgpt", "codex", "openai")):
        return "chatgpt"
    return "other"


def disabled_by(env: Mapping[str, str]) -> str | None:
    """Why the environment turns usage counts off, or None."""
    if env.get("PORTWARD_USAGE", "").strip().lower() in OFF_VALUES:
        return "PORTWARD_USAGE is set to off"
    if env.get("DO_NOT_TRACK", "").strip().lower() not in ("", *OFF_VALUES):
        return "DO_NOT_TRACK is set"
    return None


class Usage:
    """The counts kept in the local store, the first-run question, and the weekly report. Server middleware: every
    tool call passes through it."""

    def __init__(self, store: LocalStore, sender: Sender | None, *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 disabled: str | None = None, skip: frozenset[str] = frozenset(), route: str | None = None) -> None:
        """`disabled` says why counts are off for good (the environment); then `sender` is never used. `route`:
        the web route these calls come by ("direct", "hosted"; None on the desktop)."""
        self._store, self._sender, self._now, self.disabled, self.route = store, sender, now, disabled, route
        self.skip = skip  # app-only tools: their results go to the panel, so the question never goes there
        self.panel_tools: frozenset[str] = frozenset()  # tools whose result the results panel shows
        if store.setting(INSTALLED_WEEK) is None:
            store.set_setting(INSTALLED_WEEK, iso_week(now()))

    def on_store(self, store: LocalStore) -> Usage:
        """The same counts through another connection to the same file (the local page's, on its own thread)."""
        return Usage(store, self._sender, now=self._now, disabled=self.disabled, skip=self.skip, route=self.route)

    def routed(self, route: str) -> Usage:
        """The same counts, for calls that come by a web route (#103 direct, #104 hosted)."""
        return Usage(self._store, self._sender, now=self._now, disabled=self.disabled, skip=self.skip, route=route)

    def factory(self) -> Callable[[LocalStore], Usage]:
        """Makes these counts on another connection to the file (the terminal commands', the local page's)."""
        return self.on_store

    @classmethod
    def from_env(cls, store: LocalStore, env: Mapping[str, str] = os.environ) -> Usage:
        reason = disabled_by(env)
        return cls(store, None if reason else HttpSender(), disabled=reason)

    # --- state ------------------------------------------------------------------------------------------------------
    @property
    def state(self) -> str:
        """'on', 'off', 'not asked yet' or 'disabled'."""
        if self.disabled or self._sender is None:
            return "disabled"
        return self._store.setting(SETTING) or NOT_ASKED

    @property
    def on(self) -> bool:
        return self.state == "on"

    def set(self, on: bool, *, by_user: bool = False) -> None:
        """Turn counts on or off. Only the user (`by_user`: the page, the panel) can turn them on before they were
        asked; the AI can always turn them off."""
        if self.state == "disabled":
            raise ValueError(f"usage counts are off for good on this computer: {self.disabled}")
        if on and not by_user and self.state == NOT_ASKED:
            raise ValueError("Portward asks the user itself, where they see it, before any usage counts are kept; "
                             "it hasn't asked yet, so they stay off for now")
        self._store.set_setting(SETTING, "on" if on else "off")
        if self._store.setting(ASKED_AT) is None:
            self._store.set_setting(ASKED_AT, self._now().isoformat())
        if not on:
            self._store.clear_usage()

    def seen(self, where: str) -> bool:
        """The user has just seen QUESTION `where` ("prompt", "panel", "page", "terminal"): counts are on from now
        unless they say no, and the hour to say no starts. False if they had been asked already."""
        if self.state != NOT_ASKED:
            return False
        at = self._now().isoformat()
        for key, value in ((SETTING, "on"), (ASKED_AT, at), (LAST_SENT, at), (SEEN_IN, where)):
            self._store.set_setting(key, value)
        return True

    def team_link(self) -> str:
        """The team page; through the counting redirect only while counts are on."""
        return TEAM_URL_COUNTED if self.on else TEAM_URL

    def status(self) -> dict[str, Any]:
        return {"usage_counts": self.state, "counted": list(COUNTED), "never": NEVER, "fields": list(FIELDS),
                "next_reports": self.reports() if self.on else [],
                **({"why_off": self.disabled} if self.disabled else {})}

    # --- counting ---------------------------------------------------------------------------------------------------
    def _count(self, **counts: Any) -> None:
        if self.on:
            week = iso_week(self._now())
            self._store.count_usage(week, secrets.token_hex(16), **counts)

    def pulled(self, source_type: str) -> None:
        self._count(pulls=1, source_type=source_type)

    def shared(self) -> None:
        self._count(shares=1)

    # --- reports ----------------------------------------------------------------------------------------------------
    def _payload(self, week: UsageWeek) -> dict[str, Any]:
        return {"report": week.report, "week": week.week,
                "installed_week": self._store.setting(INSTALLED_WEEK) or week.week, "pulls": week.pulls,
                "shares": week.shares, "source_types": list(week.source_types), "ai_clients": list(week.ai_clients),
                **self._profile_counts(), "routes": list(week.routes)}

    def _profile_counts(self) -> dict[str, int]:
        """Counts only, as they stand when the report is made: never a profile's name or what it grants."""
        found = self._store.profiles.counts()
        return {"profiles": found["profiles"], "grants_full": found["grants"].get("full", 0),
                "grants_anonymised": found["grants"].get("anonymised", 0),
                "grants_totals": found["grants"].get("totals", 0)}

    def reports(self) -> list[dict[str, Any]]:
        """Exactly what the next send would post: one report per week with counts not yet sent."""
        return [self._payload(w) for w in self._store.usage_weeks()]

    def _due(self) -> bool:
        last = self._store.setting(LAST_SENT)
        return self.on and (last is None or self._now() - datetime.fromisoformat(last) >= SEND_INTERVAL)

    def flush(self) -> None:
        """Post every week's report, if counts are on and the last attempt was over SEND_INTERVAL ago. A finished
        week's counts are deleted once the service has them; the current week's report is posted again later, with
        the same `report` id, and replaces the earlier one."""
        if not self._due():
            return
        assert self._sender is not None  # on implies a sender
        self._store.set_setting(LAST_SENT, self._now().isoformat())
        this_week = iso_week(self._now())
        for week in self._store.usage_weeks():
            if self._sender.send(self._payload(week)) and week.week < this_week:
                self._store.drop_usage_week(week.week)

    # --- middleware -------------------------------------------------------------------------------------------------
    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method != TOOL_CALL or self.state == "disabled":
            return await call_next(ctx)
        result = await call_next(ctx)
        tool = (ctx.params or {}).get("name")
        if self.state == NOT_ASKED and tool not in self.skip and isinstance(result, dict) \
                and isinstance(result.get("content"), list):
            result = await self._ask(ctx, tool, result)
        self._count(ai_client=client_kind(_client_name(ctx.session)), route=self.route)
        await anyio.to_thread.run_sync(self.flush)
        return result

    async def _ask(self, ctx: ServerRequestContext[Any, Any], tool: str | None, result: dict[str, Any]
                   ) -> dict[str, Any]:
        """Ask in the client's prompt if it can; else hand the panel the question (it says when it showed it), and
        tell the AI once that Portward asks the user itself."""
        if await self._asked_in_prompt(ctx):
            return result
        if tool in self.panel_tools and client_supports_apps(ctx):
            result = {**result, "_meta": {**(result.get("_meta") or {}), USAGE_META: {"text": QUESTION}}}
        if self._store.setting(AI_TOLD) is None:
            self._store.set_setting(AI_TOLD, self._now().isoformat())
            result = {**result, "content": [*result["content"], {"type": "text", "text": FIRST_RUN_NOTICE}]}
        return result

    async def _asked_in_prompt(self, ctx: ServerRequestContext[Any, Any]) -> bool:
        """The question in the client's own prompt, on connections where the server can send it a request (the
        2026-07-28 protocol can't from here: the panel, the page or the terminal asks there). Declining or
        answering no turns counts off; dismissing it leaves them on, as the question says."""
        session = ctx.session
        elicitation = getattr(getattr(session, "client_capabilities", None), "elicitation", None)
        # an empty elicitation capability means form mode (the spec's default); url-only clients can't show a form
        can_form = elicitation is not None and (elicitation.form is not None or elicitation.url is None)
        if not (can_form and getattr(session, "can_send_request", False)):
            return False
        try:
            answer = await session.elicit_form(QUESTION, render_elicitation_schema(KeepOn),
                                               related_request_id=ctx.request_id)
        except Exception as err:  # noqa: BLE001 - a client that fails to ask: fall back to the other channels
            logger.info("couldn't ask about usage counts in the client: %s", err)
            return False
        self.seen("prompt")
        keep = answer.action != "decline" and (answer.content or {}).get("keep_on", True) is not False
        if not keep:
            self.set(False)
        return True


def _client_name(session: Any) -> str | None:
    params = getattr(session, "client_params", None)
    info = getattr(params, "client_info", None)
    return getattr(info, "name", None)
