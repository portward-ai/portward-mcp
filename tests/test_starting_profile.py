"""A starting profile from Free's block rules and notes (#92): when a Free user upgrades, Portward offers to turn each
connection's block rules and notes into a starting profile that grants every column not blocked, by name, in full,
with the notes on what it grants carried over as context. Driven through the MCP client harness, against an
in-memory account service."""

from __future__ import annotations

from pathlib import Path

from test_profiles import User, columns_of, seen, tool

from conftest import SECRETS


async def block_some(h) -> None:
    await h.call("block", source="people", table="customers", column="ssn")
    await h.call("block", source="people", table="notes")


async def test_the_starting_profile_grants_exactly_what_the_block_rules_let_the_ai_see(home: Path,
                                                                                      people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await block_some(h)
        before = columns_of(await h.call("list_sources"))
        started = await h.call("start_profile_from_block_rules", source="people")
        after = columns_of(await h.call("list_sources"))
        ssn = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
    assert not started.is_error, started.ai.text
    grants = {g["name"] for g in started.ai.structured["profile"]["grants"]}
    # SQLite views stay listed under block rules (the engine-level block holds inside them), so they're granted too
    assert grants == {"customers.id", "customers.name", "orders.id", "orders.customer_id", "orders.total",
                      "cards.name", "cards.number", "memo.text", "ids.r", "ids.name"}
    assert grants == {f"{t}.{c}" for t, cs in before.items() for c in cs}
    assert {g["level"] for g in started.ai.structured["profile"]["grants"]} == {"full"}
    assert after == before
    assert ssn.is_error and not any(s in seen(ssn) for s in SECRETS)


async def test_the_starting_profile_carries_notes_on_granted_tables_and_columns_and_drops_the_rest(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await block_some(h)
        await h.call("set_note", source="people", table="customers", note="one row per paying customer")
        await h.call("set_note", source="people", table="customers", column="name", note="legal name")
        await h.call("set_note", source="people", table="customers", column="ssn", note="tax id, US only")
        await h.call("set_note", source="people", table="notes", note="internal plans")
        await h.call("set_note", source="people", table="orders", column="total", note="in euros")
        started = await h.call("start_profile_from_block_rules", source="people")
        listed = await h.call("list_profiles")
    assert not started.is_error, started.ai.text
    expected = [{"about": "customers", "meaning": "one row per paying customer"},
                {"about": "customers.name", "meaning": "legal name"},
                {"about": "orders.total", "meaning": "in euros"}]
    assert started.ai.structured["profile"]["context"]["meanings"] == expected
    assert listed.ai.structured["profiles"][0]["context"]["meanings"] == expected  # kept with the saved profile
    assert "tax id" not in seen(started) and "internal plans" not in seen(started)


async def test_declining_leaves_the_connection_on_block_rules(home: Path, people_db: Path) -> None:
    user = User(yes=False)
    async with tool(home, people_db, user=user) as h:
        await block_some(h)
        started = await h.call("start_profile_from_block_rules", source="people")
        listed = await h.call("list_profiles")
        ssn = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
        name = await h.call("query", source="people", question="?", sql="SELECT name FROM customers")
        block = await h.call("block", source="people", table="orders", column="total")
    assert started.is_error and user.asked
    assert "2 block rules" in user.asked[0] and "customers.name" in user.asked[0]
    assert listed.ai.structured["profiles"] == []  # nothing saved
    assert ssn.is_error and not name.is_error  # still the block rules, allow by default
    assert not block.is_error  # block rules still edit: the connection isn't on profiles


async def test_it_needs_personal_and_a_connection_still_on_block_rules(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, plan="ended") as h:
        lapsed = await h.call("start_profile_from_block_rules", source="people")
    async with tool(home, people_db) as h:
        first = await h.call("start_profile_from_block_rules", source="people")
        again = await h.call("start_profile_from_block_rules", source="people")
    assert lapsed.is_error and "Unknown tool" in lapsed.ai.text  # #116: not listed on Free
    assert not first.is_error, first.ai.text
    assert again.is_error and "already runs on profiles" in again.ai.text


async def test_a_name_already_taken_is_refused_before_asking(home: Path, people_db: Path) -> None:
    user = User()
    async with tool(home, people_db, user=user) as h:
        await h.call("create_profile", name="people", description="x", grants=["customers.name"])
        taken = await h.call("start_profile_from_block_rules", source="people")
        named = await h.call("start_profile_from_block_rules", source="people", name="People basics")
    assert taken.is_error and "already a profile named" in taken.ai.text
    assert not named.is_error and named.ai.structured["profile"]["name"] == "People basics"


async def test_personal_users_are_offered_a_starting_profile_for_each_connection_on_block_rules(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        offered = await h.call("list_profiles")
        await h.call("start_profile_from_block_rules", source="people")
        after = await h.call("list_profiles")
    assert offered.ai.structured["starting_profile_offers"] == ["people"]
    assert "start_profile_from_block_rules" in offered.ai.text
    assert after.ai.structured["starting_profile_offers"] == []


async def test_removing_a_grant_drops_its_meaning_so_context_never_speaks_about_what_is_hidden(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await block_some(h)
        await h.call("set_note", source="people", table="customers", column="name", note="legal name")
        await h.call("set_note", source="people", table="orders", column="total", note="in euros")
        await h.call("start_profile_from_block_rules", source="people")
        updated = await h.call("update_profile", profile="people", remove=["customers.name"])
    assert not updated.is_error, updated.ai.text
    assert updated.ai.structured["profile"]["context"]["meanings"] == [{"about": "orders.total", "meaning": "in euros"}]
