"""Signing the free tool in to a Personal account (#53), against the real control app on 127.0.0.1.

`account_sign_in` opens the account page in the user's browser with a loopback address on this computer
(http://127.0.0.1:<port>/<secret>/done) and a PKCE challenge; the user signs in and approves there, the browser comes
back to that address, and the tool trades the one-time code (with its verifier) for a device token kept in the
keychain. The AI never sees the token, the code or the addresses. #55 sync reads the token with `account_token`."""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from harness import Harness, MemoryKeychain

from portward_free.account import TOKEN_ACCOUNT, Accounts, AccountService, account_token, personal_plan

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).parent / "fixtures" / "share_service.py"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


@dataclass
class UserBrowser:
    """The user's web browser on the account pages: keeps the session cookie, follows nothing by itself."""
    base: str
    cookies: dict[str, str] = field(default_factory=dict)
    opener: urllib.request.OpenerDirector = field(default_factory=lambda: urllib.request.build_opener(_NoRedirects))

    def go(self, url: str, form: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
        url = url if url.startswith("http") else self.base + url
        data = urlencode(form).encode() if form is not None else None
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())} if self.cookies else {}
        request = urllib.request.Request(url, data, headers, method="POST" if form is not None else "GET")
        try:
            with self.opener.open(request, timeout=10) as response:
                status, got, body = response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as err:
            status, got, body = err.code, err.headers, err.read().decode()
        for value in got.get_all("Set-Cookie") or []:
            name, _, rest = value.split(";", 1)[0].partition("=")
            if "Max-Age=0" in value:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = rest
        return status, {k.lower(): v for k, v in got.items()}, body


@dataclass
class Service:
    url: str
    folder: Path

    def me(self, token: str) -> int:
        request = urllib.request.Request(self.url + "/v1/me", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as err:
            return err.code


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    if shutil.which("uv") is None or not (ROOT / "src" / "portward" / "control").is_dir():
        pytest.skip("needs uv and the gateway project (accounts live in its control app)")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    folder = tmp_path_factory.mktemp("account-service")
    process = subprocess.Popen(["uv", "run", "--project", str(ROOT), "--frozen", "python", str(LAUNCHER), str(port),
                                str(folder), "--generous"], stdout=subprocess.DEVNULL,
                               stderr=open(folder / "server.log", "w"))
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(url + "/health", timeout=1).close()
                break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError((folder / "server.log").read_text()) from None
                time.sleep(0.05)
        else:
            raise AssertionError("the control app didn't start")
        yield Service(url, folder)
    finally:
        process.terminate()
        process.wait(10)


@dataclass
class Opened:
    """The user's browser as the tool sees it: the addresses it was asked to open."""
    urls: list[str] = field(default_factory=list)
    works: bool = True

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        return self.works


def sign_in_and_answer(service: Service, opened_url: str, email: str, answer: str = "approve") -> str:
    """What the user does in their browser: open the page, sign in at AuthKit, approve or not. Returns the
    loopback address the account page sends the browser back to."""
    browser = UserBrowser(service.url)
    status, headers, _ = browser.go(opened_url)
    assert status == 303, (status, headers)
    link_path = headers["location"]
    _, _, page = browser.go(link_path)
    then = re.search(r'href="(/me/signin\?then=[A-Za-z0-9_-]+)"', page).group(1)
    status, headers, _ = browser.go(then)  # to WorkOS AuthKit (a fake here), where they sign in
    assert status == 303 and headers["location"].startswith("https://authkit.invalid/authorize?")
    asked = {k: v[0] for k, v in parse_qs(urlsplit(headers["location"]).query).items()}
    callback = asked["redirect_uri"] + "?" + urlencode(
        {"state": asked["state"], "code": f"{email}|{asked['code_challenge']}"})
    status, headers, _ = browser.go(callback)
    assert status == 303 and headers["location"] == link_path
    _, _, page = browser.go(link_path)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    status, headers, _ = browser.go(link_path, {"csrf": csrf, "answer": answer})
    assert status == 303
    return headers["location"]


def back_to_the_tool(loopback: str) -> tuple[int, str]:
    """The browser following the account page's redirect to the tool's address on this computer."""
    try:
        with urllib.request.urlopen(loopback, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


async def test_the_tool_signs_in_through_the_browser_and_keeps_the_token_in_the_keychain(
        home: Path, service: Service) -> None:
    keychain, opened = MemoryKeychain(), Opened()
    accounts = Accounts(AccountService(service.url))
    async with Harness.start(home, keychain=keychain, browser=opened, accounts=accounts) as h:
        started = await h.call("account_sign_in")
        assert not started.is_error, started.ai.text
        (url,) = opened.urls
        parts = urlsplit(url)
        assert url.startswith(service.url + "/me/link?") and "code_challenge_method=S256" in parts.query
        loopback = sign_in_and_answer(service, url, "sam@gmail.example")
        assert loopback.startswith("http://127.0.0.1:") and "code=" in loopback
        status, page = back_to_the_tool(loopback)
        assert status == 200 and "sam@gmail.example" in page

        token = keychain.entries[TOKEN_ACCOUNT]
        assert token.startswith("pwat_") and account_token(keychain) == token
        status = await h.call("account_status")
        assert status.ai.structured["signed_in"] is True and status.ai.structured["email"] == "sam@gmail.example"
        plan = status.ai.structured["plan"]  # #54: a new account's 14-day trial, as the account service has it
        assert plan["state"] == "trial" and plan["personal"] is True and plan["until"] > time.time() + 13 * 86_400
        assert "trial" in status.ai.text.lower()
        assert personal_plan(keychain, accounts.service) == plan  # what sync (#55) and shares (#56) check
        seen_by_ai = started.ai.text + json.dumps(started.ai.structured) + status.ai.text
        assert f"{service.url}/me" in status.ai.text  # the account page, to subscribe or manage billing
        seen_by_ai = seen_by_ai.replace(f"{service.url}/me", "")  # the test service also runs on 127.0.0.1
        assert token not in seen_by_ai and "127.0.0.1" not in seen_by_ai and "/me/link" not in seen_by_ai

        signed_out = await h.call("account_sign_out")
        assert not signed_out.is_error, signed_out.ai.text
        assert TOKEN_ACCOUNT not in keychain.entries and service.me(token) == 401  # ended on the server too
        assert (await h.call("account_status")).ai.structured == {"signed_in": False}
        assert personal_plan(keychain, accounts.service) is None


async def test_a_denied_or_forged_return_keeps_nothing(home: Path, service: Service) -> None:
    keychain, opened = MemoryKeychain(), Opened()
    async with Harness.start(home, keychain=keychain, browser=opened,
                             accounts=Accounts(AccountService(service.url))) as h:
        await h.call("account_sign_in")
        loopback = sign_in_and_answer(service, opened.urls[-1], "dee@gmail.example", answer="deny")
        status, _ = back_to_the_tool(loopback)
        assert status == 400 and TOKEN_ACCOUNT not in keychain.entries

        await h.call("account_sign_in")
        loopback = sign_in_and_answer(service, opened.urls[-1], "dee@gmail.example")
        forged = re.sub(r"state=[^&]+", "state=" + "x" * 32, loopback)
        assert back_to_the_tool(forged)[0] == 400
        assert TOKEN_ACCOUNT not in keychain.entries
        assert not (await h.call("account_status")).ai.structured["signed_in"]


async def test_sign_in_says_how_when_no_browser_opens(home: Path) -> None:
    async with Harness.start(home, browser=Opened(works=False),
                             accounts=Accounts(AccountService("http://127.0.0.1:9"))) as h:
        started = await h.call("account_sign_in")
    assert started.is_error and "portward-free sign-in" in started.ai.text


async def test_a_token_the_service_no_longer_knows_is_dropped(home: Path, service: Service) -> None:
    keychain = MemoryKeychain()
    keychain.set(TOKEN_ACCOUNT, "pwat_" + "z" * 43)
    async with Harness.start(home, keychain=keychain, accounts=Accounts(AccountService(service.url))) as h:
        status = await h.call("account_status")
    assert status.ai.structured == {"signed_in": False} and TOKEN_ACCOUNT not in keychain.entries


def test_the_account_service_must_be_https_or_this_computer() -> None:
    with pytest.raises(ValueError):
        AccountService("http://signup.portward.ai")
    assert AccountService("https://signup.portward.ai").base_url == "https://signup.portward.ai"


def test_there_is_a_terminal_command_for_when_no_browser_opens_from_the_ai(capsys: pytest.CaptureFixture) -> None:
    from portward_free.cli import main

    with pytest.raises(SystemExit):
        main(["--help"])
    assert "sign-in" in capsys.readouterr().out
