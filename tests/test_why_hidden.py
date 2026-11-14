"""Why is this hidden? (#124): one lookup, for the user only, says why a table, column or context item is kept from
the AI on a connection that runs on profiles: no applied profile grants it, it's granted but fell to hidden here, a
granted view or partition is withheld, context is held back because it names something hidden, or two profiles
describe it differently and the user hasn't picked. It's read on the local Portward page; the AI is never told.
Driven through the MCP client harness and the page, against an in-memory account service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from harness import Harness, MemoryKeychain, connected
from test_context import Reviewer, draft_meaning
from test_local_page import Browser, fetch
from test_profiles import PlanAccounts, apply, make, seen
from test_until_per_app import CHATGPT, T0, Clock

from portward_free.account import Accounts
from portward_free.paths import database_path
from portward_free.sources import Table
from portward_free.storage import LocalStore
from portward_free.why_hidden import Kind, WhyHidden, why_hidden

PEOPLE = (  # people_db's tables, as its SQLite source lists them
    Table("customers", ("id", "name", "ssn"), schema="main", numeric=frozenset({"id"})),
    Table("orders", ("id", "customer_id", "total"), schema="main", numeric=frozenset({"id", "customer_id", "total"})),
    Table("notes", ("id", "body"), schema="main", numeric=frozenset({"id"})),
)


@asynccontextmanager
async def personal(home: Path, db: Path, *, reviewer: Reviewer | None = None,
                   browser: Browser | None = None) -> AsyncIterator[Harness]:
    keychain = MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    async with connected(home, db, name="people", keychain=keychain, elicitation=reviewer or Reviewer(),
                         accounts=Accounts(PlanAccounts("active")), browser=browser) as h:
        yield h


def lookup(home: Path, catalog: tuple[Table, ...] = PEOPLE, source: str = "people") -> WhyHidden:
    store = LocalStore.open(database_path(home))
    try:
        return why_hidden(store, source, catalog)
    finally:
        store.close()


def kinds(found: WhyHidden, about: str) -> list[Kind]:
    return [item.kind for item in found.about(about)]


# --- tables and columns ---------------------------------------------------------------------------------------------

async def test_a_column_no_applied_profile_grants_is_not_granted(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name", "orders.total"])
        await apply(h, "Sales")
    found = lookup(home)
    assert kinds(found, "customers.ssn") == [Kind.NOT_GRANTED]
    (item,) = found.about("customers.ssn")
    assert "Sales" in item.why
    assert found.about("customers.name") == ()  # the AI sees it: nothing to explain


async def test_a_table_with_nothing_granted_is_not_granted_as_a_whole(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
    found = lookup(home)
    assert kinds(found, "notes") == [Kind.NOT_GRANTED]
    assert kinds(found, "notes.body") == [Kind.NOT_GRANTED]  # asking about a column finds its table's reason
    assert found.about("notes")[0].what == "table"


async def test_with_no_profile_applied_everything_is_not_granted_and_it_says_so(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        await h.call("unapply_profile", profile="Sales", source="people")
    found = lookup(home)
    assert found.on_profiles and found.applied == ()
    assert kinds(found, "customers.name") == [Kind.NOT_GRANTED]
    assert "no profile is applied" in found.about("customers")[0].why


async def test_totals_on_a_text_column_fell_to_hidden_and_says_why(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.id"], totals=["customers.name"])
        await apply(h, "Sales")
    (item,) = lookup(home).about("customers.name")
    assert item.kind is Kind.FELL_TO_HIDDEN and item.what == "column"
    assert "totals" in item.why and "isn't one" in item.why and "Sales" in item.why


async def test_anonymised_where_no_stand_ins_can_be_made_fell_to_hidden(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.id"], anonymised=["customers.name"])
        await apply(h, "Sales")
    wrapped = tuple(Table(t.name, t.columns, schema=t.schema, numeric=t.numeric, stand_ins=False) for t in PEOPLE)
    (item,) = lookup(home, wrapped).about("customers.name")
    assert item.kind is Kind.FELL_TO_HIDDEN and "stand-ins" in item.why


async def test_a_granted_view_is_withheld(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name", "cards.name"])
        await apply(h, "Sales")
    # a Postgres-like catalog: the view is derived (SQLite's own views aren't, its authorizer holds inside them)
    catalog = (*PEOPLE, Table("cards", ("name", "number"), derived=True, schema="main"))
    found = lookup(home, catalog)
    assert kinds(found, "cards") == [Kind.WITHHELD]
    assert "view or partition" in found.about("cards")[0].why


async def test_a_view_nothing_grants_is_not_granted_not_withheld(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
    found = lookup(home, (*PEOPLE, Table("memo", ("text",), derived=True, schema="main")))
    assert kinds(found, "memo") == [Kind.NOT_GRANTED]


# --- context --------------------------------------------------------------------------------------------------------

async def test_a_definition_naming_a_hidden_column_is_held_back_and_names_it(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name", "orders.total"])
        await apply(h, "Sales")
        drafted = await h.call("draft_definition", profile="Sales", term="Verified customer",
                               text="A customer whose customers.ssn has been checked")
        (draft_id,) = drafted.ai.structured["drafts"]
        await h.call("approve_context", draft_id=draft_id)
    (item,) = lookup(home).about("Verified customer")
    assert item.kind is Kind.HELD_BACK and item.what == "definition"
    assert item.names == ("customers.ssn",) and "Sales" in item.why


async def test_a_meaning_about_a_column_that_fell_to_hidden_is_held_back(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.id"], totals=["customers.name"])
        await apply(h, "Sales")
        draft_id = await draft_meaning(h, "Sales", "customers", "Legal name", column="name")
        await h.call("approve_context", draft_id=draft_id)
    found = lookup(home)
    context = [i for i in found.items if i.what == "meaning"]
    assert [(i.about, i.kind) for i in context] == [("meaning of customers.name", Kind.HELD_BACK)]


async def test_two_profiles_describing_a_column_differently_are_unsettled(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        await make(h, "Ops", ["customers.name"])
        await apply(h, "Sales")
        await apply(h, "Ops")
        for profile, text in (("Sales", "Legal name"), ("Ops", "Display name")):
            await h.call("approve_context", draft_id=await draft_meaning(h, profile, "customers", text, "name"))
    (item,) = [i for i in lookup(home).items if i.kind is Kind.UNSETTLED]
    assert item.about == "meaning of customers.name"
    assert set(item.profiles) == {"Sales", "Ops"}


async def test_a_link_whose_other_column_the_ai_cant_see_is_held_back(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["notes.body"], totals=["customers.name"])  # name falls to hidden (it's text)
        await apply(h, "Sales")
        drafted = await h.call("draft_link", profile="Sales", column="notes.body", other_column="customers.name")
        (draft_id,) = drafted.ai.structured["drafts"]
        await h.call("approve_context", draft_id=draft_id)
    (item,) = [i for i in lookup(home).items if i.what == "link"]
    assert item.kind is Kind.HELD_BACK and item.about == "link notes.body = customers.name"
    assert item.names == ("customers.name",)


async def test_a_block_rules_connection_has_nothing_for_the_lookup(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await h.call("block", source="people", table="customers", column="ssn")
    found = lookup(home)
    assert not found.on_profiles and found.items == ()


# --- until and per AI app (#134) ------------------------------------------------------------------------------------

async def test_a_column_only_an_ended_application_granted_says_it_ended(home: Path, people_db: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        await make(h, "Orders", ["orders.total"])
        await apply(h, "Orders")
        temporary = await h.call("apply_profile", profile="Sales", source="people",
                                 until=(T0 + timedelta(days=1)).isoformat())
        assert not temporary.is_error, temporary.ai.text
    assert lookup(home).about("customers.name") == ()  # still applies
    clock.set(T0 + timedelta(days=2))
    found = lookup(home)
    assert kinds(found, "customers.name") == [Kind.ENDED]
    (item,) = found.about("customers.name")
    assert item.profiles == ("Sales",) and "2026-11-10 09:00 UTC" in item.why and item.label == "Ended"
    assert kinds(found, "customers.ssn") == [Kind.NOT_GRANTED]  # nothing ever granted it
    clock.set(T0)  # a clock set back changes nothing here either
    assert kinds(lookup(home), "customers.name") == [Kind.ENDED]


async def test_a_column_granted_only_for_other_ai_apps_says_which(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await make(h, "Sales", ["customers.name", "notes.body"])
        await make(h, "Orders", ["orders.total"])
        await apply(h, "Orders")
        limited = await h.call("apply_profile", profile="Sales", source="people", for_apps=[CHATGPT])
        assert not limited.is_error, limited.ai.text
    found = lookup(home)
    assert kinds(found, "customers.name") == [Kind.OTHER_APPS]
    assert kinds(found, "notes") == [Kind.OTHER_APPS]  # a whole table only those apps get
    (item,) = found.about("customers.name")
    assert item.profiles == ("Sales",) and CHATGPT in item.why and "desktop" in item.why
    assert item.label == "Other AI apps only"
    assert kinds(found, "customers.ssn") == [Kind.NOT_GRANTED]


# --- the AI is never told -------------------------------------------------------------------------------------------

async def every_reason(h: Harness) -> None:
    await make(h, "Sales", ["customers.id", "orders.total"], totals=["customers.name"])
    await apply(h, "Sales")
    drafted = await h.call("draft_definition", profile="Sales", term="Verified customer",
                           text="A customer whose customers.ssn has been checked")
    (draft_id,) = drafted.ai.structured["drafts"]
    await h.call("approve_context", draft_id=draft_id)


async def test_no_ai_facing_tool_result_contains_the_explanation(home: Path, people_db: Path) -> None:
    async with personal(home, people_db) as h:
        await every_reason(h)
        tools = await h.tools()
        calls: list[tuple[str, dict[str, Any]]] = [
            ("list_sources", {}), ("describe_source", {"source": "people"}), ("list_profiles", {}),
            ("get_started", {}), ("context_drafts", {}), ("ai_log", {}),
            ("query", {"source": "people", "question": "?", "sql": "SELECT * FROM customers"}),
            ("query", {"source": "people", "question": "?", "sql": "SELECT ssn FROM customers"}),
            ("query", {"source": "people", "question": "?", "sql": "SELECT body FROM notes"})]
        replies = [await h.call(name, **args) for name, args in calls if name in tools]
    found = lookup(home)
    assert {Kind.NOT_GRANTED, Kind.FELL_TO_HIDDEN, Kind.HELD_BACK} <= {i.kind for i in found.items}
    told = "".join(seen(r) for r in replies)
    assert all(item.why not in told for item in found.items)
    assert not any("why" in name.lower() for name in tools)  # no tool for the AI to ask with


# --- on the local page ----------------------------------------------------------------------------------------------

async def test_the_local_page_links_to_the_lookup_and_answers_about_one_column(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await every_reason(h)
        await h.call("open_page")
        overview = fetch(browser.url)
        listed = fetch(browser.url + "hidden?source=people")
        one = fetch(browser.url + "hidden?source=people&about=customers.ssn")
        visible = fetch(browser.url + "hidden?source=people&about=orders.total")
    assert "hidden?source=people" in overview.body
    assert listed.status == 200
    for item in lookup(home).items:
        assert item.about.replace("'", "&#x27;") in listed.body
    assert one.status == 200 and "Not granted" in one.body and "customers.ssn" in one.body
    assert "orders.total is not hidden" in visible.body


async def test_the_page_lookup_escapes_what_the_user_typed(home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await every_reason(h)
        await h.call("open_page")
        page = fetch(browser.url + "hidden?source=people&about=%3Cscript%3Ex%3C%2Fscript%3E")
    assert page.status == 200 and "<script>x" not in page.body
