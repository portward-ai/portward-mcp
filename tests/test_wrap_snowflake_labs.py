"""Wrap mode for Snowflake-Labs/mcp (#101), the open-source Snowflake MCP server people run with
`uvx snowflake-labs-mcp`, checked against its source (github.com/Snowflake-Labs/mcp, main 662cb48).

Its SQL tool is `run_snowflake_query` and takes `statement` (not `sql`): it gets the profile rewrite. Portward starts
it over stdio with a service config of its own, so only that tool exists and only SELECT runs; the login goes in
SNOWFLAKE_PASSWORD (a programmatic access token works as one), never on the command line. Its Cortex, object and
semantic-view tools stay hidden, each with its reason.

The server here is a stand-in (fixtures/fake_snowflake_labs_mcp.py) over a SQLite file."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import SECRETS
from harness import MemoryKeychain
from mcp import Client
from mcp.types import CallToolResult, Tool
from portward_free.account import Accounts
from portward_free.paths import database_path
from portward_free.profiles import Grant, Profile
from portward_free.storage import LocalStore
from portward_free.usage import Usage
from portward_free.wrap import setup
from portward_free.wrap.providers import PROVIDERS, SnowflakeMcp
from portward_free.wrap.server import build_wrap_server
from test_profiles import EMAIL, PlanAccounts

FAKE = Path(__file__).parent / "fixtures" / "fake_snowflake_labs_mcp.py"
NAME = "labs"
PAT = "pat-secret-token-labs-0123456789"
SQL_TOOL = "run_snowflake_query"


def text_of(result: CallToolResult) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content) + repr(result.structured_content)


class World:
    def __init__(self, home: Path, db: Path, tmp: Path) -> None:
        self.home, self.log, self.tmp = home, tmp / "labs.jsonl", tmp
        self.environment = {"FAKE_SNOWFLAKE_DB": str(db), "FAKE_MCP_LOG": str(self.log)}
        self.keychain = MemoryKeychain()
        self.keychain.set("account-token", "pwat_fake")
        self.accounts = Accounts(PlanAccounts("active"))
        self.store = LocalStore.open(database_path(home))

    def their_config(self) -> Path:
        """The user's own service config: everything on, every statement allowed."""
        path = self.tmp / "tools_config.yaml"
        path.write_text(json.dumps({
            "agent_services": [{"service_name": "a", "database_name": "D", "schema_name": "S"}],
            "search_services": [{"service_name": "s", "database_name": "D", "schema_name": "S"}],
            "analyst_services": [{"service_name": "x", "semantic_model": "D.S.M"}],
            "other_services": {"object_manager": True, "query_manager": True, "semantic_manager": True},
            "sql_statement_permissions": [{"All": True}]}))
        return path

    def command(self) -> list[str]:
        return [sys.executable, str(FAKE), "--account", "acme-xy12345", "--user", "PORTWARD_LABS", "--warehouse",
                "REPORTING", "--service-config-file", str(self.their_config()), "--password", "typed-on-the-line",
                "--transport", "streamable-http"]

    def add(self) -> setup.Added:
        return setup.add_wrapped(self.store, self.keychain, self.accounts, NAME, "snowflake", self.command(), PAT,
                                 clients=setup.ClientConfigs(()), env=self.environment)

    def grant(self, *names: str) -> None:
        profile = self.store.profiles.save(Profile.new("Sales", "pipeline", EMAIL,
                                                       tuple(Grant.parse(n) for n in names)))
        self.store.profiles.apply(NAME, profile, ())

    def logged(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def sent(self) -> list[str]:
        return [e["sql"] for e in self.logged() if "sql" in e]

    @asynccontextmanager
    async def ai(self) -> AsyncIterator[Client]:
        server = build_wrap_server(self.store, self.keychain, NAME,
                                   usage=Usage(self.store, None, disabled="usage counts are off in tests"))
        async with Client(server) as client:
            yield client


@pytest.fixture()
def world(home: Path, people_db: Path, tmp_path: Path) -> Iterator[World]:
    found = World(home, people_db, tmp_path)
    yield found
    found.store.close()


def test_run_snowflake_query_is_the_governed_tool(world: World) -> None:
    added = world.add()
    assert added.governed == (SQL_TOOL,)


def test_portward_starts_it_with_only_the_query_tool_and_only_select(world: World) -> None:
    world.add()
    started = [e for e in world.logged() if "started" in e]
    assert started
    for entry in started:
        assert entry["config"]["other_services"] == {"query_manager": True, "object_manager": False,
                                                     "semantic_manager": False}
        assert entry["config"]["sql_statement_permissions"] == [{"Select": True}]
        assert not any(entry["config"].get(k) for k in ("agent_services", "search_services", "analyst_services"))
        assert entry["password_in_env"]
        args = entry["started"]
        assert "typed-on-the-line" not in args and PAT not in args and "--password" not in args
        assert args[args.index("--transport") + 1] == "stdio" and args.count("--transport") == 1
        assert args.count("--service-config-file") == 1


def test_the_login_typed_on_the_command_line_isnt_saved(world: World) -> None:
    world.add()
    saved = json.dumps(world.store.connection(NAME).settings)
    assert "typed-on-the-line" not in saved and PAT not in saved
    assert "--account" in saved  # the rest of the command is kept, to start it again


async def test_the_query_tool_returns_only_granted_columns(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        names = {t.name for t in (await ai.list_tools()).tools}
        everything = await ai.call_tool(SQL_TOOL, {"statement": "SELECT * FROM customers"})
    assert SQL_TOOL in names
    shown = text_of(everything)
    assert not everything.is_error, shown
    assert "Ada" in shown and not any(s in shown for s in SECRETS)
    queries = [q for q in world.sent() if "information_schema" not in q.lower()]
    assert queries and all("ssn" not in q.lower() for q in queries)


async def test_a_write_never_reaches_snowflake(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        refused = await ai.call_tool(SQL_TOOL, {"statement": "DELETE FROM customers"})
    assert refused.is_error
    assert not any("DELETE" in q.upper() for q in world.sent())


def tool(name: str, **properties: str) -> Tool:
    return Tool(name=name, input_schema={"type": "object",
                                         "properties": {k: {"type": v} for k, v in properties.items()}})


# The tools of Snowflake-Labs/mcp at 662cb48, with their arguments, and the reason each stays hidden.
@pytest.mark.parametrize(("listed", "why"), [
    (tool("cortex_agent", service_name="string", database_name="string", schema_name="string", query="string"),
     SnowflakeMcp.AGENT),
    (tool("cortex_search", service_name="string", database_name="string", schema_name="string", query="string",
          columns="array", filter_query="object", limit="integer"), SnowflakeMcp.SEARCH),
    (tool("cortex_analyst", service_name="string", semantic_model="string", query="string"), SnowflakeMcp.ANALYST),
    (tool("create_object", object_type="string", target_object="object", mode="string"), SnowflakeMcp.OBJECTS),
    (tool("drop_object", object_type="string", target_object="object", if_exists="boolean"), SnowflakeMcp.OBJECTS),
    (tool("create_or_alter_object", object_type="string", target_object="object"), SnowflakeMcp.OBJECTS),
    (tool("describe_object", object_type="string", target_object="object"), SnowflakeMcp.CATALOG),
    (tool("list_objects", object_type="string", database_name="string", schema_name="string", like="string"),
     SnowflakeMcp.CATALOG),
    (tool("list_semantic_views", database_name="string", schema_name="string"), SnowflakeMcp.CATALOG),
    (tool("describe_semantic_view", view_name="string", database_name="string", schema_name="string"),
     SnowflakeMcp.CATALOG),
    (tool("show_semantic_dimensions", database_name="string", schema_name="string"), SnowflakeMcp.CATALOG),
    (tool("show_semantic_metrics", database_name="string", schema_name="string"), SnowflakeMcp.CATALOG),
    (tool("get_semantic_view_ddl", database_name="string", schema_name="string", view_name="string"),
     SnowflakeMcp.CATALOG),
    (tool("write_semantic_view_query_tool", database_name="string", schema_name="string", view_name="string"),
     SnowflakeMcp.SEMANTIC),
    (tool("query_semantic_view", database_name="string", schema_name="string", view_name="string"),
     SnowflakeMcp.SEMANTIC),
])
def test_each_other_snowflake_labs_tool_stays_hidden_with_its_reason(listed: Tool, why: str) -> None:
    provider = PROVIDERS["snowflake"]
    assert provider.sql_argument(listed) is None
    assert provider.why_hidden(listed) == why


def test_the_sql_tool_is_known_by_its_name_and_its_statement_argument() -> None:
    provider = PROVIDERS["snowflake"]
    assert provider.sql_argument(tool("run_snowflake_query", statement="string")) == "statement"
    assert provider.sql_argument(tool("run_snowflake_query", statement="string", other="string")) is None
    assert provider.sql_argument(tool("some_procedure", statement="string")) is None


@pytest.mark.parametrize("command", [["uvx", "snowflake-labs-mcp", "--pat", "x"],
                                     ["uvx", "snowflake-labs-mcp", "--private-key=abc"],
                                     ["uvx", "snowflake-labs-mcp", "--private-key-file-pwd", "pw"]])
def test_no_login_option_is_kept_in_the_settings(command: list[str]) -> None:
    settings = PROVIDERS["snowflake"].settings(command, None, {})
    assert settings["command"] == ["uvx", "snowflake-labs-mcp"]
