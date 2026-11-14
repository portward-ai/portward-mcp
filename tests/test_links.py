"""Links between columns (#96, Personal): the AI drafts that a column of one table matches a column of another (in the
same connection or a different one), the user approves it like any other context, and from then on the AI is told to
join on exactly those columns: in SQL when both tables are in one connection, across connections by pulling both and
joining on them. A link never names a column the AI can't see. Driven through the MCP client harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_context import Reviewer, source_entry
from test_profiles import apply, make, seen, tool


async def draft_link(h, profile: str, column: str, other: str) -> str:
    drafted = await h.call("draft_link", profile=profile, column=column, other_column=other)
    assert not drafted.is_error, drafted.ai.text
    (draft_id,) = drafted.ai.structured["drafts"]
    return draft_id


async def linked(h, profile: str, column: str, other: str) -> Any:
    approved = await h.call("approve_context", draft_id=await draft_link(h, profile, column, other))
    assert not approved.is_error, approved.ai.text
    return approved


async def test_a_link_in_one_connection_tells_the_ai_to_join_on_the_linked_columns(home: Path,
                                                                                   people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "customers.name", "orders.customer_id", "orders.total"])
        await apply(h, "Sales")
        await linked(h, "Sales", "orders.customer_id", "customers.id")
        listed = await h.call("list_sources")
        described = await h.call("describe_source", source="people")
        pulled = await h.call("query", source="people", question="Revenue by customer?",
                              sql="SELECT customer_id, sum(total) FROM orders GROUP BY customer_id")
    (link,) = source_entry(listed)["links"]
    assert link["columns"] == ["orders.customer_id", "customers.id"]
    assert "JOIN customers ON orders.customer_id = customers.id" in link["join"]
    assert described.ai.structured["links"] == source_entry(listed)["links"]
    assert link in pulled.ai.structured["links"]
    assert "orders.customer_id = customers.id" in pulled.ai.text


async def crm(h, tmp_path: Path) -> None:
    """A second source, "crm": contacts whose customer_ref is people's customers.id."""
    csv_file = tmp_path / "contacts.csv"
    csv_file.write_text("customer_ref,email,secret_score\n1,ada@example.com,9\n2,grace@example.com,7\n")
    added = await h.add_file("crm", csv_file, kind="csv")
    assert not added.is_error, added.ai.text


async def test_a_link_across_connections_tells_the_ai_to_pull_both_and_join_on_the_linked_columns(
        home: Path, people_db: Path, tmp_path: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await crm(h, tmp_path)
        await make(h, "Sales", ["customers.id", "customers.name", "contacts.customer_ref", "contacts.email"])
        await apply(h, "Sales")
        await apply(h, "Sales", source="crm")
        await linked(h, "Sales", "customers.id", "contacts.customer_ref")
        listed = await h.call("list_sources")
        pulled = await h.call("query", source="crm", question="Emails?", sql="SELECT customer_ref, email FROM contacts")
    (here,) = source_entry(listed)["links"]
    assert here["columns"] == ["customers.id", "contacts.customer_ref"] and here["source"] == "crm"
    assert "pull from both sources" in here["join"] and "customers.id = contacts.customer_ref" in here["join"]
    (there,) = source_entry(listed, "crm")["links"]
    assert there["columns"] == ["contacts.customer_ref", "customers.id"] and there["source"] == "people"
    assert there in pulled.ai.structured["links"]


async def test_a_link_whose_other_column_the_ai_cant_see_is_not_mentioned(home: Path, people_db: Path,
                                                                         tmp_path: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await crm(h, tmp_path)
        await make(h, "Sales", ["customers.id", "contacts.customer_ref"])
        await make(h, "Mailing", ["contacts.email"])
        await apply(h, "Sales")
        await apply(h, "Mailing", source="crm")  # crm shows only contacts.email: customer_ref is hidden there
        await linked(h, "Sales", "customers.id", "contacts.customer_ref")
        listed = await h.call("list_sources")
        described = await h.call("describe_source", source="people")
    assert "links" not in source_entry(listed) and "links" not in described.ai.structured
    assert "customer_ref" not in seen(listed)


async def test_only_columns_the_profile_grants_can_be_linked(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "orders.customer_id"])
        hidden = await h.call("draft_link", profile="Sales", column="orders.customer_id", other_column="customers.ssn")
        same_table = await h.call("draft_link", profile="Sales", column="customers.id", other_column="customers.ssn")
        waiting = await h.call("context_drafts")
    assert hidden.is_error and "grant" in hidden.ai.text
    assert same_table.is_error and "two different tables" in same_table.ai.text
    assert waiting.ai.structured["drafts"] == []


async def test_a_drafted_link_isnt_used_until_the_user_approves_it(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer(approve=False)) as h:
        await make(h, "Sales", ["customers.id", "orders.customer_id"])
        await apply(h, "Sales")
        draft_id = await draft_link(h, "Sales", "orders.customer_id", "customers.id")
        waiting = await h.call("context_drafts")
        before = await h.call("list_sources")
        declined = await h.call("approve_context", draft_id=draft_id)
        after = await h.call("list_sources")
    (draft,) = waiting.ai.structured["drafts"]
    assert draft["kind"] == "link" and draft["about"] == "orders.customer_id = customers.id"
    assert declined.is_error
    assert "links" not in source_entry(before) and "links" not in source_entry(after)


async def test_a_link_goes_once_either_column_is_no_longer_granted(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "customers.name", "orders.customer_id"])
        await apply(h, "Sales")
        await linked(h, "Sales", "orders.customer_id", "customers.id")
        removed = await h.call("update_profile", profile="Sales", remove=["customers.id"])
        listed = await h.call("list_sources")
        profiles = await h.call("list_profiles")
    assert not removed.is_error, removed.ai.text
    assert "links" not in source_entry(listed)
    (profile,) = profiles.ai.structured["profiles"]
    assert profile["context"]["links"] == []
