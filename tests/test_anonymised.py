"""Anonymised grants (#93): a column granted anonymised reaches the AI only as stand-ins, the same value always
getting the same stand-in for one person, so the AI can still filter, group and join on it. The user sees the real
values in the results panel. Driven through the MCP client harness, against an in-memory account service."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import SECRETS
from harness import Harness, MemoryKeychain, connected
from test_context import Reviewer
from test_links import linked
from test_profiles import PlanAccounts, User, seen
from test_rules import ROUTES_TO_A_BLOCKED_COLUMN

from portward_free import stand_ins
from portward_free.account import Accounts
from portward_free.keychain import KeychainError

STAND_IN = re.compile(r"anon_[0-9a-f]{16}")


@asynccontextmanager
async def tool(home: Path, db: Path, *, keychain: MemoryKeychain | None = None, panel: bool = False,
               user: User | None = None, name: str = "people") -> AsyncIterator[Harness]:
    """The free tool with `db` added as `name`, signed in to an account with Personal on."""
    keychain = keychain or MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    async with connected(home, db, name=name, keychain=keychain, panel=panel, elicitation=user or User(),
                         accounts=Accounts(PlanAccounts("active"))) as h:
        yield h


async def anonymise_ssn(h: Harness, source: str = "people", **grants: Any) -> None:
    made = await h.call("create_profile", name="People", description="people work",
                        grants=grants.get("grants", ["customers.id", "customers.name"]),
                        anonymised=grants.get("anonymised", ["customers.ssn"]))
    assert not made.is_error, made.ai.text
    applied = await h.call("apply_profile", profile="People", source=source)
    assert not applied.is_error, applied.ai.text


def leaked(reply) -> bool:
    return any(s in seen(reply) for s in SECRETS)


async def test_an_anonymised_column_reaches_the_ai_as_stand_ins(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await anonymise_ssn(h)
        reply = await h.call("query", source="people", question="?", sql="SELECT name, ssn FROM customers")
    assert not reply.is_error, reply.ai.text
    assert not leaked(reply)
    ssns = [r[1] for r in reply.ai.structured["rows"]]
    assert all(STAND_IN.fullmatch(s) for s in ssns) and len(set(ssns)) == 3


@pytest.mark.parametrize("sql", ROUTES_TO_A_BLOCKED_COLUMN)
async def test_no_query_shape_gets_the_real_value_of_an_anonymised_column(home: Path, people_db: Path,
                                                                          sql: str) -> None:
    async with tool(home, people_db) as h:
        await anonymise_ssn(h, grants=["customers.id", "customers.name", "orders.id", "orders.customer_id",
                                       "orders.total"])
        reply = await h.call("query", source="people", question="?", sql=sql)
    assert not leaked(reply), reply.ai.text


async def test_the_ai_can_filter_group_and_join_on_stand_ins(home: Path, people_db: Path) -> None:
    """Joins across two anonymised columns need a link saying they hold the same values (#121,
    test_stand_in_scope)."""
    async with tool(home, people_db, user=Reviewer()) as h:
        await anonymise_ssn(h, grants=["customers.name", "orders.total"],
                            anonymised=["customers.ssn", "customers.id", "orders.customer_id"])
        await linked(h, "People", "orders.customer_id", "customers.id")
        first = await h.call("query", source="people", question="?", sql="SELECT name, ssn FROM customers")
        name, ssn = first.ai.structured["rows"][0]
        again = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
        filtered = await h.call("query", source="people", question="?",
                                sql=f"SELECT name FROM customers WHERE ssn = '{ssn}'")
        grouped = await h.call("query", source="people", question="?",
                               sql="SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id")
        joined = await h.call("query", source="people", question="?",
                              sql="SELECT c.name, count(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
                                  "GROUP BY c.name ORDER BY c.name")
    assert ssn in [r[0] for r in again.ai.structured["rows"]]  # the same value, the same stand-in
    assert filtered.ai.structured["rows"] == [[name]]
    assert all(STAND_IN.fullmatch(r[0]) for r in grouped.ai.structured["rows"])
    assert not joined.is_error and len(joined.ai.structured["rows"]) >= 1, joined.ai.text
    assert not leaked(grouped) and not leaked(joined)


async def ssn_stand_ins(h: Harness, source: str = "people") -> dict[str, str]:
    reply = await h.call("query", source=source, question="?", sql="SELECT name, ssn FROM customers")
    assert not reply.is_error, reply.ai.text
    return {name: ssn for name, ssn in reply.ai.structured["rows"]}


async def test_two_connections_give_the_same_value_the_same_stand_in(home: Path, people_db: Path,
                                                                     tmp_path: Path) -> None:
    csv_file = tmp_path / "customers.csv"
    csv_file.write_text(f"name,ssn\nAda,{SECRETS[0]}\nGrace,{SECRETS[1]}\n")
    async with tool(home, people_db) as h:
        await anonymise_ssn(h)
        added = await h.add_file("crm", csv_file, kind="csv")
        assert not added.is_error, added.ai.text
        applied = await h.call("apply_profile", profile="People", source="crm")
        assert not applied.is_error, applied.ai.text
        in_sqlite = await ssn_stand_ins(h)
        in_csv = await ssn_stand_ins(h, "crm")
    assert in_csv == {"Ada": in_sqlite["Ada"], "Grace": in_sqlite["Grace"]}


async def test_two_computers_sharing_the_key_give_the_same_stand_ins(tmp_path: Path, people_db: Path) -> None:
    laptop, desktop = MemoryKeychain(), MemoryKeychain()
    homes = [tmp_path / name for name in ("laptop", "desktop", "someone-else")]
    for folder in homes:
        folder.mkdir()
    async with tool(homes[0], people_db, keychain=laptop) as h:
        await anonymise_ssn(h)
        on_laptop = await ssn_stand_ins(h)
    key = stand_ins.key_for_sync(laptop)
    assert key is not None
    stand_ins.adopt(desktop, key)  # what sync (#97) carries to the account's other computers
    async with tool(homes[1], people_db, keychain=desktop) as h:
        await anonymise_ssn(h)
        on_desktop = await ssn_stand_ins(h)
    async with tool(homes[2], people_db, keychain=MemoryKeychain()) as h:
        await anonymise_ssn(h)
        someone_else = await ssn_stand_ins(h)
    assert on_desktop == on_laptop
    assert set(someone_else.values()).isdisjoint(on_laptop.values())


async def test_the_user_sees_real_values_in_the_panel_and_the_ai_gets_stand_ins_from_it(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db, panel=True) as h:
        await anonymise_ssn(h)
        pulled = await h.call("query", source="people", question="?", sql="SELECT name, ssn FROM customers")
        pull_id = pulled.ai.structured["pull_id"]
        shown = await h.call_from_panel("panel_rows", pull_id=pull_id)
        posted = await h.call_from_panel("put_in_chat", pull_id=pull_id, rows=[0, 1, 2], columns=["name", "ssn"])
        fetched = await h.call("get_selection", selection_id=posted.ai.structured["selection_id"])
    assert not shown.is_error, shown.ai.text
    assert sorted(r[1] for r in shown.ai.structured["rows"]) == sorted(SECRETS)
    assert shown.ai.structured["stand_ins"] == ["ssn"]  # the panel can say the AI gets stand-ins there
    assert not leaked(pulled) and not leaked(posted)
    assert not leaked(fetched) and len(STAND_IN.findall(fetched.ai.text)) == 3


# --- combining profiles ---------------------------------------------------------------------------------------------

async def test_combined_profiles_give_each_column_its_highest_level_never_two(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await anonymise_ssn(h, grants=["customers.name"], anonymised=["customers.ssn", "customers.id"])
        made = await h.call("create_profile", name="Ids", description="ids", grants=["customers.id"])
        assert not made.is_error, made.ai.text
        await h.call("apply_profile", profile="Ids", source="people")
        reply = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
    assert reply.ai.structured["columns"] == ["id", "name", "ssn"]  # one column each, never full and anonymised
    ids = [r[0] for r in reply.ai.structured["rows"]]
    ssns = [r[2] for r in reply.ai.structured["rows"]]
    assert sorted(ids) == [1, 2, 3]  # full beats anonymised
    assert all(STAND_IN.fullmatch(s) for s in ssns)  # anonymised beats hidden


async def test_full_beats_anonymised_beats_totals_when_profiles_combine(home: Path, people_db: Path) -> None:
    """#93 x #94: each column gets one level, the highest any applied profile grants: full, then anonymised, then
    totals."""
    async with tool(home, people_db) as h:
        await anonymise_ssn(h, grants=["orders.id", "orders.customer_id"], anonymised=["orders.total"])
        made = await h.call("create_profile", name="Totals", description="totals",
                            totals=["orders.total", "orders.customer_id"])
        assert not made.is_error, made.ai.text
        assert not (await h.call("apply_profile", profile="Totals", source="people")).is_error
        rows = await h.call("query", source="people", question="?",
                            sql="SELECT customer_id, total FROM orders ORDER BY id")
        both = await h.call("create_profile", name="Both", description="both", anonymised=["orders.total"],
                            totals=["orders.total"])
    assert not rows.is_error, rows.ai.text  # neither column is held to totals: each has a higher grant
    assert [r[0] for r in rows.ai.structured["rows"]] == [1, 2, 3]  # full beats totals
    assert all(STAND_IN.fullmatch(r[1]) for r in rows.ai.structured["rows"])  # anonymised beats totals
    assert both.is_error and "one level" in both.ai.text  # one profile names a column at one level only


async def test_a_profile_lists_each_grant_with_its_level(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await anonymise_ssn(h)
        listed = await h.call("list_profiles")
        both = await h.call("create_profile", name="Both", description="x", grants=["customers.ssn"],
                            anonymised=["customers.ssn"])
    (profile,) = listed.ai.structured["profiles"]
    assert {"name": "customers.ssn", "level": "anonymised"} in profile["grants"]
    assert {"name": "customers.name", "level": "full"} in profile["grants"]
    assert both.is_error and "one level" in both.ai.text


async def test_raising_an_applied_anonymised_column_to_full_asks_the_user_and_lowering_does_not(
        home: Path, people_db: Path) -> None:
    user = User(yes=False)
    async with tool(home, people_db, user=user) as h:
        await anonymise_ssn(h)
        raised = await h.call("update_profile", profile="People", add=["customers.ssn"])
        still = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
        lowered = await h.call("update_profile", profile="People", anonymised=["customers.name"])
        names = await h.call("query", source="people", question="?", sql="SELECT name FROM customers")
    assert raised.is_error and any("customers.ssn" in q for q in user.asked)
    assert not leaked(still)
    assert not lowered.is_error, lowered.ai.text
    assert len(user.asked) == 1
    assert all(STAND_IN.fullmatch(r[0]) for r in names.ai.structured["rows"])


async def test_a_second_profile_showing_an_anonymised_column_in_full_asks_the_user(
        home: Path, people_db: Path) -> None:
    user = User(yes=False)
    async with tool(home, people_db, user=user) as h:
        await anonymise_ssn(h)
        await h.call("create_profile", name="Full", description="x", grants=["customers.ssn"])
        applied = await h.call("apply_profile", profile="Full", source="people")
    assert applied.is_error and any("customers.ssn" in q for q in user.asked)


# --- what the AI is told, and failing closed ------------------------------------------------------------------------

async def test_the_ai_is_told_which_columns_are_stand_ins(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await anonymise_ssn(h)
        described = await h.call("describe_source", source="people")
        listed = await h.call("list_sources")
    assert "ssn" in described.ai.text and "stand-in" in described.ai.text
    assert "stand-in" in seen(listed)


@pytest.mark.parametrize("sql", ["SELECT pw_stand_in(name) FROM customers",
                                 "SELECT getvariable('pw_stand_in_ipad') FROM customers",
                                 "SELECT current_setting('portward.stand_in_ipad') FROM customers"])
async def test_the_ai_cant_make_stand_ins_or_read_the_key(home: Path, people_db: Path, sql: str) -> None:
    async with tool(home, people_db) as h:
        await anonymise_ssn(h)
        reply = await h.call("query", source="people", question="?", sql=sql)
    assert reply.is_error and "not allowed" in reply.ai.text


class LockedKeyKeychain(MemoryKeychain):
    """A keychain that holds the account token but can't give the anonymising key."""

    def get(self, account: str) -> str | None:
        if account == stand_ins.KEY_ACCOUNT:
            raise KeychainError("locked")
        return super().get(account)


async def test_without_the_anonymising_key_an_anonymised_column_is_hidden_not_shown(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db, keychain=LockedKeyKeychain()) as h:
        await anonymise_ssn(h)
        star = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        named = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
    assert star.ai.structured["columns"] == ["id", "name"]
    assert named.is_error and not leaked(named)
