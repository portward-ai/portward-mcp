"""Deleting the Personal account from the free tool (#58), against the real control app on 127.0.0.1.

The AI can't delete an account: `account_delete` only opens the account site's delete page in the user's browser,
where they sign in again and type their email to confirm (and can download their data first). Once they have, the
server holds nothing for the account, and this computer's history, saved pulls and settings are just as they were."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from harness import MemoryKeychain, connected
from test_account import Opened, UserBrowser
from test_manage_shares import account_sign_in, share_one
from test_share import Drafts, Service

from portward_free.account import TOKEN_ACCOUNT, Accounts, AccountService

pytest_plugins = ["test_share"]  # its fixtures: the share service on 127.0.0.1, drafts, sharing, recipient


@pytest.fixture()
def sender() -> str:
    return f"sam-{uuid.uuid4().hex[:8]}@gmail.example"


def me_status(service: Service, token: str) -> int:
    request = urllib.request.Request(service.url + "/v1/me", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as err:
        return err.code


def delete_in_the_browser(service: Service, url: str, email: str) -> str:
    """What the user does on the page the tool opened: sign in again at AuthKit, type their email, delete."""
    browser = UserBrowser(service.url)
    _, _, page = browser.go(url)
    again = re.search(r'href="(/me/signin\?then=delete)"', page).group(1)
    status, headers, _ = browser.go(again)
    asked = {k: v[0] for k, v in parse_qs(urlsplit(headers["location"]).query).items()}
    status, headers, _ = browser.go(asked["redirect_uri"] + "?" + urlencode(
        {"state": asked["state"], "code": f"{email}|{asked['code_challenge']}"}))
    assert status == 303 and headers["location"] == "/me/delete"
    _, _, page = browser.go("/me/delete")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    status, _, page = browser.go("/me/delete", {"csrf": csrf, "email": email})
    assert status == 200, page
    return page


async def test_the_ai_can_only_open_the_page_and_local_history_stays_after_deleting(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing, recipient: str) -> None:
    opened, keychain = Opened(), MemoryKeychain()
    async with connected(home, team_db, keychain=keychain, sharing=sharing, browser=opened,
                         accounts=Accounts(AccountService(service.url))) as h:
        shared = await share_one(h, service, sender, recipient)
        assert not shared.is_error, shared.ai.text
        await account_sign_in(h, service, opened, sender)
        token = keychain.entries[TOKEN_ACCOUNT]
        saved = await h.call("save_pull", pull_id=h.stored("pulls")[0]["id"], name="Who is where")
        assert not saved.is_error, saved.ai.text
        before = {table: h.stored(table) for table in ("pulls", "shares", "rules", "notes")}

        asked = await h.call("account_delete")
        assert not asked.is_error, asked.ai.text
        assert opened.urls[-1] == f"{service.url}/me/delete"
        assert asked.ai.structured == {"opened": True}
        assert "browser" in asked.ai.text and "confirm" in asked.ai.text
        assert me_status(service, token) == 200  # opening the page deleted nothing

        page = delete_in_the_browser(service, opened.urls[-1], sender)
        assert "deleted" in page.lower()
        assert me_status(service, token) == 401

        status = await h.call("account_status")
        assert status.ai.structured == {"signed_in": False} and TOKEN_ACCOUNT not in keychain.entries
        assert {table: h.stored(table) for table in before} == before  # history, saved pulls, shares, rules
        history = await h.call("history")
        assert not history.is_error and "Who is where" in history.ai.text


async def test_account_delete_says_where_to_go_when_no_browser_opens(home: Path) -> None:
    from harness import Harness

    async with Harness.start(home, browser=Opened(works=False),
                             accounts=Accounts(AccountService("https://signup.portward.ai"))) as h:
        asked = await h.call("account_delete")
    assert asked.is_error and "https://signup.portward.ai/me/delete" in asked.ai.text
