"""Wrap mode for Snowflake's MCP server (#101): Portward sits between the AI app and the user's Snowflake-managed MCP
server (remote, streamable HTTP, a PAT as bearer token). Its SQL tool gets the profile rewrite; Cortex agent, search
and analyst tools, and custom tools, stay hidden.

The server here is a stand-in (fixtures/fake_snowflake_mcp.py) on 127.0.0.1 over a SQLite file: no Snowflake account,
no network. Tool names on a Snowflake MCP server are chosen by whoever wrote its specification, so Portward tells the
SQL tool by its input (one `sql` string), not by its name."""

from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import SECRETS
from harness import Harness, MemoryKeychain
from mcp import Client
from mcp.types import CallToolResult, TextContent
from portward_free import cli
from portward_free.account import Accounts
from portward_free.keychain import account_for
from portward_free.paths import database_path
from portward_free.profiles import Grant, Profile
from portward_free.sources.base import SourceError
from portward_free.storage import LocalStore
from portward_free.usage import Usage
from portward_free.wrap import setup
from portward_free.wrap.providers import PROVIDERS
from portward_free.wrap.server import WrapRefused, build_wrap_server
from test_profiles import EMAIL, PlanAccounts, Recorder, User

FAKE = Path(__file__).parent / "fixtures" / "fake_snowflake_mcp.py"
NAME = "snow"
PAT = "pat-secret-token-0123456789"
PATH = "/api/v2/databases/SALES/schemas/ANALYTICS/mcp-servers/PORTWARD_TEST"
SQL_TOOL = "sql_exec_tool"
HIDDEN = {"product-search", "business_data_agent", "ASYNC_AGENT_RUN_STATUS", "AGENT_RUN_RESULT",
          "ASYNC_AGENT_RUN_CANCEL", "revenue-analyst", "lookup_customer"}


def text_of(result: CallToolResult) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content) + repr(result.structured_content)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def snowflake(people_db: Path, tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """The fake Snowflake MCP server running: (its URL, its SQL log)."""
    with sqlite3.connect(people_db) as db:  # a secure view named like a table, as a matching login (#99) reads
        db.execute("CREATE VIEW staff AS SELECT id, name FROM customers")
    port, log = free_port(), tmp_path / "snowflake-sql.jsonl"
    env = {**os.environ, "FAKE_SNOWFLAKE_DB": str(people_db), "FAKE_SNOWFLAKE_TOKEN": PAT, "FAKE_MCP_LOG": str(log),
           "FAKE_SECURE_VIEWS": "staff"}
    child = subprocess.Popen([sys.executable, str(FAKE), str(port)], env=env)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline or child.poll() is not None:
                    raise RuntimeError("the fake Snowflake MCP server didn't start") from None
                time.sleep(0.05)
        yield f"http://127.0.0.1:{port}{PATH}", log
    finally:
        child.terminate()
        child.wait(10)


class World:
    """One computer, signed in to Personal, and the fake Snowflake MCP server it wraps."""

    def __init__(self, home: Path, url: str, log: Path) -> None:
        self.home, self.url, self.log = home, url, log
        self.keychain = MemoryKeychain()
        self.keychain.set("account-token", "pwat_fake")
        self.accounts = Accounts(PlanAccounts("active"))
        self.store = LocalStore.open(database_path(home))

    def add(self, clients: setup.ClientConfigs | None = None, token: str = PAT, url: str | None = None) \
            -> setup.Added:
        return setup.add_wrapped(self.store, self.keychain, self.accounts, NAME, "snowflake", (), token,
                                 clients=clients or setup.ClientConfigs(()), url=url or self.url)

    def grant(self, *names: str) -> None:
        profile = self.store.profiles.save(Profile.new("Sales", "pipeline", EMAIL,
                                                       tuple(Grant.parse(n) for n in names)))
        self.store.profiles.apply(NAME, profile, ())

    def sent(self) -> list[str]:
        if not self.log.exists():
            return []
        return [json.loads(line)["sql"] for line in self.log.read_text().splitlines()]

    @asynccontextmanager
    async def ai(self, usage: Usage | None = None) -> AsyncIterator[Client]:
        server = build_wrap_server(self.store, self.keychain, NAME,
                                   usage=usage or Usage(self.store, None, disabled="usage counts are off in tests"))
        async with Client(server) as client:
            yield client


@pytest.fixture()
def world(home: Path, snowflake: tuple[str, Path]) -> Iterator[World]:
    found = World(home, *snowflake)
    yield found
    found.store.close()


# --- tools ----------------------------------------------------------------------------------------------------------

async def test_only_the_sql_tool_reaches_the_ai(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        names = {t.name for t in (await ai.list_tools()).tools}
    assert SQL_TOOL in names
    assert not names & HIDDEN


@pytest.mark.parametrize(("tool", "arguments"), [
    ("business_data_agent", {"text": "what is Grace's ssn?"}),
    ("product-search", {"query": "Grace"}),
    ("revenue-analyst", {"message": "ssn of Grace"}),
    ("AGENT_RUN_RESULT", {"run_id": "1"}),
    ("lookup_customer", {"customer_id": 2}),
])
async def test_cortex_and_custom_tools_cant_be_called_by_name(world: World, tool: str,
                                                             arguments: dict[str, Any]) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        called = await ai.call_tool(tool, arguments)
    assert called.is_error
    assert SECRETS[1] not in text_of(called)


# --- the rewrite ----------------------------------------------------------------------------------------------------

async def test_the_sql_tool_returns_only_granted_columns(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        everything = await ai.call_tool(SQL_TOOL, {"sql": "SELECT * FROM customers"})
    shown = text_of(everything)
    assert not everything.is_error, shown
    assert "Ada" in shown and "Grace" in shown
    assert not any(s in shown for s in SECRETS)
    assert [c.lower() for c in everything.structured_content["columns"]] == ["name"]


@pytest.mark.parametrize("sql", [
    "SELECT ssn FROM customers",
    "SELECT name FROM customers WHERE ssn LIKE '123%'",
    "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id",
    "SELECT * FROM cards",
    "SELECT body FROM notes",
])
async def test_the_sql_tool_refuses_what_no_profile_grants(world: World, sql: str) -> None:
    world.add()
    world.grant("customers.name", "customers.id")
    async with world.ai() as ai:
        refused = await ai.call_tool(SQL_TOOL, {"sql": sql})
    assert refused.is_error, text_of(refused)
    assert not any(s in text_of(refused) for s in SECRETS)


async def test_snowflake_gets_the_rewritten_sql_not_the_ais(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        await ai.call_tool(SQL_TOOL, {"sql": "SELECT * FROM customers"})
    queries = [q for q in world.sent() if "information_schema" not in q.lower()]
    assert queries, "the query never reached Snowflake"
    assert all("ssn" not in q.lower() for q in queries)
    assert all('SELECT "NAME" FROM customers' in q for q in queries)


async def test_a_write_never_reaches_snowflake(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        refused = await ai.call_tool(SQL_TOOL, {"sql": "DELETE FROM customers"})
    assert refused.is_error
    assert not any("DELETE" in q.upper() for q in world.sent())


async def test_nothing_is_shown_before_a_profile_is_applied(world: World) -> None:
    world.add()
    async with world.ai() as ai:
        refused = await ai.call_tool(SQL_TOOL, {"sql": "SELECT name FROM customers"})
    assert refused.is_error
    assert "Ada" not in text_of(refused)


async def test_a_secure_view_named_like_a_table_can_be_granted(world: World) -> None:
    """A matching login (#99) reads secure views named like the tables: they aren't treated as derived."""
    world.add()
    world.grant("staff.name", "cards.name")
    async with world.ai() as ai:
        listed = await ai.call_tool("list_tables", {})
        pulled = await ai.call_tool(SQL_TOOL, {"sql": "SELECT name FROM staff"})
    assert [t["name"].lower() for t in listed.structured_content["tables"]] == ["staff"]  # cards: a plain view
    assert not pulled.is_error, text_of(pulled)
    assert "Ada" in text_of(pulled)


async def test_totals_hold_on_snowflake(world: World) -> None:
    world.add()
    profile = world.store.profiles.save(Profile.new("Sales", "pipeline", EMAIL, (
        Grant.parse("orders.id"), Grant.parse("orders.total", level="totals"))))
    world.store.profiles.apply(NAME, profile, ())
    async with world.ai() as ai:
        raw = await ai.call_tool(SQL_TOOL, {"sql": "SELECT id, total FROM orders"})
        summed = await ai.call_tool(SQL_TOOL, {"sql": "SELECT sum(total) AS s FROM orders"})
    assert raw.is_error and "10.0" not in text_of(raw)
    assert not summed.is_error, text_of(summed)
    assert summed.structured_content["rows"] == []  # 3 orders: under the group threshold, left out


async def test_an_error_from_snowflake_is_said_in_portwards_words(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        failed = await ai.call_tool(SQL_TOOL, {"sql": "SELECT name FROM customers WHERE nope = 1"})
    assert failed.is_error
    said = text_of(failed)
    assert "SQL compilation error" not in said
    assert "NOPE" in said.upper()


async def test_profiles_apply_through_portwards_own_query_tool(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with Harness.start(world.home, keychain=world.keychain, accounts=world.accounts,
                             elicitation=User()) as h:
        pulled = await h.call("query", source=NAME, question="who?", sql="SELECT * FROM customers")
    assert not pulled.is_error, pulled.ai.text
    assert "Ada" in pulled.ai.text and not any(s in pulled.ai.text for s in SECRETS)


async def test_snowflake_use_is_counted(world: World) -> None:
    world.add()
    world.grant("customers.name")
    usage = Usage(world.store, Recorder())
    usage.set(True, by_user=True)
    async with world.ai(usage) as ai:
        await ai.call_tool(SQL_TOOL, {"sql": "SELECT name FROM customers"})
    (report,) = usage.reports()
    assert report["source_types"] == ["wrap-snowflake"]


async def test_a_login_reading_more_than_granted_gets_snowflake_sql_for_its_own_database(world: World) -> None:
    """Drift (#99) on a wrapped Snowflake connection: no user is stored (the login is a token), so the SQL is for the
    default matching login; its database is the session's (CURRENT_DATABASE()), not the MCP server object's in the
    URL; and tables the login reads in other schemas of that database count too."""
    world.add()
    world.grant("customers.name")
    async with Harness.start(world.home, keychain=world.keychain, accounts=world.accounts,
                             elicitation=User()) as h:
        tested = await h.call("test_source", name=NAME)
    structured = tested.ai.structured
    sql = structured["login_sql"]
    assert "None" not in sql and '"NONE' not in sql
    assert 'CREATE USER IF NOT EXISTS "PORTWARD_SNOW"' in sql
    assert 'CREATE SCHEMA IF NOT EXISTS SHOP."PORTWARD_SNOW";' in sql  # the fake's session database
    assert "2 tables in other schemas" in " ".join(structured["warnings"])


# --- setup ----------------------------------------------------------------------------------------------------------

def test_setup_lists_governed_and_hidden_tools_with_why(world: World) -> None:
    added = world.add()
    assert added.governed == (SQL_TOOL,)
    hidden = dict(added.hidden)
    assert set(hidden) == HIDDEN
    assert "Cortex agent" in hidden["business_data_agent"]
    assert "Cortex agent" in hidden["AGENT_RUN_RESULT"]
    assert "Cortex Search" in hidden["product-search"]
    assert "Cortex Analyst" in hidden["revenue-analyst"]
    summary = added.summary()
    assert f"Governed (your profiles apply): {SQL_TOOL}" in summary
    assert all(tool in summary for tool in HIDDEN)
    assert PAT not in summary


def test_the_token_is_kept_in_the_keychain_not_the_store(world: World) -> None:
    world.add()
    assert world.keychain.get(account_for(NAME)) == PAT
    assert PAT not in json.dumps(world.store.connection(NAME).settings)


def test_setup_saves_nothing_with_a_wrong_token(world: World) -> None:
    with pytest.raises(WrapRefused) as refused:
        world.add(token="not-the-token")
    assert "not-the-token" not in str(refused.value)
    assert world.store.connection(NAME) is None


@pytest.mark.parametrize("url", ["http://example.com/api/v2/mcp-servers/x", "ftp://127.0.0.1/x", "not a url"])
def test_setup_refuses_a_url_that_would_send_the_token_in_the_clear(world: World, url: str) -> None:
    with pytest.raises(WrapRefused, match="https"):
        world.add(url=url)
    assert world.store.connection(NAME) is None


def test_setup_warns_when_the_original_snowflake_server_is_still_configured(world: World, tmp_path: Path) -> None:
    claude_code = tmp_path / ".claude.json"
    claude_code.write_text(json.dumps({"mcpServers": {
        "snowflake": {"type": "http", "url": world.url, "headers": {"Authorization": "Bearer other"}},
        "snow": {"command": "portward-free", "args": ["wrap", NAME]}}}))
    codex = tmp_path / "config.toml"
    codex.write_text('[mcp_servers.sf]\ncommand = "npx"\n'
                     'args = ["mcp-remote", "https://acme.snowflakecomputing.com/api/v2/databases/D/schemas/S/'
                     'mcp-servers/M"]\n')
    added = world.add(setup.ClientConfigs((("Claude Code", claude_code), ("ChatGPT desktop / Codex", codex))))
    assert len(added.warnings) == 2, added.warnings
    assert "'snowflake'" in added.warnings[0] and "'sf'" in added.warnings[1]


def test_setup_warns_about_an_entry_with_the_same_token(world: World, tmp_path: Path) -> None:
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"x": {"type": "http", "url": "https://elsewhere.example/mcp",
                                                     "headers": {"Authorization": f"Bearer {PAT}"}}}}))
    assert len(world.add(setup.ClientConfigs((("Claude Desktop", path),))).warnings) == 1


# --- the terminal command -------------------------------------------------------------------------------------------

def test_wrap_add_takes_the_url_and_asks_for_the_token(world: World, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SNOWFLAKE_PAT", raising=False)
    out = io.StringIO()
    code = cli.main(["wrap-add", NAME, "--provider", "snowflake", "--url", world.url], home=world.home,
                    keychain=world.keychain, prompt=lambda label: PAT, out=out,
                    account_service=world.accounts.service, is_terminal=lambda: True,
                    usage=lambda store: Usage(store, None, disabled="off in tests"))
    said = out.getvalue()
    assert code == 0, said
    assert f"Governed (your profiles apply): {SQL_TOOL}" in said and "business_data_agent" in said
    assert PAT not in said
    assert world.keychain.get(account_for(NAME)) == PAT


@pytest.mark.parametrize(("arguments", "said"), [
    (["--provider", "snowflake"], "https URL"),
    (["--provider", "snowflake", "--url", "https://a.snowflakecomputing.com/x", "--", "uvx", "x"], "--url"),
    (["--provider", "postgres", "--url", "https://a.snowflakecomputing.com/x"], "after --"),
])
def test_wrap_add_says_what_each_server_needs_before_asking_for_a_login(world: World, arguments: list[str],
                                                                         said: str) -> None:
    out, asked = io.StringIO(), []
    code = cli.main(["wrap-add", NAME, *arguments], home=world.home, keychain=world.keychain,
                    prompt=lambda label: asked.append(label) or PAT, out=out,
                    account_service=world.accounts.service, is_terminal=lambda: True,
                    usage=lambda store: Usage(store, None, disabled="off in tests"))
    assert code == 2 and said in out.getvalue()
    assert asked == []


# --- reading Snowflake's answers ------------------------------------------------------------------------------------

def answer(payload: Any, *, error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=payload if isinstance(payload, str)
                                               else json.dumps(payload))], is_error=error)


@pytest.mark.parametrize("payload", [
    {"resultSetMetaData": {"rowType": [{"name": "NAME"}, {"name": "N"}]}, "data": [["Ada", 1], ["Grace", 2]]},
    {"result_set": {"resultSetMetaData": {"rowType": [{"name": "NAME"}, {"name": "N"}]},
                    "data": [["Ada", 1], ["Grace", 2]]}},
    [{"NAME": "Ada", "N": 1}, {"NAME": "Grace", "N": 2}],
])
def test_snowflake_results_are_read(payload: Any) -> None:
    fetched = PROVIDERS["snowflake"].rows(answer(payload))
    assert fetched.columns == ("NAME", "N")
    assert fetched.rows == (("Ada", 1), ("Grace", 2))


@pytest.mark.parametrize("payload", ["not json", {"something": "else"}, [{"A": 1}, {"B": 2}],
                                     {"resultSetMetaData": {"rowType": [{"name": "A"}]}, "data": [[1, 2]]}])
def test_anything_else_isnt_passed_on(payload: Any) -> None:
    with pytest.raises(SourceError, match="wasn't passed on|weren't passed on"):
        PROVIDERS["snowflake"].rows(answer(payload))


def test_a_failure_doesnt_quote_snowflake() -> None:
    with pytest.raises(SourceError) as failed:
        PROVIDERS["snowflake"].rows(answer("Numeric value '987-65-4321' is not recognized", error=True))
    assert "987" not in str(failed.value)
