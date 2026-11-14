"""Every tool has a title and says whether it only reads or may change or delete something (#147, spec #141).

Claude's directory flags a tool without a `title` and a `readOnlyHint` or `destructiveHint`; ChatGPT asks the user
before any tool that isn't read-only. So every tool in every tool list carries both hints, decided, never left out:
Free's and Personal's lists on this computer, and what the web routes serve (direct: this computer over HTTP, minus
the tools that change things there; hosted: the engine's read-only allow-list). Reading and changing never share one
tool, and descriptions say what a tool does, not which plan to buy.

The lists are read the way an AI app reads them: tools/list through the MCP client harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness import MemoryKeychain
from test_tool_plans import PERSONAL_TOOLS, PlanAccounts, staff_db, tool  # noqa: F401 (staff_db is a fixture)

from portward_free.account import Accounts
from portward_free.hosted.engine import TOOLS as HOSTED_TOOLS
from portward_free.paths import database_path
from portward_free.remote.serve import COMPUTER_ONLY
from portward_free.remote.signin import HttpSignIn
from portward_free.server import build_server
from portward_free.storage import LocalStore
from portward_free.usage import Usage

PLAN_WORDS = ("with Portward Personal", "Personal only", "(Personal", "upgrade", "Upgrade")


class NoTokens:
    async def verify_token(self, token: str) -> None:
        return None


def web_lists(home: Path) -> dict[str, dict[str, Any]]:
    """The direct and hosted routes' tools: the server for MCP over HTTP, less what each route never serves (as
    remote.RemoteMcp removes them)."""
    store = LocalStore.open(database_path(home))
    try:
        server = build_server(store, MemoryKeychain(), usage=Usage(store, None, disabled="off in tests"),
                              accounts=Accounts(PlanAccounts("active")),
                              http_sign_in=HttpSignIn("https://signin.example", "https://me.example/mcp",
                                                      NoTokens()))  # type: ignore[arg-type]
        served = {t.name: t for t in server._tool_manager.list_tools() if t.name not in COMPUTER_ONLY}
    finally:
        store.close()
    assert HOSTED_TOOLS <= set(served)
    return {"direct": served, "hosted": {n: t for n, t in served.items() if n in HOSTED_TOOLS}}


def problems(name: str, listed: Any) -> list[str]:
    """What a directory reviewer would flag on this tool."""
    found = []
    hints = listed.annotations
    if not listed.title:
        found.append(f"{name}: no title")
    if hints is None or hints.read_only_hint is None or hints.destructive_hint is None:
        found.append(f"{name}: readOnlyHint and destructiveHint not both set")
    elif hints.read_only_hint and hints.destructive_hint:
        found.append(f"{name}: read-only and destructive at once")
    if hints is not None and hints.open_world_hint is None:
        found.append(f"{name}: openWorldHint not set")
    found += [f"{name}: description says {w!r}" for w in PLAN_WORDS if w in (listed.description or "")]
    return found


async def test_every_tool_in_every_plans_list_has_a_title_and_says_if_it_reads_or_changes(
        home: Path, tmp_path: Path, staff_db: Path) -> None:  # noqa: F811
    lists: dict[str, dict[str, Any]] = {}
    async with tool(home, staff_db) as h:
        lists["free"] = await h.tools()
    async with tool(tmp_path / "personal", staff_db, plan="active") as h:
        h.forget_tools()
        lists["personal"] = await h.tools()
    lists.update(web_lists(tmp_path / "web"))

    assert PERSONAL_TOOLS <= set(lists["personal"]) and "query" in lists["free"]
    found = [f"{route} {p}" for route, listed in lists.items() for name, t in listed.items()
             for p in problems(name, t)]
    assert found == []


async def test_usage_counts_only_reads_and_switching_them_is_its_own_tool(home: Path, staff_db: Path) -> None:
    async with tool(home, staff_db) as h:
        listed = await h.tools()
        status = await h.call("usage_counts")
        off = await h.call("set_usage_counts", on=False)
    reading, switching = listed["usage_counts"], listed["set_usage_counts"]
    assert "on" not in reading.input_schema.get("properties", {})
    assert reading.annotations.read_only_hint is True
    assert switching.annotations.read_only_hint is False and switching.annotations.destructive_hint is True
    assert not status.is_error and "off for good" in off.ai.text  # the harness turns usage counts off for good
