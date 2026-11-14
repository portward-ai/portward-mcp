"""Wrap mode (#100) against the real Postgres MCP Pro (crystaldba/postgres-mcp, from PyPI through uvx) and a throwaway
Postgres in Docker, driven the way an AI app drives it: `portward-free wrap <name>` started over stdio.

Opt-in: set PORTWARD_TEST_REAL_MCP=1. It skips when Docker or uvx is missing, or the server can't be fetched. The
container (official postgres image) is bound to 127.0.0.1 on a random port and removed afterwards.

Postgres MCP Pro's latest release (0.3.0) pins pglast==7.2.0, which PyPI no longer has, and leaves `mcp` unpinned, so
it doesn't install as published; plain `uvx postgres-mcp` falls back to 0.1.0, which crashes on mcp 2.x. The test runs
0.3.0 with those two overridden (POSTGRES_MCP_OVERRIDES)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from harness import MemoryKeychain
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult
from memory_keyring import SEED_ENV, server_env
from test_profiles import EMAIL, PlanAccounts

import portward_free.sources  # noqa: F401 - loads the wrap providers in their import order
from portward_free.account import Accounts
from portward_free.paths import HOME_ENV, database_path
from portward_free.profiles import Grant, Profile
from portward_free.storage import LocalStore
from portward_free.wrap import setup
from portward_free.wrap.providers import PROVIDERS

pytestmark = pytest.mark.integration

NAME = "warehouse"
IMAGE = "postgres:16-alpine"
SERVER = "postgres-mcp@0.3.0"
POSTGRES_MCP_OVERRIDES = ("pglast==7.22", "mcp<2")
ADMIN_PASSWORD = "pw"
READER, READER_PASSWORD = "pwfree_reader", "reader-Marker-7Qx"
SSNS = ("123-45-6789", "987-65-4321")
SEED = (
    f"CREATE ROLE {READER} LOGIN PASSWORD '{READER_PASSWORD}'",
    "CREATE TABLE customers (id int PRIMARY KEY, name text, ssn text, region text)",
    f"INSERT INTO customers VALUES (1, 'Ada', '{SSNS[0]}', 'north'), (2, 'Grace', '{SSNS[1]}', 'south')",
    "CREATE TABLE orders (id int PRIMARY KEY, customer_id int, total numeric, placed date)",
    # Ada: 7 orders (a group big enough for totals); Grace: 3 (left out)
    "INSERT INTO orders SELECT g, CASE WHEN g <= 7 THEN 1 ELSE 2 END, g * 10.5, DATE '2026-01-01' + g "
    "FROM generate_series(1, 10) AS g",
    "CREATE VIEW cards AS SELECT name, ssn AS number FROM customers",
    f"GRANT SELECT ON customers, orders, cards TO {READER}",
)
GOVERNED_ONLY = {"execute_sql", "list_tables", "more_rows"}


def text_of(result: CallToolResult) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content) + repr(result.structured_content)


def _wait_for(port: int, deadline: float) -> None:
    while True:
        try:
            with psycopg.connect(host="127.0.0.1", port=port, user="postgres", password=ADMIN_PASSWORD,
                                 dbname="postgres", connect_timeout=2):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="module")
def real_postgres() -> Iterator[int]:
    """A throwaway seeded Postgres in Docker on 127.0.0.1: its port."""
    if os.environ.get("PORTWARD_TEST_REAL_MCP") != "1":
        pytest.skip("opt-in: set PORTWARD_TEST_REAL_MCP=1 to run against the real Postgres MCP Pro")
    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("needs Docker running")
    container = f"pwfree_real_mcp_{uuid.uuid4().hex[:8]}"
    started = subprocess.run(["docker", "run", "-d", "--rm", "--name", container, "-e",
                              f"POSTGRES_PASSWORD={ADMIN_PASSWORD}", "-p", "127.0.0.1::5432", IMAGE],
                             capture_output=True, text=True)
    if started.returncode != 0:
        pytest.skip(f"couldn't start {IMAGE} in Docker")
    try:
        mapped = subprocess.run(["docker", "port", container, "5432"], capture_output=True, text=True, check=True)
        port = int(mapped.stdout.split()[0].rsplit(":", 1)[1])
        _wait_for(port, time.monotonic() + 60)
        with psycopg.connect(host="127.0.0.1", port=port, user="postgres", password=ADMIN_PASSWORD,
                             dbname="postgres", autocommit=True) as db:
            for statement in SEED:
                db.execute(statement.encode())
        yield port
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


@pytest.fixture(scope="module")
def server_command(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    """The command that starts the real Postgres MCP Pro, fetched once up front."""
    if shutil.which("uvx") is None:
        pytest.skip("needs uvx on PATH")
    overrides = tmp_path_factory.mktemp("uvx") / "overrides.txt"
    overrides.write_text("\n".join(POSTGRES_MCP_OVERRIDES) + "\n")
    command = ["uvx", "--overrides", str(overrides), SERVER]
    if subprocess.run([*command, "--help"], capture_output=True, timeout=300).returncode != 0:
        pytest.skip(f"couldn't fetch {SERVER} from PyPI")
    return command


class Computer:
    """One computer with Portward Personal, the real server wrapped as `warehouse`, and a profile applied."""

    def __init__(self, home: Path, tmp: Path, command: list[str], port: int) -> None:
        self.home, self.tmp, self.command = home, tmp, command
        self.uri = f"postgresql://{READER}:{READER_PASSWORD}@127.0.0.1:{port}/postgres"
        self.keychain = MemoryKeychain()
        self.keychain.set("account-token", "pwat_fake")
        self.store = LocalStore.open(database_path(home))
        desktop = tmp / "claude_desktop_config.json"  # the original server, still set up in Claude Desktop
        desktop.write_text(json.dumps({"mcpServers": {"postgres": {
            "command": "uvx", "args": ["postgres-mcp", "--access-mode=unrestricted"],
            "env": {"DATABASE_URI": self.uri}}}}))
        self.added = setup.add_wrapped(self.store, self.keychain, Accounts(PlanAccounts("active")), NAME, "postgres",
                                       command, self.uri, clients=setup.ClientConfigs((("Claude Desktop", desktop),)))
        profile = self.store.profiles.save(Profile.new("Sales", "pipeline", EMAIL, (
            Grant.parse("customers.id"), Grant.parse("customers.name"), Grant.parse("customers.region"),
            Grant.parse("orders.id"),
            Grant.parse("orders.customer_id"), Grant.parse("orders.total", level="totals"),
            Grant.parse("customers.ssn", level="anonymised"))))  # orders.placed and the cards view: not granted
        self.store.profiles.apply(NAME, profile, ())

    @asynccontextmanager
    async def ai(self) -> AsyncIterator[Client]:
        """The AI app, running `portward-free wrap warehouse` over stdio (its keychain holds what setup saved)."""
        seed = self.tmp / "keyring.json"
        seed.write_text(json.dumps(self.keychain.entries))
        seed.chmod(0o600)
        env = server_env(**{HOME_ENV: str(self.home), "PYTHON_KEYRING_BACKEND": "memory_keyring.SeededKeyring",
                            SEED_ENV: str(seed)})
        params = StdioServerParameters(command=sys.executable, args=["-m", "portward_free", "wrap", NAME], env=env)
        async with Client(params, read_timeout_seconds=120) as client:
            yield client


@pytest.fixture()
def computer(home: Path, tmp_path: Path, server_command: list[str], real_postgres: int) -> Iterator[Computer]:
    found = Computer(home, tmp_path, server_command, real_postgres)
    yield found
    found.store.close()


def test_the_real_servers_tools_are_the_ones_portward_describes(computer: Computer) -> None:
    provider = PROVIDERS["postgres"]
    assert computer.added.governed == ("execute_sql",)
    hidden = dict(computer.added.hidden)
    assert set(hidden) == set(provider.hidden_because)  # type: ignore[attr-defined]
    assert all(hidden[t] == provider.hidden_because[t] for t in hidden)  # type: ignore[attr-defined]


def test_setup_warns_that_the_original_server_is_still_set_up(computer: Computer) -> None:
    (warning,) = computer.added.warnings
    assert "'postgres'" in warning and "Claude Desktop" in warning


def test_the_child_runs_in_restricted_mode(computer: Computer) -> None:
    provider = PROVIDERS["postgres"]
    child = provider.open_child(computer.store.connection(NAME).settings, computer.uri)
    try:
        refused = child.call("execute_sql", {"sql": "CREATE TABLE pwfree_sneaky (a int)"})
        made = child.call("execute_sql", {"sql": "SELECT to_regclass('pwfree_sneaky') IS NOT NULL AS made"})
    finally:
        child.close()
    assert "Error" in text_of(refused)
    assert provider.rows(made).rows == ((False,),)


async def test_only_governed_tools_reach_the_ai(computer: Computer) -> None:
    async with computer.ai() as ai:
        listed = {t.name for t in (await ai.list_tools()).tools}
        sneaky = await ai.call_tool("get_object_details", {"schema_name": "public", "object_name": "customers"})
        health = await ai.call_tool("analyze_db_health", {})
    assert listed == GOVERNED_ONLY
    assert sneaky.is_error and health.is_error
    assert not any(s in text_of(r) for s in SSNS for r in (sneaky, health))


async def test_profiles_hold_through_execute_sql(computer: Computer) -> None:
    async with computer.ai() as ai:
        tables = await ai.call_tool("list_tables", {})
        star = await ai.call_tool("execute_sql", {"sql": "SELECT * FROM customers ORDER BY name"})
        joined = await ai.call_tool("execute_sql", {"sql": "SELECT o.id, c.name, c.region FROM orders o "
                                                           "JOIN customers c ON c.id = o.customer_id ORDER BY o.id"})
        hidden_join = await ai.call_tool("execute_sql", {"sql": "SELECT c.name FROM customers c JOIN orders o "
                                                                "ON o.placed = DATE '2026-01-02'"})
        nested = await ai.call_tool("execute_sql", {"sql": "SELECT name FROM (SELECT * FROM customers) AS c "
                                                           "WHERE region IN (SELECT region FROM customers) "
                                                           "ORDER BY name"})
        qualified = await ai.call_tool("execute_sql", {"sql": "SELECT a.name FROM customers a JOIN customers b "
                                                              "ON b.ssn = a.name"})
        ssn = await ai.call_tool("execute_sql", {"sql": "SELECT ssn FROM customers"})
        via_view = await ai.call_tool("execute_sql", {"sql": "SELECT * FROM cards"})
        probe = await ai.call_tool("execute_sql", {"sql": "SELECT name FROM customers WHERE ssn LIKE '1%'"})
        hidden_col = await ai.call_tool("execute_sql", {"sql": "SELECT placed FROM orders"})
        raw_total = await ai.call_tool("execute_sql", {"sql": "SELECT id, total FROM orders"})
        summed = await ai.call_tool("execute_sql", {"sql": "SELECT customer_id, sum(total) AS s FROM orders "
                                                           "GROUP BY customer_id"})
    assert tables.structured_content["tables"] == [{"name": "customers", "columns": ["id", "name", "region"]},
                                                   {"name": "orders", "columns": ["id", "customer_id", "total"]}]
    assert not star.is_error, text_of(star)
    assert star.structured_content["columns"] == ["id", "name", "region"]
    assert star.structured_content["rows"] == [[1, "Ada", "north"], [2, "Grace", "south"]]
    assert not joined.is_error, text_of(joined)
    assert joined.structured_content["columns"] == ["id", "name", "region"]
    assert joined.structured_content["rows"][0] == [1, "Ada", "north"]
    assert not nested.is_error, text_of(nested)
    assert nested.structured_content["rows"] == [["Ada"], ["Grace"]]
    assert hidden_join.is_error and "column 'placed'; call list_tables" in text_of(hidden_join), text_of(hidden_join)
    assert qualified.is_error and "no column b.ssn is available" in text_of(qualified), text_of(qualified)
    for refused in (ssn, via_view, probe, hidden_col, raw_total):
        assert refused.is_error, text_of(refused)
    assert not summed.is_error, text_of(summed)
    ((customer, total),) = summed.structured_content["rows"]  # Grace's 3 orders: under the group size, left out
    assert customer == 1 and Decimal(str(total)) == Decimal("294.0")
    everything = "".join(text_of(r) for r in (star, joined, nested, qualified, ssn, via_view, probe, hidden_col, hidden_join,
                                                  raw_total))
    assert not any(s in everything for s in SSNS) and "2026-01-02" not in everything


async def test_a_write_never_reaches_the_database(computer: Computer) -> None:
    async with computer.ai() as ai:
        refused = await ai.call_tool("execute_sql", {"sql": "DELETE FROM orders"})
    assert refused.is_error
    with psycopg.connect(computer.uri) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 10
