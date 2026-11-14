"""Notes on the local Portward page (#119): the user writes and edits what tables and columns mean without asking
the AI. A note saved there reaches the AI with the schema, as notes do. Driven through the MCP client harness and
the page's own form, as a browser would submit it."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from harness import Harness, MemoryKeychain, connected
from test_local_page import Answer, Browser, fetch, origin_of
from test_profiles import PlanAccounts

from portward_free.account import Accounts
from portward_free.page import PortwardPage
from portward_free.paths import database_path
from portward_free.usage import Usage

REGION_NOTE = "north means the Leeds depot, south the Bristol one"


def field(table: str, column: str = "") -> str:
    return "n:" + json.dumps([table, column])


def form_of(page: Answer) -> dict[str, str]:
    """What a browser sends from the notes form as it stands: hidden fields and every textarea's text."""
    fields = {html.unescape(n): html.unescape(v)
              for n, v in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page.body)}
    for name, text in re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', page.body, re.S):
        fields[html.unescape(name)] = html.unescape(text)
    return fields


async def open_notes(h: Harness, browser: Browser) -> tuple[str, Answer]:
    await h.call("open_page")
    overview = fetch(browser.url)
    link = re.search(r'href="([^"]*/notes\?source=shop)"', overview.body)
    assert link is not None, "the overview links to the source's notes"
    url = origin_of(browser.url) + html.unescape(link.group(1))
    return url, fetch(url)


def save(url: str, page: Answer, **changes: str) -> Answer:
    sent = {**form_of(page), **changes}
    done = fetch(url.split("?")[0], method="POST", form=sent)
    return fetch(origin_of(url) + done.headers["location"]) if done.status == 303 else done


def tables_listed(listed) -> dict[str, dict]:
    return {t["name"]: t for t in listed.ai.structured["sources"][0]["tables"]}


async def test_notes_written_on_the_page_reach_the_ai_with_the_schema(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        url, page = await open_notes(h, browser)
        after = save(url, page, **{field("customers", "region"): REGION_NOTE, field("orders"): "One row per order"})
        listed = await h.call("list_sources")
    assert "Notes saved" in after.body
    tables = tables_listed(listed)
    assert tables["customers"]["column_notes"] == {"region": REGION_NOTE}
    assert tables["orders"]["note"] == "One row per order"


async def test_the_page_shows_a_note_to_edit_and_an_emptied_note_is_removed(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("set_note", source="shop", table="customers", column="region", note=REGION_NOTE)
        await h.call("set_note", source="shop", table="orders", column="total", note="In GBP")
        url, page = await open_notes(h, browser)
        shown = form_of(page)
        save(url, page, **{field("customers", "region"): "north is Leeds", field("orders", "total"): ""})
        listed = await h.call("list_sources")
    assert shown[field("customers", "region")] == REGION_NOTE
    tables = tables_listed(listed)
    assert tables["customers"]["column_notes"] == {"region": "north is Leeds"}
    assert "column_notes" not in tables["orders"]


async def test_saving_leaves_notes_the_user_didnt_touch_as_they_are_now(home: Path, shop_db: Path) -> None:
    """A note the AI changed after the page was opened isn't put back to what the page showed."""
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("set_note", source="shop", table="orders", column="total", note="In GBP")
        url, page = await open_notes(h, browser)
        await h.call("set_note", source="shop", table="orders", column="total", note="In GBP, after discounts")
        save(url, page, **{field("customers", "region"): REGION_NOTE})
        listed = await h.call("list_sources")
    assert tables_listed(listed)["orders"]["column_notes"] == {"total": "In GBP, after discounts"}


async def test_the_page_says_which_columns_are_kept_from_claude(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("block", source="shop", table="customers", column="name")
        _, page = await open_notes(h, browser)
    row = re.search(r"customers\.name.{0,600}", page.body, re.S)
    assert row is not None and "Kept from Claude" in row.group(0)


async def test_notes_on_the_page_are_escaped(home: Path, shop_db: Path) -> None:
    browser = Browser()
    async with connected(home, shop_db, browser=browser) as h:
        await h.call("set_note", source="shop", table="orders", note="</textarea><script>x</script>")
        _, page = await open_notes(h, browser)
    assert "<script>x" not in page.body
    assert form_of(page)[field("orders")] == "</textarea><script>x</script>"


async def test_a_page_served_for_the_web_routes_cannot_change_notes(home: Path, shop_db: Path) -> None:
    from portward_free.localweb import LocalWeb
    from portward_free.runtime import opener_for

    async with connected(home, shop_db) as h:
        await h.call("set_note", source="shop", table="orders", note="One row per order")
    keychain = MemoryKeychain()
    page_app = PortwardPage(database_path(home), keychain, opener_for(keychain), Usage.from_env,
                            accounts=Accounts(PlanAccounts("active")), profiles_editable=False)
    web = LocalWeb(page_app).start()
    try:
        url = web.url("/notes?source=shop")
        page = fetch(url)
        refused = fetch(web.url("/notes"), method="POST",
                        form={"form_token": web.form_token, "source": "shop", field("orders"): "changed"})
    finally:
        web.stop()
        page_app.close()
    assert "One row per order" in page.body and "<textarea" not in page.body
    assert "only from the Portward page on this computer" in refused.body
    async with connected(home, shop_db) as h:
        listed = await h.call("list_sources")
    assert tables_listed(listed)["orders"]["note"] == "One row per order"
