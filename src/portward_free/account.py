"""Signing in to a Personal account (#53) from this computer: the loopback flow for native apps (RFC 8252) with PKCE.

The tool listens on 127.0.0.1 (localweb.LocalWeb: a port the OS picks, a secret path, its own Host only) and opens
the account page in the user's browser with that address, a random state and the S256 challenge of a verifier only
this process holds. The user signs in there (an emailed code, Google or GitHub) and approves; the account page sends
the browser back to the loopback address with a one-time code; the tool checks the state and trades the code and the
verifier for a device token. Someone who tricks a user into approving gets nothing: the code lands on the user's own
computer and is useless without the verifier.

The device token is kept in the OS keychain only (TOKEN_ACCOUNT); the account's email and id are a setting
(ACCOUNT_SETTING). The AI is told whether the user is signed in and as whom, never the token or the addresses.
#55 sync reads the token with `account_token` and calls the control app with it as a Bearer token; it and #56 check
the plan (trial, active, grace or ended; #54) with `personal_plan`."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import quote, urlencode

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from portward_free.http_json import JsonService, ServiceError
from portward_free.keychain import Keychain, KeychainError
from portward_free.localweb import LocalWeb, Request, Response
from portward_free.replies import error, reply
from portward_free.storage import LocalStore

log = logging.getLogger("portward_free.account")

DEFAULT_ACCOUNT_URL = "https://signup.portward.ai"
ACCOUNT_URL_ENV = "PORTWARD_ACCOUNT_URL"
TOKEN_ACCOUNT = "account-token"
ACCOUNT_SETTING = "account"
SHARE_TOKEN_ACCOUNT = "share-token"  # sharing.tools.TOKEN_ACCOUNT: this computer shares (counted in share_devices)
SYNC_SETTING = "sync"  # sync.tools.SYNC_SETTING
SIGN_IN_COMMAND = "uvx portward-free sign-in"
SIGN_IN_SECONDS = 10 * 60  # the account page's request lasts this long; the loopback address closes then


class AccountServiceError(ServiceError):
    """The account service refused or couldn't be reached; the message is safe to show. `status` 0: not reached."""


def account_token(keychain: Keychain) -> str | None:
    """This computer's device token for the user's Personal account, or None when it isn't signed in."""
    return keychain.get(TOKEN_ACCOUNT)


PLAN_STATES = ("trial", "active", "grace", "ended")


def register_signing_key(keychain: Keychain, service: AccountService, token: str) -> str:
    """Register this computer's report signing key (#135; made on first use) to the account; returns its key id.
    Raises AccountServiceError and KeychainError."""
    from portward_free.signed_report import key_id, public_raw, public_key_text, signing_key

    key = signing_key(keychain)
    service.register_signing_key(token, public_key_text(key))
    return key_id(public_raw(key))


def plan_of(me: dict[str, Any]) -> dict[str, Any] | None:
    """The account's plan from /v1/me (#54), in a fixed shape: {state: trial|active|grace|ended, personal, until,
    interval, payment_failed}. The account service works it out from Stripe; nothing here can change it. None if
    the service sent none (an older service)."""
    plan = me.get("plan")
    if not isinstance(plan, dict) or plan.get("state") not in PLAN_STATES:
        return None
    until = plan.get("until")
    return {"state": plan["state"], "personal": plan["state"] != "ended",
            "until": int(until) if isinstance(until, int) and not isinstance(until, bool) else None,
            "interval": plan.get("interval") if plan.get("interval") in ("month", "year") else None,
            "payment_failed": plan.get("payment_failed") is True}


PLAN_SETTING = "plan"  # the plan as the account service last gave it (#108)


def remember_plan(store: LocalStore, plan: dict[str, Any] | None) -> None:
    """Keep the plan the account service just gave (None: signed out), so a lapse can be told without the network.
    It only ever explains a refusal; nothing is allowed because of it."""
    store.set_setting(PLAN_SETTING, json.dumps(plan) if plan is not None else "")


def known_plan(store: LocalStore) -> dict[str, Any] | None:
    """The plan last seen (remember_plan), or None when none was."""
    stored = store.setting(PLAN_SETTING)
    try:
        plan = json.loads(stored) if stored else None
    except ValueError:
        return None
    return plan if isinstance(plan, dict) and plan.get("state") in PLAN_STATES else None


def personal_plan(keychain: Keychain, service: AccountService) -> dict[str, Any] | None:
    """This computer's account's plan, asked of the account service now (plan_of), or None when it isn't signed in.
    For sync (#55) and share management (#56): Personal features are on while plan["personal"] is true. Raises
    AccountServiceError (status 401: the token is no longer known) and KeychainError."""
    token = account_token(keychain)
    if not token:
        return None
    return plan_of(service.me(token))


def plan_words(plan: dict[str, Any]) -> str:
    """One sentence about the plan for the AI to pass on."""
    day = (datetime.fromtimestamp(plan["until"], UTC).strftime("%-d %b %Y") if plan.get("until") else "")
    if plan["state"] == "trial":
        return f"Personal (the 14-day trial) until {day}."
    if plan["state"] == "active":
        failed = " The last payment failed: update the card on the account page." if plan["payment_failed"] else ""
        return f"Personal, paid {'monthly' if plan['interval'] == 'month' else 'yearly'}, renews {day}.{failed}"
    if plan["state"] == "grace":
        return f"Personal is cancelled and stays on until {day}."
    return "Personal has ended; Free keeps working."  # no pitch: offers are only made in offers.py (#57)


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class AccountService(JsonService):
    """The control app's /me and /v1/me routes, over HTTP (this package never imports that code)."""

    WHAT = "account service"
    FAILURE = AccountServiceError

    @classmethod
    def from_env(cls) -> AccountService:
        return cls(os.environ.get(ACCOUNT_URL_ENV, DEFAULT_ACCOUNT_URL))

    def link_url(self, redirect_uri: str, state: str, challenge: str, name: str) -> str:
        return f"{self.base_url}/me/link?" + urlencode({"redirect_uri": redirect_uri, "state": state,
                                                        "code_challenge": challenge,
                                                        "code_challenge_method": "S256", "name": name})

    def trade(self, code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
        """{token, account_id, email}: this computer's device token, given once."""
        return self._call("POST", "/v1/me/token", {"code": code, "code_verifier": verifier,
                                                   "redirect_uri": redirect_uri})

    def me(self, token: str) -> dict[str, Any]:
        return self._call("GET", "/v1/me", token=token)

    def sign_out(self, token: str) -> None:
        self._call("POST", "/v1/me/signout", {}, token=token)

    # --- report signing keys (#135): public halves only ------------------------------------------------------------
    def register_signing_key(self, token: str, public_key: str) -> dict[str, Any]:
        """{key_id, registered_at}: registers this computer's public key to the account (again: no change)."""
        return self._call("POST", "/v1/me/signing-keys", {"public_key": public_key}, token=token)

    def signing_keys(self, token: str) -> dict[str, Any]:
        """{keys: [{key_id, public_key, label, registered_at}]}: every key registered to the account."""
        return self._call("GET", "/v1/me/signing-keys", token=token)

    # --- MCP over HTTP (#102): a connector's sign-in, checked for this computer ----------------------------------
    def mcp_sign_in(self, token: str) -> dict[str, Any]:
        """{authorization_server}: where claude.ai and ChatGPT sign the user in for this computer's MCP."""
        return self._call("GET", "/v1/me/mcp", token=token)

    def mcp_check(self, token: str, access_token: str, resource: str) -> dict[str, Any]:
        """{account_id, email} when `access_token` is the account owner's, for `resource`. Raises
        AccountServiceError: 401 not a good token, 403 someone else's, 402 Personal is off."""
        return self._call("POST", "/v1/me/mcp/check", {"token": access_token, "resource": resource}, token=token)

    # --- the direct route (#103) --------------------------------------------------------------------------------
    def relay_access(self, token: str) -> dict[str, Any]:
        """{name, address, relay_host, relay_domain, key}: this account's name under the personal relay domain and a
        new relay key for this computer. 402 Personal is off, 503 no direct route on this service yet."""
        return self._call("POST", "/v1/me/relay", {}, token=token)

    # --- weekly facts for the account page (#105) ----------------------------------------------------------------
    def report_facts(self, token: str, facts: dict[str, Any]) -> None:
        """Posts weekly_facts.facts(): 400 anything beyond the listed facts, 402 Personal is off."""
        self._call("POST", "/v1/me/facts", facts, token=token)

    # --- route switches asked for on the account page (#106) ----------------------------------------------------
    def wanted_routes(self, token: str) -> dict[str, Any]:
        """{routes: {connection name: "hosted" | "direct"}}: switches waiting for a computer to make them."""
        return self._call("GET", "/v1/me/routes", token=token)

    def settle_routes(self, token: str, done: list[str], refused: list[str]) -> None:
        """Which waiting switches this computer made, and which it couldn't."""
        self._call("POST", "/v1/me/routes/done", {"done": done, "refused": refused}, token=token)

    # --- managing shares in Personal (#56); 402 when Personal is off ---------------------------------------------
    def shares(self, token: str) -> dict[str, Any]:
        """{shares: [{id, recipient, state, created_at, expires_at, revoked_at, opens}], expiry_days}"""
        return self._call("GET", "/v1/me/shares", token=token)

    def withdraw_share(self, token: str, share_id: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/me/shares/{quote(share_id, safe='')}/revoke", {}, token=token)

    def set_share_expiry(self, token: str, share_id: str, days: int) -> dict[str, Any]:
        return self._call("POST", f"/v1/me/shares/{quote(share_id, safe='')}/expiry", {"days": days}, token=token)


@dataclass
class Accounts:
    """Where accounts live (build_server's `accounts`; by default PORTWARD_ACCOUNT_URL)."""
    service: AccountService
    label: Callable[[], str] = field(default=lambda: (platform.node() or "your computer")[:80])

    @classmethod
    def from_env(cls) -> Accounts:
        return cls(AccountService.from_env())


def _page(title: str, text: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{escape(title)} · Portward</title></head>'
            f"<body><main><h1>{escape(title)}</h1><p>{escape(text)}</p></main></body></html>")


class SignIn:
    """One sign-in in progress: the loopback address it listens on, its state and PKCE verifier."""

    def __init__(self, accounts: Accounts, keychain: Keychain, store: LocalStore) -> None:
        self._accounts, self._keychain, self._store = accounts, keychain, store
        self._verifier, self._state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        self._web = LocalWeb(self)
        self.finished = threading.Event()
        self.email: str | None = None

    def start(self) -> str:
        """Listen on 127.0.0.1 and give the account page's address to open in the user's browser."""
        self._web.start()
        closer = threading.Timer(SIGN_IN_SECONDS, self.stop)
        closer.daemon = True
        closer.start()
        return self._accounts.service.link_url(self._redirect_uri, self._state, _s256(self._verifier),
                                               self._accounts.label())

    @property
    def _redirect_uri(self) -> str:
        return self._web.url("/done")

    def stop(self) -> None:
        self.finished.set()
        self._web.stop()

    def _finish(self) -> None:
        self.finished.set()
        closer = threading.Timer(1.0, self._web.stop)  # after this answer has gone back to the browser
        closer.daemon = True
        closer.start()

    def __call__(self, request: Request, web: LocalWeb) -> Response:
        if request.method != "GET" or request.path != "/done":
            return Response.text(404, "Not found.")
        if self.finished.is_set():
            return Response(410, _page("Already done", "Close this tab and go back to your AI app."))
        if not hmac.compare_digest(request.query.get("state", "").encode(), self._state.encode()):
            return Response(400, _page("Not signed in", "This answer isn't for this sign-in. Start again from "
                                                        "Portward."))
        code = request.query.get("code", "")
        if not code:
            self._finish()
            return Response(400, _page("Not signed in", "The account page didn't link this computer. Ask your AI "
                                                        "to sign in to your Portward account again."))
        try:
            got = self._accounts.service.trade(code, self._verifier, self._redirect_uri)
            self._keychain.set(TOKEN_ACCOUNT, str(got["token"]))
        except (AccountServiceError, KeychainError, KeyError) as err:
            self._finish()
            return Response(502, _page("Not signed in", f"Couldn't finish signing in: {err}. Try again."))
        self.email = str(got.get("email", ""))
        try:  # #135: the account knows this computer's report signing key from now on
            register_signing_key(self._keychain, self._accounts.service, str(got["token"]))
        except (AccountServiceError, KeychainError) as err:
            log.warning("the report signing key wasn't registered at sign-in (%s); exporting a report tries again",
                        type(err).__name__)
        self._store.set_setting(ACCOUNT_SETTING, json.dumps({"email": self.email,
                                                             "account_id": str(got.get("account_id", ""))}))
        self._finish()
        return Response(200, _page("Signed in", f"Portward on this computer is signed in as {self.email}. You can "
                                                "close this tab."))


@dataclass(frozen=True)
class Me:
    """The account service's /v1/me, parsed once: the account's email, how many computers share from it (#57) and
    its plan (plan_of; None from an older service)."""

    email: str
    share_devices: int
    plan: dict[str, Any] | None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Me:
        devices = raw.get("share_devices")
        return cls(str(raw.get("email", "")),
                   devices if isinstance(devices, int) and not isinstance(devices, bool) else 0, plan_of(raw))


@dataclass
class AccountTools:
    """What the account tools share: the store, the keychain, the account service, the browser, and the sign-in in
    progress (if any)."""

    store: LocalStore
    keychain: Keychain
    accounts: Accounts
    browser: Callable[[str], bool]
    current: list[SignIn] = field(default_factory=list)

    def identity(self) -> dict[str, str]:
        stored = self.store.setting(ACCOUNT_SETTING)
        return json.loads(stored) if stored else {}

    def forget(self) -> None:
        self.keychain.delete(TOKEN_ACCOUNT)
        self.store.set_setting(ACCOUNT_SETTING, "")

    def sign_in(self) -> CallToolResult:
        while self.current:
            self.current.pop().stop()
        started = SignIn(self.accounts, self.keychain, self.store)
        url = started.start()
        if not self.browser(url):
            started.stop()
            return error(f"No web browser could be opened from here. Ask the user to run `{SIGN_IN_COMMAND}` in a "
                         "terminal on this computer.")
        self.current.append(started)
        return reply({"opened": True}, "The Portward sign-in page is open in the user's browser. When they've "
                                       "signed in and approved this computer, call account_status.")

    def status(self) -> CallToolResult:
        try:
            token = account_token(self.keychain)
        except KeychainError as err:
            return error(f"The account token couldn't be read from the keychain: {err}.")
        if not token:
            waiting = bool(self.current) and not self.current[-1].finished.is_set()
            return reply({"signed_in": False}, "Not signed in." + (
                " The sign-in page is waiting for the user in their browser." if waiting else ""))
        try:
            me = Me.parse(self.accounts.service.me(token))
        except AccountServiceError as err:
            return self._status_unknown(err)
        answer: dict[str, Any] = {"signed_in": True, "email": me.email}
        if me.plan is None:
            return reply(answer, f"Signed in to Portward as {me.email}.")
        return reply({**answer, "plan": me.plan}, f"Signed in to Portward as {me.email}. {plan_words(me.plan)} "
                                                  f"Account page: {self.accounts.service.base_url}/me")

    def _status_unknown(self, err: AccountServiceError) -> CallToolResult:
        if err.status == 401:
            self.forget()
            return reply({"signed_in": False}, "Not signed in: the account no longer knows this computer. "
                                               "Call account_sign_in to sign in again.")
        email = self.identity().get("email", "")
        return reply({"signed_in": True, "email": email}, f"Signed in as {email} (the account service "
                                                          f"couldn't be reached just now: {err}).")

    def delete_page(self) -> CallToolResult:
        url = f"{self.accounts.service.base_url}/me/delete"
        if not self.browser(url):
            return error(f"No web browser could be opened from here. Ask the user to open {url} in their browser.")
        return reply({"opened": True}, (
            "The page to download account data or delete the account is open in the user's browser. Nothing is "
            "deleted until they sign in again there and confirm. Deleting removes everything Portward's server holds "
            "for the account (synced history, shares, the email); the history on this computer stays."))

    def sign_out(self) -> CallToolResult:
        try:
            token = account_token(self.keychain)
        except KeychainError as err:
            return error(f"The account token couldn't be read from the keychain: {err}.")
        if not token:
            return reply({"signed_in": False}, "This computer wasn't signed in.")
        note = ""
        try:
            self.accounts.service.sign_out(token)
        except AccountServiceError as err:
            if err.status != 401:
                note = f" The account service couldn't be told ({err}); the user can end it on their account page."
        try:
            self.forget()
        except KeychainError as err:
            return error(f"Couldn't remove the token from the keychain: {err}.")
        return reply({"signed_in": False}, "Signed out on this computer." + note)


def register_account_tools(server: MCPServer, store: LocalStore, keychain: Keychain, accounts: Accounts,
                           browser: Callable[[str], bool]) -> None:
    tools = AccountTools(store, keychain, accounts, browser)

    @server.tool(description=(
        "Sign this computer in to the user's Portward Personal account: opens the account page in their browser, "
        "where they sign in (emailed code, Google or GitHub) and approve. Then call account_status. Only call it "
        "when the user asks to sign in or to use their account."))
    def account_sign_in() -> CallToolResult:
        return tools.sign_in()

    @server.tool(description="Whether this computer is signed in to the user's Portward account, and as whom.")
    def account_status() -> CallToolResult:
        return tools.status()

    @server.tool(description=(
        "Open the page where the user can download their Portward account data or delete their account, in their "
        "browser. It deletes nothing: the user signs in again there and confirms. Only call it when the user asks "
        "to download their account data, delete their account or leave Personal. History on this computer stays."))
    def account_delete() -> CallToolResult:
        return tools.delete_page()

    @server.tool(description="Sign this computer out of the user's Portward account (the token is ended on the "
                             "server and removed from the keychain).")
    def account_sign_out() -> CallToolResult:
        return tools.sign_out()
