"""Upgrade prompts in the right places only (#57, moments redrawn by #108).

Personal is offered at three moments and nowhere else in Free, each the first time it happens on this computer:
connecting claude.ai or ChatGPT on the web (`serve-http`, `serve-direct`, in the terminal), and reaching for a profile
or for anonymised or totals in Portward's results panel (#116: Personal's tools aren't listed on Free, so the AI never
relays an offer; those moments are tested in test_tool_plans.py). Each offer is one line, and a dismissed offer never
comes back. With Personal there is no offer, and when the plan can't be checked there is none either. The earlier
moments (a second computer, a share about to expire, the manage-shares page) are gone.

The tests use in-memory fakes of the share and account services. Nothing leaves this machine."""

from __future__ import annotations

import ast
import io
import json
import re
import urllib.request
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from harness import Harness, MemoryKeychain, NoMail, connected

import portward_free
from portward_free.account import ACCOUNT_SETTING, Accounts, AccountService, AccountServiceError
from portward_free.cli import main
from portward_free.paths import database_path
from portward_free.storage import LocalStore
from portward_free.sharing import ShareService, Sharing
from portward_free.sharing.service import Created, ShareServiceError

# Upgrade wording: a pitch or a way to buy, in any words the tool might use.
UPGRADE = re.compile(r"upgrad|subscri|free trial|days free|\$\s?\d|per (month|year)|pricing|what personal adds|"
                     r"personal (keeps|adds|syncs|lets|gives)|comes? with (portward )?personal|"
                     r"try (portward )?personal|(?<!/v1)/me/billing", re.I)

pytest_plugins = ["test_share"]  # its fixtures: the share service on 127.0.0.1, drafts, sharing, recipient


# --- in-memory share and account services ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class FakeShares(ShareService):
    """The share service's answers, in memory. `other_devices` is what confirming says about other computers."""

    def __init__(self, other_devices: int = 0) -> None:
        super().__init__("http://127.0.0.1:9")
        self.other_devices, self.made = other_devices, 0

    def start_sender(self, email: str, name: str, device: str) -> str:
        return "verification-1"

    def confirm_sender(self, verification: str, code: str, device: str = "") -> dict[str, Any]:
        if code != "123456":
            raise ShareServiceError("that code isn't right or has expired", 403, {"attempts_left": 4})
        return {"token": "pwst_fake", "email": "sam@acme.example", "name": "Sam",
                "expires_at": "2027-11-03T12:00:00Z", "other_devices": self.other_devices}

    def rotate_sender(self, token: str) -> dict[str, Any]:
        return self.confirm_sender("", "123456")

    def create(self, token: str, recipient: str, sealed: str, days: int) -> Created:
        self.made += 1
        return Created(f"share-{self.made:012d}", (NOW + timedelta(days=days)).isoformat())


class FakeAccounts(AccountService):
    """The account service in memory: /v1/me with `plan`, and 402 for everything Personal when it's ended."""

    def __init__(self, plan: str = "ended", share_devices: int = 1) -> None:
        super().__init__("http://127.0.0.1:9")
        self.plan, self.share_devices = plan, share_devices

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
              token: str | None = None) -> dict[str, Any]:
        if token != "pwat_fake":
            raise AccountServiceError("sign in first", 401)
        if path == "/v1/me":
            return {"id": "acct_1", "email": "sam@acme.example", "share_devices": self.share_devices,
                    "plan": {"state": self.plan, "until": None, "interval": None, "payment_failed": False}}
        if self.plan == "ended":
            raise AccountServiceError("this is part of Personal", 402)
        if path == "/v1/me/shares":
            return {"shares": [], "expiry_days": [7, 30, 90]}
        if path.endswith("/expiry"):
            return {"id": path.split("/")[-2], "expires_at": (NOW + timedelta(days=(body or {})["days"])).isoformat()}
        return {}


def signed_in(keychain: MemoryKeychain) -> MemoryKeychain:
    keychain.set("account-token", "pwat_fake")
    return keychain


@asynccontextmanager
async def free_tool(home: Path, db: Path, *, shares: FakeShares | None = None, accounts: FakeAccounts | None = None,
                    keychain: MemoryKeychain | None = None, panel: bool = False,
                    browser: Callable[[str], bool] | None = None) -> AsyncIterator[Harness]:
    """The free tool with one SQLite source and one pull, against in-memory services."""
    sharing = Sharing(shares or FakeShares(), NoMail(), now=lambda: NOW)
    async with connected(home, db, keychain=keychain or MemoryKeychain(), sharing=sharing, panel=panel,
                         browser=browser, accounts=Accounts(accounts or FakeAccounts())) as h:
        pulled = await h.call("query", source="shop", sql="SELECT name, region FROM people", question="Who is where?")
        h.last_pull = pulled.ai.structured["pull_id"]
        yield h


async def confirm_sharing(h: Harness) -> None:
    started = await h.call("share_sign_in", email="sam@acme.example", name="Sam")
    assert not started.is_error, started.ai.text
    typed = h.type_share_code_in_terminal("123456")
    assert typed.code == 0, typed.out



def signed_in_keychain() -> MemoryKeychain:
    return signed_in(MemoryKeychain())


class UnreachableAccounts(FakeAccounts):
    """The account service can't be reached: the plan is unknown."""

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
              token: str | None = None) -> dict[str, Any]:
        raise AccountServiceError("not reached", 0)


# --- connecting claude.ai or ChatGPT on the web ---------------------------------------------------------------------

@pytest.mark.parametrize("command", ["serve-http", "serve-direct"])
@pytest.mark.parametrize("signed_in", [True, False])
def test_connecting_the_web_on_free_offers_personal_once(home: Path, command: str, signed_in: bool) -> None:
    keychain = signed_in_keychain() if signed_in else MemoryKeychain()
    first, again = io.StringIO(), io.StringIO()
    for out in (first, again):
        assert main([command], home=home, keychain=keychain, out=out, is_terminal=lambda: False,
                    account_service=FakeAccounts("ended")) == 1
    assert "claude.ai and ChatGPT on the web" in first.getvalue() and "/me" in first.getvalue()
    assert "claude.ai and ChatGPT on the web" not in again.getvalue()
    assert not UPGRADE.search(again.getvalue())


def test_connecting_the_web_when_the_plan_cant_be_checked_offers_nothing(home: Path) -> None:
    out = io.StringIO()
    assert main(["serve-http"], home=home, keychain=signed_in_keychain(), out=out, is_terminal=lambda: False,
                account_service=UnreachableAccounts()) == 1
    assert not UPGRADE.search(out.getvalue()), out.getvalue()


async def test_dismiss_offer_names_the_kinds_it_knows(home: Path, team_db: Path) -> None:
    async with free_tool(home, team_db, panel=True) as h:
        refused = await h.call_from_panel("dismiss_offer", kind="second_device")  # an earlier moment, gone
    assert refused.is_error and "profile" in refused.ai.text and "web" in refused.ai.text


# --- nowhere else ---------------------------------------------------------------------------------------------------


# Every tool, with arguments that make it do its usual thing in Free (or refuse the way it does in Free).
# Sign-out last: it changes the state the others run in.
CALLS: dict[str, dict[str, Any]] = {
    "query": {"source": "shop", "sql": "SELECT name, region FROM people", "question": "Who is where?"},
    "rerun": {"pull_id": "PULL"}, "run_saved": {"name": "Mine"}, "kept_rows": {"pull_id": "PULL"},
    "tweak": {"pull_id": "PULL", "sql": "SELECT name FROM people", "question": "Just names"},
    "add_source": {"name": "other", "kind": "sqlite", "path": "DB"}, "update_password": {"name": "shop"},
    "more_rows": {"pull_id": "PULL"}, "test_source": {"name": "shop"}, "history": {}, "account_delete": {},
    "export": {"pull_id": "PULL", "format": "csv"}, "usage_counts": {}, "set_usage_counts": {"on": False}, "get_started": {}, "sent_to_ai": {},
    "save_pull": {"pull_id": "PULL", "name": "Mine"}, "keep_rows": {"on": True}, "saved_pulls": {},
    "compare": {"pull_id": "PULL", "other_pull_id": "PULL"}, "lineage": {"pull_id": "PULL"}, "list_sources": {},
    "describe_source": {"source": "shop"}, "set_note": {"source": "shop", "table": "people", "note": "Staff"},
    "block": {"source": "shop", "table": "people", "column": "ssn"},
    "unblock": {"source": "shop", "table": "people", "column": "ssn"}, "list_rules": {}, "set_send_mode": {"source": "shop", "mode": "pick"}, "open_page": {},
    "share_sign_in": {"email": "sam@acme.example", "name": "Sam"}, "share_confirm": {},
    "share": {"pull_id": "PULL", "recipient": "kim@acme.example"}, "shares": {},
    "account_sign_in": {}, "account_status": {}, "account_shares": {}, "withdraw_share": {"share_id": "SHARE"},
    "set_share_expiry": {"share_id": "SHARE", "days": 30}, "sync_turn_on": {}, "sync_now": {}, "sync_status": {},
    "sync_turn_off": {}, "sync_new_recovery_key": {}, "approve_browser": {},
    "get_selection": {"selection_id": "sel_nothing"},
    "create_profile": {"name": "Sales", "description": "pipeline", "grants": ["people.name"]},
    "update_profile": {"profile": "Sales", "add": ["people.region"]}, "list_profiles": {},
    "apply_profile": {"profile": "Sales", "source": "shop"}, "unapply_profile": {"profile": "Sales", "source": "shop"},
    "use_block_rules": {"source": "shop"}, "start_profile_from_block_rules": {"source": "shop"},
    "matching_login_sql": {"source": "shop"},
    "draft_meanings": {"profile": "Sales", "meanings": [{"table": "people", "column": "name", "meaning": "Name"}]},
    "draft_definition": {"profile": "Sales", "term": "staff", "text": "Everyone in people"},
    "context_drafts": {}, "approve_context": {"draft_id": "nothing"}, "discard_draft": {"draft_id": "nothing"},
    "settle_context": {"source": "shop", "about": "people.name"},
    "draft_link": {"profile": "Sales", "column": "people.id", "other_column": "orders.person_id"},
    "draft_metric": {"profile": "Sales", "name": "Staff", "measure": "COUNT(*)", "table": "people"},  # #129
    "describe_metric": {"source": "shop", "name": "Staff"},
    "metric": {"source": "shop", "name": "Staff", "question": "How many staff?"},
    "promote_saved_pull": {"name": "Staff", "profile": "Sales"},  # #130
    "describe_verified_answer": {"source": "shop", "question": "How many staff?"},
    "run_verified_answer": {"source": "shop", "question": "How many staff?"},
    "assumptions": {}, "define_assumption": {"assumption_id": "nothing", "term": "staff"},
    "import_dictionary": {"profile": "Sales", "path": "nothing.csv"},
    "ai_log": {}, "export_proof_report": {"start": "2026-11-01", "end": "2026-11-02"},
    "web_routes": {}, "web_route": {"source": "shop", "route": "hosted"},  # #104
    "remove_source": {"name": "other"}, "account_sign_out": {},
}
PANEL_CALLS: dict[str, dict[str, Any]] = {
    "save_secret": {"request": "nothing", "secret": "nothing"}, "secret_request": {"request_id": "nothing"},
    "panel_rows": {"pull_id": "PULL"},
    "put_in_chat": {"pull_id": "PULL", "rows": [0], "columns": ["name"]},
    "value_source": {"pull_id": "PULL", "row": 0, "column": "name"},
    "usage_seen": {}, "usage_choice": {"on": False},
    "share_link": {"share_id": "SHARE"}, "share_mail": {"share_id": "SHARE"},
    "dismiss_offer": {"kind": "everything"}, "personal_offer": {"moment": "anonymised_or_totals"},
    "column_menu": {"pull_id": "PULL", "column": "name"},
    "keep_from_claude": {"pull_id": "PULL", "column": "name"},
    "column_note": {"pull_id": "PULL", "column": "name", "note": "the customer's name"},
    "level_preview": {"pull_id": "PULL", "column": "region", "level": "anonymised"},  # #120
}


def said(reply: Any) -> str:
    """What a reply said, less an offer it carried at one of its moments (those are tested above): any upgrade
    wording left is somewhere it shouldn't be."""
    structured = dict(reply.ai.structured or {})
    offer = structured.pop("offer", None)
    text = reply.ai.text.replace(offer["text"], "") if offer else reply.ai.text
    return text + "\n" + json.dumps(structured, default=str)


async def sweep(h: Harness, db: Path) -> dict[str, str]:
    """Call every tool listed once (the AI's and the panel's); what each said. On Free, Personal's tools aren't
    listed (#116); test_the_sweep_has_every_tool checks the sweep knows them all."""
    tools = await h.tools()
    assert set(tools) <= set(CALLS) | set(PANEL_CALLS), "a tool isn't in the sweep: add it to CALLS or PANEL_CALLS"
    shared = await h.call("share", pull_id=h.last_pull, recipient="kim@acme.example")
    fill = {"PULL": h.last_pull, "DB": str(db), "SHARE": (shared.ai.structured or {}).get("share_id", "x")}
    answers = {}
    for calls, call in ((CALLS, h.call), (PANEL_CALLS, h.call_from_panel)):
        for name, arguments in calls.items():
            if name not in tools:
                continue
            answers[name] = said(await call(name, **{k: fill.get(v, v) if isinstance(v, str) else v
                                                     for k, v in arguments.items()}))
    return answers


async def test_the_sweep_has_every_tool(home: Path, team_db: Path) -> None:
    async with free_tool(home, team_db, panel=True, accounts=FakeAccounts("active"),
                         keychain=signed_in_keychain()) as h:
        tools = await h.tools()
    assert set(CALLS) | set(PANEL_CALLS) == set(tools), "a tool isn't in the sweep: add it to CALLS or PANEL_CALLS"


def prompts_in(answers: dict[str, str]) -> dict[str, list[str]]:
    return {name: [m.group(0) for m in UPGRADE.finditer(text)] for name, text in answers.items()
            if UPGRADE.search(text)}


class Browser:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def __call__(self, url: str) -> bool:
        self.opened.append(url)
        return True


def page_text(browser: Browser) -> str:
    pages = [u for u in browser.opened if "/me/link" not in u]
    with urllib.request.urlopen(pages[-1], timeout=5) as response:
        return response.read().decode()


async def test_no_tool_or_local_page_says_upgrade_signed_out(home: Path, team_db: Path) -> None:
    browser = Browser()
    async with free_tool(home, team_db, panel=True, browser=browser) as h:
        await confirm_sharing(h)
        answers = await sweep(h, team_db)
        answers["local page"] = page_text(browser)
    assert prompts_in(answers) == {}


async def test_no_tool_or_local_page_says_upgrade_on_a_free_account(home: Path, team_db: Path) -> None:
    """Signed in to an account whose trial is over: Personal's tools refuse, quietly."""
    browser = Browser()
    keychain = signed_in(MemoryKeychain())
    async with free_tool(home, team_db, panel=True, browser=browser, keychain=keychain) as h:
        store = LocalStore.open(database_path(home))
        store.set_setting(ACCOUNT_SETTING, json.dumps({"email": "sam@acme.example", "account_id": "acct_1"}))
        store.close()
        await confirm_sharing(h)
        answers = await sweep(h, team_db)
        answers["local page"] = page_text(browser)
    assert "Personal has ended" in answers["account_status"] or "ended" in answers["account_status"]
    assert prompts_in(answers) == {}


def _strings(path: Path) -> list[str]:
    """Every string the module could show (docstrings are for readers of the code, not the user)."""
    tree = ast.parse(path.read_text())
    docstrings = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.append(node.value)
    return found


def test_upgrade_wording_lives_only_in_the_offers_module() -> None:
    """Belt and braces for screens the sweep can't reach (one-time pages, terminal commands, the panel): no string in
    the package outside offers.py reads as an upgrade prompt (the manage-shares tools no longer explain Personal)."""
    package = Path(portward_free.__file__).parent
    allowed = {package / "offers.py"}
    found = {}
    for path in sorted(package.rglob("*")):
        if path in allowed or path.suffix not in (".py", ".html", ".js", ".css"):
            continue
        texts = _strings(path) if path.suffix == ".py" else [path.read_text()]
        hits = [t for t in texts if UPGRADE.search(t)]
        if hits:
            found[str(path.relative_to(package))] = hits
    assert found == {}
