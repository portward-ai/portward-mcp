"""Assumptions review (#96, Personal): when no definition covers part of a question, the AI says what it assumed as
it runs the query. Each assumption is recorded with that answer, on this computer only, and the user can review them
and turn one into a named definition with one confirmation; from then on the AI is given that definition instead of
guessing. Driven through the MCP client harness."""

from __future__ import annotations

import json
from pathlib import Path

from test_context import Reviewer, source_entry
from test_profiles import apply, make, tool

from portward_free.paths import database_path
from portward_free.storage import LocalStore
from portward_free.sync import records
from portward_free.sync.kinds import Place

ASSUMED = "Active customer means a customer with an order in the last 90 days"


async def asked_with_an_assumption(h) -> str:
    pulled = await h.call("query", source="people", question="How many active customers?",
                          sql="SELECT count(DISTINCT customer_id) FROM orders", assumptions=[ASSUMED])
    assert not pulled.is_error, pulled.ai.text
    return pulled.ai.structured["pull_id"]


async def test_assumptions_are_recorded_per_answer(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["orders.customer_id", "orders.total"])
        await apply(h, "Sales")
        pull_id = await asked_with_an_assumption(h)
        await h.call("query", source="people", question="Total?", sql="SELECT sum(total) FROM orders")
        listed = await h.call("assumptions")
    (found,) = listed.ai.structured["assumptions"]
    assert found["text"] == ASSUMED and found["pull_id"] == pull_id
    assert found["question"] == "How many active customers?" and found["source"] == "people"
    assert found["defined_as"] is None


async def test_one_confirmation_turns_an_assumption_into_a_definition_the_ai_then_gets(
        home: Path, people_db: Path) -> None:
    reviewer = Reviewer()
    async with tool(home, people_db, user=reviewer) as h:
        await make(h, "Sales", ["orders.customer_id", "orders.total"])
        await apply(h, "Sales")
        await asked_with_an_assumption(h)
        (found,) = (await h.call("assumptions")).ai.structured["assumptions"]
        defined = await h.call("define_assumption", assumption_id=found["id"], term="active customer")
        listed = await h.call("list_sources")
        after = await h.call("assumptions")
    assert not defined.is_error, defined.ai.text
    assert len(reviewer.asked) == 1 and ASSUMED in reviewer.asked[0]
    assert source_entry(listed)["definitions"] == [{"term": "active customer", "text": ASSUMED}]
    assert defined.ai.structured["profile"]["version"] == 2
    (reviewed,) = after.ai.structured["assumptions"]
    assert reviewed["defined_as"] == "active customer"


async def test_the_user_can_reword_the_definition_or_say_no(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer(edit="A customer who ordered in the last 30 days")) as h:
        await make(h, "Sales", ["orders.customer_id"])
        await apply(h, "Sales")
        await asked_with_an_assumption(h)
        (found,) = (await h.call("assumptions")).ai.structured["assumptions"]
        await h.call("define_assumption", assumption_id=found["id"], term="active customer")
        reworded = source_entry(await h.call("list_sources"))["definitions"]
    async with tool(home, people_db, user=Reviewer(approve=False)) as h:
        await asked_with_an_assumption(h)
        newest = (await h.call("assumptions")).ai.structured["assumptions"][0]
        declined = await h.call("define_assumption", assumption_id=newest["id"], term="engaged customer")
        terms = [d["term"] for d in source_entry(await h.call("list_sources"))["definitions"]]
    assert reworded == [{"term": "active customer", "text": "A customer who ordered in the last 30 days"}]
    assert declined.is_error and terms == ["active customer"]


async def test_with_two_profiles_applied_the_ai_names_the_one_to_define_it_in(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["orders.customer_id"])
        await make(h, "Finance", ["orders.total"])
        await apply(h, "Sales")
        await apply(h, "Finance")
        await asked_with_an_assumption(h)
        (found,) = (await h.call("assumptions")).ai.structured["assumptions"]
        unsure = await h.call("define_assumption", assumption_id=found["id"], term="active customer")
        named = await h.call("define_assumption", assumption_id=found["id"], term="active customer",
                             profile="Finance")
        profiles = {p["name"]: p for p in (await h.call("list_profiles")).ai.structured["profiles"]}
    assert unsure.is_error and "Finance" in unsure.ai.text and "Sales" in unsure.ai.text
    assert not named.is_error, named.ai.text
    assert [d["term"] for d in profiles["Finance"]["context"]["definitions"]] == ["active customer"]
    assert profiles["Sales"]["context"]["definitions"] == []


async def test_assumptions_and_drafts_are_never_among_what_sync_sends(home: Path, people_db: Path) -> None:
    """Sync sends what records.local_state reads from this computer; the assumptions, the drafts waiting for the
    user and a dictionary import the user hasn't confirmed are never part of it, while an approved link is."""
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["orders.customer_id", "customers.id"])
        await apply(h, "Sales")
        await h.call("query", source="people", question="zq-question: active?", sql="SELECT count(*) FROM orders",
                     assumptions=["zq-assumption-marker: active means ordered this quarter"])
        await h.call("draft_definition", profile="Sales", term="zq draft term", text="zq-draft-marker")
        drafted = await h.call("draft_link", profile="Sales", column="orders.customer_id", other_column="customers.id")
        await h.call("approve_context", draft_id=drafted.ai.structured["drafts"][0])
        await h.call("draft_link", profile="Sales", column="customers.id", other_column="orders.customer_id",
                     text="zq-link-draft-marker")
    store = LocalStore.open(database_path(home))
    sent = json.dumps(records.local_state(Place(store)))
    assert "zq-assumption-marker" not in sent and "zq-draft-marker" not in sent and "zq-link-draft-marker" not in sent
    assert "orders.customer_id holds the same values as customers.id" in sent  # the approved link goes with Sales
    assert store.assumptions.recent(10)[0].text.startswith("zq-assumption-marker")  # kept here, on this computer
