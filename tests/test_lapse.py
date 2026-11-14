"""When Personal lapses (#108, #84), on the computer: profiles freeze. They keep applying exactly as they were
(anonymised and totals included), so no column loosens, but they can't be edited, created or applied to new
connections; paying again unfreezes them. Wrapped servers keep running under the frozen profiles. The web routes
and sync refusing after a lapse are tested with them (test_remote_mcp, test_direct_route, test_sync).

The plan last seen is kept on the computer, so a lapsed account offline still hears its profiles are frozen; an
unknown plan never lets anything through. In-memory account service; nothing leaves this machine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import SECRETS
from test_profiles import PlanAccounts, User, apply, columns_of, make, seen, tool
from test_wrap import World, text_of

from portward_free.account import AccountServiceError


class Switchable(PlanAccounts):
    """The account service in memory, which can also stop answering (`reachable`)."""

    def __init__(self, plan: str = "active") -> None:
        super().__init__(plan)
        self.reachable = True

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
              token: str | None = None) -> dict[str, Any]:
        if not self.reachable:
            raise AccountServiceError("not reached", 0)
        return super()._call(method, path, body, token)


GRANTS = {"grants": ["customers.name"], "anonymised": ["customers.ssn"], "totals": ["orders.total"]}


async def shown(h: Any) -> tuple[Any, ...]:
    """Everything the AI is shown of the source: tables and columns, a read, the stand-ins and what totals allow."""
    listed = await h.call("list_sources")
    star = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
    raw = await h.call("query", source="people", question="?", sql="SELECT total FROM orders")
    return (columns_of(listed), star.ai.structured["columns"], star.ai.structured["rows"], raw.is_error)


async def test_after_a_lapse_no_column_loosens(home: Path, people_db: Path) -> None:
    accounts = Switchable()
    async with tool(home, people_db, accounts=accounts) as h:
        await make(h, "Sales", **GRANTS)
        await apply(h, "Sales")
        before = await shown(h)
        accounts.plan = "ended"
        after = await shown(h)
        accounts.reachable = False  # and offline, with the plan unknown
        offline = await shown(h)
    assert before == after == offline
    assert before[3] is True  # totals still only totals
    assert not any(s in repr(after) for s in SECRETS)  # stand-ins, never the real values


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("create_profile", {"name": "Ops", "description": "ops", "grants": ["customers.name"]}),
    ("update_profile", {"profile": "Sales", "add": ["customers.phone"]}),
    ("update_profile", {"profile": "Sales", "remove": ["customers.name"]}),
    ("apply_profile", {"profile": "Sales", "source": "other"}),
])
async def test_frozen_profiles_cant_be_edited_created_or_applied(home: Path, people_db: Path, tool_name: str,
                                                                 arguments: dict[str, Any]) -> None:
    accounts = Switchable()
    async with tool(home, people_db, accounts=accounts) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        added = await h.call("add_source", name="other", kind="sqlite", path=str(people_db))
        assert not added.is_error, added.ai.text
        accounts.plan = "ended"
        refused = await h.call(tool_name, **arguments)
        listed = await h.call("list_sources")
    assert refused.is_error and "frozen" in refused.ai.text, refused.ai.text
    assert "keep applying" in refused.ai.text
    assert columns_of(listed) == {"customers": ["name"]}  # unchanged
    assert {s["name"] for s in listed.ai.structured["sources"]} == {"people", "other"}


@pytest.mark.parametrize("online", [True, False])
async def test_after_a_lapse_a_profile_can_still_be_taken_off_since_that_only_shows_less(
        home: Path, people_db: Path, online: bool) -> None:
    accounts = Switchable()
    async with tool(home, people_db, accounts=accounts) as h:
        await make(h, "Sales", ["customers.name"])
        await make(h, "Ops", ["orders.total"])
        await apply(h, "Sales")
        await apply(h, "Ops")
        accounts.plan = "ended"
        await h.call("list_profiles")  # the plan is seen ended
        accounts.reachable = online
        one_off = await h.call("unapply_profile", profile="Ops", source="people")
        fewer = await h.call("list_sources")
        none_left = await h.call("unapply_profile", profile="Sales", source="people")
        nothing = await h.call("list_sources")
        back = await h.call("use_block_rules", source="people")  # still refused: nothing falls back to Free
        still_nothing = await h.call("list_sources")
    assert not one_off.is_error, one_off.ai.text
    assert columns_of(fewer) == {"customers": ["name"]}
    assert not none_left.is_error, none_left.ai.text
    assert columns_of(nothing) == {} == columns_of(still_nothing)
    assert back.is_error and "frozen" in back.ai.text


async def test_list_profiles_says_they_are_frozen(home: Path, people_db: Path) -> None:
    accounts = Switchable()
    async with tool(home, people_db, accounts=accounts) as h:
        await make(h, "Sales", ["customers.name"])
        active = await h.call("list_profiles")
        accounts.plan = "ended"
        await h.call("update_profile", profile="Sales", add=["customers.phone"])  # the plan is seen ended
        ended = await h.call("list_profiles")
    assert active.ai.structured["frozen"] is False
    assert ended.ai.structured["frozen"] is True and "frozen" in ended.ai.text


async def test_offline_after_a_lapse_the_refusal_still_says_frozen(home: Path, people_db: Path) -> None:
    accounts = Switchable()
    async with tool(home, people_db, accounts=accounts) as h:
        await make(h, "Sales", ["customers.name"])
        accounts.plan = "ended"
        await h.call("list_profiles")
        await h.call("update_profile", profile="Sales", add=["customers.phone"])
        accounts.reachable = False
        refused = await h.call("update_profile", profile="Sales", add=["customers.phone"])
    assert refused.is_error and "frozen" in refused.ai.text


async def test_offline_with_personal_on_nothing_is_let_through(home: Path, people_db: Path) -> None:
    accounts = Switchable()
    async with tool(home, people_db, accounts=accounts) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        accounts.reachable = False
        refused = await h.call("update_profile", profile="Sales", add=["customers.ssn"])
        star = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
    assert refused.is_error and "couldn't check" in refused.ai.text
    assert star.ai.structured["columns"] == ["name"]


async def test_paying_again_unfreezes_profiles(home: Path, people_db: Path) -> None:
    accounts = Switchable()
    async with tool(home, people_db, accounts=accounts, user=User()) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        accounts.plan = "ended"
        frozen = await h.call("update_profile", profile="Sales", add=["customers.phone"])
        accounts.plan = "active"
        changed = await h.call("update_profile", profile="Sales", add=["customers.phone"])
        listed = await h.call("list_profiles")
        made = await h.call("create_profile", name="Ops", description="ops", grants=["orders.total"])
    assert frozen.is_error
    assert not changed.is_error, changed.ai.text
    assert listed.ai.structured["frozen"] is False and not made.is_error, seen(made)


# --- going back to block rules would loosen; taking a profile off only hides more (sweep open problem 2) -----------

@pytest.mark.parametrize("then", ["ended", "offline", "signed out"])
async def test_after_a_lapse_a_connection_cant_go_back_to_block_rules(home: Path, people_db: Path,
                                                                     then: str) -> None:
    accounts, user = Switchable(), User()
    async with tool(home, people_db, accounts=accounts, user=user) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        asked = len(user.asked)
        accounts.plan = "ended"
        await h.call("list_profiles")  # the plan is seen ended
        if then == "offline":
            accounts.reachable = False
        elif then == "signed out":
            h.keychain.delete("account-token")
        back = await h.call("use_block_rules", source="people")
        listed = await h.call("list_sources")
    assert back.is_error and "frozen" in back.ai.text, back.ai.text
    assert len(user.asked) == asked  # refused before the user is asked anything
    assert columns_of(listed) == {"customers": ["name"]}  # still only what the frozen profile showed


async def test_after_a_lapse_block_rules_keep_working_where_there_were_never_profiles(
        home: Path, people_db: Path, tmp_path: Path) -> None:
    other_db = tmp_path / "other.sqlite"
    other_db.write_bytes(people_db.read_bytes())
    accounts = Switchable()
    async with tool(home, people_db, accounts=accounts, user=User()) as h:
        await make(h, "Sales", ["customers.name"])
        await apply(h, "Sales")
        assert not (await h.call("add_source", name="other", kind="sqlite", path=str(other_db))).is_error
        accounts.plan = "ended"
        await h.call("list_profiles")
        blocked = await h.call("block", source="other", table="customers", column="ssn")
        hidden = await h.call("list_sources")
        back = await h.call("use_block_rules", source="other")
    assert not blocked.is_error, blocked.ai.text
    other = {s["name"]: s for s in hidden.ai.structured["sources"]}["other"]
    columns = {c for t in other["tables"] for c in t["columns"]}
    assert "ssn" not in columns and "name" in columns  # everything but what's blocked, as on Free
    assert back.is_error and "already runs on block rules" in back.ai.text


# --- wrapped servers keep applying the frozen profiles ----------------------------------------------------------------

@pytest.fixture()
def wrapped(home: Path, people_db: Path, tmp_path: Path):
    found = World(home, people_db, tmp_path)
    yield found
    found.store.close()


async def test_a_wrapped_server_keeps_applying_frozen_profiles(wrapped: World) -> None:
    wrapped.add()
    wrapped.grant("customers.name")
    wrapped.accounts = type(wrapped.accounts)(PlanAccounts("ended"))
    wrapped.keychain.delete("account-token")  # signed out too: the wrap server never asks
    async with wrapped.ai() as ai:
        everything = await ai.call_tool("execute_sql", {"sql": "SELECT * FROM customers"})
        hidden = await ai.call_tool("execute_sql", {"sql": "SELECT ssn FROM customers"})
    assert not everything.is_error, text_of(everything)
    assert everything.structured_content["columns"] == ["name"]
    assert hidden.is_error and not any(s in text_of(hidden) for s in SECRETS)
