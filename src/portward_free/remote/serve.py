"""MCP over HTTP from this computer (#102): the same tools as stdio, for claude.ai and ChatGPT on the web.

`RemoteMcp` serves only what MCP needs: the MCP endpoint (MCP_PATH) behind Portward sign-in (signin.ConnectorCheck)
and the protected-resource metadata that tells a connector where to sign in. Nothing else answers (404): the
Portward page, password pages and recovery-key pages stay on their own 127.0.0.1 servers, and over HTTP a secret is
only ever asked for in a terminal on this computer (build_server's http_sign_in).

It starts only while this computer is signed in and Personal is on, and stops when Personal lapses: the account
service refusing a token with 402, or the plan seen ended at the next look (every `plan_seconds`).

Built to be reused: the direct relay (#103) runs it with a certificate on this computer (`certificate`) and its public
name as `public_url`; the hosted route (#104) runs it in a container (`host="0.0.0.0"`).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from portward_free import proof
from portward_free.account import Accounts, AccountServiceError, account_token, personal_plan
from portward_free.keychain import Keychain, KeychainError
from portward_free.remote.signin import ConnectorCheck, HttpSignIn, authorization_server
from portward_free.server import build_server
from portward_free.storage import LocalStore
from portward_free.usage import Usage

log = logging.getLogger("portward.remote")

MCP_PATH = "/mcp"
PLAN_SECONDS = 60.0
START_SECONDS = 10.0
ENDED = "Personal has ended, so Portward on the web stopped. Free keeps working in your desktop AI apps."
# Tools never served on the web (#90 story 42; sweep open problem 6). A web AI could be prompt-injected, and the
# user can't confirm anything on their computer from claude.ai, so these are left out instead of asking there:
# deleting (connections, shares), changing rules, profiles or context, turning sync or kept rows on or off, and
# signing the computer in or out or deleting the account. They stay in the user's desktop AI app and Portward on the
# computer. Setup that needs a secret (add_source, update_password) is served, because the secret is only ever typed
# in a terminal on the computer. remove_source is served but removes nothing here: it gives the AI the
# `portward-free remove-source <name>` command to hand the user (source_tools.remove_on_the_computer).
COMPUTER_ONLY = frozenset({
    "block", "unblock", "create_profile", "update_profile", "start_profile_from_block_rules",
    "apply_profile", "unapply_profile", "use_block_rules", "set_note", "draft_meanings", "draft_definition",
    "draft_link", "draft_metric", "promote_saved_pull", "discard_draft", "approve_context", "settle_context", "define_assumption",
    "import_dictionary",
    "keep_rows", "usage_counts", "set_usage_counts", "account_sign_in", "account_sign_out", "account_delete", "withdraw_share",
    "set_share_expiry", "sync_turn_on", "sync_turn_off", "sync_new_recovery_key", "set_send_mode",
    "personal_offer",  # #116: offers are for Free, and the web routes are Personal's
    "column_menu", "keep_from_claude", "column_note",  # #119: the panel's column menu changes rules and notes
    "level_preview",  # #120: the preview beside that offer
})


class RemoteRefused(Exception):
    """It can't serve (not signed in, Personal off, the account service unreachable); the message is safe to show.
    `free`: refused because this computer is on Free (signed out, or Personal off), where the CLI makes #108's
    offer; never when the plan couldn't be checked."""

    def __init__(self, message: str, *, free: bool = False) -> None:
        super().__init__(message)
        self.free = free


@dataclass(frozen=True)
class Certificate:
    """A TLS certificate and key held on this computer (#103: the relay passes TLS through, so it ends here)."""

    certfile: Path
    keyfile: Path


class RemoteMcp:
    def __init__(self, store: LocalStore, keychain: Keychain, accounts: Accounts, *, public_url: str,
                 host: str = "127.0.0.1", port: int = 8765, plan_seconds: float = PLAN_SECONDS,
                 certificate: Certificate | None = None, usage: Usage | None = None,
                 route: str = proof.DESKTOP, tools: frozenset[str] | None = None) -> None:
        parts = urlsplit(public_url.rstrip("/"))
        if parts.scheme not in ("https", "http") or not parts.netloc or parts.path or parts.query:
            raise ValueError("public_url must be scheme://host[:port], the address connectors are given")
        self.store, self.keychain, self.accounts = store, keychain, accounts
        self.public_url, self.host, self.port = f"{parts.scheme}://{parts.netloc}", host, port
        self.resource = self.public_url + MCP_PATH
        self.plan_seconds, self.certificate, self.usage = plan_seconds, certificate, usage
        self.route = route  # how the AI app reaches this server, for the AI log (#98): direct through the relay
        self.tools = tools  # the only tools served (#104: the hosted engine reads, never edits); None: all
        self.stopped_because: str | None = None
        self._stopped = threading.Event()
        self._server: uvicorn.Server | None = None
        self._check: ConnectorCheck | None = None

    # --- starting --------------------------------------------------------------------------------------------------
    def _device_token(self) -> str:
        try:
            token = account_token(self.keychain)
        except KeychainError as err:
            raise RemoteRefused(f"the account token couldn't be read from the keychain: {err}") from None
        if not token:
            raise RemoteRefused("this computer isn't signed in to Portward; run `portward-free sign-in` first",
                                free=True)
        return token

    def _ready(self) -> HttpSignIn:
        """Signed in, Personal on, and where connectors sign in: else RemoteRefused."""
        token = self._device_token()
        try:
            plan = personal_plan(self.keychain, self.accounts.service)
            if plan is None or not plan["personal"]:
                raise RemoteRefused("Portward on the web needs Personal, and this account doesn't have it now",
                                    free=True)
            where = authorization_server(self.accounts.service.mcp_sign_in(token))
        except (AccountServiceError, ValueError) as err:
            raise RemoteRefused(f"couldn't start: {err}") from None
        self._check = ConnectorCheck(self.keychain, self.accounts.service, self.resource)
        return HttpSignIn(where, self.resource, self._check)

    def _uvicorn(self) -> uvicorn.Server:
        server = build_server(self.store, self.keychain, usage=self.usage if self.usage is not None
                              else Usage.from_env(self.store), accounts=self.accounts, http_sign_in=self._ready(),
                              route=self.route)
        for tool in server._tool_manager.list_tools():
            if tool.name in COMPUTER_ONLY or (self.tools is not None and tool.name not in self.tools):
                server.remove_tool(tool.name)
        public = urlsplit(self.public_url).netloc
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public, f"127.0.0.1:{self.port}", f"localhost:{self.port}"],
            allowed_origins=[self.public_url])
        app = server.streamable_http_app(streamable_http_path=MCP_PATH, transport_security=security, host=self.host)
        tls = ({"ssl_certfile": str(self.certificate.certfile), "ssl_keyfile": str(self.certificate.keyfile)}
               if self.certificate else {})
        return uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port, log_level="warning",
                                             access_log=False, lifespan="on", **tls))  # type: ignore[arg-type]

    def start(self) -> None:
        """Serve in the background; returns once it listens. RemoteRefused when it can't."""
        self._server = self._uvicorn()
        threading.Thread(target=self._run, name="portward-remote", daemon=True).start()
        self._watch()
        while not self._server.started and not self._stopped.is_set():
            self._stopped.wait(0.02)
        if not self._server.started:
            raise RemoteRefused(f"couldn't listen on {self.host}:{self.port}")

    def serve_forever(self) -> None:
        """Serve on this thread until Ctrl-C, stop() or Personal lapses (the CLI's `serve-http`)."""
        self._server = self._uvicorn()
        self._watch()
        self._run()

    def _run(self) -> None:
        try:
            assert self._server is not None
            self._server.run()
        finally:
            self._stopped.set()

    # --- stopping --------------------------------------------------------------------------------------------------
    def _watch(self) -> None:
        threading.Thread(target=self._watch_plan, name="portward-remote-plan", daemon=True).start()

    def _watch_plan(self) -> None:
        assert self._check is not None
        while not self._stopped.is_set():
            if self._check.lapsed.wait(self.plan_seconds) or self._ended():
                self.stop(ENDED)
                return

    def _ended(self) -> bool:
        try:
            plan = personal_plan(self.keychain, self.accounts.service)
        except AccountServiceError as err:
            if err.status == 401:
                return True  # this computer was signed out (or its token ended): nobody's token can be checked
            log.info("couldn't check the plan just now (%s); still serving", err.status or "not reached")
            return False
        except KeychainError:
            return False
        return plan is None or not plan["personal"]

    def stop(self, reason: str | None = None) -> None:
        if self.stopped_because is None:
            self.stopped_because = reason or "stopped"
        if self._server is not None:
            self._server.should_exit = True

    def wait(self, timeout: float | None = None) -> bool:
        """True once it has stopped serving."""
        return self._stopped.wait(timeout)
