"""Managing shares from the free tool (#56), against the real control app on 127.0.0.1: with the computer signed in
to a Personal account (#53's device token), the AI can list the shares sent from the account's email with when each
was opened, withdraw one (the link stops opening and the service deletes the encrypted copy), and keep one longer;
`share` can make a share last longer than Free's 7 days. A Free account hears what Personal adds.

The test service makes the accounts of addresses starting "ended-" 30 days old, so their trial is over (Free)."""

from __future__ import annotations

import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from harness import Harness, MemoryKeychain, connected
from test_account import Opened, back_to_the_tool, sign_in_and_answer
from test_share import Drafts, Service, receive, sign_in

from mcp.types import ElicitResult

from portward_free.account import Accounts, AccountService
from portward_free.sharing import Sharing

pytest_plugins = ["test_share"]  # its fixtures: the share service on 127.0.0.1, drafts, sharing, recipient


class User:
    """Answers the client's prompt (elicitation) the way a person would, and remembers what they were asked."""

    def __init__(self, action: str = "accept", content: dict | None = None) -> None:
        self.action, self.content = action, content if content is not None else {"change": True}
        self.asked: list[str] = []

    async def __call__(self, context, params) -> ElicitResult:
        self.asked.append(params.message)
        return ElicitResult(action=self.action, content=self.content if self.action == "accept" else None)


@pytest.fixture()
def sender() -> str:
    return f"sam-{uuid.uuid4().hex[:8]}@gmail.example"


async def account_sign_in(h: Harness, service: Service, opened: Opened, email: str) -> None:
    """The user signs this computer in to their account in the browser (#53)."""
    started = await h.call("account_sign_in")
    assert not started.is_error, started.ai.text
    status, _ = back_to_the_tool(sign_in_and_answer(service, opened.urls[-1], email))
    assert status == 200


async def share_one(h: Harness, service: Service, sender: str, recipient: str, **extra):
    pulled = await h.call("query", source="shop", sql="SELECT name, region FROM people", question="Who is where?")
    await sign_in(h, service, sender)
    return await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient, **extra)


def _status(service: Service, link: str) -> int:
    share_id = urlsplit(link).path.rsplit("/", 1)[1]
    try:
        with urllib.request.urlopen(f"{service.url}/v1/shares/{share_id}", timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as err:
        return err.code


async def test_a_personal_user_lists_withdraws_and_extends_shares_from_the_tool(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    opened, user = Opened(), User()
    async with connected(home, team_db, keychain=MemoryKeychain(), sharing=sharing, browser=opened, elicitation=user,
                         accounts=Accounts(AccountService(service.url))) as h:
        await account_sign_in(h, service, opened, sender)
        shared = await share_one(h, service, sender, recipient, days=30)
        assert not shared.is_error, shared.ai.text
        share_id, link = shared.ai.structured["share_id"], drafts.link()
        expires = datetime.fromisoformat(shared.ai.structured["expires_at"].replace("Z", "+00:00"))
        assert expires - datetime.now(UTC) > timedelta(days=29)
        receive(service, link, recipient)

        listed = await h.call("account_shares")
        assert not listed.is_error, listed.ai.text
        (only,) = listed.ai.structured["shares"]
        assert only["id"] == share_id and only["state"] == "live" and len(only["opens"]) == 1
        assert recipient in listed.ai.text and "opened 1" in listed.ai.text.lower()

        longer = await h.call("set_share_expiry", share_id=share_id, days=90)
        assert not longer.is_error, longer.ai.text
        (asked,) = [q for q in user.asked if share_id in q]
        assert "90 days" in asked
        assert (await h.call("shares")).ai.structured["shares"][0]["expires_at"] == longer.ai.structured["expires_at"]

        withdrawn = await h.call("withdraw_share", share_id=share_id)
        assert not withdrawn.is_error, withdrawn.ai.text
        assert _status(service, link) == 410
        assert (await h.call("account_shares")).ai.structured["shares"][0]["state"] == "revoked"
        again = await h.call("withdraw_share", share_id=share_id)
        assert again.is_error


async def test_a_free_user_hears_managing_shares_is_off_without_a_pitch(home: Path, team_db: Path, service: Service, drafts: Drafts,
                                                    sharing: Sharing, recipient: str) -> None:
    sender = f"ended-{uuid.uuid4().hex[:8]}@gmail.example"
    opened = Opened()
    async with connected(home, team_db, keychain=MemoryKeychain(), sharing=sharing, browser=opened,
                         accounts=Accounts(AccountService(service.url))) as h:
        await account_sign_in(h, service, opened, sender)
        longer = await share_one(h, service, sender, recipient, days=30)
        assert longer.is_error and "Personal" in longer.ai.text and "7 days" in longer.ai.text
        shared = await h.call("share", pull_id=(await h.call("history")).ai.structured["pulls"][0]["id"],
                              recipient=recipient)
        assert not shared.is_error, shared.ai.text

        for answer in (await h.call("account_shares"),
                       await h.call("withdraw_share", share_id=shared.ai.structured["share_id"])):
            assert answer.is_error and "Unknown tool" in answer.ai.text  # #116: not listed on Free
            assert "/me/shares" not in answer.ai.text and "subscribe" not in answer.ai.text.lower()  # #108
        assert _status(service, drafts.link()) == 200


async def test_managing_shares_needs_the_computer_signed_in(home: Path, team_db: Path, sharing: Sharing) -> None:
    async with connected(home, team_db, keychain=MemoryKeychain(), sharing=sharing,
                         accounts=Accounts(AccountService("http://127.0.0.1:9"))) as h:
        listed = await h.ai_tools()
        answer = await h.call("account_shares")
    assert "account_shares" not in listed and "account_sign_in" in listed  # #116: signing in brings it
    assert answer.is_error and "Unknown tool" in answer.ai.text  # #116: not listed on Free


@pytest.mark.parametrize("user", [None, User("decline"), User("cancel"), User("accept", {"change": False})])
async def test_a_share_lasts_longer_only_when_the_user_confirms_it_themselves(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing, recipient: str,
        user: User | None) -> None:
    """An injected prompt must not keep a share open longer: the user says yes in their client's own prompt, and a
    client that can't ask is sent to the account page."""
    opened = Opened()
    async with connected(home, team_db, keychain=MemoryKeychain(), sharing=sharing, browser=opened, elicitation=user,
                         accounts=Accounts(AccountService(service.url))) as h:
        await account_sign_in(h, service, opened, sender)
        shared = await share_one(h, service, sender, recipient)
        share_id, before = shared.ai.structured["share_id"], shared.ai.structured["expires_at"]
        refused = await h.call("set_share_expiry", share_id=share_id, days=90)
        listed = await h.call("account_shares")
    assert refused.is_error
    if user is None:
        assert f"{service.url}/me/shares" in refused.ai.text
    (only,) = listed.ai.structured["shares"]
    assert only["expires_at"][:16] == before[:16]  # unchanged
