"""Row rules on the local page (#132): the profile editor adds and changes a table's row rule, its preview names the
column and rows when one profile lowers another, and why-hidden says which rows and cells row rules keep from the
AI. User-only: none of it reaches the AI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from harness import Harness, MemoryKeychain, connected
from test_context import Reviewer
from test_local_page import Browser
from test_profile_editor import PREVIEW, SAVE, EditorForm, open_editor, submit
from test_profiles import PlanAccounts, seen
from test_row_rules_leaks import ALL_FULL, COACH, EU, deals_db, profile  # noqa: F401  (fixture)

from portward_free.account import Accounts
from portward_free.paths import database_path
from portward_free.profiles import Profile
from portward_free.sources.sqlite import SqliteSource
from portward_free.storage import LocalStore
from portward_free.why_hidden import Kind, why_hidden

ROWS_FIELD = "r:deals"


@asynccontextmanager
async def personal(home: Path, db: Path, browser: Browser | None = None) -> AsyncIterator[Harness]:
    keychain = MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    async with connected(home, db, name="deals", keychain=keychain, elicitation=Reviewer(),
                         accounts=Accounts(PlanAccounts("active")), browser=browser) as h:
        yield h


def applied(home: Path, *profiles: Profile) -> None:
    store = LocalStore.open(database_path(home))
    try:
        store.profiles.switch_to_profiles("deals")
        for p in profiles:
            store.profiles.apply("deals", store.profiles.save(p), ())
    finally:
        store.close()


def held(home: Path, name: str) -> Profile:
    store = LocalStore.open(database_path(home))
    try:
        found = store.profiles.named(name)
        assert found is not None
        return found
    finally:
        store.close()


def lookup(home: Path, db: Path):
    store, source = LocalStore.open(database_path(home)), SqliteSource(db)
    try:
        return why_hidden(store, "deals", source.tables())
    finally:
        store.close()
        source.close()


async def test_why_hidden_names_the_rows_and_cells_row_rules_keep_from_the_ai(home: Path, deals_db: Path) -> None:
    async with personal(home, deals_db):
        applied(home, profile(*EU), profile(*COACH))
    found = lookup(home, deals_db)
    (rows,) = found.about("deals")
    assert rows.kind is Kind.ROWS and "region = 'EU'" in rows.why and "EU pipeline" in rows.why
    assert "region IN ('US', 'APAC')" in rows.why
    (amount,) = found.about("deals.amount")
    assert amount.kind is Kind.ROWS and "on other rows the AI gets deals.amount empty" in amount.why
    lowered = [i for i in found.about("deals.owner_email") if i.kind is Kind.LOWERED]
    assert lowered and "EU pipeline" in lowered[0].why and "Americas and APAC, anonymised" in lowered[0].why


async def test_the_editor_preview_names_the_column_and_rows_one_profile_lowers(home: Path, deals_db: Path) -> None:
    browser = Browser()
    async with personal(home, deals_db, browser) as h:
        applied(home, profile(*EU), profile(*COACH))
        _, page = await open_editor(h, browser, "EU pipeline")
    assert EditorForm(page.body).fields[ROWS_FIELD] == "region = 'EU'"
    assert "deals.owner_email" in page.body
    assert "shows it in full on rows where region = &#x27;EU&#x27;" in page.body
    assert "Americas and APAC, anonymised" in page.body


async def test_adding_a_row_rule_in_the_editor_saves_one_version_and_is_enforced(home: Path,
                                                                                 deals_db: Path) -> None:
    browser = Browser()
    async with personal(home, deals_db, browser) as h:
        applied(home, profile("All deals", ALL_FULL))
        url, page = await open_editor(h, browser, "All deals")
        previewed = submit(url, page, PREVIEW, **{ROWS_FIELD: "region IN ('EU')"})
        assert "1 change since v1" in previewed.body
        saved = submit(url, previewed, SAVE)
        assert saved.status == 303, saved.body[:400]  # fewer rows: nothing to ask
        reply = await h.call("query", source="deals", question="?", sql="SELECT owner_email FROM deals")
    assert held(home, "All deals").row_rule("deals").text == "region IN ('EU')"
    assert "secret" not in seen(reply)


async def test_changing_or_removing_a_row_rule_on_an_applied_profile_asks_first(home: Path, deals_db: Path) -> None:
    browser = Browser()
    async with personal(home, deals_db, browser) as h:
        applied(home, profile(*EU))
        url, page = await open_editor(h, browser, "EU pipeline")
        asked = submit(url, page, SAVE, **{ROWS_FIELD: ""})
        assert asked.status == 200 and "row rule" in asked.body.lower() and "EU pipeline" in asked.body
        assert held(home, "EU pipeline").version == 1
        submit(url, page, SAVE, **{ROWS_FIELD: "region IN ('EU', 'US')", "confirm": "yes"})
    assert held(home, "EU pipeline").row_rule("deals").text == "region IN ('EU', 'US')"


async def test_a_row_rule_outside_the_grammar_is_refused_in_the_editor(home: Path, deals_db: Path) -> None:
    browser = Browser()
    async with personal(home, deals_db, browser) as h:
        applied(home, profile(*EU))
        url, page = await open_editor(h, browser, "EU pipeline")
        refused = submit(url, page, SAVE, **{ROWS_FIELD: "region = 'EU' OR 1 = 1", "confirm": "yes"})
    assert refused.status == 200 and "Not saved" in refused.body
    assert held(home, "EU pipeline").row_rule("deals").text == "region = 'EU'"
