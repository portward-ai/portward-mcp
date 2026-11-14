"""The context editor on the local Portward page (#127, Personal): a review queue for the AI's drafts (approve, edit,
skip or discard, one at a time), tabs for definitions, links, a dictionary import and what the AI assumed, and a read
view of everything the AI is told. Every decision made in one sitting stays in the page's form until Save, which
publishes one profile version. Driven through the MCP client harness and the page's own forms, as a browser would
submit them."""

from __future__ import annotations

import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

from harness import Harness
from test_context import draft_meaning, table_entry
from test_local_page import Answer, Browser, fetch
from test_profile_editor import follow, origin, personal, stored
from test_profiles import apply, make, seen

SAVE = {"action": "save"}


class ContextForm(HTMLParser):
    """The fields a browser would send from the context editor's form: hidden, text and search inputs, textareas,
    checked radios and checkboxes, and the chosen option of each select."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.action = ""
        self._in_form = False
        self._select: str | None = None
        self._textarea: str | None = None
        self._first_option: dict[str, str] = {}
        self.feed(html)
        for name, value in self._first_option.items():
            self.fields.setdefault(name, value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form" and a.get("action", "").endswith("/context") and a.get("method") == "post":
            self._in_form, self.action = True, a["action"]
        if not self._in_form:
            return
        if tag == "input" and a.get("name"):
            kind = a.get("type", "text")
            if kind in ("hidden", "text", "search") or (kind in ("radio", "checkbox") and "checked" in a):
                self.fields[a["name"]] = a.get("value", "on")
        elif tag == "textarea" and a.get("name"):
            self._textarea = a["name"]
            self.fields[self._textarea] = ""
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select:
            self._first_option.setdefault(self._select, a.get("value", ""))
            if "selected" in a:
                self.fields[self._select] = a.get("value", "")

    def handle_data(self, data: str) -> None:
        if self._textarea:
            self.fields[self._textarea] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            if self._textarea and self.fields[self._textarea].startswith("\n"):
                self.fields[self._textarea] = self.fields[self._textarea][1:]
            self._textarea = None


def press(url: str, page: Answer, button: dict[str, str], **changes: str) -> Answer:
    """Submit the context editor on `page` as a browser would, with `changes` made and `button` pressed."""
    form = ContextForm(page.body)
    assert form.action, "no context editor form on the page"
    sent = {k: v for k, v in {**form.fields, **changes, **button}.items() if v is not None}
    return fetch(origin(url) + form.action, method="POST", form=sent)


async def open_context(h: Harness, browser: Browser, profile: str = "Sales") -> tuple[str, Answer]:
    """The context editor for `profile`, as linked from the profiles page."""
    await h.call("open_page")
    listed = fetch(browser.url + "profiles")
    found = re.search(r'href="([^"]*/context\?[^"]*)"[^>]*>[^<]*' + re.escape(profile), listed.body)
    assert found, "no context link for the profile on the profiles page"
    url = origin(browser.url) + found.group(1).replace("&amp;", "&")
    return url, fetch(url)


def current(page: Answer) -> str:
    """The id of the draft the queue shows now."""
    return ContextForm(page.body).fields["draft"]


# --- the review queue -----------------------------------------------------------------------------------------------

async def test_approvals_in_one_sitting_publish_one_version_and_nothing_reaches_the_ai_before(
        home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name", "orders.total"])
        await apply(h, "Sales")
        first = await draft_meaning(h, "Sales", "customers", "The customer's legal name", column="name")
        second = await draft_meaning(h, "Sales", "orders", "What the order cost", column="total")
        url, page = await open_context(h, browser)
        assert page.status == 200 and "The customer&#x27;s legal name" in page.body
        assert current(page) == first
        one = press(url, page, {"action": "approve"})
        assert current(one) == second
        two = press(url, one, {"action": "approve"}, text="What the order cost, in pounds")
        assert "2 changes since v1" in two.body
        before = await h.call("list_sources")  # approved in the page, not saved: the AI gets nothing yet
        assert stored(home, "Sales")[0].version == 1
        saved = follow(url, press(url, two, SAVE))
        after = await h.call("list_sources")
        waiting = await h.call("context_drafts")
    assert "legal name" not in seen(before) and "in pounds" not in seen(before)
    assert "Saved as v2" in saved.body
    profile = stored(home, "Sales")[0]
    assert profile.version == 2  # two approvals, one version
    assert table_entry(after, "customers")["column_notes"] == {"name": "The customer's legal name"}
    assert table_entry(after, "orders")["column_notes"] == {"total": "What the order cost, in pounds"}
    assert waiting.ai.structured["drafts"] == []


async def test_skipped_drafts_wait_discarded_ones_go_and_neither_reaches_the_ai(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name", "orders.total", "orders.id"])
        await apply(h, "Sales")
        skipped = await draft_meaning(h, "Sales", "customers", "Skipped words", column="name")
        await draft_meaning(h, "Sales", "orders", "Discarded words", column="total")
        await draft_meaning(h, "Sales", "orders", "One row per order", column="id")
        url, page = await open_context(h, browser)
        page = press(url, page, {"action": "skip"})
        page = press(url, page, {"action": "discard"})
        assert "1 draft to discard" in page.body
        page = press(url, page, {"action": "approve"})
        follow(url, press(url, page, SAVE))
        listed = await h.call("list_sources")
        waiting = await h.call("context_drafts")
    assert [d["id"] for d in waiting.ai.structured["drafts"]] == [skipped]
    assert "Skipped words" not in seen(listed) and "Discarded words" not in seen(listed)
    assert table_entry(listed, "orders")["column_notes"] == {"id": "One row per order"}
    assert stored(home, "Sales")[0].version == 2


async def test_a_draft_naming_something_the_profile_hides_cant_be_approved_until_reworded(
        home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        await draft_meaning(h, "Sales", "customers", "Legal name, matches customers.ssn on file", column="name")
        url, page = await open_context(h, browser)
        refused = press(url, page, {"action": "approve"})
        assert "can&#x27;t be approved yet" in refused.body and "customers.ssn" in refused.body
        assert "No changes since v1" in refused.body
        approved = press(url, refused, {"action": "approve"}, text="Legal name, as invoiced")
        follow(url, press(url, approved, SAVE))
        listed = await h.call("list_sources")
    assert table_entry(listed, "customers")["column_notes"] == {"name": "Legal name, as invoiced"}


async def test_a_queue_item_can_be_reopened_and_changed_before_saving(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        draft = await draft_meaning(h, "Sales", "customers", "The name", column="name")
        url, page = await open_context(h, browser)
        page = press(url, page, {"action": "approve"})
        assert "Every draft has a decision" in page.body
        page = press(url, page, {"show": draft})
        page = press(url, page, {"action": "skip"})
        assert "No changes since v1" in page.body
    assert stored(home, "Sales")[0].version == 1


# --- held back ------------------------------------------------------------------------------------------------------

async def test_context_naming_a_column_thats_gone_shows_as_held_back_with_its_fix(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["orders.total", "orders.id"])
        await apply(h, "Sales")
        await h.call("draft_definition", profile="Sales", term="Revenue", text="Sum of orders.total")
        url, page = await open_context(h, browser)
        follow(url, press(url, press(url, page, {"action": "approve"}), SAVE))
        with sqlite3.connect(people_db) as db:
            db.execute("ALTER TABLE orders RENAME COLUMN total TO total_paid")
        db.close()
        told = fetch(url + "&tab=told")
        defs = fetch(url + "&tab=definitions")
    assert "Held back from the AI" in told.body and "definition Revenue" in told.body
    assert "renamed to total_paid" in told.body  # the fix
    assert "Held back" in defs.body and "renamed to total_paid" in defs.body


# --- definitions, links, import, assumptions ------------------------------------------------------------------------

async def test_definitions_and_links_added_changed_and_removed_in_one_sitting_publish_one_version(
        home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.id", "customers.name", "orders.customer_id", "orders.total"])
        await apply(h, "Sales")
        await h.call("draft_definition", profile="Sales", term="Old term", text="Something about orders.total")
        url, page = await open_context(h, browser)
        follow(url, press(url, press(url, page, {"action": "approve"}), SAVE))  # v2 has "Old term"
        page = press(url, fetch(url), {"to_tab": "definitions"})
        page = press(url, page, {"action": "define"}, def_term="Big spender", def_text="orders.total over 15")
        refused = press(url, page, {"action": "define"}, def_term="Secret", def_text="Uses customers.ssn")
        assert "Secret can&#x27;t be added yet" in refused.body
        page = press(url, refused, {"remove_def": "old term"})
        assert "Will be removed" in page.body
        page = press(url, page, {"to_tab": "links"})
        page = press(url, page, {"action": "add_link"}, link_one="orders.customer_id", link_other="customers.id")
        assert "3 changes since v2" in page.body
        follow(url, press(url, page, SAVE))
        described = await h.call("describe_source", source="people")
    profile = stored(home, "Sales")[0]
    assert profile.version == 3
    assert [d.term for d in profile.context.definitions] == ["Big spender"]
    assert [k.about for k in profile.context.links] == ["orders.customer_id = customers.id"]
    assert "Big spender" in seen(described) and "Old term" not in seen(described)
    assert "JOIN customers ON orders.customer_id = customers.id" in seen(described)


async def test_an_assumption_becomes_a_definition_when_the_sitting_is_saved(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["orders.total"])
        await apply(h, "Sales")
        asked = await h.call("query", source="people", question="what is a big order?",
                             sql="SELECT total FROM orders", assumptions=["Big order means total over 15"])
        assert not asked.is_error, asked.ai.text
        url, page = await open_context(h, browser)
        page = press(url, page, {"to_tab": "assumed"})
        (assumption,) = re.findall(r'name="assume" value="([^"]+)"', page.body)
        page = press(url, page, {"assume": assumption})
        page = press(url, page, {"action": "define"}, def_term="Big order")
        assert "Becomes Big order" in press(url, page, {"to_tab": "assumed"}).body
        follow(url, press(url, page, SAVE))
        review = await h.call("assumptions")
    profile = stored(home, "Sales")[0]
    assert [(d.term, d.text) for d in profile.context.definitions] == [("Big order", "Big order means total over 15")]
    assert review.ai.structured["assumptions"][0]["defined_as"] == "Big order"


async def test_a_dictionary_file_is_mapped_and_taken_into_the_sitting(home: Path, people_db: Path,
                                                                      tmp_path: Path) -> None:
    dictionary = tmp_path / "dictionary.csv"
    dictionary.write_text("table,column,description\ncustomers,name,Full legal name\n"
                          "customers,ssn,Tax number\norders,total,Order value in GBP\nMisc,Fee,Courier charge\n")
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name", "orders.total"])
        await apply(h, "Sales")
        url, page = await open_context(h, browser)
        page = press(url, page, {"to_tab": "import"})
        read = press(url, page, {"action": "read_file"}, dict_path=str(dictionary))
        assert "Full legal name" in read.body and "Matched nothing granted" in read.body
        taken = press(url, read, {"action": "import"}, **{"keep:m1": None})  # untick the second matched row
        assert "1 meaning from dictionary.csv" in taken.body
        follow(url, press(url, taken, SAVE))
        listed = await h.call("list_sources")
    assert table_entry(listed, "customers")["column_notes"] == {"name": "Full legal name"}
    assert "Order value" not in seen(listed) and "Tax number" not in seen(listed)
    assert stored(home, "Sales")[0].version == 2


# --- the read view --------------------------------------------------------------------------------------------------

async def test_the_read_view_shows_what_the_ai_is_told_and_never_a_draft(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name", "orders.total"])
        await apply(h, "Sales")
        await draft_meaning(h, "Sales", "customers", "Approved words", column="name")
        await draft_meaning(h, "Sales", "orders", "Waiting words", column="total")
        url, page = await open_context(h, browser)
        page = press(url, page, {"action": "approve"})
        follow(url, press(url, page, SAVE))
        told = fetch(url + "&tab=told")
    view = told.body.split('class="panel"', 1)[1]
    assert "On <span class=\"id\">people</span>" in view
    assert "Approved words" in view and "Sales v2" in view
    assert "Waiting words" not in view
    assert "ssn" not in view  # a hidden column is never listed as told


# --- who can save ---------------------------------------------------------------------------------------------------

async def test_a_page_served_for_the_web_routes_cannot_change_context(home: Path, people_db: Path) -> None:
    from harness import MemoryKeychain
    from test_profiles import PlanAccounts

    from portward_free.account import Accounts
    from portward_free.localweb import LocalWeb
    from portward_free.page import PortwardPage
    from portward_free.paths import database_path
    from portward_free.runtime import opener_for
    from portward_free.usage import Usage

    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        await draft_meaning(h, "Sales", "customers", "The name", column="name")
    keychain = MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    page_app = PortwardPage(database_path(home), keychain, opener_for(keychain), Usage.from_env,
                            accounts=Accounts(PlanAccounts("active")), profiles_editable=False)
    web = LocalWeb(page_app).start()
    try:
        url = web.url("/context")
        page = press(url, fetch(url), {"action": "approve"})
        refused = press(url, page, SAVE)
    finally:
        web.stop()
        page_app.close()
    assert "only from the Portward page on this computer" in refused.body
    assert 'value="save"' not in refused.body
    assert stored(home, "Sales")[0].version == 1


async def test_without_personal_nothing_is_saved(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        await draft_meaning(h, "Sales", "customers", "The name", column="name")
    browser2 = Browser()
    async with personal(home, people_db, plan="ended", browser=browser2) as h:
        url, page = await open_context(h, browser2)
        refused = press(url, press(url, page, {"action": "approve"}), SAVE)
    assert "frozen" in refused.body.lower()
    assert stored(home, "Sales")[0].version == 1


async def test_a_profile_changed_since_the_page_opened_is_not_overwritten(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        await draft_meaning(h, "Sales", "customers", "The name", column="name")
        url, page = await open_context(h, browser)
        page = press(url, page, {"action": "approve"})
        await h.call("update_profile", profile="Sales", add=["orders.total"])
        refused = press(url, page, SAVE)
    assert "changed since you opened it" in refused.body
    assert stored(home, "Sales")[0].version == 2


async def test_the_editor_escapes_what_it_shows(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.name"])
        await draft_meaning(h, "Sales", "customers", "<script>alert(1)</script>", column="name")
        url, page = await open_context(h, browser)
        shown = press(url, page, {"to_tab": "definitions"}, def_term="<b>x</b>")
    assert "<script>alert" not in page.body and "<b>x</b>" not in shown.body
