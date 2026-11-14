"""The profile editor on the local Portward page (#126, Personal): one level control per column, the match preview
beside it, and sample rows as the AI would get them. A save publishes one version, whatever was changed in that
sitting; letting the AI see more through an applied profile asks first, on a page only the user reaches. Driven
through the MCP client harness and the page's own forms, as a browser would submit them."""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path

from harness import Harness, MemoryKeychain, connected
from test_context import Reviewer
from test_local_page import Answer, Browser, fetch
from test_profiles import PlanAccounts, apply, make

from portward_free.account import Accounts
from portward_free.page import PortwardPage
from portward_free.paths import database_path
from portward_free.storage import LocalStore
from portward_free.usage import Usage

SECRETS = ("123-45-6789", "987-65-4321", "555-12-3456")
NAMES = ("Ada", "Grace", "Linus")


class EditorForm(HTMLParser):
    """The fields a browser would send from the page's profile editor form: hidden and text inputs, the checked radio
    of each group and the chosen option of each select."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.action = ""
        self._in_form = False
        self._select: str | None = None
        self._first_option: dict[str, str] = {}
        self.feed(html)
        for name, value in self._first_option.items():
            self.fields.setdefault(name, value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form" and a.get("action", "").endswith("/profile"):
            self._in_form, self.action = True, a["action"]
        if not self._in_form:
            return
        if tag == "input" and a.get("name"):
            kind = a.get("type", "text")
            if kind in ("hidden", "text", "search") or (kind == "radio" and "checked" in a):
                self.fields[a["name"]] = a.get("value", "")
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select:
            self._first_option.setdefault(self._select, a.get("value", ""))
            if "selected" in a:
                self.fields[self._select] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False
        elif tag == "select":
            self._select = None


def origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def submit(url: str, page: Answer, button: dict[str, str], **changes: str) -> Answer:
    """Submit the editor on `page` as a browser would, with `changes` made to its fields and `button` pressed."""
    form = EditorForm(page.body)
    assert form.action, "no editor form on the page"
    return fetch(origin(url) + form.action, method="POST", form={**form.fields, **changes, **button})


PREVIEW, SAVE = {"action": "preview"}, {"action": "save"}


def level(name: str) -> str:
    return f"g:{name}"


def editor_url(base: str, page: Answer, profile: str) -> str:
    """The editor's address for `profile`, as linked from the profiles page."""
    found = re.search(r'href="([^"]*/profile\?[^"]*)"[^>]*>' + re.escape(profile) + "<", page.body)
    assert found, f"no link to {profile} on the profiles page"
    return origin(base) + found.group(1).replace("&amp;", "&")


def follow(base: str, answer: Answer) -> Answer:
    assert answer.status == 303, (answer.status, answer.body[:500])
    return fetch(origin(base) + answer.headers["location"])


@asynccontextmanager
async def personal(home: Path, db: Path, *, plan: str = "active", browser: Browser,
                   reviewer: Reviewer | None = None) -> AsyncIterator[Harness]:
    keychain = MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    async with connected(home, db, name="people", keychain=keychain, elicitation=reviewer or Reviewer(),
                         accounts=Accounts(PlanAccounts(plan)), browser=browser) as h:
        yield h


def stored(home: Path, name: str):
    store = LocalStore.open(database_path(home))
    try:
        return store.profiles.named(name), store.rules_for("people")
    finally:
        store.close()


async def open_editor(h: Harness, browser: Browser, profile: str = "Sales") -> tuple[str, Answer]:
    await h.call("open_page")
    listed = fetch(browser.url + "profiles")
    assert listed.status == 200
    url = editor_url(browser.url, listed, profile)
    return url, fetch(url)


# --- the editor -----------------------------------------------------------------------------------------------------

async def test_the_editor_shows_a_level_control_per_column_and_the_match_preview(home: Path,
                                                                                  people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name", "orders.total", "customers.loyalty"])
        await apply(h, "Sales")
        _, page = await open_editor(h, browser)
    assert page.status == 200
    form = EditorForm(page.body)
    assert form.fields[level("customers.name")] == "full"
    assert form.fields[level("customers.ssn")] == "hidden"  # every column of a granted table has a control
    assert form.fields[level("orders.total")] == "full"
    assert "Not in your data" in page.body and "loyalty" in page.body
    assert re.search(r'class="n">2</span>\s*<span class="w">matched', page.body)


async def test_previewing_changes_saves_nothing_and_a_save_publishes_one_version(home: Path,
                                                                                 people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name", "orders.total"])
        url, page = await open_editor(h, browser)
        changes = {level("customers.name"): "anonymised", level("customers.id"): "full",
                   level("orders.total"): "hidden", "description": "Who buys"}
        previewed = submit(url, page, PREVIEW, **changes)
        assert previewed.status == 200 and "4 changes since v1" in previewed.body
        assert stored(home, "Sales")[0].version == 1
        saved = follow(url, submit(url, previewed, SAVE))
    profile, _ = stored(home, "Sales")
    assert profile.version == 2  # three grant changes and a description: one version
    assert {(g.name, g.level) for g in profile.grants} == {("customers.name", "anonymised"), ("customers.id", "full")}
    assert profile.description == "Who buys"
    assert "Saved as v2" in saved.body


async def test_a_change_in_the_editor_is_enforced_exactly_as_the_same_change_by_tool(tmp_path: Path,
                                                                                     people_db: Path) -> None:
    by_tool, by_page = tmp_path / "tool", tmp_path / "page"
    async with personal(by_tool, people_db, browser=Browser()) as h:
        await make(h, "Sales", ["customers.name", "orders.total", "orders.id"])
        await apply(h, "Sales")
        changed = await h.call("update_profile", profile="Sales", anonymised=["customers.name"],
                               totals=["orders.total"], add=["customers.id"], remove=["orders.id"])
        assert not changed.is_error, changed.ai.text
    browser = Browser()
    async with personal(by_page, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name", "orders.total", "orders.id"])
        await apply(h, "Sales")
        url, page = await open_editor(h, browser)
        submit(url, page, SAVE, **{level("customers.name"): "anonymised", level("orders.total"): "totals",
                                   level("customers.id"): "full", level("orders.id"): "hidden",
                                   "confirm": "yes"})
        seen = await h.call("query", source="people", question="q", sql="SELECT * FROM customers")
    tool_profile, tool_rules = stored(by_tool, "Sales")
    page_profile, page_rules = stored(by_page, "Sales")
    assert page_profile.version == tool_profile.version == 2
    assert page_profile.grants == tool_profile.grants
    assert page_rules == tool_rules  # what the AI may see of the connection is the same object
    assert not any(s in seen.ai.text for s in (*SECRETS, *NAMES))


async def test_sample_rows_show_stand_ins_and_totals_as_the_ai_gets_them(home: Path, tmp_path: Path) -> None:
    db = tmp_path / "big.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, ssn TEXT, spend REAL)")
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)",
                         [(i, f"Person{i:02d}", f"SSN-{i:04d}", float(i * 10)) for i in range(1, 13)])
    conn.close()
    browser = Browser()
    async with personal(home, db, browser=browser) as h:
        await make(h, "Sales", ["customers.id"], anonymised=["customers.name"], totals=["customers.spend"])
        await apply(h, "Sales")
        url, page = await open_editor(h, browser)
        opened = submit(url, page, {"open": "customers"})
    sample = opened.body.split('id="sample"', 1)[1]
    assert "anon_" in sample  # the AI gets stand-ins for names
    assert "Person0" not in sample and "SSN-" not in sample  # never the real names, never a hidden column
    assert "780" in sample  # the total of spend, as the AI would get it
    assert "120.0" not in sample  # never one row's spend


async def test_loosening_an_applied_profile_asks_first_and_no_keeps_it(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        url, page = await open_editor(h, browser)
        asked = submit(url, page, SAVE, **{level("customers.ssn"): "full"})
        assert asked.status == 200 and "Let the AI see customers.ssn" in asked.body
        assert stored(home, "Sales")[0].version == 1
        kept = submit(url, asked, {"action": "preview"})  # "No, keep editing"
        assert stored(home, "Sales")[0].version == 1 and EditorForm(kept.body).fields[level("customers.ssn")] == "full"
        follow(url, submit(url, asked, SAVE, confirm="yes"))
    assert ("customers.ssn", "full") in {(g.name, g.level) for g in stored(home, "Sales")[0].grants}


async def test_narrowing_saves_at_once(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name", "customers.id"])
        await apply(h, "Sales")
        url, page = await open_editor(h, browser)
        follow(url, submit(url, page, SAVE, **{level("customers.name"): "anonymised", level("customers.id"): "hidden"}))
    assert {(g.name, g.level) for g in stored(home, "Sales")[0].grants} == {("customers.name", "anonymised")}


async def test_the_editor_names_a_tables_person_column_for_totals(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["orders.customer_id"], totals=["orders.total"])
        await apply(h, "Sales")
        url, page = await open_editor(h, browser)
        follow(url, submit(url, page, SAVE, **{"p:orders": "customer_id"}))
        profile, rules = stored(home, "Sales")
        assert profile.version == 2
        assert [m.person for m in profile.context.meanings if m.table == "orders"] == ["customer_id"]
        refused = submit(url, fetch(url), SAVE, **{"p:orders": "id"})  # not granted
    assert "isn&#x27;t granted" in refused.body
    assert stored(home, "Sales")[0].version == 2


async def test_without_personal_the_editor_saves_nothing(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
    # the plan ends: the next server sees it, and the page's save is refused
    browser2 = Browser()
    async with personal(home, people_db, plan="ended", browser=browser2) as h:
        url, page = await open_editor(h, browser2)
        refused = submit(url, page, SAVE, **{level("customers.id"): "full"})
    assert refused.status == 200 and "frozen" in refused.body.lower()
    assert stored(home, "Sales")[0].version == 1


async def test_a_profile_changed_since_the_editor_opened_is_not_overwritten(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        url, page = await open_editor(h, browser)
        await h.call("update_profile", profile="Sales", add=["orders.total"])
        refused = submit(url, page, SAVE, **{level("customers.id"): "full"})
    assert "changed since" in refused.body and stored(home, "Sales")[0].version == 2


async def test_a_page_served_for_the_web_routes_cannot_change_profiles(home: Path, people_db: Path) -> None:
    from portward_free.localweb import LocalWeb
    from portward_free.runtime import opener_for

    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
    keychain = MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    page_app = PortwardPage(database_path(home), keychain, opener_for(keychain), Usage.from_env,
                            accounts=Accounts(PlanAccounts("active")), profiles_editable=False)
    web = LocalWeb(page_app).start()
    try:
        listed = fetch(web.url("/profiles"))
        url = editor_url(web.url(), listed, "Sales")
        refused = submit(url, fetch(url), SAVE, **{level("customers.id"): "full"})
        made = fetch(web.url("/profiles/new"), method="POST",
                     form={"form_token": web.form_token, "name": "Other", "description": ""})
    finally:
        web.stop()
        page_app.close()
    assert "only from the Portward page on this computer" in refused.body
    assert made.status in (200, 303) and stored(home, "Other")[0] is None
    assert stored(home, "Sales")[0].version == 1


async def test_the_editor_escapes_what_it_shows(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        url, page = await open_editor(h, browser)
        shown = submit(url, page, PREVIEW, description="<script>x</script>")
    assert "<script>x" not in shown.body
