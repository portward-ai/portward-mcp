"""The local Portward page: history, saved pulls, rules, settings and what the AI was sent, in the user's browser.

The AI opens it with `open_page`; the server opens the browser itself, so the page's address (and the unguessable
token in it) never reaches the AI. The page is served on 127.0.0.1 only, answers only to its own Host, and every
change it makes needs the page's own form token, so other local apps and websites can't read it or post to it.
"""

from __future__ import annotations

import html
import http.client
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from harness import Harness, MemoryKeychain, connected
from memory_keyring import server_env

from portward_free.kept import KEY_ACCOUNT
from portward_free.paths import HOME_ENV, database_path
from portward_free.storage import LocalStore, ShareRecord
from portward_free.usage import TEAM_URL, TEAM_URL_COUNTED


class Browser:
    """Stands in for the user's browser: records the address the server asked it to open."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    def __call__(self, url: str) -> bool:
        self.opened.append(url)
        return True

    @property
    def url(self) -> str:
        assert self.opened, "the browser was never opened"
        return self.opened[-1]


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: str


def fetch(url: str, *, method: str = "GET", host: str | None = None, form: dict[str, str] | None = None,
          headers: dict[str, str] | None = None) -> Answer:
    """One HTTP request to the page, as a browser (or an attacker) would send it."""
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    body = urllib.parse.urlencode(form) if form is not None else None
    sent = {"Host": host or parts.netloc, **(headers or {})}
    if body is not None:
        sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        sent.setdefault("Origin", f"{parts.scheme}://{parts.netloc}")
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    connection.request(method, path, body=body, headers=sent)
    response = connection.getresponse()
    answer = Answer(response.status, {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode("utf-8", "replace"))
    connection.close()
    return answer


async def test_the_ai_opens_the_page_in_the_browser_without_learning_its_address(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("query", source="shop", question="Who are the customers?", sql="SELECT name FROM customers")
        opened = await h.call("open_page")
        page = fetch(browser.url)

    assert not opened.is_error, opened.ai.text
    parts = urllib.parse.urlsplit(browser.url)
    assert parts.hostname == "127.0.0.1"
    token = re.search(r"[A-Za-z0-9_-]{32,}", browser.url)
    assert token is not None, "the address carries an unguessable token"
    assert token.group(0) not in opened.ai.text + repr(opened.ai.structured)
    assert str(parts.port) not in opened.ai.text + repr(opened.ai.structured)
    assert page.status == 200
    assert "Who are the customers?" in page.body


def base_of(url: str) -> tuple[str, str]:
    """(origin, token) of the page's address."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parts.path.strip("/").split("/")[0]


def form_token(page: Answer) -> str:
    found = re.search(r'name="form_token" value="([^"]+)"', page.body)
    assert found is not None, "the page's forms carry its form token"
    return found.group(1)


async def test_the_page_answers_only_with_its_token(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        origin, token = base_of(browser.url)
        answers = [fetch(f"{origin}/"), fetch(f"{origin}/{token[:-1]}x/"), fetch(f"{origin}/page.css")]
    assert [a.status for a in answers] == [404, 404, 404]
    assert all("Pulls" not in a.body for a in answers)


async def test_a_website_rebinding_its_name_to_this_computer_is_refused(home: Path, shop_db: Path) -> None:
    """DNS rebinding: the browser sends the website's own name as Host, so the page refuses to answer."""
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        refused = fetch(browser.url, host="evil.example:80")
        port = urllib.parse.urlsplit(browser.url).port
        same_name_other_port = fetch(browser.url, host=f"127.0.0.1:{port + 1}")
        by_localhost = fetch(browser.url, host=f"localhost:{port}")
    assert refused.status == 421 and "Pulls" not in refused.body
    assert same_name_other_port.status == 421
    assert by_localhost.status == 200


async def test_the_page_sends_headers_that_keep_it_out_of_other_sites(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        page = fetch(browser.url)
    policy = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy and "default-src 'none'" in policy
    assert "script-src" not in policy  # default-src 'none': no scripts at all
    # same-origin, not no-referrer: with no-referrer a browser posts the page's own forms with Origin: null, which
    # the page refuses; the address still never leaves in a Referer to another site
    assert page.headers["referrer-policy"] == "same-origin"
    assert 'content="no-referrer"' not in page.body
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert "set-cookie" not in page.headers


async def test_opening_the_page_again_keeps_one_address(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        await h.call("open_page")
    assert len(browser.opened) == 2 and len(set(browser.opened)) == 1


async def test_the_page_says_how_to_open_it_when_no_browser_opens(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, browser=lambda url: False) as h:
        opened = await h.call("open_page")
    assert opened.is_error
    assert "portward-free page" in opened.ai.text
    assert "127.0.0.1" not in opened.ai.text


def origin_of(url: str) -> str:
    return base_of(url)[0]


async def test_the_user_blocks_a_column_from_the_page_and_the_ai_cannot_read_it(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        page = fetch(browser.url)
        assert 'value="[&quot;customers&quot;, &quot;region&quot;]"' in page.body  # offered from the source itself
        done = fetch(browser.url + "rules/block", method="POST",
                     form={"form_token": form_token(page), "source": "shop", "target": '["customers", "region"]'})
        after = fetch(origin_of(browser.url) + done.headers["location"])
        query = await h.call("query", source="shop", question="?", sql="SELECT name, region FROM customers")
        rules = await h.call("list_rules")
    assert done.status == 303
    assert "customers.region" in after.body
    assert query.is_error
    assert rules.ai.structured["rules"] == [{"source": "shop", "table": "customers", "column": "region"}]


async def test_unblocking_from_the_page_asks_first_and_only_a_yes_removes_the_rule(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("block", source="shop", table="customers", column="region")
        await h.call("open_page")
        token = form_token(fetch(browser.url))
        rule = {"form_token": token, "source": "shop", "table": "customers", "column": "region"}
        asked = fetch(browser.url + "rules/unblock", method="POST", form=rule)
        still = (await h.call("list_rules")).ai.structured["rules"]
        kept = fetch(browser.url + "rules/unblock", method="POST", form={**rule, "confirm": "no"})
        still_after_no = (await h.call("list_rules")).ai.structured["rules"]
        removed = fetch(browser.url + "rules/unblock", method="POST", form={**rule, "confirm": "yes"})
        gone = (await h.call("list_rules")).ai.structured["rules"]
    assert asked.status == 200 and "Let the AI read customers.region in shop again?" in asked.body
    assert len(still) == 1 and len(still_after_no) == 1
    assert kept.status == 303
    assert removed.status == 303 and gone == []


async def test_a_change_needs_the_pages_own_form(home: Path, shop_db: Path) -> None:
    """CSRF: another website (or app) can't post a change, even one that knows the address."""
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("block", source="shop", table="customers", column="region")
        await h.call("open_page")
        token = form_token(fetch(browser.url))
        rule = {"source": "shop", "table": "customers", "column": "region", "confirm": "yes"}
        url = browser.url + "rules/unblock"
        tries = [
            fetch(url, method="POST", form=rule),  # no form token
            fetch(url, method="POST", form={**rule, "form_token": token[:-1] + "x"}),
            fetch(url, method="POST", form={**rule, "form_token": token}, headers={"Origin": "https://evil.example"}),
            fetch(url, method="POST", form={**rule, "form_token": token}, headers={"Origin": "null"}),
            fetch(url, method="POST", form={**rule, "form_token": token}, headers={"Sec-Fetch-Site": "cross-site"}),
            fetch(url, method="GET"),
        ]
        rules = (await h.call("list_rules")).ai.structured["rules"]
    assert [t.status for t in tries[:5]] == [403] * 5
    assert tries[5].status == 404
    assert len(rules) == 1


async def test_the_page_shows_saved_pulls_by_name(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        pulled = await h.call("query", source="shop", question="Revenue by region", sql="SELECT region FROM customers")
        await h.call("save_pull", pull_id=pulled.ai.structured["pull_id"], name="Monthly revenue by region")
        await h.call("open_page")
        page = fetch(browser.url)
    saved = page.body.split('id="saved"', 1)[1].split("</section>", 1)[0]
    assert "Monthly revenue by region" in saved
    assert "SELECT region FROM customers" in page.body  # the SQL behind each pull is on the page


async def test_the_user_turns_keeping_rows_on_from_the_page(home: Path, shop_db: Path) -> None:
    browser = Browser()
    keychain = MemoryKeychain()
    async with connected(home, shop_db, browser=browser, keychain=keychain) as h:
        await h.call("open_page")
        page = fetch(browser.url)
        assert "Rows aren't kept" in html.unescape(page.body)
        done = fetch(browser.url + "settings/keep-rows", method="POST",
                     form={"form_token": form_token(page), "keep_rows": "on"})
        after = fetch(origin_of(browser.url) + done.headers["location"])
        pulled = await h.call("query", source="shop", question="?", sql="SELECT name FROM customers")
    assert done.status == 303
    assert "Rows of new pulls are kept" in after.body
    assert KEY_ACCOUNT in keychain.entries  # the key was made first, so a keychain problem shows on the page
    assert pulled.ai.structured["pull_id"] in {r["pull_id"] for r in h.stored("kept_rows")}


async def test_the_page_lists_sources_without_their_secrets(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        page = fetch(browser.url)
    settings = page.body.split('id="settings"', 1)[1]
    assert "shop" in settings and "sqlite" in settings and str(shop_db) in settings
    assert str(home) in settings  # where the local file lives


async def test_the_page_shows_what_the_ai_was_sent_in_each_conversation(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser, mode="results") as h:
        await h.call("query", source="shop", question="Names?", sql="SELECT name FROM customers ORDER BY name")
        await h.call("open_page")
        page = fetch(browser.url)
        link = re.search(r'href="(/[^"]+/sent/[^"]+)"', page.body)
        assert link is not None, "each conversation links to what was sent in it"
        sent = fetch(origin_of(browser.url) + link.group(1).replace("&amp;", "&"))
    assert sent.status == 200
    assert "query" in sent.body and "Ada" in sent.body and "Linus" in sent.body  # the preview the AI got
    assert "open_page" in sent.body


async def test_an_unknown_conversation_is_not_found(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        missing = fetch(browser.url + "sent/nope")
    assert missing.status == 404


async def test_the_page_links_to_teams_and_promotes_nothing_else(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        page = fetch(browser.url)
    outside = re.findall(r'href="(https?://[^"]+)"', page.body)
    assert outside == [TEAM_URL]
    assert "Need this for your team?" in page.body
    for nag in ("upgrade", "trial", "pricing", "personal plan", "limit"):
        assert nag not in page.body.lower()


class Recorder:
    """Stands in for the usage-count endpoint (see test_usage.py); nothing touches the network."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, payload: dict) -> bool:
        self.sent.append(payload)
        return True


async def test_with_usage_counts_on_the_team_link_is_counted(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser, usage_sender=Recorder()) as h:  # on after the first reply
        await h.call("open_page")
        page = fetch(browser.url)
    assert re.findall(r'href="(https?://[^"]+)"', page.body) == [TEAM_URL_COUNTED]


async def test_the_user_turns_usage_counts_off_from_the_page(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser, usage_sender=Recorder()) as h:
        await h.call("open_page")
        page = fetch(browser.url)
        assert "Anonymous usage counts are on" in html.unescape(page.body)
        done = fetch(browser.url + "settings/usage-counts", method="POST",
                     form={"form_token": form_token(page), "usage_counts": "off"})
        after = fetch(origin_of(browser.url) + done.headers["location"])
        status = await h.call("usage_counts")
    assert done.status == 303
    assert "Anonymous usage counts are off" in html.unescape(after.body)
    assert status.ai.structured["usage_counts"] == "off"
    assert re.findall(r'href="(https?://[^"]+)"', after.body) == [TEAM_URL]


async def test_opening_the_page_is_where_the_user_first_sees_the_usage_question(home: Path) -> None:
    browser = Browser()
    async with Harness.start(home, browser=browser, usage_sender=Recorder()) as h:
        before = await h.call("usage_counts")
        await h.call("open_page")
        page = fetch(browser.url)
        after = await h.call("usage_counts")
    assert before.ai.structured["usage_counts"] == "not asked yet"
    text = html.unescape(page.body)
    assert "Portward shares anonymous usage counts" in text and "Turn usage counts off" in text
    assert after.ai.structured["usage_counts"] == "on"


async def test_usage_counts_turned_off_for_good_have_no_switch_on_the_page(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:  # the harness's default: off by the environment
        await h.call("open_page")
        page = fetch(browser.url)
    assert "settings/usage-counts" not in page.body
    assert "Anonymous usage counts are off on this computer" in html.unescape(page.body)


def test_the_terminal_command_serves_the_page_and_prints_its_address(home: Path) -> None:
    """`portward-free page`, for when the AI app can't open a browser. The address is printed in the user's own
    terminal; BROWSER=true stands in for the browser so the test opens nothing."""
    env = server_env(**{HOME_ENV: str(home), "BROWSER": "true", "PYTHONUNBUFFERED": "1"})
    process = subprocess.Popen([sys.executable, "-m", "portward_free", "page"], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        line = ""
        while "http://" not in line:
            line = process.stdout.readline()
            assert line, process.stderr.read() if process.stderr else "no address printed"
        url = re.search(r"http://\S+", line).group(0)
        page = fetch(url)
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert urllib.parse.urlsplit(url).hostname == "127.0.0.1"
    assert page.status == 200 and "What Portward did here" in page.body


async def test_values_from_the_data_are_escaped_on_the_page(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("query", source="shop", question="<script>alert(1)</script>", sql="SELECT name FROM customers")
        await h.call("open_page")
        page = fetch(browser.url)
    assert "<script>" not in page.body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.body


def shared_part(page: Answer) -> str:
    return page.body.split('id="shares"', 1)[1].split("</section>", 1)[0]


async def test_the_page_shows_each_share_with_its_recipient_time_pull_and_expiry(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        pulled = await h.call("query", source="shop", question="Revenue by region", sql="SELECT region FROM customers")
        pull_id = pulled.ai.structured["pull_id"]
        store = LocalStore.open(database_path(home))
        try:
            store.record_share(ShareRecord("shareAAAAAAAAAAAAAAAA", pull_id, "teammate@acme.example",
                                           "2026-11-02T09:15:00+00:00", "2099-11-09T09:15:00+00:00",
                                           ("region",), 3))
            store.record_share(ShareRecord("shareBBBBBBBBBBBBBBBB", None, "old@acme.example",
                                           "2026-10-01T08:00:00+00:00", "2026-10-08T08:00:00+00:00", ("name",), 1))
        finally:
            store.close()
        await h.call("open_page")
        page = fetch(browser.url)
    assert 'href="#shares"' in page.body  # in the tally that doubles as the menu
    shared = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", shared_part(page))).split())  # as the user reads it
    first, second = shared.split("old@acme.example", 1)
    assert "teammate@acme.example" in first  # newest first
    assert "2026-11-02 09:15 UTC teammate@acme.example Revenue by region" in first
    assert "Expires 2099-11-09 09:15 UTC" in first and "3 rows" in first
    assert "Expired 2026-10-08 08:00 UTC" in second and "1 row" in second
    assert "no longer in history" in second  # a share whose pull is no longer in history
    assert "/s/" not in shared_part(page)  # the link (and its key) is never kept, so never shown


async def test_the_page_says_nothing_was_shared_yet(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("open_page")
        page = fetch(browser.url)
    assert "Nothing shared yet" in shared_part(page)


async def test_a_recipient_is_escaped_on_the_page(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        store = LocalStore.open(database_path(home))
        try:
            store.record_share(ShareRecord("shareCCCCCCCCCCCCCCCC", None, "<b>x</b>@acme.example",
                                           "2026-11-02T09:15:00+00:00", "2099-11-09T09:15:00+00:00", (), 0))
        finally:
            store.close()
        await h.call("open_page")
        page = fetch(browser.url)
    assert "<b>x</b>" not in page.body and "&lt;b&gt;x&lt;/b&gt;@acme.example" in shared_part(page)
