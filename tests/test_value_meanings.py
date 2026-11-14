"""Value meanings, sample values and synonyms (#131, Personal): a meaning can say what a column's values mean ("V means
void"), give a few sample values so the AI filters on real spellings, and list other words for a table or column
("deal" for "opportunity"). They reach the AI with the schema once approved. Sample values and value meanings are
real values, so they are only given where the AI gets the column in full: never for a column it gets as stand-ins or
totals. Driven through the MCP client harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_context import Reviewer, source_entry, table_entry
from test_context_editor import SAVE, open_context, press
from test_local_page import Browser
from test_profile_editor import follow, personal, stored
from test_profiles import apply, make, seen, tool


async def approved_meaning(h, profile: str, **meaning: Any) -> Any:
    drafted = await h.call("draft_meanings", profile=profile, meanings=[meaning])
    assert not drafted.is_error, drafted.ai.text
    (draft_id,) = drafted.ai.structured["drafts"]
    approved = await h.call("approve_context", draft_id=draft_id)
    assert not approved.is_error, approved.ai.text
    return approved


async def test_synonyms_and_value_meanings_reach_the_ai_with_the_schema(home: Path, people_db: Path) -> None:
    reviewer = Reviewer()
    async with tool(home, people_db, user=reviewer) as h:
        await make(h, "Sales", ["customers.id", "customers.name", "orders.customer_id", "orders.total"])
        await apply(h, "Sales")
        await approved_meaning(h, "Sales", table="orders", meaning="One row per sale", synonyms=["deals", "sales"])
        await approved_meaning(h, "Sales", table="customers", column="name", meaning="The customer's name",
                               values={"Ada": "our first customer"}, synonyms=["client name"],
                               samples=["Ada", "Grace"])
        listed = await h.call("list_sources")
        described = await h.call("describe_source", source="people")
    orders, customers = table_entry(listed, "orders"), table_entry(listed, "customers")
    assert orders["synonyms"] == ["deals", "sales"]
    assert customers["column_synonyms"] == {"name": ["client name"]}
    assert customers["column_values"] == {"name": {"Ada": "our first customer"}}
    assert customers["column_samples"] == {"name": ["Ada", "Grace"]}
    (name,) = [c for t in described.ai.structured["tables"] if t["name"] == "customers"
               for c in t["columns"] if c["name"] == "name"]
    assert name["values"] == {"Ada": "our first customer"} and name["sample_values"] == ["Ada", "Grace"]
    assert name["synonyms"] == ["client name"]
    assert any("our first customer" in asked and "client name" in asked for asked in reviewer.asked)


async def test_sample_values_and_value_meanings_are_given_only_for_columns_granted_in_full(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "customers.name"])
        await apply(h, "Sales")
        await approved_meaning(h, "Sales", table="customers", column="name", meaning="The customer's name",
                               values={"Ada": "our first customer"}, samples=["Ada", "Grace"],
                               synonyms=["client name"])
        full = await h.call("list_sources")
        changed = await h.call("update_profile", profile="Sales", anonymised=["customers.name"])
        assert not changed.is_error, changed.ai.text
        anonymised = await h.call("list_sources")
        described = await h.call("describe_source", source="people")
    assert table_entry(full, "customers")["column_samples"] == {"name": ["Ada", "Grace"]}
    customers = table_entry(anonymised, "customers")
    assert "column_samples" not in customers and "column_values" not in customers
    assert customers["column_synonyms"] == {"name": ["client name"]}
    assert "Grace" not in seen(anonymised) and "our first customer" not in seen(anonymised)
    assert "Grace" not in seen(described) and "our first customer" not in seen(described)


async def test_a_column_the_ai_only_gets_totals_of_gets_no_sample_values(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["orders.id"], totals=["orders.total"])
        await apply(h, "Sales")
        await approved_meaning(h, "Sales", table="orders", column="total", meaning="What the order cost",
                               samples=["10.0", "20.0"], values={"0": "free sample"})
        listed = await h.call("list_sources")
    orders = table_entry(listed, "orders")
    assert "column_samples" not in orders and "column_values" not in orders
    assert orders["column_notes"] == {"total": "What the order cost"}


async def test_value_meanings_and_samples_belong_to_a_column_not_a_table(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["orders.id", "orders.total"])
        refused = await h.call("draft_meanings", profile="Sales",
                               meanings=[{"table": "orders", "meaning": "Sales", "samples": ["1"]}])
    assert refused.is_error and "column" in refused.ai.text


async def test_the_context_editor_shows_a_drafted_meanings_values_samples_and_synonyms(
        home: Path, people_db: Path) -> None:
    browser = Browser()
    async with personal(home, people_db, browser=browser) as h:
        await make(h, "Sales", ["customers.id", "customers.name"])
        await apply(h, "Sales")
        drafted = await h.call("draft_meanings", profile="Sales", meanings=[{
            "table": "customers", "column": "name", "meaning": "The customer's name",
            "values": {"Ada": "our first customer"}, "samples": ["Grace"], "synonyms": ["client name"]}])
        assert not drafted.is_error, drafted.ai.text
        url, page = await open_context(h, browser)
        follow(url, press(url, press(url, page, {"action": "approve"}), SAVE))
        described = await h.call("describe_source", source="people")
    assert "our first customer" in page.body and "client name" in page.body and "Grace" in page.body
    (meaning,) = stored(home, "Sales")[0].context.meanings
    assert meaning.values == (("Ada", "our first customer"),) and meaning.synonyms == ("client name",)
    assert "client name" in seen(described)


async def test_a_synonym_naming_a_hidden_column_holds_the_meaning_back(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "customers.name"])
        await apply(h, "Sales")
        await approved_meaning(h, "Sales", table="customers", column="name", meaning="The customer's name",
                               synonyms=["ssn"])
        listed = await h.call("list_sources")
    assert "column_notes" not in table_entry(listed, "customers")
    assert "ssn" not in repr(source_entry(listed)["tables"])
