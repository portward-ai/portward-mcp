"""Stand-ins match only across linked columns (#121, spec #114 story 15): a stand-in is scoped to the column's link
group, the columns joined by the profile's links (or the column alone when it is unlinked). So customer 42 and product
42 get different stand-ins unless a link says the two columns hold the same values, and then they match in every
connection the profile is applied to and on every computer holding the account's anonymising key. Driven through the
MCP client harness."""

from __future__ import annotations

from pathlib import Path

from conftest import SECRETS
from harness import Harness, MemoryKeychain
from test_anonymised import STAND_IN, leaked, tool
from test_context import Reviewer
from test_links import linked

from portward_free import stand_ins

IDS = ["customers.id", "orders.customer_id"]


async def anonymise_ids(h: Harness, source: str = "people") -> None:
    made = await h.call("create_profile", name="Sales", description="sales work",
                        grants=["customers.name", "orders.total"], anonymised=IDS)
    assert not made.is_error, made.ai.text
    applied = await h.call("apply_profile", profile="Sales", source=source)
    assert not applied.is_error, applied.ai.text


async def stand_ins_of(h: Harness, sql: str, source: str = "people") -> list[str]:
    reply = await h.call("query", source=source, question="?", sql=sql)
    assert not reply.is_error, reply.ai.text
    found = [r[0] for r in reply.ai.structured["rows"]]
    assert all(STAND_IN.fullmatch(s) for s in found), found
    return found


CUSTOMER_ONE = "SELECT id FROM customers WHERE name = 'Ada'"
ORDER_OF_ONE = "SELECT customer_id FROM orders WHERE total = 10"  # customer 1's order


async def test_two_unlinked_anonymised_columns_with_the_same_value_get_different_stand_ins(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await anonymise_ids(h)
        (customer,) = await stand_ins_of(h, CUSTOMER_ONE)
        (order,) = await stand_ins_of(h, ORDER_OF_ONE)
        joined = await h.call("query", source="people", question="?",
                              sql="SELECT c.name FROM orders o JOIN customers c ON c.id = o.customer_id")
    assert customer != order
    assert not joined.is_error and joined.ai.structured["rows"] == []  # nothing joins without a link


async def test_two_linked_columns_get_the_same_stand_in_and_join_and_group_on_it(home: Path,
                                                                                 people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        await anonymise_ids(h)
        await linked(h, "Sales", "orders.customer_id", "customers.id")
        (customer,) = await stand_ins_of(h, CUSTOMER_ONE)
        (order,) = await stand_ins_of(h, ORDER_OF_ONE)
        joined = await h.call("query", source="people", question="?",
                              sql="SELECT c.name, sum(o.total) AS spent FROM orders o "
                                  "JOIN customers c ON c.id = o.customer_id GROUP BY c.name ORDER BY c.name")
        grouped = await h.call("query", source="people", question="?",
                               sql="SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id")
        filtered = await h.call("query", source="people", question="?",
                                sql=f"SELECT total FROM orders WHERE customer_id = '{customer}'")
    assert customer == order
    assert joined.ai.structured["rows"] == [["Ada", 10.0], ["Grace", 20.0], ["Linus", 30.0]]
    assert len(grouped.ai.structured["rows"]) == 3 and not leaked(grouped)
    assert filtered.ai.structured["rows"] == [[10.0]]


async def test_a_link_doesnt_change_the_stand_ins_of_columns_outside_it(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, user=Reviewer()) as h:
        made = await h.call("create_profile", name="Sales", description="sales work", grants=["customers.name"],
                            anonymised=[*IDS, "customers.ssn"])
        assert not made.is_error, made.ai.text
        assert not (await h.call("apply_profile", profile="Sales", source="people")).is_error
        ssn_sql = "SELECT ssn FROM customers WHERE name = 'Ada'"
        before = await stand_ins_of(h, ssn_sql)
        await linked(h, "Sales", "orders.customer_id", "customers.id")
        after = await stand_ins_of(h, ssn_sql)
        reply = await h.call("query", source="people", question="?", sql=ssn_sql)
    assert before == after
    assert SECRETS[0] not in repr(reply.ai.structured)


async def test_linked_columns_match_across_connections(home: Path, people_db: Path, tmp_path: Path) -> None:
    csv_file = tmp_path / "contacts.csv"
    csv_file.write_text("customer_ref,email\n1,ada@example.com\n2,grace@example.com\n")
    async with tool(home, people_db, user=Reviewer()) as h:
        added = await h.add_file("crm", csv_file, kind="csv")
        assert not added.is_error, added.ai.text
        made = await h.call("create_profile", name="Sales", description="sales work",
                            grants=["customers.name", "contacts.email"],
                            anonymised=["customers.id", "contacts.customer_ref"])
        assert not made.is_error, made.ai.text
        for source in ("people", "crm"):
            assert not (await h.call("apply_profile", profile="Sales", source=source)).is_error
        before = await stand_ins_of(h, "SELECT customer_ref FROM contacts WHERE email LIKE 'ada%'", "crm")
        await linked(h, "Sales", "customers.id", "contacts.customer_ref")
        (in_crm,) = await stand_ins_of(h, "SELECT customer_ref FROM contacts WHERE email LIKE 'ada%'", "crm")
        (in_people,) = await stand_ins_of(h, CUSTOMER_ONE)
    assert before != [in_people]  # unlinked, they didn't match
    assert in_crm == in_people


async def test_linked_columns_match_on_two_computers_sharing_the_key(tmp_path: Path, people_db: Path) -> None:
    laptop, desktop = MemoryKeychain(), MemoryKeychain()
    seen = []
    for name, keychain in (("laptop", laptop), ("desktop", desktop)):
        folder = tmp_path / name
        folder.mkdir()
        if name == "desktop":
            key = stand_ins.key_for_sync(laptop)
            assert key is not None
            stand_ins.adopt(desktop, key)  # what sync (#97) carries
        async with tool(folder, people_db, keychain=keychain, user=Reviewer()) as h:
            await anonymise_ids(h)
            await linked(h, "Sales", "customers.id", "orders.customer_id")  # either end first
            seen.append((await stand_ins_of(h, CUSTOMER_ONE), await stand_ins_of(h, ORDER_OF_ONE)))
    assert seen[0] == seen[1]
    assert seen[0][0] == seen[0][1]
