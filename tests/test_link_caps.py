"""Linked columns share one stand-in key (#121), so a column linked to an anonymised one can't be shown in full: the
AI would read the real value off the full column and the stand-in off the anonymised one, and map every stand-in
back. The spec's rule "a value anonymised on any row is anonymised on every row" holds across the link group: when
any column of a group is anonymised (or totals only) in any applied profile, on any connection, every column of the
group is at most anonymised for the AI. The user is told so in why-hidden and the profile editor's preview; the AI
is only told the columns are anonymised. Driven through the MCP client harness."""

from __future__ import annotations

import shutil
from pathlib import Path

from test_anonymised import STAND_IN, tool
from test_context import Reviewer
from test_links import linked
from test_profiles import apply, make
from test_why_hidden import lookup

from portward_free import profile_editor as pe
from portward_free.paths import database_path
from portward_free.storage import LocalStore
from portward_free.why_hidden import Kind

ADA_ID = "SELECT id FROM customers WHERE name = 'Ada'"
ADA_ORDER = "SELECT customer_id FROM orders WHERE total = 10"  # Ada's order


async def first_value(h, sql: str, source: str = "people") -> object:
    reply = await h.call("query", source=source, question="?", sql=sql)
    assert not reply.is_error, reply.ai.text
    return reply.ai.structured["rows"][0][0]


async def full_linked_to_anonymised(h) -> None:
    await make(h, "Sales", ["customers.id", "customers.name", "orders.total"], anonymised=["orders.customer_id"])
    await apply(h, "Sales")
    await linked(h, "Sales", "orders.customer_id", "customers.id")


async def test_a_full_column_linked_to_an_anonymised_one_is_anonymised_too(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await full_linked_to_anonymised(h)
        customer = await first_value(h, ADA_ID)
        order = await first_value(h, ADA_ORDER)
        joined = await h.call("query", source="people", question="?",
                              sql="SELECT c.name, o.total FROM orders o JOIN customers c ON c.id = o.customer_id "
                                  "ORDER BY c.name")
        described = await h.call("describe_source", source="people")
    assert isinstance(customer, str) and STAND_IN.fullmatch(customer), customer  # never the real 1
    assert customer == order  # one link group: they still join
    assert joined.ai.structured["rows"] == [["Ada", 10.0], ["Grace", 20.0], ["Linus", 30.0]]
    assert "customers.id" in str(described.ai.structured.get("notices"))


async def test_a_column_anonymised_on_another_connection_lowers_its_link_group_here(home: Path, people_db: Path,
                                                                                   tmp_path: Path) -> None:
    copy = tmp_path / "copy.sqlite"
    shutil.copy(people_db, copy)
    async with tool(home, people_db, user=Reviewer()) as h:
        added = await h.add_file("copy", copy)
        assert not added.is_error, added.ai.text
        await make(h, "Open", ["customers.id", "customers.name", "orders.customer_id", "orders.total"])
        await apply(h, "Open")
        await linked(h, "Open", "orders.customer_id", "customers.id")
        await make(h, "Careful", ["customers.id", "customers.name", "orders.total"],
                   anonymised=["orders.customer_id"])
        await apply(h, "Careful", "copy")
        await linked(h, "Careful", "orders.customer_id", "customers.id")
        there = await first_value(h, ADA_ORDER, "copy")
        here_id = await first_value(h, ADA_ID)
        here_order = await first_value(h, ADA_ORDER)
    assert STAND_IN.fullmatch(str(there))
    assert here_id == here_order == there  # not 1: the stand-in can't be mapped back through people


async def test_why_hidden_and_the_editor_preview_say_the_link_lowered_it(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await full_linked_to_anonymised(h)
        listed = await h.call("list_sources")
    found = lookup(home)
    (item,) = [i for i in found.about("customers.id") if i.kind is Kind.LINKED]
    assert "orders.customer_id" in item.why and "anonymised" in item.why
    store = LocalStore.open(database_path(home))
    try:
        profile = store.profiles.named("Sales")
        assert profile is not None
        from test_why_hidden import PEOPLE
        shown = pe.rows_preview(store, pe.Draft(profile), "people", PEOPLE)
    finally:
        store.close()
    assert not isinstance(shown, str)
    assert [c.name for c in shown.linked] == ["customers.id"]
    assert "orders.customer_id" in shown.linked[0].text
    ai = str(listed.ai.text) + str(listed.ai.structured)
    assert item.why not in ai and shown.linked[0].text not in ai  # the AI is never told why


async def test_unlinked_columns_keep_their_own_levels(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "customers.name", "orders.total"], anonymised=["orders.customer_id"])
        await apply(h, "Sales")
        customer = await first_value(h, ADA_ID)
    assert customer == 1
