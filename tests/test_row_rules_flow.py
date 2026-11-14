"""Row rules through the product (#132): stored with the profile, enforced on every route (the MCP tools, the hosted
engine), synced to the user's other computers, named in the AI log, and never told to the AI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import Harness, MemoryKeychain
from test_profiles import apply, make, seen, tool
from test_sync import Service, device, marked_db, service, two_profiled_computers  # noqa: F401  (fixtures)

from portward_free.hosted import bundle
from portward_free.keychain import account_for
from portward_free.paths import database_path
from portward_free.profiles import Grant, Profile
from portward_free.row_rules import RowRule
from portward_free.storage import LocalStore
from portward_free.sync.profile_records import _valid_profile, profile_content, profile_from


def add_row_rule(home: Path, profile: str, table: str, text: str) -> Profile:
    """What the profile editor's save does: the next version, with the rule."""
    store = LocalStore.open(database_path(home))
    try:
        held = store.profiles.named(profile)
        assert held is not None
        rule = RowRule.parse(table, text)
        return store.profiles.save(held.saved("me@example.com", rows=(*held.rows, rule)))
    finally:
        store.close()


async def sales_on_two_customers(h: Harness, home: Path) -> None:
    await make(h, "Sales", ["customers.id", "customers.name"])
    await apply(h, "Sales")
    add_row_rule(home, "Sales", "customers", "id IN (1, 2)")


async def test_a_row_rule_saved_on_a_profile_keeps_the_ai_to_its_rows(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await sales_on_two_customers(h, home)
        reply = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        counted = await h.call("query", source="people", question="?", sql="SELECT COUNT(*) AS n FROM customers")
    assert not reply.is_error, reply.ai.text
    assert sorted(r[1] for r in reply.ai.structured["rows"]) == ["Ada", "Grace"]
    assert "Linus" not in seen(reply)
    assert counted.ai.structured["rows"] == [[2]]


async def test_the_ai_is_never_told_a_row_rule_exists(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await sales_on_two_customers(h, home)
        said = [seen(await h.call("list_sources")), seen(await h.call("describe_source", source="people")),
                seen(await h.call("list_profiles")),
                seen(await h.call("query", source="people", question="?", sql="SELECT name FROM customers"))]
    for text in said:
        assert "IN (1, 2)" not in text and "row rule" not in text.lower(), text


async def test_sample_values_and_value_meanings_arent_given_for_a_column_of_a_table_with_a_row_rule(
        home: Path, people_db: Path) -> None:
    """Samples are real values the user approved earlier, maybe from rows the rule now keeps from the AI."""
    from test_context import Reviewer, table_entry
    from test_value_meanings import approved_meaning

    async with tool(home, people_db, user=Reviewer()) as h:
        await make(h, "Sales", ["customers.id", "customers.name"])
        await apply(h, "Sales")
        await approved_meaning(h, "Sales", table="customers", column="name", meaning="The customer's name",
                               values={"Linus": "our third customer"}, samples=["Ada", "Linus"])
        before = table_entry(await h.call("list_sources"), "customers")
        add_row_rule(home, "Sales", "customers", "id IN (1, 2)")
        after = table_entry(await h.call("list_sources"), "customers")
    assert before["column_samples"] == {"name": ["Ada", "Linus"]}
    assert "column_samples" not in after and "column_values" not in after


async def test_the_ai_log_names_the_row_rules_a_read_went_through(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await sales_on_two_customers(h, home)
        await h.call("query", source="people", question="?", sql="SELECT name FROM customers")
        await h.call("query", source="people", question="?", sql="SELECT total FROM orders")
        logged = await h.call("ai_log")
        reads = h.stored("ai_reads")
    contexts = [json.loads(r["context"]) for r in reads]
    assert {"kind": "row_rule", "about": "customers: id IN (1, 2)", "profile": "Sales", "version": 2} in contexts[0]
    assert len(contexts) == 1 or all(c["kind"] != "row_rule" for c in contexts[1])
    assert "row rule customers: id IN (1, 2) (Sales v2)" in logged.ai.text


def test_a_row_rule_travels_with_its_profile_and_a_rule_outside_the_grammar_is_refused() -> None:
    profile = Profile.new("EU", "", "me@example.com", [Grant("deals", "id")],
                          rows=(RowRule.parse("deals", "region IN ('EU') AND amount > 5"),))
    content = profile_content(profile)
    assert _valid_profile(content)
    assert profile_from(content).rows == profile.rows
    for bad in ("region = 'EU' OR 1=1", {"op": "raw", "sql": "1=1"}, {"op": "=", "column": "region"}):
        tampered = json.loads(json.dumps(content))
        tampered["rows"][0]["where"] = bad
        assert not _valid_profile(tampered)


def test_rows_held_for_later_count_as_rules_changed_when_a_row_rule_changes() -> None:
    """Held pulls and selections keep the policy they were made under (Grants.payload) and compare it again."""
    from portward_free.grants import Grants
    from portward_free.rules import policy_from

    def under(text: str) -> Grants:
        return Grants.applied([Profile("p", "EU", "", "me", "me", 1, (Grant("deals", "id"),),
                                       rows=(RowRule.parse("deals", text),))])

    eu = under("region = 'EU'")
    assert policy_from(json.loads(json.dumps(eu.payload()))) == eu
    assert under("region IN ('EU', 'US')") != eu


def test_the_hosted_engine_gets_the_row_rules_and_keeps_to_them() -> None:
    computer, engine = LocalStore.memory(), LocalStore.memory()
    keychain, engine_keychain = MemoryKeychain(), MemoryKeychain()
    computer.add_connection("warehouse", "postgres", {"host": "db.zq-hosted-host.example", "port": 5432,
                                                      "database": "shop", "user": "reader"})
    keychain.set(account_for("warehouse"), "zq-login")
    eu = computer.profiles.save(Profile.new("EU", "", "me@example.com", [Grant("deals", "id")],
                                            rows=(RowRule.parse("deals", "region = 'EU'"),)))
    computer.profiles.switch_to_profiles("warehouse")
    computer.profiles.apply("warehouse", eu, ())
    sent = bundle.build(computer, keychain, ["warehouse"], b"k" * 32)
    bundle.land(engine, engine_keychain, sent)
    assert engine.rules_for("warehouse") == computer.rules_for("warehouse")
    assert engine.rules_for("warehouse").reach  # type: ignore[union-attr]
    sent["profiles"][0]["rows"][0]["where"] = "region = 'EU' OR 1 = 1"
    with pytest.raises(bundle.BadBundle):
        bundle.check(sent)


async def test_a_row_rule_syncs_to_the_users_other_computers(service: Service, tmp_path: Path,  # noqa: F811
                                                             marked_db: Path) -> None:  # noqa: F811
    async with two_profiled_computers(service, tmp_path, marked_db, "rows@gmail.example") as (laptop, desktop):
        add_row_rule(laptop.home, "zq profile marker", "zq_table_marker", "amount > 2")
        assert not (await laptop.h.call("sync_now")).is_error
        assert not (await desktop.h.call("sync_now")).is_error
        reply = await desktop.h.call("query", source="books", question="?", sql="SELECT amount FROM zq_table_marker")
    assert reply.ai.structured["rows"] == [[2.5]]


def test_a_profile_keeps_its_row_rules_across_saves_until_the_table_is_no_longer_granted() -> None:
    store = LocalStore.memory()
    made = store.profiles.save(Profile.new("EU", "", "me@example.com", [Grant("deals", "id"), Grant("owners", "x")],
                                           rows=(RowRule.parse("deals", "region = 'EU'"),)))
    assert store.profiles.by_id(made.id).rows == made.rows
    renamed = store.profiles.save(made.saved("me@example.com", name="EU deals"))
    assert renamed.rows == made.rows
    dropped = renamed.saved("me@example.com", grants=[Grant("owners", "x")])
    assert dropped.rows == ()
