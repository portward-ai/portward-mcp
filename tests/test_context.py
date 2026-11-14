"""Drafted meanings and named definitions (#95): the AI app connected over MCP drafts what granted tables and columns
mean and proposes named definitions; Portward keeps each draft on this computer, unused, until the user approves it
(or their edit of it) in their client's own prompt. Approved context is part of the profile and reaches the AI with
the schema and the results it explains, and never mentions anything the profile doesn't grant. Driven through the MCP
client harness, against an in-memory account service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness import MemoryKeychain, connected
from mcp.types import ElicitResult
from test_profiles import EMAIL, PlanAccounts, apply, make, seen, tool

from portward_free.account import Accounts


class Reviewer:
    """The user answering Portward's prompts: approves drafts (optionally with their own edit) and picks between
    clashing descriptions, remembering what they were asked."""

    def __init__(self, approve: bool = True, edit: str | None = None, pick: str | None = None) -> None:
        self.approve, self.edit, self.pick = approve, edit, pick
        self.asked: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.asked.append(params.message)
        if not hasattr(params, "requested_schema"):  # a URL-mode prompt (the password page): the user opens it
            return ElicitResult(action="accept")
        schema = params.requested_schema
        self.schemas.append(schema)
        properties = schema.get("properties", {})
        if "pick" in properties:
            return ElicitResult(action="accept", content={"pick": self.pick or properties["pick"]["enum"][0]})
        if "confirm" in properties:
            return ElicitResult(action="accept", content={"confirm": True})
        content: dict[str, Any] = {"approve": self.approve}
        if self.edit is not None:
            content["text"] = self.edit
        return ElicitResult(action="accept", content=content)


def table_entry(listed, table: str, source: str = "people") -> dict[str, Any]:
    (entry,) = [s for s in listed.ai.structured["sources"] if s["name"] == source]
    (found,) = [t for t in entry["tables"] if t["name"] == table]
    return found


def source_entry(listed, source: str = "people") -> dict[str, Any]:
    (entry,) = [s for s in listed.ai.structured["sources"] if s["name"] == source]
    return entry


async def draft_meaning(h, profile: str, table: str, meaning: str, column: str | None = None) -> str:
    drafted = await h.call("draft_meanings", profile=profile,
                           meanings=[{"table": table, "column": column, "meaning": meaning}])
    assert not drafted.is_error, drafted.ai.text
    (draft_id,) = drafted.ai.structured["drafts"]
    return draft_id


# --- drafts wait for the user ---------------------------------------------------------------------------------------

async def test_a_drafted_meaning_reaches_the_ai_only_once_the_user_approves_it(home: Path, people_db: Path) -> None:
    reviewer = Reviewer()
    async with tool(home, people_db, user=reviewer) as h:
        await make(h, "Sales", ["customers.name", "customers.id"])
        await apply(h, "Sales")
        draft_id = await draft_meaning(h, "Sales", "customers", "The customer's legal name", column="name")
        before = await h.call("list_sources")
        approved = await h.call("approve_context", draft_id=draft_id)
        after = await h.call("list_sources")
    assert "legal name" not in seen(before)
    assert not approved.is_error, approved.ai.text
    assert any("legal name" in asked for asked in reviewer.asked)
    assert table_entry(after, "customers")["column_notes"] == {"name": "The customer's legal name"}


async def test_the_user_can_edit_a_draft_as_they_approve_it_and_that_publishes_a_new_version(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer(edit="Full legal name, as invoiced")) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        draft_id = await draft_meaning(h, "Sales", "customers", "name of customer", column="name")
        approved = await h.call("approve_context", draft_id=draft_id)
        listed = await h.call("list_sources")
        waiting = await h.call("context_drafts")
    assert approved.ai.structured["profile"]["version"] == 2
    assert f"last changed by {EMAIL}, v2" in approved.ai.text
    assert table_entry(listed, "customers")["column_notes"] == {"name": "Full legal name, as invoiced"}
    assert waiting.ai.structured["drafts"] == []  # approved drafts are done with


async def test_a_declined_draft_is_never_used(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer(approve=False)) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        draft_id = await draft_meaning(h, "Sales", "customers", "The customer's legal name", column="name")
        declined = await h.call("approve_context", draft_id=draft_id)
        listed = await h.call("list_sources")
        described = await h.call("describe_source", source="people")
    assert declined.is_error
    assert "legal name" not in seen(listed) and "legal name" not in seen(described)


async def test_without_a_prompt_in_the_client_a_draft_cant_be_approved_from_chat(home: Path, people_db: Path) -> None:
    keychain = MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    async with connected(home, people_db, name="people", keychain=keychain,
                         accounts=Accounts(PlanAccounts("active"))) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")  # nothing to ask: the connection had no block rules
        draft_id = await draft_meaning(h, "Sales", "customers", "The customer's legal name", column="name")
        approved = await h.call("approve_context", draft_id=draft_id)
        listed = await h.call("list_sources")
    assert approved.is_error and "can't ask" in approved.ai.text
    assert "legal name" not in seen(listed)


async def test_the_list_of_drafts_says_what_each_is_about_but_not_its_text(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.name"])
        draft_id = await draft_meaning(h, "Sales", "customers", "The customer's legal name", column="name")
        waiting = await h.call("context_drafts", profile="Sales")
    (draft,) = waiting.ai.structured["drafts"]
    assert draft["id"] == draft_id and draft["about"] == "customers.name" and draft["profile"] == "Sales"
    assert "legal name" not in seen(waiting)


async def test_only_what_the_profile_grants_can_be_given_a_meaning(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.name"])
        hidden = await h.call("draft_meanings", profile="Sales",
                              meanings=[{"table": "customers", "column": "ssn", "meaning": "tax id"}])
        table = await h.call("draft_meanings", profile="Sales",
                             meanings=[{"table": "orders", "meaning": "one row per order"}])
        waiting = await h.call("context_drafts")
    assert hidden.is_error and "doesn't grant" in hidden.ai.text
    assert table.is_error
    assert waiting.ai.structured["drafts"] == []


async def test_drafting_needs_personal(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, plan="ended") as h:
        listed = await h.ai_tools()
        drafted = await h.call("draft_definition", profile="Sales", term="MRR", text="sum of orders.total")
    assert "draft_definition" not in listed
    assert drafted.is_error and "Unknown tool" in drafted.ai.text  # #116: not listed on Free


# --- named definitions ----------------------------------------------------------------------------------------------

async def define(h, profile: str, term: str, text: str):
    drafted = await h.call("draft_definition", profile=profile, term=term, text=text)
    assert not drafted.is_error, drafted.ai.text
    (draft_id,) = drafted.ai.structured["drafts"]
    return await h.call("approve_context", draft_id=draft_id)


REVENUE = "Revenue is the sum of orders.total over every order"


async def test_an_approved_definition_reaches_the_ai_with_the_schema_and_is_cited_with_results(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["orders.id", "orders.total"])
        await apply(h, "Sales")
        approved = await define(h, "Sales", "revenue", REVENUE)
        listed = await h.call("list_sources")
        described = await h.call("describe_source", source="people")
        pulled = await h.call("query", source="people", question="What is our revenue?",
                              sql="SELECT sum(total) AS revenue FROM orders")
    assert not approved.is_error, approved.ai.text
    assert source_entry(listed)["definitions"] == [{"term": "revenue", "text": REVENUE}]
    assert described.ai.structured["definitions"] == [{"term": "revenue", "text": REVENUE}]
    assert [(d["about"], d["text"]) for d in pulled.ai.structured["definitions"]] == [("revenue", REVENUE)]
    assert REVENUE in pulled.ai.text and "cite" in pulled.ai.text


async def test_a_result_cites_only_the_context_it_relies_on(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["orders.total", "customers.name"])
        await apply(h, "Sales")
        await define(h, "Sales", "revenue", REVENUE)
        draft_id = await draft_meaning(h, "Sales", "customers", "The customer's legal name", column="name")
        await h.call("approve_context", draft_id=draft_id)
        pulled = await h.call("query", source="people", question="Who are our customers?",
                              sql="SELECT name FROM customers")
    assert [(d["about"], d["text"]) for d in pulled.ai.structured["definitions"]] == [
        ("customers.name", "The customer's legal name")]


async def test_a_drafted_definition_isnt_used_before_approval(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["orders.id", "orders.total"])
        await apply(h, "Sales")
        await h.call("draft_definition", profile="Sales", term="revenue", text=REVENUE)
        listed = await h.call("list_sources")
        pulled = await h.call("query", source="people", question="What is our revenue?",
                              sql="SELECT sum(total) FROM orders")
    assert REVENUE not in seen(listed) and REVENUE not in seen(pulled)


async def test_a_definition_naming_a_hidden_column_warns_the_user_before_saving_and_never_reaches_the_ai(
        home: Path, people_db: Path) -> None:
    reviewer = Reviewer()
    text = "An identified customer is one with customers.ssn filled in"
    async with tool(home, people_db, user=reviewer) as h:
        await make(h, "Sales", ["customers.id", "customers.name"])
        await apply(h, "Sales")
        drafted = await h.call("draft_definition", profile="Sales", term="identified customer", text=text)
        (draft_id,) = drafted.ai.structured["drafts"]
        approved = await h.call("approve_context", draft_id=draft_id)
        listed = await h.call("list_sources")
        profiles = await h.call("list_profiles")
        pulled = await h.call("query", source="people", question="How many identified customers?",
                              sql="SELECT count(id) FROM customers")
    (asked,) = reviewer.asked
    assert "Warning" in asked and "customers.ssn" in asked and "doesn't grant" in asked
    assert "Warning" not in seen(approved) and "Warning" not in seen(drafted)
    for reply in (listed, profiles, pulled):
        assert "ssn" not in seen(reply)


async def test_a_bare_word_naming_a_hidden_column_also_warns(home: Path, people_db: Path) -> None:
    reviewer = Reviewer()
    async with tool(home, people_db, user=reviewer) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        draft_id = await draft_meaning(h, "Sales", "customers", "People we bill; keyed by ssn", column="name")
        await h.call("approve_context", draft_id=draft_id)
        listed = await h.call("list_sources")
    (asked,) = reviewer.asked
    assert "Warning" in asked and "ssn" in asked
    assert "ssn" not in seen(listed)


async def test_context_stops_once_its_grant_is_removed(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "customers.name"])
        await apply(h, "Sales")
        draft_id = await draft_meaning(h, "Sales", "customers", "The customer's legal name", column="name")
        await h.call("approve_context", draft_id=draft_id)
        await h.call("update_profile", profile="Sales", remove=["customers.name"])
        listed = await h.call("list_sources")
        profiles = await h.call("list_profiles")
    assert "legal name" not in seen(listed) and "legal name" not in seen(profiles)


# --- two profiles, two descriptions ---------------------------------------------------------------------------------

async def two_profiles_describing_name(h, sales: str, billing: str) -> None:
    await make(h, "Sales", ["customers.id", "customers.name"])
    await make(h, "Billing", ["customers.name"])
    for profile, text in (("Sales", sales), ("Billing", billing)):
        draft_id = await draft_meaning(h, profile, "customers", text, column="name")
        approved = await h.call("approve_context", draft_id=draft_id)
        assert not approved.is_error, approved.ai.text
    await apply(h, "Sales")
    await apply(h, "Billing")


async def test_a_clash_between_two_profiles_asks_the_user_once_and_remembers_the_pick(
        home: Path, people_db: Path) -> None:
    reviewer = Reviewer(pick="Billing")
    async with tool(home, people_db, user=reviewer) as h:
        await two_profiles_describing_name(h, "Contact person", "Legal name on invoices")
        before = await h.call("list_sources")
        settled = await h.call("settle_context", source="people", about="customers.name")
        after = await h.call("list_sources")
        again = await h.call("list_sources")
        asked_before = len(reviewer.asked)
        twice = await h.call("settle_context", source="people", about="customers.name")
    assert "column_notes" not in table_entry(before, "customers")
    assert any("settle_context" in w for w in source_entry(before)["warnings"])
    assert "Contact person" not in seen(before) and "Legal name on invoices" not in seen(before)
    assert not settled.is_error, settled.ai.text
    assert any("Contact person" in a and "Legal name on invoices" in a for a in reviewer.asked)
    assert table_entry(after, "customers")["column_notes"] == {"name": "Legal name on invoices"}
    assert table_entry(again, "customers")["column_notes"] == {"name": "Legal name on invoices"}
    assert "warnings" not in source_entry(after)
    assert twice.is_error and len(reviewer.asked) == asked_before  # nothing left to ask


async def test_the_pick_holds_until_either_description_changes(home: Path, people_db: Path) -> None:
    reviewer = Reviewer(pick="Sales")
    async with tool(home, people_db, user=reviewer) as h:
        await two_profiles_describing_name(h, "Contact person", "Legal name on invoices")
        await h.call("settle_context", source="people", about="customers.name")
        await h.call("update_profile", profile="Billing", add=["orders.total"])  # not the description: still holds
        held = await h.call("list_sources")
        draft_id = await draft_meaning(h, "Billing", "customers", "Name printed on invoices", column="name")
        await h.call("approve_context", draft_id=draft_id)
        changed = await h.call("list_sources")
    assert table_entry(held, "customers")["column_notes"] == {"name": "Contact person"}
    assert "column_notes" not in table_entry(changed, "customers")
    assert any("settle_context" in w for w in source_entry(changed)["warnings"])


async def test_two_profiles_defining_a_term_differently_clash_too(home: Path, people_db: Path) -> None:
    reviewer = Reviewer(pick="Billing")
    async with tool(home, people_db, user=reviewer) as h:
        await make(h, "Sales", ["orders.id", "orders.total"])
        await make(h, "Billing", ["orders.total"])
        await define(h, "Sales", "revenue", REVENUE)
        await define(h, "Billing", "Revenue", "Revenue is orders.total after refunds")
        await apply(h, "Sales")
        await apply(h, "Billing")
        before = await h.call("list_sources")
        await h.call("settle_context", source="people", about="revenue")
        after = await h.call("list_sources")
    assert "definitions" not in source_entry(before)
    (definition,) = source_entry(after)["definitions"]
    assert definition["term"].lower() == "revenue" and definition["text"] == "Revenue is orders.total after refunds"


async def test_the_same_words_in_two_profiles_dont_clash(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await two_profiles_describing_name(h, "Legal name", "Legal name")
        listed = await h.call("list_sources")
    assert table_entry(listed, "customers")["column_notes"] == {"name": "Legal name"}
