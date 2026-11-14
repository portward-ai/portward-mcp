"""Profiles with full grants (#91): a Personal user writes profiles that grant tables and columns by name and applies
them to a connection; from then on the AI sees only what the applied profiles grant. Driven through the MCP client
harness, against an in-memory account service (nothing leaves this machine)."""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from harness import Harness, MemoryKeychain, connected
from mcp.types import ElicitResult
from test_rules import ROUTES_TO_A_BLOCKED_COLUMN

from conftest import SECRETS
from portward_free import usage
from portward_free.account import Accounts, AccountService, AccountServiceError
from portward_free.grants import Grants
from portward_free.paths import database_path
from portward_free.profiles import Grant, preview
from portward_free.sources import Table
from portward_free.storage import LocalStore

EMAIL = "sam@acme.example"


class PlanAccounts(AccountService):
    """The account service in memory: /v1/me answers with `plan` (trial, active, grace or ended)."""

    def __init__(self, plan: str = "active") -> None:
        super().__init__("http://127.0.0.1:9")
        self.plan = plan
        self.registered_keys: dict[str, str] = {}  # #135: key id -> public key, as the account has them registered

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
              token: str | None = None) -> dict[str, Any]:
        if token != "pwat_fake":
            raise AccountServiceError("sign in first", 401)
        if path == "/v1/me":
            return {"id": "acct_1", "email": EMAIL, "share_devices": 1,
                    "plan": {"state": self.plan, "until": None, "interval": None, "payment_failed": False}}
        if path == "/v1/me/signing-keys" and method == "POST":
            public = str((body or {})["public_key"])
            made = hashlib.sha256(base64.urlsafe_b64decode(public + "=" * (-len(public) % 4))).hexdigest()[:16]
            self.registered_keys[made] = public
            return {"key_id": made, "registered_at": 0}
        if path == "/v1/me/signing-keys":
            return {"keys": [{"key_id": k, "public_key": p, "label": "computer", "registered_at": 0}
                             for k, p in self.registered_keys.items()]}
        raise AccountServiceError("not in this fake", 404)


class Recorder:
    def send(self, payload: dict) -> bool:
        return True


class User:
    """Answers the client's own prompt the way a person would, and remembers what they were asked."""

    def __init__(self, yes: bool = True) -> None:
        self.yes = yes
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": self.yes})


@asynccontextmanager
async def tool(home: Path, db: Path, *, plan: str | None = "active", user: User | None = None,
               accounts: PlanAccounts | None = None) -> AsyncIterator[Harness]:
    """The free tool with `db` added as the source "people", signed in to an account on `plan` (None: signed out)."""
    keychain = MemoryKeychain()
    if plan is not None:
        keychain.set("account-token", "pwat_fake")
    async with connected(home, db, name="people", keychain=keychain, elicitation=user or User(),
                         accounts=Accounts(accounts or PlanAccounts(plan or "ended"))) as h:
        yield h


def seen(reply) -> str:
    return reply.ai.text + repr(reply.ai.structured)


def leaked(reply) -> bool:
    return any(s in seen(reply) for s in SECRETS)


async def make(h: Harness, name: str, grants: list[str], **extra: Any):
    made = await h.call("create_profile", name=name, description=f"{name} work", grants=grants, **extra)
    assert not made.is_error, made.ai.text
    return made


async def apply(h: Harness, profile: str, source: str = "people"):
    applied = await h.call("apply_profile", profile=profile, source=source)
    assert not applied.is_error, applied.ai.text
    return applied


def columns_of(listed, source: str = "people") -> dict[str, list[str]]:
    (entry,) = [s for s in listed.ai.structured["sources"] if s["name"] == source]
    return {t["name"]: t["columns"] for t in entry["tables"]}


# --- plan -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("plan", [None, "ended"])
async def test_profile_tools_refuse_without_an_active_personal_plan(home: Path, people_db: Path,
                                                                     plan: str | None) -> None:
    async with tool(home, people_db, plan=plan) as h:
        h.choose_mode("people", "results")  # Free with rows reaching the AI (#117)
        made = await h.call("create_profile", name="Sales", description="pipeline", grants=["customers.name"])
        applied = await h.call("apply_profile", profile="Sales", source="people")
        # Free is unchanged: everything not blocked is seen, and block rules work
        whole = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
        blocked = await h.call("block", source="people", table="customers", column="ssn")
    assert made.is_error and "Unknown tool" in made.ai.text  # #116: Personal's tools aren't listed on Free
    assert applied.is_error and "Unknown tool" in applied.ai.text
    assert not whole.is_error and SECRETS[0] in seen(whole)
    assert not blocked.is_error


async def test_a_trial_counts_as_personal(home: Path, people_db: Path) -> None:
    async with tool(home, people_db, plan="trial") as h:
        made = await h.call("create_profile", name="Sales", description="pipeline", grants=["customers.name"])
    assert not made.is_error, made.ai.text


# --- profiles and versions ------------------------------------------------------------------------------------------

async def test_a_new_profile_shows_who_made_it_and_its_version(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        made = await make(h, "Sales", ["customers.name", "main.orders.total"])
        listed = await h.call("list_profiles")
    assert made.ai.structured["profile"]["version"] == 1
    assert f"made by {EMAIL} · last changed by {EMAIL}, v1" in made.ai.text
    (profile,) = listed.ai.structured["profiles"]
    assert profile["name"] == "Sales" and profile["description"] == "Sales work"
    assert profile["grants"] == [{"name": "customers.name", "level": "full"},
                                 {"name": "main.orders.total", "level": "full"}]


async def test_saving_a_profile_publishes_a_whole_new_version(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        saved = await h.call("update_profile", profile="sales", add=["orders.total"], remove=["customers.name"],
                             description="orders only")
        again = await h.call("update_profile", profile="Sales", name="Orders")
        listed = await h.call("list_profiles")
    assert not saved.is_error, saved.ai.text
    assert saved.ai.structured["profile"]["version"] == 2
    assert again.ai.structured["profile"]["version"] == 3 and "v3" in again.ai.text
    (profile,) = listed.ai.structured["profiles"]
    assert profile["name"] == "Orders" and profile["description"] == "orders only"
    assert [g["name"] for g in profile["grants"]] == ["orders.total"]


async def test_granting_a_table_lists_its_columns_by_name(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        made = await make(h, "Orders", [], tables=["orders"], source="people")
    assert [g["name"] for g in made.ai.structured["profile"]["grants"]] == [
        "orders.id", "orders.customer_id", "orders.total"]


async def test_a_grant_that_isnt_schema_table_column_is_refused(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        bad = await h.call("create_profile", name="Sales", description="x", grants=["customers"])
        wild = await h.call("create_profile", name="Sales", description="x", grants=["customers.*"])
    assert bad.is_error and "schema.table.column" in bad.ai.text
    assert wild.is_error


async def test_two_profiles_cannot_share_a_name(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        twice = await h.call("create_profile", name="sales", description="x", grants=["orders.total"])
    assert twice.is_error


# --- applying: the match preview ------------------------------------------------------------------------------------

async def test_applying_shows_matched_hidden_here_and_not_in_your_data_with_counts(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await h.call("block", source="people", table="notes")  # nothing to switch away from but a rule
        await make(h, "Sales", ["customers.name", "CUSTOMERS.ID", "main.orders.total", "customers.phone",
                                "other.orders.total"])
        applied = await apply(h, "Sales")
    preview = applied.ai.structured["preview"]
    assert preview["counts"] == {"matched": 3, "hidden_here": 0, "not_in_your_data": 2}
    assert sorted(preview["matched"]) == ["CUSTOMERS.ID", "customers.name", "main.orders.total"]
    assert sorted(preview["not_in_your_data"]) == ["customers.phone", "other.orders.total"]


def test_a_granted_view_or_partition_is_hidden_here_instead_with_why() -> None:
    catalog = (Table("customers", ("id", "ssn"), schema="public"),
               Table("cards", ("number",), derived=True, schema="public"))
    found = preview([Grant.parse("customers.id"), Grant.parse("public.cards.number")], catalog)
    assert [g.name for g in found.matched] == ["customers.id"]
    assert [(g.name, "view" in why) for g, why in found.hidden_here] == [("public.cards.number", True)]
    assert Grants.of(found.matched + tuple(g for g, _ in found.hidden_here)).visible(catalog) == (
        Table("customers", ("id",), schema="public"),)


async def test_a_granted_sqlite_view_cant_read_a_column_no_profile_grants(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Cards", ["cards.name", "cards.number"])  # cards reads customers.ssn as number
        await apply(h, "Cards")
        reply = await h.call("query", source="people", question="?", sql="SELECT number FROM cards")
    assert reply.is_error and not leaked(reply)


async def test_apply_is_refused_only_when_nothing_matches(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Elsewhere", ["accounts.balance"])
        refused = await h.call("apply_profile", profile="Elsewhere", source="people")
        listed = await h.call("list_sources")
    assert refused.is_error and "nothing" in refused.ai.text.lower()
    assert refused.ai.structured is None or refused.ai.structured.get("applied") is None
    assert "ssn" in columns_of(listed)["customers"]  # still on Free, unchanged


# --- hidden by default ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sql", ROUTES_TO_A_BLOCKED_COLUMN)
async def test_a_column_no_applied_profile_grants_never_reaches_the_ai(home: Path, people_db: Path, sql: str) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.id", "customers.name", "orders.id", "orders.customer_id", "orders.total"])
        await apply(h, "Sales")
        reply = await h.call("query", source="people", question="?", sql=sql)
    assert not leaked(reply), reply.ai.text


async def test_select_star_returns_only_granted_columns_and_ungranted_tables_dont_exist(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.id", "customers.name"])
        await apply(h, "Sales")
        star = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        orders = await h.call("query", source="people", question="?", sql="SELECT * FROM orders")
        listed = await h.call("list_sources")
        described = await h.call("describe_source", source="people")
    assert not star.is_error, star.ai.text
    assert star.ai.structured["columns"] == ["id", "name"]
    assert orders.is_error
    assert columns_of(listed) == {"customers": ["id", "name"]}
    assert "ssn" not in seen(described) and "orders" not in seen(described)


async def test_a_column_added_to_a_granted_table_later_stays_hidden(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.id", "customers.name"])
        await apply(h, "Sales")
        with sqlite3.connect(people_db) as db:
            db.execute("ALTER TABLE customers ADD COLUMN email TEXT")
            db.execute("UPDATE customers SET email = 'ada@secret.example'")
        db.close()
        star = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        listed = await h.call("list_sources")
    assert star.ai.structured["columns"] == ["id", "name"]
    assert "secret.example" not in seen(star)
    assert "email" not in columns_of(listed)["customers"]


async def test_a_named_column_that_turns_up_later_lights_up_with_a_notice(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.name", "customers.phone"])
        applied = await apply(h, "Sales")
        assert applied.ai.structured["preview"]["not_in_your_data"] == ["customers.phone"]
        with sqlite3.connect(people_db) as db:
            db.execute("ALTER TABLE customers ADD COLUMN phone TEXT")
        db.close()
        listed = await h.call("list_sources")
        again = await h.call("list_sources")
    assert columns_of(listed)["customers"] == ["name", "phone"]
    (entry,) = listed.ai.structured["sources"]
    assert any("customers.phone" in w and "Sales" in w for w in entry["warnings"])
    (entry,) = again.ai.structured["sources"]
    assert not any("customers.phone" in w for w in entry.get("warnings", []))  # said once


async def test_profiles_keep_hiding_after_personal_ends(home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        accounts.plan = "ended"
        reply = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        edit = await h.call("update_profile", profile="Sales", add=["customers.ssn"])
    assert reply.ai.structured["columns"] == ["name"] and not leaked(reply)
    assert edit.is_error


# --- combining ------------------------------------------------------------------------------------------------------

async def test_two_profiles_on_one_connection_give_the_union_of_their_grants(home: Path, people_db: Path) -> None:
    user = User()
    async with tool(home, people_db, user=user) as h:
        await make(h, "Names", ["customers.name"])
        await make(h, "Totals", ["orders.total", "customers.name"])
        await apply(h, "Names")
        await apply(h, "Totals")
        listed = await h.call("list_sources")
        taken_off = await h.call("unapply_profile", profile="Names", source="people")
        after = await h.call("list_sources")
    assert columns_of(listed) == {"customers": ["name"], "orders": ["total"]}
    assert not taken_off.is_error, taken_off.ai.text
    assert columns_of(after) == {"customers": ["name"], "orders": ["total"]}


async def test_applying_a_second_profile_that_shows_more_asks_the_user(home: Path, people_db: Path) -> None:
    user = User(yes=False)
    async with tool(home, people_db, user=user) as h:
        await make(h, "Names", ["customers.name"])
        await make(h, "Totals", ["orders.total"])
        await apply(h, "Names")
        refused = await h.call("apply_profile", profile="Totals", source="people")
        listed = await h.call("list_sources")
    assert refused.is_error
    assert user.asked and "orders.total" in user.asked[-1]
    assert columns_of(listed) == {"customers": ["name"]}


async def test_taking_off_the_last_profile_hides_everything_instead_of_falling_back_to_free(
        home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Names", ["customers.name"])
        await apply(h, "Names")
        await h.call("unapply_profile", profile="Names", source="people")
        listed = await h.call("list_sources")
        reply = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
    assert columns_of(listed) == {}
    assert reply.is_error and not leaked(reply)


async def test_saving_a_new_grant_on_an_applied_profile_asks_the_user(home: Path, people_db: Path) -> None:
    user = User(yes=False)
    async with tool(home, people_db, user=user) as h:
        await make(h, "Names", ["customers.name"])
        await apply(h, "Names")
        refused = await h.call("update_profile", profile="Names", add=["customers.ssn"])
        listed = await h.call("list_sources")
    assert refused.is_error and user.asked
    assert columns_of(listed) == {"customers": ["name"]}


# --- block rules or profiles, never both ----------------------------------------------------------------------------

async def test_a_connection_on_profiles_ignores_block_rules_and_switching_is_explicit(
        home: Path, people_db: Path) -> None:
    user = User()
    async with tool(home, people_db, user=user) as h:
        await h.call("block", source="people", table="customers", column="name")
        await make(h, "Names", ["customers.name"])
        applied = await apply(h, "Names")
        on_profiles = await h.call("list_sources")
        block = await h.call("block", source="people", table="customers", column="name")
        back = await h.call("use_block_rules", source="people")
        on_rules = await h.call("list_sources")
    assert user.asked and "block rules" in user.asked[0]  # the switch was confirmed by the user
    assert applied.ai.structured["switched"] is True
    assert columns_of(on_profiles) == {"customers": ["name"]}  # the block rule on name is ignored
    assert block.is_error and "profiles" in block.ai.text
    assert not back.is_error, back.ai.text
    listed = columns_of(on_rules)
    assert "name" not in listed["customers"] and "ssn" in listed["customers"]  # rules apply again, profiles don't


async def test_declining_the_switch_from_block_rules_changes_nothing(home: Path, people_db: Path) -> None:
    user = User(yes=False)
    async with tool(home, people_db, user=user) as h:
        await h.call("block", source="people", table="customers", column="ssn")
        await make(h, "Names", ["customers.name"])
        refused = await h.call("apply_profile", profile="Names", source="people")
        listed = await h.call("list_sources")
    assert refused.is_error
    assert "ssn" not in columns_of(listed)["customers"] and "orders" in columns_of(listed)


async def test_switching_back_to_block_rules_needs_the_users_yes(home: Path, people_db: Path) -> None:
    user = User()
    async with tool(home, people_db, user=user) as h:
        await make(h, "Names", ["customers.name"])
        await apply(h, "Names")
        user.yes = False
        back = await h.call("use_block_rules", source="people")
        listed = await h.call("list_sources")
    assert back.is_error and user.asked
    assert columns_of(listed) == {"customers": ["name"]}


# --- usage counts ---------------------------------------------------------------------------------------------------

async def test_usage_counts_carry_the_profile_count_and_grant_count_by_level(home: Path, people_db: Path) -> None:
    async with tool(home, people_db) as h:
        await make(h, "Names", ["customers.name"])
        await make(h, "Orders", ["orders.total", "orders.id"])
    store = LocalStore.open(database_path(home))
    counts = usage.Usage(store, Recorder(), now=lambda: datetime(2026, 11, 2, tzinfo=UTC))
    counts.seen("page")
    counts.pulled("sqlite")
    (report,) = counts.reports()
    assert report["profiles"] == 2
    assert (report["grants_full"], report["grants_anonymised"], report["grants_totals"]) == (3, 0, 0)
