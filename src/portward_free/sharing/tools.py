"""The share tools (#50): confirm the sender's email once, share a pull, list what was shared.

`share` re-runs the pull through run_pull under the source's current block rules (never rows from history or
memory), seals the snapshot here and uploads only the ciphertext. The link carries the key in its # fragment, so the
AI is told who the share is for, what is in it and when it expires, never the link. With a panel the link goes to the
share panel (`links`: held sealed for the panel, read with the app-only share_link), where the user copies it or
opens it in their mail app (share_mail); nothing opens by default. Without a panel (or when the link can't be sealed
for one) a draft to the teammate with the link opens in the user's own mail app, as before. The user sending the
link is the human step: nothing can be opened until the teammate has both the link and the code the service emails
them.

The share token (device-bound: one per machine, made when the email is confirmed) is kept in the OS keychain under
TOKEN_ACCOUNT. The sender's email and name, and when the token expires on the service, are a setting
(SENDER_SETTING); they aren't secret. A share made within ROTATE_BEFORE of that expiry first swaps the token for a new
one (the service ends the old one), so a device that shares now and then never has to confirm again until the
service's yearly email check.

Limits (#51): a snapshot that seals to more than MAX_SEALED_BYTES is refused here, before anything is uploaded; the
service's own limits (shares per sender and per recipient) come back as its message, which says when to try again.

The code the service emails to confirm the sender never passes through the chat (#62): `share_sign_in` asks the
user for it out of band, like a database password (`secret_entry`: the panel, a one-time local page, or the
`portward-free share-code` terminal command). The confirmation in progress is a setting (PENDING_SETTING) so the
terminal command, a separate process, can finish it; `confirm_code` does, from any of the three.
"""

from __future__ import annotations

import json
import secrets
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free import passwords
from portward_free.account import Accounts
from portward_free.asks import Ask, Refused
from portward_free.keychain import Keychain, KeychainError
from portward_free.pull import Pull, markdown_table
from portward_free.replies import error, reply
from portward_free.clients import client_support
from portward_free.sharing.links import SHARE_PANEL_URI, HeldLink, ShareLinks, draft
from portward_free.sharing.options import ANONYMISE, HIDE, MIN_GROUP_ROWS, TOTALS, ShareOptionError, chosen, with_options
from portward_free.sharing.service import Created, ShareService, ShareServiceError
from portward_free.secret_entry import OutOfBand, Texts
from portward_free.sharing.snapshot import (MAX_SEALED_BYTES, allows_download, seal_snapshot, sealed_size,
                                            snapshot_of)
from portward_free.storage import LocalStore, PullRecord, ShareRecord

TOKEN_ACCOUNT = "share-token"
SENDER_SETTING = "share_sender"
PENDING_SETTING = "share_pending"  # {verification, email, name, device} while a confirmation is in progress
CODE_ASK = "share-code"
SHARE_DAYS = 7
PERSONAL_SHARE_DAYS = 90  # the longest a share lasts with Portward Personal (#56); the service decides who has it
MAX_SHARES_LISTED = 100
DOWNLOAD_ALLOWED, VIEW_ONLY = "Download: allowed", "View only"  # as the share panel says them
ROTATE_BEFORE = timedelta(days=30)  # swap the share token once its expiry on the service is this close
SIGN_IN_AGAIN = ("The share service no longer knows this device: call share_sign_in to confirm the user's email "
                 "again.")
TOO_BIG = (f"This table is too big to share: it is {{size}} MB encrypted and the most is "
           f"{MAX_SEALED_BYTES // (1024 * 1024)} MB. Share fewer rows or columns: tweak the pull (pick the columns "
           "needed, filter, or add a LIMIT) and share that.")

Run = Callable[[str, str, str], "Pull | CallToolResult"]  # build_server's _run: source, question, sql


class Drafts(Protocol):
    def open(self, to: str, subject: str, body: str) -> bool:
        """Open a new email in the user's mail app; False if it couldn't be opened."""


class MailApp:
    """The user's default mail app, through a mailto: address."""

    def open(self, to: str, subject: str, body: str) -> bool:
        address = f"mailto:{quote(to, safe='@')}?subject={quote(subject)}&body={quote(body)}"
        try:
            return webbrowser.open(address)
        except webbrowser.Error:
            return False


@dataclass
class Sharing:
    """Where shares go (the service) and how the link reaches the teammate (the user's mail app)."""
    service: ShareService
    drafts: Drafts = field(default_factory=MailApp)
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @classmethod
    def from_env(cls) -> Sharing:
        return cls(ShareService.from_env())


@dataclass(frozen=True)
class Confirmed:
    email: str
    name: str

    def summary(self) -> str:
        return f"Confirmed: shares from this device come from {self.name} <{self.email}>."

    def payload(self) -> dict[str, Any]:
        return {"email": self.email, "name": self.name}


def pending(store: LocalStore) -> dict[str, str] | None:
    stored = store.setting(PENDING_SETTING)
    return json.loads(stored) if stored else None


def keep_issued(store: LocalStore, keychain: Keychain, issued: dict[str, Any]) -> dict[str, str]:
    """Keep a token the service issued (confirm or rotate) in the keychain and the identity that goes with it, with
    when the token expires (SENDER_SETTING). Raises KeychainError."""
    keychain.set(TOKEN_ACCOUNT, str(issued["token"]))
    identity = {"email": str(issued["email"]), "name": str(issued["name"]),
                "token_expires_at": str(issued.get("expires_at", ""))}
    store.set_setting(SENDER_SETTING, json.dumps(identity))
    return identity


def confirm_code(store_path: Path, keychain: Keychain, service: ShareService, code: str) -> Confirmed:
    """Finish the confirmation in progress with the code the user typed; raises Refused (never with the code).
    Opens its own connection to the local file: a local page answers on its own thread."""
    store = LocalStore.open(store_path)
    try:
        started = pending(store)
        if started is None:
            raise Refused("no email confirmation is in progress; ask the AI to start sharing again")
        code = code.strip()
        if not code:
            raise Refused("type the code")
        try:
            confirmed = service.confirm_sender(started["verification"], code, started.get("device", ""))
        except ShareServiceError as err:
            left = err.body.get("attempts_left")
            more = f" ({left} tries left)" if left else "; ask the AI for a new code" if left == 0 else ""
            raise Refused(f"Not confirmed: {str(err).replace(code, '(code)')}{more}") from None
        try:
            kept = keep_issued(store, keychain, confirmed)
        except KeychainError as err:
            raise Refused(f"confirmed, but the share token couldn't be kept: {err}") from None
        store.set_setting(PENDING_SETTING, "")
        return Confirmed(kept["email"], kept["name"])
    finally:
        store.close()


def code_command() -> str:
    return passwords.command("share-code")


@dataclass(frozen=True)
class Sealed:
    """A pull made ready to share: what the teammate sees, its key (only ever in the link) and the ciphertext."""

    shown: dict[str, Any]
    key: str
    ciphertext: str
    pulled_at: str
    options: dict[str, list[str]]  # the columns hidden, anonymised and totalled (the user's options, no values)
    allow_download: bool  # sealed in the snapshot: the teammate may download it as a CSV


@dataclass
class ShareTools:
    """What the share tools share. `on_shared` runs once for each share the service took (the usage counts'
    `shares`, #49). The sender's code is asked for through `out_of_band`, never as a tool argument. `accounts` is the
    user's Personal account service."""

    store: LocalStore
    keychain: Keychain
    run: Run
    sharing: Sharing
    out_of_band: OutOfBand
    accounts: Accounts
    on_shared: Callable[[], None]
    links: ShareLinks

    # --- confirming the sender ----------------------------------------------------------------------------------------
    def sender(self) -> dict[str, str] | None:
        stored = self.store.setting(SENDER_SETTING)
        return json.loads(stored) if stored else None

    def code_ask(self, params: Mapping[str, Any]) -> Ask:
        """The out-of-band ask for the emailed code; also rebuilds one the other server process opened."""
        return Ask(key=CODE_ASK, title="Sharing code", label="Code", details=(("Email", str(params["email"])),),
                   done="I've entered the sharing code.",
                   save=lambda code: confirm_code(self.store.path, self.keychain, self.sharing.service, code),
                   kind=CODE_ASK, params={"email": str(params["email"])})

    async def ask_for_code(self, ctx: Context, started: dict[str, str]) -> CallToolResult | InputRequiredResult:
        email = started["email"]
        ask = self.code_ask({"email": email})
        texts = Texts(what=f"the 6-digit code Portward emailed to {email}", then="call share",
                      command=code_command(), does="confirms their email and keeps the share token in the keychain",
                      lead=f"Portward emailed a 6-digit code to {email}.")
        return await self.out_of_band.ask(ctx, ask, {"code_sent_to": email}, texts, "code_entry")

    async def sign_in(self, ctx: Context, email: str, name: str) -> CallToolResult | InputRequiredResult:
        started = pending(self.store)
        email = email.strip().lower()
        if not (self.out_of_band.is_retry(ctx) and started is not None and started["email"] == email):
            device = secrets.token_urlsafe(32)  # stays on this computer: the emailed code confirms only with it
            try:
                verification = self.sharing.service.start_sender(email, name, device)
            except ShareServiceError as err:
                return error(f"Couldn't start: {err}.")
            self.out_of_band.close(CODE_ASK)
            started = {"verification": verification, "email": email, "name": name, "device": device}
            self.store.set_setting(PENDING_SETTING, json.dumps(started))
        return await self.ask_for_code(ctx, started)

    async def confirm(self, ctx: Context) -> CallToolResult | InputRequiredResult:
        started = pending(self.store)
        if started is None:
            return error("No confirmation is in progress; call share_sign_in first.")
        return await self.ask_for_code(ctx, started)

    # --- sharing ------------------------------------------------------------------------------------------------------
    def fresh(self, token: str, identity: dict[str, str]) -> str | CallToolResult:
        """The token to share with: this one, or a new one when it expires within ROTATE_BEFORE (or its expiry
        isn't known). If the service can't be reached to swap it, the old one is used while it lasts."""
        try:
            expires = datetime.fromisoformat(identity.get("token_expires_at", "").replace("Z", "+00:00"))
        except ValueError:
            expires = None
        if expires is not None and expires - self.sharing.now() > ROTATE_BEFORE:
            return token
        try:
            rotated = self.sharing.service.rotate_sender(token)
        except ShareServiceError as err:
            if err.status == 401:
                self.keychain.delete(TOKEN_ACCOUNT)
                return error(SIGN_IN_AGAIN)
            return token
        try:
            keep_issued(self.store, self.keychain, rotated)
        except KeychainError as err:  # the old token is gone on the service, so nothing to fall back on
            return error(f"The share token was renewed but couldn't be kept: {err}. Call share_sign_in to confirm "
                         "the user's email again.")
        return str(rotated["token"])

    def _confirmed_sender(self) -> tuple[str, dict[str, str]] | CallToolResult:
        identity = self.sender()
        try:
            token = self.keychain.get(TOKEN_ACCOUNT)
        except KeychainError as err:
            return error(f"The share token couldn't be read from the keychain: {err}.")
        if not token or identity is None:
            return error("Sharing needs the user's email confirmed once on this device: call share_sign_in with "
                         "their email and name (the user types the emailed code where you can't see it).")
        return token, identity

    def _sealed(self, past: PullRecord, options: dict[str, str] | None,
                allow_download: bool) -> Sealed | CallToolResult:
        """The pull's fresh rows under the source's current block rules, with the user's options, sealed here."""
        pull = self.run(past.source, past.question, past.sql)
        if isinstance(pull, CallToolResult):
            return pull
        pulled_at = datetime.now(UTC).isoformat(timespec="seconds")
        snapshot = snapshot_of(pull, title=past.saved_name or past.question, pulled_at=pulled_at,
                               allow_download=allow_download)
        try:
            shown = with_options(snapshot, options)
        except ShareOptionError as err:
            return error(f"Not shared: {err}.")
        key, sealed = seal_snapshot(shown)
        if sealed_size(sealed) > MAX_SEALED_BYTES:
            return error(TOO_BIG.format(size=f"{sealed_size(sealed) / (1024 * 1024):.1f}"))
        return Sealed(shown, key, sealed, pulled_at, _options_summary(list(snapshot["columns"]), options),
                      allows_download(shown))

    def _upload(self, token: str, recipient: str, sealed: Sealed, days: int) -> tuple[Created, str] | CallToolResult:
        """The share made on the service, and its link (built here; the key only ever in the fragment)."""
        try:
            made = self.sharing.service.create(token, recipient, sealed.ciphertext, days)
            return made, self.sharing.service.link(made.id, sealed.key)
        except ShareServiceError as err:
            if err.status == 401:
                self.keychain.delete(TOKEN_ACCOUNT)
                return error(SIGN_IN_AGAIN)
            if err.body.get("field") == "days" and days > SHARE_DAYS:
                return error(f"Not shared: {err}. Shares longer than {SHARE_DAYS} days (up to {PERSONAL_SHARE_DAYS}) "
                             "are part of Portward Personal, for the account with the email the user shares from.")
            return error(f"Not shared: {err}.")

    def share(self, pull_id: str, recipient: str, options: dict[str, str] | None, days: int,
              panel: bool, allow_download: bool = False) -> CallToolResult:
        sender = self._confirmed_sender()
        if isinstance(sender, CallToolResult):
            return sender
        token, identity = sender
        past = self.store.pull(pull_id)
        if past is None:
            return error(f"no pull with id {pull_id!r}; call history for the ids")
        sealed = self._sealed(past, options, allow_download)
        if isinstance(sealed, CallToolResult):
            return sealed
        token = self.fresh(token, identity)
        if isinstance(token, CallToolResult):
            return token
        uploaded = self._upload(token, recipient.strip().lower(), sealed, days)
        if isinstance(uploaded, CallToolResult):
            return uploaded
        made, link = uploaded
        entry = self.store.record_share(ShareRecord(made.id, past.id, recipient.strip().lower(), sealed.pulled_at,
                                                    made.expires_at, tuple(sealed.shown["columns"]),
                                                    len(sealed.shown["rows"])))
        self.on_shared()
        held = HeldLink(made.id, link, entry.recipient, identity["name"], made.expires_at)
        if panel and self.links.hold(held):
            delivery = Delivery("panel", opened=False)
        else:
            delivery = Delivery("mail", opened=self.sharing.drafts.open(*draft(held)), panel=panel)
        return _shared_reply(entry, delivery, sealed)

    def listed(self, limit: int) -> CallToolResult:
        listed = self.store.shares(max(1, min(limit, MAX_SHARES_LISTED)))
        entries = [{**asdict(e), "columns": list(e.columns)} for e in listed]
        text = markdown_table(("shared", "with", "rows", "expires", "pull"),
                              [(e.created_at, e.recipient, e.row_count, e.expires_at, e.pull_id) for e in listed]) \
            if listed else "Nothing shared yet."
        return reply({"shares": entries}, text)


def _options_summary(columns: list[str], options: dict[str, str] | None) -> dict[str, list[str]]:
    kinds = list(zip(columns, chosen(columns, options), strict=True))
    return {"hidden": [c for c, k in kinds if k == HIDE], "anonymised": [c for c, k in kinds if k == ANONYMISE],
            "totals": [c for c, k in kinds if k == TOTALS]}


@dataclass(frozen=True)
class Delivery:
    """How the link reaches the user: the share panel ("panel"), or a draft in their mail app ("mail")."""

    link_in: str
    opened: bool  # a mail draft was opened
    panel: bool = True  # the client has a panel (a "mail" delivery then means the link couldn't be held for it)

    def said(self, recipient: str) -> str:
        if self.link_in == "panel":
            return (f"Nothing has been sent yet: the link is in the Portward share panel, where the user presses 'Copy "
                    f"link' (or 'Open in mail app') and sends it to {recipient} themselves. Don't ask them for it.")
        why = ("The link couldn't be kept for the Portward panel (the keychain couldn't encrypt it), so" if self.panel
               else "This client shows no Portward panel, so")
        if self.opened:
            return f"{why} a draft email to {recipient} with the link is open in the user's mail app: they send it."
        return (f"{why} the link went to the user's mail app, which couldn't be opened: the link wasn't delivered; "
                "share again from a machine with a mail app.")


def _shared_reply(entry: ShareRecord, delivery: Delivery, sealed: Sealed) -> CallToolResult:
    totals = "totals" in sealed.shown
    payload = {"share_id": entry.id, "recipient": entry.recipient, "expires_at": entry.expires_at,
               "columns": list(entry.columns), "row_count": entry.row_count, "totals": totals,
               "options": sealed.options, "allow_download": sealed.allow_download, "link_in": delivery.link_in,
               "draft_opened": delivery.opened}
    what = "groups of totals" if totals else "rows"
    access = (DOWNLOAD_ALLOWED + ": they can save it as a CSV." if sealed.allow_download
              else VIEW_ONLY + ": no download button for them.")
    return reply(payload, f"Shared {entry.row_count} {what} ({', '.join(entry.columns)}) with {entry.recipient}, "
                          f"encrypted; it expires {entry.expires_at}. {access} {delivery.said(entry.recipient)} The "
                          "link isn't shown to you: it holds the key that opens the table.")


SHARE_DESCRIPTION = (
    "Share a pull from history with a teammate by email: the table is encrypted on this machine and the link is "
    "shown to the user in the Portward share panel to copy and send (or open in their mail app from there; "
    "without a panel a draft opens in their mail app). You never see the link. The teammate opens it with a code "
    f"emailed to them; it expires after `days` ({SHARE_DAYS} unless the user asks for longer: "
    f"up to {PERSONAL_SHARE_DAYS} where the plan allows). Blocked columns are never "
    "included. `options` sets what the teammate sees of each column, as the user chooses: \"include\" (the "
    "default for columns not named), \"hide\", \"anonymise\" (each value becomes a code, the same value the same "
    "code within this share only) or \"totals\" (only sums per group of the other columns, groups of fewer than "
    f"{MIN_GROUP_ROWS} rows left out). Ask the user which they want; don't choose for them. "
    "`allow_download` (default false: view only) lets the teammate download the shared table as a CSV; leave it "
    "false unless the user asks for the teammate to be able to download it. It can't be changed after sharing. "
    "Only call this when the user asks to share and names the teammate.")


def register_share_tools(server: MCPServer, store: LocalStore, keychain: Keychain, run: Run, sharing: Sharing,
                         out_of_band: OutOfBand, accounts: Accounts, links: ShareLinks,
                         on_shared: Callable[[], None] = lambda: None) -> None:
    """See ShareTools. The share panel's app-only tools (share_link, share_mail) are registered with the panels
    (sharing.links)."""
    tools = ShareTools(store, keychain, run, sharing, out_of_band, accounts, on_shared, links)
    out_of_band.register(CODE_ASK, tools.code_ask)

    @server.tool(meta=out_of_band.panel_meta(), description=(
        "Confirm the user's email for sharing, once per device: Portward emails them a code, and the user types it "
        "where you can't see it (never ask for it in the chat). `name` is how they're shown to the people they share "
        "with. Only call this when the user wants to share and gives their own email."))
    async def share_sign_in(email: str, name: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        return await tools.sign_in(ctx, email, name)

    @server.tool(meta=out_of_band.panel_meta(), description=(
        "Ask the user again for the code Portward emailed them (when the form or page closed before they typed it). "
        "Takes no code: the user types it where you can't see it."))
    async def share_confirm(ctx: Context) -> CallToolResult | InputRequiredResult:
        return await tools.confirm(ctx)

    @server.tool(meta={"ui": {"resourceUri": SHARE_PANEL_URI}}, description=SHARE_DESCRIPTION)
    def share(pull_id: str, recipient: str, ctx: Context, options: dict[str, str] | None = None,
              days: int = SHARE_DAYS, allow_download: bool = False) -> CallToolResult:
        return tools.share(pull_id, recipient, options, days, client_support(ctx).panel, allow_download)

    @server.tool(description="List what the user has shared: who with, when, and when each share expires.")
    def shares(limit: int = 20) -> CallToolResult:
        return tools.listed(limit)
