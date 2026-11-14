"""Personal's tools only where Personal is on (#116).

A Free session lists Free's tools and nothing else: Personal's tools are listed only while this computer is signed in
to an account on the trial or a paid plan (grace included). Signing in mid-conversation brings them in without a
restart: the server tells the client its tool list changed, and the reply says so (with a restart as the fallback for
apps that don't re-read the list). After a lapse the tools that still matter for frozen profiles stay: listing them
and taking one off a connection. Offers of Personal are shown in Portward's panel only (app-only tools, wording from
offers.py), once per moment; no reply ever asks the AI to pass an offer on.

Driven through the MCP client harness against an in-memory account service. Nothing leaves this machine."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from harness import Harness, MemoryKeychain, connected

from portward_free.account import Accounts, AccountService, AccountServiceError

EMAIL = "sam@acme.example"

FREE_TOOLS = {
    "account_delete", "account_sign_in", "account_sign_out", "account_status", "add_source", "ai_log", "block",
    "compare", "describe_source", "export", "get_selection", "get_started", "history", "keep_rows", "kept_rows",
    "lineage", "list_rules", "list_sources", "matching_login_sql", "more_rows", "open_page", "query", "remove_source",
    "rerun", "run_saved", "save_pull", "saved_pulls", "sent_to_ai", "set_note", "set_send_mode", "share",
    "share_confirm", "share_sign_in", "shares", "test_source", "tweak", "unblock", "update_password", "usage_counts", "set_usage_counts",
}
PERSONAL_TOOLS = {
    "create_profile", "update_profile", "apply_profile", "unapply_profile", "use_block_rules", "list_profiles",
    "start_profile_from_block_rules", "draft_meanings", "draft_definition", "draft_link", "context_drafts",
    "approve_context", "discard_draft", "settle_context", "assumptions", "define_assumption", "import_dictionary",
    "export_proof_report", "approve_browser", "sync_turn_on", "sync_now", "sync_status", "sync_turn_off",
    "sync_new_recovery_key", "web_route", "web_routes", "account_shares", "withdraw_share", "set_share_expiry",
    "draft_metric", "describe_metric", "metric",
    "promote_saved_pull", "describe_verified_answer", "run_verified_answer",  # #130
}
AFTER_A_LAPSE = {"list_profiles", "unapply_profile"}


class PlanAccounts(AccountService):
    """The account service in memory: /v1/me answers with `plan` (trial, active, grace or ended), or can't be
    reached at all (`reachable` False)."""

    def __init__(self, plan: str = "active", reachable: bool = True) -> None:
        super().__init__("http://127.0.0.1:9")
        self.plan, self.reachable = plan, reachable

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
              token: str | None = None) -> dict[str, Any]:
        if not self.reachable:
            raise AccountServiceError("not reached", 0)
        if token != "pwat_fake":
            raise AccountServiceError("sign in first", 401)
        if path == "/v1/me":
            return {"id": "acct_1", "email": EMAIL, "share_devices": 1,
                    "plan": {"state": self.plan, "until": None, "interval": None, "payment_failed": False}}
        raise AccountServiceError("not in this fake", 404)


@pytest.fixture
def staff_db(tmp_path: Path) -> Path:
    path = tmp_path / "staff.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE people (id INTEGER, name TEXT, region TEXT, salary INTEGER)")
        db.executemany("INSERT INTO people VALUES (?, ?, ?, ?)",
                       [(1, "Ada", "North", 100), (2, "Bo", "South", 90), (3, "Cy", "North", 80)])
    return path


@asynccontextmanager
async def tool(home: Path, db: Path, *, plan: str | None = None, accounts: PlanAccounts | None = None,
               keychain: MemoryKeychain | None = None, panel: bool = False) -> AsyncIterator[Harness]:
    """The free tool with `db` as the source "staff", signed in to an account on `plan` (None: signed out)."""
    keychain = keychain if keychain is not None else MemoryKeychain()
    if plan is not None:
        keychain.set("account-token", "pwat_fake")
    async with connected(home, db, name="staff", keychain=keychain, panel=panel, mode="results",
                         accounts=Accounts(accounts or PlanAccounts(plan or "ended"))) as h:
        yield h


async def listed(h: Harness) -> set[str]:
    """The tools the model is shown now, read again from the server (as a client does after list_changed)."""
    h.forget_tools()
    return set(await h.ai_tools())


# --- the tool list by plan ------------------------------------------------------------------------------------------

async def test_a_free_session_lists_only_free_tools(home: Path, staff_db: Path) -> None:
    async with tool(home, staff_db) as h:
        shown = await listed(h)
    assert shown == FREE_TOOLS
    assert len(shown) == 40


# --- offers in Portward's panel -------------------------------------------------------------------------------------

def offer_in(reply: Any) -> dict[str, str] | None:
    return (reply.ai.structured or {}).get("offer")


@pytest.mark.parametrize("plan", [None, "ended"])
async def test_reaching_for_totals_on_free_shows_the_offer_in_the_panel_once(home: Path, staff_db: Path,
                                                                              plan: str | None) -> None:
    async with tool(home, staff_db, plan=plan, panel=True) as h:
        first = await h.call_from_panel("personal_offer", moment="anonymised_or_totals")
        again = await h.call_from_panel("personal_offer", moment="anonymised_or_totals")
    offer = offer_in(first)
    assert offer is not None and offer["kind"] == "anonymised_or_totals"
    assert "stand-ins" in offer["text"] and "/me" in offer["text"] and "\n" not in offer["text"]
    assert "pass on" not in first.ai.text and "dismiss_offer" not in first.ai.text  # the panel shows it, not the AI
    assert offer_in(again) is None


async def test_the_panel_offers_and_its_dismissal_are_not_the_ais_tools(home: Path, staff_db: Path) -> None:
    async with tool(home, staff_db, panel=True) as h:
        ai = await h.ai_tools()
        everything = await h.tools()
    assert "personal_offer" in everything and "dismiss_offer" in everything
    assert "personal_offer" not in ai and "dismiss_offer" not in ai


async def test_a_moment_dismissed_in_the_panel_never_comes_back(home: Path, staff_db: Path) -> None:
    async with tool(home, staff_db, panel=True) as h:
        dismissed = await h.call_from_panel("dismiss_offer", kind="profile")
        later = await h.call_from_panel("personal_offer", moment="profile")
    assert not dismissed.is_error, dismissed.ai.text
    assert offer_in(later) is None


@pytest.mark.parametrize("accounts", [PlanAccounts("trial"), PlanAccounts("active", reachable=False)],
                         ids=["on Personal", "plan unknown"])
async def test_no_offer_on_personal_or_when_the_plan_cant_be_checked(home: Path, staff_db: Path,
                                                                     accounts: PlanAccounts) -> None:
    async with tool(home, staff_db, plan="trial", accounts=accounts, panel=True) as h:
        found = await h.call_from_panel("personal_offer", moment="anonymised_or_totals")
    assert not found.is_error and offer_in(found) is None


async def test_the_panel_names_the_moments_it_knows(home: Path, staff_db: Path) -> None:
    async with tool(home, staff_db, panel=True) as h:
        refused = await h.call_from_panel("personal_offer", moment="second_device")
    assert refused.is_error and "anonymised_or_totals" in refused.ai.text


@pytest.mark.parametrize("plan", ["trial", "active", "grace"])
async def test_with_personal_on_every_personal_tool_is_listed(home: Path, staff_db: Path, plan: str) -> None:
    async with tool(home, staff_db, plan=plan) as h:
        shown = await listed(h)
    assert shown == FREE_TOOLS | PERSONAL_TOOLS


async def test_a_lapsed_account_without_profiles_sees_free_tools(home: Path, staff_db: Path) -> None:
    async with tool(home, staff_db, plan="ended") as h:
        shown = await listed(h)
    assert shown == FREE_TOOLS


async def test_after_a_lapse_frozen_profiles_can_still_be_listed_and_taken_off(home: Path, staff_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, staff_db, plan="active", accounts=accounts) as h:
        made = await h.call("create_profile", name="Sales", description="pipeline", grants=["people.name"])
        assert not made.is_error, made.ai.text
        applied = await h.call("apply_profile", profile="Sales", source="staff")
        assert not applied.is_error, applied.ai.text
        accounts.plan = "ended"
        shown = await listed(h)
        frozen = await h.call("list_profiles")
        taken_off = await h.call("unapply_profile", profile="Sales", source="staff")
    assert shown == FREE_TOOLS | AFTER_A_LAPSE
    assert frozen.ai.structured["frozen"] is True
    assert not taken_off.is_error, taken_off.ai.text


async def test_a_personal_tool_called_on_free_is_unknown(home: Path, staff_db: Path) -> None:
    async with tool(home, staff_db) as h:
        refused = await h.call("create_profile", name="Sales", description="pipeline", grants=["people.name"])
    assert refused.is_error and "Unknown tool" in refused.ai.text
    assert "pass on" not in refused.ai.text and "offer" not in repr(refused.ai.structured)


async def test_signing_in_mid_conversation_brings_personal_tools_without_a_restart(home: Path,
                                                                                  staff_db: Path) -> None:
    keychain = MemoryKeychain()
    async with tool(home, staff_db, accounts=PlanAccounts("trial"), keychain=keychain) as h:
        before = await listed(h)
        keychain.set("account-token", "pwat_fake")  # the sign-in finished in the browser (account.SignIn)
        status = await h.call("account_status")
        after = await listed(h)
        made = await h.call("create_profile", name="Sales", description="pipeline", grants=["people.name"])
    assert before == FREE_TOOLS
    assert "Personal's tools are on in this conversation now" in status.ai.text and "restart" in status.ai.text
    assert after == FREE_TOOLS | PERSONAL_TOOLS
    assert not made.is_error, made.ai.text


async def test_the_client_is_told_the_tool_list_changed(home: Path, staff_db: Path) -> None:
    """Over the handshake protocol (as Claude Code connects today) the server sends tools/list_changed."""
    keychain = MemoryKeychain()
    async with connected(home, staff_db, name="staff", keychain=keychain, protocol="legacy",
                         accounts=Accounts(PlanAccounts("trial"))) as h:
        await listed(h)
        keychain.set("account-token", "pwat_fake")
        await h.call("account_status")
        told = h.notifications()
    assert "notifications/tools/list_changed" in told


async def test_signing_out_takes_personal_tools_away(home: Path, staff_db: Path) -> None:
    async with tool(home, staff_db, plan="active") as h:
        before = await listed(h)
        out = await h.call("account_sign_out")
        after = await listed(h)
    assert before == FREE_TOOLS | PERSONAL_TOOLS and after == FREE_TOOLS
    assert "Personal's tools are off" in out.ai.text
