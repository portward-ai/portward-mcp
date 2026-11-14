"""Managing shares in Personal (#56): the shares sent from the user's account email (on every computer), when each
was opened, withdrawing one and keeping one longer. They go through the account service with this computer's
device token (#53, `account.account_token`), not the share token: the account decides whether Personal is on, and
the service answers 402 when it isn't. A withdrawn share stops opening at once and the service deletes its
encrypted copy. The AI sees share ids, recipients and times; never a token or a link.

Changing how long a share lasts can keep it open longer, and the AI is the one writing tool calls, so
`set_share_expiry` asks the user in their client's own prompt (elicitation, as #39's `unblock` does) and changes
nothing without their explicit yes; a client that can't ask is sent to the account page. Withdrawing only narrows
who can read a share, so it runs at once, like `block`.

No `from __future__ import annotations` here: the `Resolve(...)` annotation must hold the resolver itself."""

from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from portward_free.account import TOKEN_ACCOUNT, Accounts, AccountService, AccountServiceError, account_token
from portward_free.clients import client_support
from portward_free.keychain import Keychain, KeychainError
from portward_free.pull import markdown_table
from portward_free.replies import error, reply
from portward_free.storage import LocalStore

NOT_ON = ("Managing shares is part of Portward Personal, which isn't on for this account. Shares from Free "
          "expire on their own after 7 days")  # no pitch: offers are made only in offers.py (#108)
SIGN_IN_FIRST = ("Managing shares needs this computer signed in to the user's Portward account: call account_sign_in "
                 "(the user signs in in their browser).")


class ChangeExpiry(BaseModel):
    change: bool = Field(description="Yes, change how long this share lasts")


@dataclass(frozen=True)
class SharedOne:
    """One share as the account service lists it, parsed once from its JSON."""

    id: str
    recipient: str
    state: str
    created_at: str
    expires_at: str
    opens: tuple[str, ...]

    @classmethod
    def parse(cls, raw: Any) -> "SharedOne | None":
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            return None
        opens = raw.get("opens") if isinstance(raw.get("opens"), list) else []
        return cls(raw["id"], str(raw.get("recipient", "")), str(raw.get("state", "")),
                   str(raw.get("created_at", "")), str(raw.get("expires_at", "")), tuple(str(o) for o in opens))

    def payload(self) -> dict[str, Any]:
        return {"id": self.id, "recipient": self.recipient, "state": self.state, "created_at": self.created_at,
                "expires_at": self.expires_at, "opens": list(self.opens)}

    def opened(self) -> str:
        return f"opened {len(self.opens)}x, last {self.opens[-1]}" if self.opens else "not opened"


@dataclass(frozen=True)
class Manager:
    """What the share-management tools share: the account service and this computer's token for it."""

    store: LocalStore
    keychain: Keychain
    service: AccountService

    def token(self) -> str | CallToolResult:
        try:
            found = account_token(self.keychain)
        except KeychainError as err:
            return error(f"The account token couldn't be read from the keychain: {err}.")
        return found or error(SIGN_IN_FIRST)

    def refused(self, err: AccountServiceError) -> CallToolResult:
        if err.status == 401:
            self.keychain.delete(TOKEN_ACCOUNT)
            return error("The account no longer knows this computer: call account_sign_in to sign in again.")
        if err.status == 402:
            return error(f"{NOT_ON}.")
        return error(f"Not done: {err}.")

    def page(self) -> str:
        return f"{self.service.base_url}/me/shares"

    def listed(self) -> CallToolResult:
        held = self.token()
        if isinstance(held, CallToolResult):
            return held
        try:
            answer = self.service.shares(held)
        except AccountServiceError as err:
            return self.refused(err)
        raw = answer.get("shares")
        shares = [s for s in (SharedOne.parse(r) for r in (raw if isinstance(raw, list) else [])) if s is not None]
        text = markdown_table(("share", "with", "state", "shared", "expires", "opens"),
                              [(s.id, s.recipient, s.state, s.created_at, s.expires_at, s.opened()) for s in shares]) \
            if shares else "Nothing shared from this account yet."
        days = answer.get("expiry_days")
        return reply({"shares": [s.payload() for s in shares], "expiry_days": days if isinstance(days, list) else []},
                     text)

    def withdraw(self, share_id: str) -> CallToolResult:
        held = self.token()
        if isinstance(held, CallToolResult):
            return held
        try:
            self.service.withdraw_share(held, share_id)
        except AccountServiceError as err:
            return self.refused(err)
        return reply({"share_id": share_id, "state": "revoked"},
                     f"Withdrawn: share {share_id} no longer opens, and its encrypted copy is deleted.")

    def set_expiry(self, ctx: Context, share_id: str, days: int,
                   answer: ElicitationResult[ChangeExpiry]) -> CallToolResult:
        if not client_support(ctx).elicitation:
            return error(f"Not changed: this app can't ask the user to confirm, so how long a share lasts can't be "
                         f"changed from chat. The user can change it on their account page: {self.page()}")
        if not (isinstance(answer, AcceptedElicitation) and answer.data.change is True):
            return error(f"Not changed: the user didn't confirm. Share {share_id} lasts as long as before.")
        held = self.token()
        if isinstance(held, CallToolResult):
            return held
        try:
            changed = self.service.set_share_expiry(held, share_id, days)
        except AccountServiceError as err:
            return self.refused(err)
        expires_at = str(changed.get("expires_at", ""))
        self.store.set_share_expiry(share_id, expires_at)
        return reply({"share_id": share_id, "expires_at": expires_at}, f"Share {share_id} now expires {expires_at}.")


def confirm_expiry(ctx: Context, share_id: str, days: int) -> Elicit[ChangeExpiry] | ChangeExpiry:
    """Ask the user; answer no without asking when the client can't ask."""
    if not client_support(ctx).elicitation:
        return ChangeExpiry(change=False)
    return Elicit(f"Make share {share_id} last {days} days from when it was shared? If that is longer than now, the "
                  "person it was shared with can open it for longer.", ChangeExpiry)


def register_manage_tools(server: MCPServer, store: LocalStore, keychain: Keychain, accounts: Accounts) -> None:
    manager = Manager(store, keychain, accounts.service)

    @server.tool(description=(
        "List the shares the user sent from their Portward account's email (every computer), with when each was "
        "opened by the person it was shared with and whether it is live, expired or withdrawn."))
    def account_shares() -> CallToolResult:
        return manager.listed()

    @server.tool(description=(
        "Withdraw one of the user's shares: its link stops opening at once and Portward deletes the encrypted copy. "
        "Only when the user asks to withdraw (or revoke, unshare) that share."))
    def withdraw_share(share_id: str) -> CallToolResult:
        return manager.withdraw(share_id)

    @server.tool(description=(
        "Change how long one of the user's live shares lasts: `days` from when it was shared, one of 7, 30 or 90. "
        "Portward asks the user to confirm it themselves; only call this when the user asks."))
    def set_share_expiry(ctx: Context, share_id: str, days: int,
                         answer: Annotated[ElicitationResult[ChangeExpiry], Resolve(confirm_expiry)]
                         ) -> CallToolResult:
        return manager.set_expiry(ctx, share_id, days, answer)
