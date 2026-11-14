"""Free's setup page (#118): connecting a database or file is one local page in three steps (where the data is,
check the login, then choose the mode and the columns to keep from Claude). Columns whose names look personal are
pre-ticked and shown; nothing is kept, stored or sent until the user confirms. Driven through the MCP client harness
and the page's own forms, as a browser would submit them."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from harness import Harness, MemoryKeychain, connected
from setup_form import SetupForm, submit
from setup_form import request as fetch
from test_local_page import Browser

from portward_free.paths import database_path
from portward_free.storage import LocalStore
from portward_free.suggested_keeps import looks_personal


@pytest.fixture
def people_db(tmp_path: Path) -> Path:
    path = tmp_path / "people.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, full_name TEXT, email TEXT, phone TEXT, "
                   "city TEXT, date_of_birth TEXT)")
        db.execute("INSERT INTO customers VALUES (1, 'Ada', 'ada@mail.example', '555-0100', 'Leeds', '1990-01-02')")
        db.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY, salary REAL, home_address TEXT, team TEXT)")
        db.execute("INSERT INTO staff VALUES (1, 50000, '1 High St', 'ops')")
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL)")
        db.execute("INSERT INTO orders VALUES (1, 1, 9.5)")
    db.close()
    return path


def stored(home: Path) -> tuple[list[str], list[tuple[str, str, str | None]], dict[str, str]]:
    """The connections, block rules and modes in the local file, read straight from disk."""
    store = LocalStore.open(database_path(home))
    try:
        names = [c.name for c in store.connections()]
        rules = sorted((r.source, r.table, r.column) for n in names for r in store.rules(n))
        return names, rules, store.send_modes()
    finally:
        store.close()


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- suggested keeps ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["email", "Email", "customer_email", "contactEmail", "phone", "mobile_number",
                                  "salary", "annual_salary", "home_address", "street_address", "date_of_birth",
                                  "dob", "birth_date", "birthdate", "ssn", "postcode", "zip_code", "iban",
                                  "passport_number", "ip_address"])
def test_columns_named_like_personal_data_are_suggested(name: str) -> None:
    assert looks_personal(name)


@pytest.mark.parametrize("name", ["id", "full_name", "city", "country", "signed_up_at", "segment", "total",
                                  "marketing_opt_in", "email_verified", "address_count", "customer_id", "team"])
def test_other_columns_are_not_suggested(name: str) -> None:
    assert not looks_personal(name)


# --- the AI starts setup; the user finishes it on the page ----------------------------------------------------------

async def test_add_source_opens_the_setup_page_and_stores_nothing(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with Harness.start(home, browser=browser, setup_page=True) as h:
        added = await h.call("add_source", name="people", kind="sqlite", path=str(people_db))
        listed = await h.call("list_sources")
    assert not added.is_error
    assert added.ai.structured["setup_entry"] == "page"
    assert "127.0.0.1" not in added.ai.text and h.setup_pages[-1] not in added.ai.text  # the AI never gets the address
    assert "/connect?" in h.setup_pages[-1]
    assert stored(home) == ([], [], {})
    assert "people" not in listed.ai.text


async def test_the_page_walks_three_steps_and_keeps_nothing_before_confirm(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with Harness.start(home, browser=browser, setup_page=True) as h:
        await h.call("add_source", name="people", kind="sqlite", path=str(people_db))
        first = fetch(h.setup_pages[-1])
        assert first.status == 200
        assert SetupForm(first.body).fields["name"] == "people"  # prefilled from what the AI asked
        assert "Where is it?" in first.body

        checked = submit(h.setup_pages[-1], first, "check")
        assert checked.status == 200
        words = text_of(checked.body)
        assert "Check the login" in words and "Opened people.sqlite" in words
        assert "Choose how Claude works" in words
        ticked = SetupForm(checked.body).ticked
        assert sorted(ticked) == sorted(['["customers", "email"]', '["customers", "phone"]',
                                         '["customers", "date_of_birth"]', '["staff", "salary"]',
                                         '["staff", "home_address"]'])
        for column in ("email", "phone", "date_of_birth", "salary", "home_address", "full_name", "city"):
            assert column in words  # shown, not only ticked
        assert stored(home) == ([], [], {})  # nothing kept before confirm
        assert "people" not in (await h.call("list_sources")).ai.text

        done = submit(h.setup_pages[-1], checked, "save", mode="results")
        assert done.status == 200
        assert "people is connected" in text_of(done.body)
        names, rules, modes = stored(home)
        assert names == ["people"] and modes == {"people": "results"}
        assert rules == sorted([("people", "customers", "email"), ("people", "customers", "phone"),
                                ("people", "customers", "date_of_birth"), ("people", "staff", "salary"),
                                ("people", "staff", "home_address")])

        query = await h.call("query", source="people", question="contacts",
                             sql="SELECT full_name, email FROM customers")
    assert "ada@mail.example" not in query.ai.text and "Ada" in query.ai.text


async def test_unticked_suggestions_are_not_kept_and_the_mode_is_the_users(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with Harness.start(home, browser=browser, setup_page=True) as h:
        await h.call("add_source", name="people", kind="sqlite", path=str(people_db))
        checked = submit(h.setup_pages[-1], fetch(h.setup_pages[-1]), "check")
        submit(h.setup_pages[-1], checked, "save", mode="pick", keep=['["staff", "salary"]', '["orders", "total"]'])
    assert stored(home) == (["people"], [("people", "orders", "total"), ("people", "staff", "salary")],
                            {"people": "pick"})


async def test_a_column_the_source_doesnt_have_is_refused_and_nothing_is_saved(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with Harness.start(home, browser=browser, setup_page=True) as h:
        await h.call("add_source", name="people", kind="sqlite", path=str(people_db))
        checked = submit(h.setup_pages[-1], fetch(h.setup_pages[-1]), "check")
        refused = submit(h.setup_pages[-1], checked, "save", mode="results", keep=['["customers", "nope"]'])
    assert "Nothing was saved" in text_of(refused.body)
    assert stored(home) == ([], [], {})


async def test_a_file_that_wont_open_stays_on_the_first_step(home: Path, tmp_path: Path) -> None:
    browser = Browser()
    missing = tmp_path / "gone.sqlite"
    async with Harness.start(home, browser=browser, setup_page=True) as h:
        await h.call("add_source", name="gone", kind="sqlite", path=str(missing))
        page = fetch(h.setup_pages[-1])
        checked = submit(h.setup_pages[-1], page, "check")
    words = text_of(checked.body)
    assert "Where is it?" in words and "Choose how Claude works" not in words
    assert 'role="alert"' in checked.body
    assert stored(home) == ([], [], {})


async def test_a_stale_or_forged_setup_saves_nothing(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with Harness.start(home, browser=browser, setup_page=True) as h:
        await h.call("add_source", name="people", kind="sqlite", path=str(people_db))
        checked = submit(h.setup_pages[-1], fetch(h.setup_pages[-1]), "check")
        forged = submit(h.setup_pages[-1], checked, "save", mode="results", setup="not-a-real-one")
        submit(h.setup_pages[-1], checked, "save", mode="results")
        again = submit(h.setup_pages[-1], checked, "save", mode="pick")  # the same setup twice: it was used up
    assert "start again" in text_of(forged.body).lower()
    assert "start again" in text_of(again.body).lower()
    assert stored(home)[2] == {"people": "results"}


async def test_a_database_password_is_checked_never_shown_and_never_saved_on_failure(home: Path) -> None:
    browser = Browser()
    keychain = MemoryKeychain()
    async with Harness.start(home, browser=browser, keychain=keychain, setup_page=True) as h:
        added = await h.call("add_source", name="shop", kind="postgres", host="127.0.0.1", port=1,
                             database="shop", user="reader")
        assert added.ai.structured["setup_entry"] == "page"
        first = fetch(h.setup_pages[-1])
        assert SetupForm(first.body).fields["host"] == "127.0.0.1"
        assert 'type="password"' in first.body
        checked = submit(h.setup_pages[-1], first, "check", password="hunter2-secret")
    assert "hunter2-secret" not in checked.body
    assert 'role="alert"' in checked.body and "Choose how Claude works" not in text_of(checked.body)
    assert stored(home) == ([], [], {})
    assert keychain.passwords == {}


async def test_the_overview_links_to_the_setup_page(home: Path) -> None:
    browser = Browser()
    async with Harness.start(home, browser=browser, setup_page=True) as h:
        await h.call("open_page")
        overview = fetch(browser.url)
    assert re.search(r'href="[^"]*/connect"', overview.body)
    blank = fetch(browser.url.rstrip("/") + "/connect")
    assert blank.status == 200 and "Connect your first data" in text_of(blank.body)


async def test_without_a_browser_add_source_asks_as_before(home: Path, people_db: Path) -> None:
    async with Harness.start(home) as h:
        added = await h.call("add_source", name="people", kind="sqlite", path=str(people_db))
    assert added.ai.structured.get("confirm_entry") == "terminal"


async def test_a_source_set_up_on_the_page_answers_the_ai(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people", setup_page=True) as h:
        listed = await h.call("list_sources")
        pulled = await h.call("query", source="people", question="teams", sql="SELECT team FROM staff")
    assert "people" in listed.ai.text and not pulled.is_error
    assert stored(home)[2] == {"people": "pick"}


async def test_over_the_web_add_source_never_opens_a_page_on_the_computer(home: Path, people_db: Path) -> None:
    """MCP over HTTP (#102): the user may be far from this computer, so the other ways (a terminal here) apply."""
    from portward_free.runtime import Runtime
    from portward_free.source_tools import setup_on_page

    opened: list[str] = []
    store = LocalStore.open(database_path(home))
    try:
        rt = Runtime(store=store, keychain=MemoryKeychain(), usage=None, browser=lambda u: opened.append(u) or True,
                     opened=None, local_only=True)  # type: ignore[arg-type]
        assert setup_on_page(rt, {"kind": "sqlite", "name": "people", "path": str(people_db), "host": None,
                                  "port": None, "database": None, "user": None}) is None
    finally:
        store.close()
    assert opened == []
