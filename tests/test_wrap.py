"""Wrap mode for Postgres MCP (#100): Portward runs the user's Postgres MCP server as a child process and sits between
it and the AI app. The SQL tool gets the profile rewrite; every tool Portward can't tie to granted data stays hidden.

The child here is a stand-in for Postgres MCP Pro (fixtures/fake_postgres_mcp.py) over a SQLite file, so nothing is
downloaded. An AI client talks to the wrap server in process; the wrap server talks to the child over stdio."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from harness import Harness, MemoryKeychain
from mcp import Client
from mcp.types import CallToolResult
from test_profiles import EMAIL, PlanAccounts, Recorder, User
from test_rules import ROUTES_TO_A_BLOCKED_COLUMN

from conftest import ADMIN_PASSWORD, PG_PORT, READER, READER_PASSWORD, SECRETS
from portward_free import cli
from portward_free.account import Accounts
from portward_free.keychain import account_for
from portward_free.paths import database_path
from portward_free.profiles import Grant, Profile
from portward_free.storage import LocalStore
from portward_free.usage import Usage
from portward_free.wrap import setup
from portward_free.wrap.providers import failure
from portward_free.wrap.pyrepr import NotReadable, rows_from_repr
from portward_free.wrap.server import WrapRefused, build_wrap_server

FAKE = Path(__file__).parent / "fixtures" / "fake_postgres_mcp.py"
NAME = "warehouse"


def text_of(result: CallToolResult) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content) + repr(result.structured_content)


class World:
    """One computer: its Portward store and keychain, signed in to Personal, and the child server's SQL log."""

    def __init__(self, home: Path, db: Path, tmp: Path) -> None:
        self.home, self.db, self.log = home, db, tmp / "child-sql.jsonl"
        self.keychain = MemoryKeychain()
        self.keychain.set("account-token", "pwat_fake")
        self.accounts = Accounts(PlanAccounts("active"))
        self.store = LocalStore.open(database_path(home))

    def command(self) -> list[str]:
        return [sys.executable, str(FAKE)]

    def environment(self) -> dict[str, str]:
        return {"FAKE_MCP_LOG": str(self.log)}

    def add(self, clients: setup.ClientConfigs | None = None) -> setup.Added:
        return setup.add_wrapped(self.store, self.keychain, self.accounts, NAME, "postgres", self.command(),
                                 f"sqlite:///{self.db}", clients=clients or setup.ClientConfigs(()),
                                 env=self.environment())

    def grant(self, *names: str) -> None:
        profile = self.store.profiles.save(Profile.new("Sales", "pipeline", EMAIL,
                                                       tuple(Grant.parse(n) for n in names)))
        self.store.profiles.apply(NAME, profile, ())

    def sent_to_child(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    @asynccontextmanager
    async def ai(self) -> AsyncIterator[Client]:
        """The AI app connected to `portward-free wrap warehouse`."""
        usage = Usage(self.store, None, disabled="usage counts are off in tests")
        server = build_wrap_server(self.store, self.keychain, NAME, usage=usage)
        async with Client(server) as client:
            yield client


@pytest.fixture()
def world(home: Path, people_db: Path, tmp_path: Path) -> AsyncIterator[World]:
    found = World(home, people_db, tmp_path)
    yield found
    found.store.close()


# --- tools ----------------------------------------------------------------------------------------------------------

async def test_only_the_sql_tool_reaches_the_ai(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        names = {t.name for t in (await ai.list_tools()).tools}
    assert "execute_sql" in names
    assert not names & {"list_objects", "analyze_db_health", "summarize_everything"}


async def test_every_wrapped_tool_has_a_title_and_reads_only(world: World) -> None:  # #147
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        listed = (await ai.list_tools()).tools
    assert {t.name for t in listed} >= {"execute_sql", "list_tables", "more_rows"}
    for t in listed:
        assert t.title and t.annotations is not None, t.name
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False, t.name
        assert t.annotations.open_world_hint is False, t.name


async def test_a_hidden_tool_cant_be_called_by_name(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        called = await ai.call_tool("analyze_db_health", {})
    assert called.is_error
    assert SECRETS[1] not in text_of(called)


# --- the rewrite ----------------------------------------------------------------------------------------------------

async def test_the_sql_tool_returns_only_granted_columns(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        everything = await ai.call_tool("execute_sql", {"sql": "SELECT * FROM customers"})
    shown = text_of(everything)
    assert not everything.is_error, shown
    assert "Ada" in shown and "Grace" in shown
    assert not any(s in shown for s in SECRETS)
    assert everything.structured_content["columns"] == ["name"]


@pytest.mark.parametrize("sql", [
    "SELECT ssn FROM customers",
    "SELECT name FROM customers WHERE ssn LIKE '123%'",
    "SELECT name FROM customers ORDER BY ssn",
    "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id",
    "SELECT * FROM cards",
    "SELECT body FROM notes",
])
async def test_the_sql_tool_refuses_what_no_profile_grants(world: World, sql: str) -> None:
    world.add()
    world.grant("customers.name", "customers.id")
    async with world.ai() as ai:
        refused = await ai.call_tool("execute_sql", {"sql": sql})
    assert refused.is_error, text_of(refused)
    assert not any(s in text_of(refused) for s in SECRETS)


@pytest.mark.parametrize("sql", ROUTES_TO_A_BLOCKED_COLUMN)
async def test_a_column_no_applied_profile_grants_never_reaches_the_ai_through_a_wrapped_server(
        world: World, sql: str) -> None:
    """The same joins, subqueries, CTEs and functions test_profiles runs on Portward's own connectors."""
    world.add()
    world.grant("customers.id", "customers.name", "orders.id", "orders.customer_id", "orders.total")
    async with world.ai() as ai:
        answered = await ai.call_tool("execute_sql", {"sql": sql})
    assert not any(s in text_of(answered) for s in SECRETS), text_of(answered)


async def test_the_child_gets_the_rewritten_sql_not_the_ais(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        await ai.call_tool("execute_sql", {"sql": "SELECT * FROM customers"})
    queries = [q["sql"] for q in world.sent_to_child() if "information_schema" not in q["sql"]]
    assert queries, "the query never reached the child"
    assert all("ssn" not in q.lower() for q in queries)
    assert all('SELECT "name" FROM customers' in q for q in queries)


async def test_the_child_runs_in_restricted_mode(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        await ai.call_tool("execute_sql", {"sql": "SELECT name FROM customers"})
    assert {q["access_mode"] for q in world.sent_to_child()} == {"restricted"}


async def test_nothing_is_shown_before_a_profile_is_applied(world: World) -> None:
    world.add()
    async with world.ai() as ai:
        refused = await ai.call_tool("execute_sql", {"sql": "SELECT name FROM customers"})
    assert refused.is_error
    assert "Ada" not in text_of(refused)


async def test_a_write_never_reaches_the_child(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        refused = await ai.call_tool("execute_sql", {"sql": "DELETE FROM customers"})
    assert refused.is_error
    assert not any("DELETE" in q["sql"].upper() for q in world.sent_to_child())


async def test_an_error_from_the_child_isnt_passed_on_as_is(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:  # the child's own error text could quote a value: Portward says it its own way
        failed = await ai.call_tool("execute_sql", {"sql": "SELECT name + 'x' FROM customers WHERE nope = 1"})
    assert failed.is_error
    assert "Error:" not in text_of(failed)


# --- through Portward's own tools ------------------------------------------------------------------------------------

async def test_profiles_apply_to_a_wrapped_server_as_to_portwards_own_connectors(world: World) -> None:
    world.add()
    async with Harness.start(world.home, keychain=world.keychain, accounts=world.accounts,
                             elicitation=User()) as h:
        made = await h.call("create_profile", name="Sales", description="pipeline", grants=["customers.name"])
        applied = await h.call("apply_profile", profile="Sales", source=NAME)
        pulled = await h.call("query", source=NAME, question="who?", sql="SELECT * FROM customers")
        refused = await h.call("query", source=NAME, question="?", sql="SELECT ssn FROM customers")
    assert not made.is_error and not applied.is_error, applied.ai.text
    assert "1 matched" in applied.ai.text
    assert not pulled.is_error, pulled.ai.text
    assert pulled.ai.structured["columns"] == ["name"]
    assert "Ada" in pulled.ai.text and not any(s in pulled.ai.text for s in SECRETS)
    assert refused.is_error


async def test_totals_and_anonymised_grants_hold_on_a_wrapped_server(world: World) -> None:
    """#93/#94 through wrap: totals holds as on Portward's own connectors (only SUM, AVG, COUNT; small groups left
    out); anonymised falls to hidden, because stand-ins would need the key inside SQL the wrapped server sees."""
    world.add()
    profile = world.store.profiles.save(Profile.new("Sales", "pipeline", EMAIL, (
        Grant.parse("customers.name"), Grant.parse("orders.id"), Grant.parse("orders.total", level="totals"),
        Grant.parse("customers.ssn", level="anonymised"))))
    world.store.profiles.apply(NAME, profile, ())
    async with Harness.start(world.home, keychain=world.keychain, accounts=world.accounts,
                             elicitation=User()) as h:
        raw = await h.call("query", source=NAME, question="?", sql="SELECT id, total FROM orders")
        summed = await h.call("query", source=NAME, question="?", sql="SELECT sum(total) AS s FROM orders")
        ssn = await h.call("query", source=NAME, question="?", sql="SELECT ssn FROM customers")
        star = await h.call("query", source=NAME, question="?", sql="SELECT * FROM customers")
    assert raw.is_error and "10.0" not in raw.ai.text
    assert not summed.is_error, summed.ai.text
    assert summed.ai.structured["rows"] == []  # 3 orders: under the group threshold, left out
    assert ssn.is_error
    assert not star.is_error and star.ai.structured["columns"] == ["name"]
    assert not any(s in t for s in SECRETS for t in (ssn.ai.text, star.ai.text))


async def test_anonymised_on_a_wrapped_server_says_it_is_hidden_and_why(world: World) -> None:
    """The column stays hidden (never full); the apply preview says so with the reason, and the connection's
    description, which only says what the AI can use, doesn't name it (spec story 20: the AI is never told why)."""
    world.add()
    async with Harness.start(world.home, keychain=world.keychain, accounts=world.accounts,
                             elicitation=User()) as h:
        await h.call("create_profile", name="Sales", description="pipeline", grants=["customers.name"],
                     anonymised=["customers.ssn"])
        applied = await h.call("apply_profile", profile="Sales", source=NAME)
        described = await h.call("describe_source", source=NAME)
        star = await h.call("query", source=NAME, question="?", sql="SELECT * FROM customers")
    assert not applied.is_error, applied.ai.text
    assert "1 matched, 1 hidden here instead" in applied.ai.text
    assert "hidden here instead: customers.ssn (" in applied.ai.text and "stand-in" in applied.ai.text
    notices = described.ai.structured.get("notices", [])
    assert not any("ssn" in n for n in notices), notices  # the AI is never told what's hidden, or why
    assert not any("anonymised: you get a stand-in" in n for n in notices)
    assert star.ai.structured["columns"] == ["name"]


async def test_a_refusal_names_only_tools_the_wrap_server_has(world: World) -> None:
    """The guard's words name the main server's catalog tools; through wrap they name list_tables instead."""
    world.add()
    profile = world.store.profiles.save(Profile.new("Sales", "pipeline", EMAIL, (
        Grant.parse("customers.name"), Grant.parse("orders.total", level="totals"))))
    world.store.profiles.apply(NAME, profile, ())
    async with world.ai() as ai:
        refused = await ai.call_tool("execute_sql", {"sql": "SELECT sum(o.total) FROM orders o JOIN customers c "
                                                            "ON c.ssn = 'x'"})
        unknown = await ai.call_tool("execute_sql", {"sql": "SELECT nope FROM customers"})
    assert refused.is_error and "call list_tables" in text_of(refused), text_of(refused)
    for result in (refused, unknown):
        assert result.is_error and not any(t in text_of(result) for t in ("describe_source", "list_sources"))


async def test_list_tables_shows_only_what_is_granted(world: World) -> None:
    world.add()
    world.grant("customers.name", "orders.total")
    async with world.ai() as ai:
        listed = await ai.call_tool("list_tables", {})
    assert listed.structured_content["tables"] == [{"name": "customers", "columns": ["name"]},
                                                   {"name": "orders", "columns": ["total"]}]


async def test_the_ai_cant_set_up_a_wrapped_server(world: World, people_db: Path) -> None:
    async with Harness.start(world.home, keychain=world.keychain, accounts=world.accounts) as h:
        refused = await h.call("add_source", name="sneaky", kind="wrap-postgres", path=str(people_db))
    assert refused.is_error and "wrap-add" in refused.ai.text
    assert world.store.connection("sneaky") is None


async def test_a_pull_through_the_wrap_server_is_in_history(world: World) -> None:
    world.add()
    world.grant("customers.name")
    async with world.ai() as ai:
        await ai.call_tool("execute_sql", {"sql": "SELECT name FROM customers"})
    (entry,) = world.store.all_pulls()
    assert entry.source == NAME and entry.row_count == 3


# --- setup ----------------------------------------------------------------------------------------------------------

def claude_desktop(tmp: Path, servers: dict[str, Any]) -> setup.ClientConfigs:
    path = tmp / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": servers}))
    return setup.ClientConfigs((("Claude Desktop", path),))


def test_setup_says_which_tools_are_governed_and_which_stay_hidden(world: World) -> None:
    added = world.add()
    assert added.governed == ("execute_sql",)
    hidden = dict(added.hidden)
    assert set(hidden) == {"list_objects", "analyze_db_health", "summarize_everything"}
    assert "lists tables" in hidden["list_objects"]
    summary = added.summary()
    assert "summarize_everything" in summary and "portward-free wrap warehouse" in summary


def test_setup_warns_when_the_original_server_is_still_configured(world: World, tmp_path: Path) -> None:
    clients = claude_desktop(tmp_path, {
        "postgres": {"command": "uvx", "args": ["postgres-mcp", "--access-mode=unrestricted"],
                     "env": {"DATABASE_URI": "postgresql://elsewhere"}},
        "warehouse": {"command": "portward-free", "args": ["wrap", "warehouse"]},
    })
    added = world.add(clients)
    (warning,) = added.warnings
    assert "'postgres'" in warning and "Claude Desktop" in warning
    assert "Warning:" in added.summary()


def test_setup_warns_about_any_entry_with_the_same_login(world: World, tmp_path: Path) -> None:
    clients = claude_desktop(tmp_path, {"db": {"command": "some-other-server",
                                               "env": {"DATABASE_URI": f"sqlite:///{world.db}"}}})
    assert len(world.add(clients).warnings) == 1


def test_setup_reads_codex_config(world: World, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[mcp_servers.pg]\ncommand = "docker"\nargs = ["run", "-i", "crystaldba/postgres-mcp"]\n')
    added = world.add(setup.ClientConfigs((("ChatGPT desktop / Codex", path),)))
    assert len(added.warnings) == 1 and "'pg'" in added.warnings[0]


def test_setup_doesnt_warn_when_only_portward_is_configured(world: World, tmp_path: Path) -> None:
    clients = claude_desktop(tmp_path, {"warehouse": {"command": "uvx", "args": ["portward-free", "wrap", "x"]}})
    added = world.add(clients)
    assert added.warnings == ()
    assert "claude.ai" in added.summary()  # cloud connectors can't be seen from here: always a reminder


@pytest.mark.parametrize("plan", ["ended", None])
def test_setup_needs_personal(world: World, plan: str | None) -> None:
    if plan is None:
        world.keychain.delete("account-token")
    else:
        world.accounts = Accounts(PlanAccounts(plan))
    with pytest.raises(WrapRefused, match="Personal"):
        world.add()
    assert world.store.connection(NAME) is None


def test_setup_saves_nothing_when_the_server_wont_start(world: World) -> None:
    world.command = lambda: [sys.executable, "-c", "raise SystemExit(3)"]  # type: ignore[method-assign]
    with pytest.raises(WrapRefused):
        world.add()
    assert world.store.connection(NAME) is None
    assert world.keychain.get(account_for(NAME)) is None


def test_the_login_is_kept_in_the_keychain_not_the_store(world: World) -> None:
    world.add()
    assert world.keychain.get(account_for(NAME)) == f"sqlite:///{world.db}"
    assert str(world.db) not in json.dumps(world.store.connection(NAME).settings)


# --- the terminal commands ------------------------------------------------------------------------------------------

def wrap_add(world: World, *, at_terminal: bool = True, typed: str | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(["wrap-add", NAME, "--", *world.command()], home=world.home, keychain=world.keychain,
                    prompt=lambda label: typed if typed is not None else f"sqlite:///{world.db}", out=out,
                    account_service=world.accounts.service, is_terminal=lambda: at_terminal,
                    usage=lambda store: Usage(store, None, disabled="off in tests"))
    return code, out.getvalue()


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, world: World) -> Path:
    """No AI app config but the test's own, the child's SQL log on, and no DATABASE_URI in the environment."""
    fake_home = tmp_path / "user-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.chdir(fake_home)
    monkeypatch.delenv("DATABASE_URI", raising=False)
    return fake_home


def test_wrap_add_runs_only_at_a_terminal(world: World, clean_env: Path) -> None:
    code, said = wrap_add(world, at_terminal=False)
    assert code == 2 and "terminal" in said
    assert world.store.connection(NAME) is None


def test_wrap_add_asks_for_the_login_and_says_what_it_governs(world: World, clean_env: Path) -> None:
    (clean_env / ".claude.json").write_text(json.dumps({"mcpServers": {"pg": {"command": "postgres-mcp"}}}))
    code, said = wrap_add(world)
    assert code == 0, said
    assert "Governed (your profiles apply): execute_sql" in said
    assert "Warning: Claude Code still has the original Postgres MCP server 'pg'" in said
    assert str(world.db) not in said  # the login is never printed
    assert world.keychain.get(account_for(NAME)) == f"sqlite:///{world.db}"


def test_wrap_add_takes_the_login_from_database_uri(world: World, clean_env: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URI", f"sqlite:///{world.db}")
    code, said = wrap_add(world, typed="never asked")
    assert code == 0, said
    assert world.keychain.get(account_for(NAME)) == f"sqlite:///{world.db}"


def test_wrap_refuses_a_name_that_isnt_wrapped(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["wrap", "nothing"], home=world.home, keychain=world.keychain,
                    usage=lambda store: Usage(store, None, disabled="off in tests"))
    assert code == 1 and "wrap-add" in capsys.readouterr().err


# --- usage ----------------------------------------------------------------------------------------------------------

async def test_wrap_use_is_counted(world: World) -> None:
    world.add()
    world.grant("customers.name")
    usage = Usage(world.store, Recorder())
    usage.set(True, by_user=True)
    server = build_wrap_server(world.store, world.keychain, NAME, usage=usage)
    async with Client(server) as ai:
        await ai.call_tool("execute_sql", {"sql": "SELECT name FROM customers"})
    (report,) = usage.reports()
    assert report["pulls"] == 1 and report["source_types"] == ["wrap-postgres"]


# --- against real Postgres ------------------------------------------------------------------------------------------

WRAP_DB = "pwfree_wrap"
WRAP_SEED = (
    "DROP TABLE IF EXISTS sales, staff CASCADE",
    "DROP VIEW IF EXISTS staff_view",
    "CREATE TABLE staff (id int PRIMARY KEY, name text, salary numeric, hired date)",
    "INSERT INTO staff VALUES (1, 'Ada', 250000.50, '2020-01-02'), (2, 'Grace', 180000, '2021-03-04')",
    "CREATE VIEW staff_view AS SELECT name, salary AS pay FROM staff",
    "CREATE TABLE sales (id int, region text, amount numeric) PARTITION BY LIST (region)",
    "CREATE TABLE sales_north PARTITION OF sales FOR VALUES IN ('north')",
    f"GRANT SELECT ON staff, staff_view, sales, sales_north TO {READER}",
)


@pytest.fixture()
def wrap_pg(postgres_db: Any) -> str:
    """A database of its own on the test Postgres, read through the reader login: its URI."""
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname="postgres", autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (WRAP_DB,)).fetchone():
            admin.execute(f"CREATE DATABASE {WRAP_DB}")
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname=WRAP_DB, autocommit=True) as db:
        for statement in WRAP_SEED:
            db.execute(statement)
    return f"postgresql://{READER}:{READER_PASSWORD}@127.0.0.1:{PG_PORT}/{WRAP_DB}"


@pytest.mark.integration
async def test_against_real_postgres_the_catalog_and_rewrite_hold(world: World, wrap_pg: str) -> None:
    setup.add_wrapped(world.store, world.keychain, world.accounts, NAME, "postgres", world.command(), wrap_pg,
                      clients=setup.ClientConfigs(()), env=world.environment())
    world.grant("staff.name", "staff.hired", "staff_view.name", "staff_view.pay", "sales.region",
                "sales_north.amount")
    async with world.ai() as ai:
        listed = await ai.call_tool("list_tables", {})
        pulled = await ai.call_tool("execute_sql", {"sql": "SELECT * FROM staff ORDER BY name"})
        probe = await ai.call_tool("execute_sql", {"sql": "SELECT name FROM staff WHERE salary > 200000"})
    assert listed.structured_content["tables"] == [{"name": "sales", "columns": ["region"]},
                                                   {"name": "staff", "columns": ["name", "hired"]}]
    assert not pulled.is_error, text_of(pulled)
    assert pulled.structured_content["columns"] == ["name", "hired"]
    assert pulled.structured_content["rows"][0] == ["Ada", "2020-01-02"]
    assert "250000" not in text_of(pulled)
    assert probe.is_error and "Ada" not in text_of(probe)


# --- reading Postgres MCP's results ---------------------------------------------------------------------------------

def test_postgres_mcp_results_are_read_with_their_types() -> None:
    text = ("[{'id': 1, 'total': Decimal('1.50'), 'day': datetime.date(2026, 11, 6), 'at': datetime.datetime(2026, "
            "11, 6, 9, 30, tzinfo=datetime.timezone.utc), 'ok': True, 'note': None, 'tags': ['a', 'b']}]")
    (row,) = rows_from_repr(text)
    assert row == {"id": 1, "total": Decimal("1.50"), "day": date(2026, 11, 6),
                   "at": datetime(2026, 11, 6, 9, 30, tzinfo=UTC), "ok": True, "note": None, "tags": ["a", "b"]}


@pytest.mark.parametrize("text", ["__import__('os').system('true')", "[{'a': open('x')}]", "[1, 2]",
                                  "[{'a': [x for x in 'ab']}]", "{'a': 1}", "Error: nope"])
def test_anything_else_isnt_read(text: str) -> None:
    with pytest.raises(NotReadable):
        rows_from_repr(text)


@pytest.mark.parametrize(("message", "said"), [
    # real Postgres MCP Pro's words (crystaldba/postgres-mcp 0.3.0): quoted, and qualified without quotes
    ('Error: column "ssn" does not exist\nLINE 1: /* crystaldba */ SELECT ssn FROM ...', "no column ssn"),
    ("Error: column c.id does not exist\nLINE 1: ... ON c.id = o.c...\nHINT:  Perhaps you meant to reference the "
     'column "o.id".', "no column c.id"),
])
def test_a_missing_column_is_named_as_the_ai_wrote_it(message: str, said: str) -> None:
    assert failure(message) == f"the query failed: {said} is available there"
