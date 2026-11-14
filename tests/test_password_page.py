"""Where the client supports URL-mode elicitation, a one-time page on this machine takes the password.

The page listens on 127.0.0.1 only, at a path holding a single-use unguessable token; it expires, refuses other
hosts and origins, and sends strict headers. The URL goes to the client's own prompt, never to the AI.
"""

import asyncio
import re
import time
import urllib.parse
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest
from conftest import READER_PASSWORD, Database
from harness import Harness, MemoryKeychain
from mcp.types import ElicitRequestParams, ElicitResult

from portward_free import asks, passwords

pytestmark = pytest.mark.integration
PROTOCOLS = pytest.mark.parametrize("protocol", ["modern", "legacy"])


class User:
    """Answers the client's URL prompt (accept opens the page) and remembers what it was shown."""

    def __init__(self, action: str = "accept") -> None:
        self.action = action
        self.shown: list[Any] = []

    async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
        self.shown.append(params)
        return ElicitResult(action=self.action)

    @property
    def url(self) -> str:
        [params] = self.shown
        assert params.mode == "url"
        return params.url


FORM_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')


def request(url: str, method: str = "GET", form: dict[str, str] | None = None,
            headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    """One HTTP request as the user's browser sends it; (status, headers, body). Status -1: nothing listens there.
    A POST sends the page's own Origin and the form token from the page, as submitting its form does."""
    parts = urllib.parse.urlsplit(url)
    sent: dict[str, str] = {}
    if method == "POST":
        sent = {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://{parts.netloc}"}
        if form is not None and "form_token" not in form:
            found = FORM_TOKEN.search(request(url)[2])
            form = {**form, "form_token": found.group(1) if found else ""}
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    connection = HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        try:
            connection.request(method, parts.path, body=body, headers={**sent, **(headers or {})})
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read().decode()
        except OSError:  # refused, reset, broken pipe, or (macOS) EINVAL on a socket the page is closing
            return -1, {}, ""  # closed (or closing as the request came in)
    finally:
        connection.close()


async def browser(*args: Any, **kwargs: Any) -> tuple[int, dict[str, str], str]:
    return await asyncio.to_thread(request, *args, **kwargs)


@PROTOCOLS
async def test_the_user_types_the_password_in_a_one_time_local_page(
        home: Path, database: Database, keychain: MemoryKeychain, protocol: str) -> None:
    user = User()
    async with Harness.start(home, keychain=keychain, elicitation=user, protocol=protocol) as h:
        added = await h.call("add_source", name="shop", **database.fields())
        status, headers, page = await browser(user.url)
        saved = await browser(user.url, "POST", {"secret": READER_PASSWORD})
        again = await browser(user.url, "POST", {"secret": READER_PASSWORD})
        tested = await h.call("test_source", name="shop")
    assert not added.is_error, added.ai.text
    assert added.ai.structured["password_entry"] == "page"
    assert user.url not in added.ai.text + str(added.ai.structured)
    assert urllib.parse.urlsplit(user.url).hostname == "127.0.0.1"
    assert status == 200 and 'type="password"' in page and database.fields()["user"] in page
    assert saved[0] == 200 and "Saved" in saved[2] and READER_PASSWORD not in saved[2]
    assert again[0] in (410, -1)  # used up: gone, or already closed
    assert keychain.passwords == {"connection:shop": READER_PASSWORD}
    assert not tested.is_error


async def test_the_page_sends_strict_headers(home: Path, database: Database) -> None:
    user = User()
    async with Harness.start(home, elicitation=user, protocol="legacy") as h:
        await h.call("add_source", name="shop", **database.fields())
        _, headers, _ = await browser(user.url)
    csp = headers["content-security-policy"]
    for rule in ("default-src 'none'", "frame-ancestors 'none'", "form-action 'self'", "base-uri 'none'"):
        assert rule in csp
    assert "script-src" not in csp  # no scripts at all
    assert headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "same-origin"  # no-referrer makes browsers send "Origin: null" on the post
    assert headers["x-content-type-options"] == "nosniff"


async def test_the_page_refuses_a_wrong_token_another_host_another_origin_or_a_forged_form(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    user = User()
    async with Harness.start(home, keychain=keychain, elicitation=user, protocol="legacy") as h:
        await h.call("add_source", name="shop", **database.fields())
        parts = urllib.parse.urlsplit(user.url)
        wrong_token = await browser(f"http://127.0.0.1:{parts.port}/not-the-token")
        rebound = await browser(user.url, headers={"Host": f"evil.example:{parts.port}"})
        cross = await browser(user.url, "POST", {"secret": READER_PASSWORD},
                              headers={"Origin": "https://evil.example"})
        forged = await browser(user.url, "POST", {"secret": READER_PASSWORD, "form_token": "a-guess"})
        still = await browser(user.url)
    assert wrong_token[0] == 404
    assert forged[0] == 403
    assert rebound[0] == 421
    assert cross[0] == 403
    assert still[0] == 200  # none of those used it up
    assert keychain.passwords == {}


async def test_a_wrong_password_shows_the_reason_on_the_page_and_can_be_retried(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    user = User()
    async with Harness.start(home, keychain=keychain, elicitation=user, protocol="legacy") as h:
        await h.call("add_source", name="shop", **database.fields())
        wrong = await browser(user.url, "POST", {"secret": "not-the-password"})
        right = await browser(user.url, "POST", {"secret": READER_PASSWORD})
    assert wrong[0] == 200 and "wrong user or password" in wrong[2] and "not-the-password" not in wrong[2]
    assert right[0] == 200 and keychain.passwords == {"connection:shop": READER_PASSWORD}


@PROTOCOLS
async def test_declining_the_page_falls_back_to_the_terminal_and_closes_it(
        home: Path, database: Database, protocol: str) -> None:
    user = User(action="decline")
    async with Harness.start(home, elicitation=user, protocol=protocol) as h:
        added = await h.call("add_source", name="shop", **database.fields())
        closed = await browser(user.shown[-1].url)
    assert closed[0] == -1
    assert added.ai.structured["password_entry"] == "terminal"
    assert "password shop" in added.ai.text


async def test_the_page_expires(home: Path, database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asks, "REQUEST_MINUTES", 0.5 / 60)
    user = User()
    async with Harness.start(home, elicitation=user, protocol="legacy") as h:
        await h.call("add_source", name="shop", **database.fields())
        before = await browser(user.url)
        await asyncio.sleep(1.0)
        after = (await browser(user.url))[0]
    assert before[0] == 200
    assert after in (410, -1)


async def test_the_page_never_logs_the_token_or_password(home: Path, database: Database,
                                                         capfd: pytest.CaptureFixture[str]) -> None:
    user = User()
    async with Harness.start(home, elicitation=user, protocol="legacy") as h:
        await h.call("add_source", name="shop", **database.fields())
        await browser(user.url)
        await browser(user.url, "POST", {"secret": READER_PASSWORD})
    out, err = capfd.readouterr()
    token = urllib.parse.urlsplit(user.url).path.strip("/")
    for text in (out, err):
        assert token not in text and READER_PASSWORD not in text


async def test_a_malformed_post_is_refused_quietly(home: Path, database: Database,
                                                   capfd: pytest.CaptureFixture[str]) -> None:
    user = User()
    async with Harness.start(home, elicitation=user, protocol="legacy") as h:
        await h.call("add_source", name="shop", **database.fields())
        bad = await browser(user.url, "POST", headers={"Content-Length": "abc"})
        still = await browser(user.url)
    _, err = capfd.readouterr()
    assert bad[0] == 400 and still[0] == 200
    assert "Traceback" not in err


def test_requests_read_the_expiry_when_made(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asks, "REQUEST_MINUTES", 0)
    ask = asks.Requests().open(passwords.password_ask(passwords.Connection("x", "postgres", {}), MemoryKeychain()))
    assert ask.expires_at <= time.time()  # wall clock: every server process reads the same expiry
