"""The hosted route (#104), end to end against the real control app on 127.0.0.1 and its local engine host.

The user puts a connection on the hosted route ("Portward runs this"). The computer asks the control plane for the
account's engine; the host (a fake that starts the engine in this process, as a real host starts the container)
starts it, and it registers its own public key. The computer seals the connection, its login and its applied
profiles to that key and sends the ciphertext through the control plane. claude.ai then reaches the database
through the engine with the computer off, under the same profiles, and the engine's AI log summary comes back,
sealed, into the computer's (and so the synced) history."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import ADMIN_PASSWORD, PG_PORT, READER, SECRETS, Database
import psycopg
from test_context import Reviewer
from test_links import linked
from test_rules import ROUTES_TO_A_BLOCKED_COLUMN
from harness import Harness, MemoryKeychain
import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from test_remote_mcp import fake_authkit_token, free_port
from mcp.types import ElicitResult
from portward_free import cli
from test_sync import Confirm, Device, Service, device, recovery_key_in, service  # noqa: F401  (the control app)

from portward_free import stand_ins
from portward_free.account import Accounts, AccountService
from portward_free.hosted import bundle, engine as hosted_engine
from portward_free.hosted.engine import TOOLS, Engine
from portward_free.sources import egress
from portward_free.keychain import account_for
from portward_free.paths import database_path
from portward_free.profiles import Grant, Profile
from portward_free.proof import AiRead, ShownColumn
from portward_free.remote import routes
from portward_free.sources import network
from portward_free.storage import LocalStore
from portward_free.usage import Usage

PROFILE = "zq hosted profile marker"
DESCRIPTION = "zq hosted description marker"
LOGIN = "zq-hosted-login-Marker-5Rw"


@dataclass
class Host:
    """Where engines run, as a real host would: it starts each engine the control plane provisions."""
    service: Service
    root: Path
    engines: dict[str, Engine] = field(default_factory=dict)
    earlier: set[str] = field(default_factory=set)  # other tests' engines (the control app is shared)
    _stop: threading.Event = field(default_factory=threading.Event)

    def _watch(self) -> None:
        folder = self.service.folder / "engines"
        while not self._stop.wait(0.05):
            for path in folder.glob("h-*.json") if folder.is_dir() else ():
                held = json.loads(path.read_text())
                if held["state"] == "running" and held["engine_id"] not in {*self.engines, *self.earlier}:
                    self.start(held["engine_id"], held["env"])

    def start(self, engine_id: str, env: dict[str, str], name: str | None = None) -> Engine:
        engine = Engine(self.root / engine_id, env["PORTWARD_ACCOUNT_URL"], env["PORTWARD_ENGINE_TOKEN"],
                        env["PORTWARD_PUBLIC_URL"], host="127.0.0.1", port=free_port(), poll_seconds=0.2,
                        usage=Usage(LocalStore.memory(), None, disabled="usage counts are off in tests"))
        engine.start()
        self.engines[name or engine_id] = engine
        return engine

    def only(self, wait_seconds: float = 15.0) -> Engine:
        """The one engine this test's host started. The watcher starts it from another thread a moment after the
        control plane writes its file, so wait for it: asking at once lost that race on a slow machine."""
        deadline = time.monotonic() + wait_seconds
        while not self.engines and time.monotonic() < deadline:
            time.sleep(0.02)
        (engine,) = self.engines.values()
        return engine


@contextmanager
def host(service: Service, root: Path) -> Iterator[Host]:
    running = Host(service, root, earlier={p.stem for p in (service.folder / "engines").glob("h-*.json")})
    watcher = threading.Thread(target=running._watch, daemon=True)
    watcher.start()
    try:
        yield running
    finally:
        running._stop.set()
        watcher.join(5)
        for engine in running.engines.values():
            engine.close()


@asynccontextmanager
async def connector(engine: Engine, email: str) -> AsyncIterator[Client]:
    """claude.ai signed in as `email` for the engine's public address; it reaches the engine on 127.0.0.1 here."""
    headers = {"Authorization": f"Bearer {fake_authkit_token(email, engine.resource)}"}
    async with httpx2.AsyncClient(headers=headers, timeout=10) as http:
        async with Client(streamable_http_client(f"http://127.0.0.1:{engine.port}/mcp", http_client=http)) as client:
            yield client


@pytest.fixture()
def on_the_internet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test databases run on 127.0.0.1; here they stand for databases on the internet (to the computer's check
    of the name and to the engine's check of the address it resolves to, sources/egress.py)."""
    monkeypatch.setattr(routes, "laptop_only", lambda host: False)
    monkeypatch.setattr(egress, "is_public", lambda address: True)


@asynccontextmanager
async def computer(service: Service, home: Path, keychain: MemoryKeychain) -> AsyncIterator[Harness]:
    """The same computer turned on again: its file and keychain, still signed in."""
    async with Harness.start(home, keychain=keychain, accounts=Accounts(AccountService(service.url))) as h:
        yield h


async def profiled(h: Harness, db: Database) -> None:
    connected = await h.connect_database("shop", db.fields(), db.password())
    assert "error" not in json.dumps(connected.ai.structured or {}).lower() or not connected.is_error
    made = await h.call("create_profile", name=PROFILE, description=DESCRIPTION, grants=["customers.name"])
    assert not made.is_error, made.ai.text
    applied = await h.call("apply_profile", profile=PROFILE, source="shop")
    assert not applied.is_error, applied.ai.text


class Yes:
    """The user answering yes to whatever the client's prompt asks: applying a profile that shows more, and (H1)
    letting a connection's login go to the hosted engine."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.asked.append(params.message)
        fields = (getattr(params, "requested_schema", None) or {}).get("properties", {})  # a page ask has none
        return ElicitResult(action="accept", content={"confirm": True, **{name: True for name in fields}})


def allowed_at_the_terminal(laptop: Device, name: str) -> int:
    """The user runs `portward-free hosted-allow <name>` on the computer and types the source's name."""
    return cli.main(["hosted-allow", name], home=laptop.home, keychain=laptop.keychain, prompt=lambda _: name,
                    out=io.StringIO(), is_terminal=lambda: True,
                    usage=lambda store: Usage(store, None, disabled="usage counts are off in tests"))


def wait_for(check: Any, seconds: float = 10) -> Any:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        found = check()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("it didn't happen in time")


def landed(engine: Engine) -> None:
    """Wait until the engine has landed a whole bundle. A bundle lands piece by piece (the login, the connection, the
    profiles), so one piece being there doesn't mean the rest is: reading the others at once lost that race on a
    slow machine. The engine sets LANDED_SETTING only after the last piece."""
    wait_for(lambda: engine.store.setting(hosted_engine.LANDED_SETTING))


@pytest.mark.integration
async def test_claude_reaches_a_cloud_database_through_the_hosted_route_with_the_laptop_off(
        service: Service, tmp_path: Path, postgres_db: Database, on_the_internet: None) -> None:
    email = "hosted-owner@gmail.example"
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", email, elicitation=Yes()) as laptop:
            await profiled(laptop.h, postgres_db)
            put = await laptop.h.call("web_route", source="shop", route="hosted")
            assert not put.is_error, put.ai.text
            assert put.ai.structured["label"] == "Portward runs this" and put.ai.structured["delivered"] is True
            assert "Portward runs this" in put.ai.text
            keychain = laptop.keychain
        # the laptop is off: nothing of it runs; claude.ai talks to the engine only
        engine = engines.only()
        wait_for(lambda: engine.store.connection("shop") is not None)
        landed(engine)
        async with connector(engine, email) as claude:
            listed = await claude.call_tool("list_sources", {})
            (shop,) = listed.structured_content["sources"]
            assert {t["name"]: t["columns"] for t in shop["tables"]} == {"customers": ["name"]}
            pulled = await claude.call_tool("query", {"source": "shop", "question": "everyone",
                                                      "sql": "SELECT * FROM customers"})
            assert not pulled.is_error, pulled.content
            assert pulled.structured_content["columns"] == ["name"]  # region and id were never granted
            assert "north" not in json.dumps(pulled.structured_content)
        assert engine.forward() == 1

        # the laptop comes back: the engine's read is in its AI log, with route "hosted", and sync carries it on
        async with computer(service, laptop.home, keychain) as h:
            routed = await h.call("web_routes")
            assert routed.ai.structured["collected"] == 1
            (entry,) = (await h.call("ai_log")).ai.structured["entries"]
            assert entry["route"] == "hosted" and entry["connection"] == "shop"
            assert [p["name"] for p in entry["profiles"]] == [PROFILE]
            assert {(c["table"], c["column"]) for c in entry["columns"]} == {("customers", "name")}


async def test_the_engine_makes_the_same_stand_ins_as_the_computer(service: Service, tmp_path: Path) -> None:
    """The account's anonymising key travels sealed in the bundle, so a value has one stand-in everywhere: a join or
    filter the AI learnt on the computer holds on the web."""
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", "hosted-stand-ins@gmail.example", elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service)
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
            engine = engines.only()
            wait_for(lambda: engine.store.connection("warehouse") is not None)
            landed(engine)
            here, there = stand_ins.key_for_sync(laptop.keychain), stand_ins.key_for_sync(engine.keychain)
    assert here is not None and there is not None
    for value in ("Ada", 42, "zq-value"):
        assert stand_ins.stand_in_for(there, value, "0" * 32) == stand_ins.stand_in_for(here, value, "0" * 32)


@pytest.mark.integration
async def test_an_anonymised_column_reads_the_same_stand_ins_on_the_computer_and_the_hosted_route(
        service: Service, tmp_path: Path, postgres_db: Database, on_the_internet: None) -> None:
    email = "hosted-anon@gmail.example"
    sql = "SELECT name FROM customers ORDER BY name"
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", email, elicitation=Yes()) as laptop:
            await laptop.h.connect_database("shop", postgres_db.fields(), postgres_db.password())
            made = await laptop.h.call("create_profile", name=PROFILE, description=DESCRIPTION, grants=[],
                                       anonymised=["customers.name"])
            assert not made.is_error, made.ai.text
            assert not (await laptop.h.call("apply_profile", profile=PROFILE, source="shop")).is_error
            local = await laptop.h.call("query", source="shop", question="who?", sql=sql)
            assert not local.is_error, local.ai.text
            assert not (await laptop.h.call("web_route", source="shop", route="hosted")).is_error
        engine = engines.only()
        wait_for(lambda: engine.store.connection("shop") is not None)
        landed(engine)
        async with connector(engine, email) as claude:
            hosted = await claude.call_tool("query", {"source": "shop", "question": "who?", "sql": sql})
    assert not hosted.is_error, hosted.content
    rows = local.ai.structured["rows"]
    assert rows and all(str(r[0]).startswith("anon_") for r in rows)
    assert hosted.structured_content["rows"] == rows


@pytest.mark.integration
async def test_the_hosted_engine_scopes_stand_ins_to_links_as_the_computer_does(
        service: Service, tmp_path: Path, postgres_db: Database, people_pg: dict[str, Any],
        on_the_internet: None) -> None:
    """#121: the profile's links travel in the bundle with it, so the engine puts the same columns in the same link
    groups: linked columns match and join there too, and an unlinked column with the same values doesn't."""
    email = "hosted-scope@gmail.example"
    reads = {"customer": "SELECT id FROM customers ORDER BY name",
             "order": "SELECT customer_id FROM orders ORDER BY total",
             "order_id": "SELECT id FROM orders ORDER BY total",
             "joined": "SELECT c.name, o.total FROM orders o JOIN customers c ON c.id = o.customer_id ORDER BY o.total"}
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", email, elicitation=Reviewer()) as laptop:
            await laptop.h.connect_database("people", people_pg, postgres_db.password())
            made = await laptop.h.call("create_profile", name=PROFILE, description=DESCRIPTION,
                                       grants=["customers.name", "orders.total"],
                                       anonymised=["customers.id", "orders.customer_id", "orders.id"])
            assert not made.is_error, made.ai.text
            assert not (await laptop.h.call("apply_profile", profile=PROFILE, source="people")).is_error
            await linked(laptop.h, PROFILE, "orders.customer_id", "customers.id")
            local = {k: (await laptop.h.call("query", source="people", question="?", sql=sql)).ai.structured["rows"]
                     for k, sql in reads.items()}
            assert allowed_at_the_terminal(laptop, "people") == 0  # Reviewer answers context prompts only
            assert not (await laptop.h.call("web_route", source="people", route="hosted")).is_error
        engine = engines.only()
        wait_for(lambda: engine.store.setting(hosted_engine.LANDED_SETTING))
        async with connector(engine, email) as claude:
            hosted = {}
            for k, sql in reads.items():
                answered = await claude.call_tool("query", {"source": "people", "question": "?", "sql": sql})
                assert not answered.is_error, answered.content
                hosted[k] = answered.structured_content["rows"]
    assert hosted == local
    assert local["customer"] == local["order"]  # linked: the same stand-ins
    assert set(map(str, local["order_id"])).isdisjoint(map(str, local["customer"]))  # unlinked, same values 1-3
    assert len(local["joined"]) == 3


PEOPLE_DB = "pwfree_hosted_people"
PEOPLE_SEED = (  # conftest.people_db's tables, on Postgres: hosted connections are databases on the internet
    "DROP TABLE IF EXISTS orders, customers, notes",
    "CREATE TABLE customers (id int PRIMARY KEY, name text, ssn text)",
    f"INSERT INTO customers VALUES (1, 'Ada', '{SECRETS[0]}'), (2, 'Grace', '{SECRETS[1]}'), "
    f"(3, 'Linus', '{SECRETS[2]}')",
    "CREATE TABLE orders (id int PRIMARY KEY, customer_id int, total numeric)",
    "INSERT INTO orders VALUES (1, 1, 10), (2, 2, 20), (3, 3, 30)",
    "CREATE TABLE notes (id int PRIMARY KEY, body text)",
    "INSERT INTO notes VALUES (1, 'top secret plan')",
    f"GRANT SELECT ON customers, orders, notes TO {READER}",
)


@pytest.fixture()
def people_pg(postgres_db: Database) -> dict[str, Any]:
    """people_db's tables in a Postgres database of their own, read through the reader login: add_source's fields."""
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname="postgres", autocommit=True) as admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (PEOPLE_DB,)).fetchone():
            admin.execute(f"CREATE DATABASE {PEOPLE_DB}")
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname=PEOPLE_DB, autocommit=True) as db:
        for statement in PEOPLE_SEED:
            db.execute(statement)
    return postgres_db.fields(database=PEOPLE_DB)


@pytest.mark.integration
async def test_a_column_no_applied_profile_grants_never_reaches_the_ai_on_the_hosted_route(
        service: Service, tmp_path: Path, postgres_db: Database, people_pg: dict[str, Any],
        on_the_internet: None) -> None:
    """The same joins, subqueries, CTEs and functions test_profiles runs on a computer, sent to the engine. One
    engine serves them all (starting one per statement would take minutes); every leak is listed."""
    email = "hosted-routes@gmail.example"
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", email, elicitation=Yes()) as laptop:
            await laptop.h.connect_database("people", people_pg, postgres_db.password())
            made = await laptop.h.call("create_profile", name=PROFILE, description=DESCRIPTION, grants=[
                "customers.id", "customers.name", "orders.id", "orders.customer_id", "orders.total"])
            assert not made.is_error, made.ai.text
            assert not (await laptop.h.call("apply_profile", profile=PROFILE, source="people")).is_error
            assert not (await laptop.h.call("web_route", source="people", route="hosted")).is_error
        engine = engines.only()
        wait_for(lambda: engine.store.setting(hosted_engine.LANDED_SETTING))  # set once the whole bundle landed
        leaks = []
        async with connector(engine, email) as claude:
            granted = await claude.call_tool("query", {"source": "people", "question": "?",
                                                       "sql": "SELECT name FROM customers ORDER BY name"})
            assert not granted.is_error and "Ada" in json.dumps(granted.structured_content)  # it does answer
            for sql in ROUTES_TO_A_BLOCKED_COLUMN:
                answered = await claude.call_tool("query", {"source": "people", "question": "?", "sql": sql})
                said = json.dumps(answered.structured_content) + repr(answered.content)
                leaks += [sql for s in SECRETS if s in said]
    assert not leaks, leaks


async def hosted_by_hand(laptop: Device, service: Service, name: str = "warehouse", profiled: bool = False) -> None:
    """A connection to a database on the internet, with a login (and a profile applied), without connecting to it."""
    with closing(LocalStore.open(database_path(laptop.home))) as store:
        store.add_connection(name, "postgres", {"host": "db.zq-hosted-host.example", "port": 5432,
                                                "database": "zq_hosted_db", "user": "zq_hosted_user"})
        if profiled:
            profile = store.profiles.save(Profile.new(PROFILE, DESCRIPTION, "me@example.com",
                                                      [Grant("zq_hosted_table", "zq_hosted_column")]))
            store.profiles.switch_to_profiles(name)
            store.profiles.apply(name, profile, ())
    laptop.keychain.set(account_for(name), LOGIN)


async def test_logins_and_profiles_never_appear_readable_in_control_plane_storage_or_logs(
        service: Service, tmp_path: Path) -> None:
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", "hosted-markers@gmail.example", elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service, profiled=True)
            put = await laptop.h.call("web_route", source="warehouse", route="hosted")
            assert not put.is_error and put.ai.structured["delivered"], put.ai.text
            engine = engines.only()
            wait_for(lambda: engine.keychain.get(account_for("warehouse")) == LOGIN)  # the engine opened it
            landed(engine)
            assert [a.profile.name for a in engine.store.profiles.applied("warehouse")] == [PROFILE]
    stored, log = service.stored_bytes(), service.log()
    # (profile names are among the facts the account page reads, #105, so they're not markers here)
    for marker in (LOGIN, DESCRIPTION, "zq_hosted_table", "zq_hosted_column", "zq-hosted-host",
                   "zq_hosted_user", "zq_hosted_db"):
        assert marker.encode() not in stored, marker
        assert marker not in log, marker
    started = b"".join(p.read_bytes() for p in (service.folder / "engines").glob("*.json"))
    for marker in (LOGIN, DESCRIPTION, "zq_hosted_db", "zq-hosted-host"):
        assert marker.encode() not in started, marker  # nor in what the host is given


async def test_files_laptop_only_databases_and_wrapped_servers_cant_be_put_on_the_hosted_route(
        service: Service, tmp_path: Path, shop_db: Path) -> None:
    engines_before = set((service.folder / "engines").glob("*.json"))
    async with device(service, tmp_path / "laptop", "hosted-refused@gmail.example", elicitation=Yes()) as laptop:
        assert not (await laptop.h.add_file("file", str(shop_db))).is_error
        with closing(LocalStore.open(database_path(laptop.home))) as store:
            store.add_connection("local", "postgres", {"host": "localhost", "port": 5432, "database": "d",
                                                       "user": "u"})
            store.add_connection("tailnet", "mysql", {"host": "100.101.102.103", "port": 3306, "database": "d",
                                                      "user": "u"})
            store.add_connection("wrapped", "wrap-postgres", {"command": ["postgres-mcp"]})
        for name in ("local", "tailnet", "wrapped"):
            laptop.keychain.set(account_for(name), LOGIN)
        for name in ("file", "local", "tailnet", "wrapped"):
            refused = await laptop.h.call("web_route", source=name, route="hosted")
            assert refused.is_error and "direct route" in refused.ai.text, refused.ai.text
        listed = (await laptop.h.call("web_routes")).ai.structured["connections"]
        assert {c["source"]: c["routes"] for c in listed} == {n: ["direct"] for n in ("file", "local", "tailnet",
                                                                                      "wrapped")}
    assert set((service.folder / "engines").glob("*.json")) == engines_before  # no engine was made


def test_the_engine_refuses_a_bundle_that_puts_a_file_on_the_hosted_route() -> None:
    made = {"format": 1, "connections": [{"name": "f", "kind": "sqlite", "settings": {"path": "/x.db"},
                                          "login": "x"}],
            "rules": [], "notes": [], "profiles": [], "on_profiles": [], "applied": [],
            "anon_key": "A" * 43 + "=", "inbox_key": "A" * 43 + "="}
    with pytest.raises(bundle.BadBundle, match="hosted route"):
        bundle.check(made)


async def test_the_engine_answers_requests_at_once_while_it_lands_bundles_and_hands_on_reads(
        service: Service, tmp_path: Path) -> None:
    """claude.ai may call several tools at once (each on its own worker thread) while the engine's poll thread lands
    bundles and hands on its AI log: one local file, many threads, and every call still answers."""
    email = "hosted-busy@gmail.example"
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", email, elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service, profiled=True)
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
        engine = engines.only()
        wait_for(lambda: engine.store.setting(hosted_engine.LANDED_SETTING))
        stop, errors = threading.Event(), []

        def poll_hard() -> None:  # the poll thread's work, without its pause
            try:
                while not stop.is_set():
                    engine.store.setting(hosted_engine.LANDED_SETTING)
                    engine.store.set_setting("zq_busy", str(time.monotonic()))
                    engine.forward()
            except BaseException as err:  # noqa: BLE001 (asserted on below)
                errors.append(err)

        busy = threading.Thread(target=poll_hard)
        busy.start()
        try:
            async with connector(engine, email) as claude:
                answers = await asyncio.gather(*(claude.call_tool(tool, {}) for tool in
                                                 ("list_sources", "history", "ai_log") * 8))
        finally:
            stop.set()
            busy.join(10)
    assert errors == []
    assert [a for a in answers if a.is_error] == []


async def test_the_engine_serves_only_tools_that_read(service: Service, tmp_path: Path) -> None:
    email = "hosted-tools@gmail.example"
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", email, elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service)
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
        engine = engines.only()
        async with connector(engine, email) as claude:
            served = {t.name for t in (await claude.list_tools()).tools}
        assert served <= TOOLS and {"list_sources", "query"} <= served
        for editing in ("add_source", "block", "unblock", "create_profile", "apply_profile", "web_route",
                        "sync_turn_on", "account_sign_out", "share"):
            assert editing not in served


async def test_the_hosted_label_links_the_security_page_saying_what_the_engine_holds(service: Service,
                                                                                      tmp_path: Path) -> None:
    security = service.url + "/security"
    with host(service, tmp_path / "host"):
        async with device(service, tmp_path / "laptop", "hosted-label@gmail.example", elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service)
            put = await laptop.h.call("web_route", source="warehouse", route="hosted")
            assert not put.is_error, put.ai.text
            assert put.ai.structured["label_url"] == security and security in put.ai.text
            routed = (await laptop.h.call("web_routes")).ai.structured
            assert routed["label_url"] == security and security in routed["about"]
            async with httpx2.AsyncClient() as web:
                assert (await web.get(security)).status_code == 200


async def test_taking_the_last_connection_off_deletes_the_engine_and_what_it_held(service: Service,
                                                                                  tmp_path: Path) -> None:
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", "hosted-off@gmail.example", elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service)
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
            engine_id = next(iter(engines.engines))
            off = await laptop.h.call("web_route", source="warehouse", route="direct")
            assert not off.is_error and "deleted" in off.ai.text, off.ai.text
            assert not (service.folder / "engines" / f"{engine_id}.json").exists()
            listed = (await laptop.h.call("web_routes")).ai.structured["connections"]
            assert listed == [{"source": "warehouse", "routes": ["hosted", "direct"], "on": "direct"}]


async def test_the_direct_route_says_portward_never_sees_your_data(service: Service, tmp_path: Path) -> None:
    with host(service, tmp_path / "host"):
        async with device(service, tmp_path / "laptop", "hosted-trust@gmail.example", elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service)
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
            direct = await laptop.h.call("web_route", source="warehouse", route="direct")
            about = (await laptop.h.call("web_routes")).ai.structured["about"]
    assert "Portward never sees your data" in direct.ai.text
    assert "Portward never sees your data" in about
    assert "never sees inside" not in direct.ai.text + about


async def test_a_change_on_the_computer_reaches_the_engine(service: Service, tmp_path: Path) -> None:
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", "hosted-change@gmail.example", elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service)
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
            engine = engines.only()
            await hosted_by_hand(laptop, service, name="second")
            assert not (await laptop.h.call("web_route", source="second", route="hosted")).is_error
            wait_for(lambda: engine.store.connection("second") is not None)
            removed = await laptop.h.call("remove_source", name="second")
            assert not removed.is_error, removed.ai.text
            wait_for(lambda: engine.store.connection("second") is None)  # gone from the engine, its login too
            assert engine.keychain.get(account_for("second")) is None
            assert engine.store.connection("warehouse") is not None


async def test_the_engine_starts_and_serves_quickly_from_a_cold_start(service: Service, tmp_path: Path) -> None:
    """Locally, from a stopped process to answering: the part of waking that is the engine's own. The host's part
    (a suspended or stopped machine resuming) is measured on the real host (docs, manual steps)."""
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", "hosted-cold@gmail.example", elicitation=Yes()) as laptop:
            await hosted_by_hand(laptop, service)
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
        first = engines.only()
        engine_id = next(iter(engines.engines))
        env = json.loads((service.folder / "engines" / f"{engine_id}.json").read_text())["env"]
        first.close()
        began = time.monotonic()
        again = engines.start(engine_id, env, name="again")  # the same volume: its key and file
        took = time.monotonic() - began
        assert again.store.connection("warehouse") is not None
        assert took < 10, f"the engine took {took:.1f}s to serve"


async def test_hosted_reads_reach_every_synced_computer_and_the_service_sees_only_ciphertext(
        service: Service, tmp_path: Path) -> None:
    """The engine never holds the sync key: it seals its AI log summary to the account's inbox key, which syncs
    between the computers, so whichever computer syncs next brings the hosted reads into the synced history."""
    email = "hosted-sync@gmail.example"
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", email) as laptop:  # an AI app that can't ask the user
            await hosted_by_hand(laptop, service)
            unasked = await laptop.h.call("web_route", source="warehouse", route="hosted")
            assert unasked.is_error and "hosted-allow warehouse" in unasked.ai.text  # H1: not on the AI's say-so
            assert allowed_at_the_terminal(laptop, "warehouse") == 0
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
            assert not (await laptop.h.call("sync_turn_on")).is_error
            key = recovery_key_in(laptop.recovery_key_page())
            async with device(service, tmp_path / "desktop", email) as desktop:
                await desktop.h.call("sync_turn_on")
                assert desktop.h.run_sync_command("sync-restore", key).code == 0
                assert not (await desktop.h.call("sync_now")).is_error  # the inbox key arrives here

                engine = engines.only()
                wait_for(lambda: engine.store.connection("warehouse") is not None)
                landed(engine)
                engine.store.reads.record(AiRead(app="claude.ai", route="hosted", connection="warehouse",
                                                 profiles=(), columns=(ShownColumn("zq_hosted_table",
                                                                                   "zq_hosted_column", "full"),),
                                                 row_count=7, result_hash="h"))
                assert engine.forward() == 1
                assert not (await desktop.h.call("sync_now")).is_error  # collected there, and synced on
                assert not (await laptop.h.call("sync_now")).is_error
                logs = [(await h.call("ai_log")).ai.structured["entries"] for h in (laptop.h, desktop.h)]
    assert logs[0] == logs[1]
    assert [(e["route"], e["row_count"]) for e in logs[0]] == [("hosted", 7)]
    assert b"zq_hosted_column" not in service.stored_bytes() and "zq_hosted_column" not in service.log()


async def test_an_engine_cant_write_a_computers_history() -> None:
    """Entries the engine sends that don't say route "hosted" are dropped when collected."""
    from portward_free.hosted import inbox, seal

    private = seal.new_private_key()
    keychain = MemoryKeychain()
    inbox.adopt_inbox_key(keychain, private)
    store = LocalStore.memory()
    forged = AiRead(app="Claude Desktop", route="desktop", connection="x", profiles=(), columns=(), row_count=1,
                    result_hash="h")
    honest = AiRead(app="claude.ai", route="hosted", connection="x", profiles=(), columns=(), row_count=2,
                    result_hash="h")

    class Mailbox:
        acked: list[str] = []

        def reads(self, token: str) -> list[tuple[str, bytes]]:
            return [("r1", seal.seal(seal.public_key(private), seal.READS, "acct",
                                     {"reads": [forged.as_dict(), honest.as_dict()]}))]

        def ack(self, token: str, ids: list[str]) -> None:
            self.acked.extend(ids)

    mailbox = Mailbox()
    assert inbox.collect(store, keychain, mailbox, "token", "acct") == 1  # type: ignore[arg-type]
    assert [r.route for r in store.reads.between()] == ["hosted"] and mailbox.acked == ["r1"]


def test_the_container_command_says_what_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from portward_free import cli

    for name in hosted_engine.ENV:
        monkeypatch.delenv(name, raising=False)
    out = io.StringIO()
    assert cli.main(["serve-hosted"], out=out) == 1
    assert "PORTWARD_ENGINE_TOKEN" in out.getvalue()


@pytest.mark.integration
async def test_the_engine_refuses_a_database_whose_name_passes_but_whose_address_is_private(
        service: Service, tmp_path: Path, postgres_db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sweep open problem 1: the computer only sees the name; the engine checks where it really goes (127.0.0.1)."""
    monkeypatch.setattr(routes, "laptop_only", lambda host: False)
    email = "hosted-private@gmail.example"
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", email, elicitation=Yes()) as laptop:
            await profiled(laptop.h, postgres_db)
            assert not (await laptop.h.call("web_route", source="shop", route="hosted")).is_error
        engine = engines.only()
        wait_for(lambda: engine.store.connection("shop") is not None)
        landed(engine)
        async with connector(engine, email) as claude:
            pulled = await claude.call_tool("query", {"source": "shop", "question": "everyone",
                                                      "sql": "SELECT * FROM customers"})
            assert pulled.is_error and "private address" in json.dumps([c.model_dump() for c in pulled.content])


class No(Yes):
    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.asked.append(params.message)
        return ElicitResult(action="decline")


async def test_the_user_is_asked_what_goes_where_before_a_login_leaves_and_a_no_sends_nothing(
        service: Service, tmp_path: Path) -> None:
    """H1/M4: web_route is the AI's call, so the yes is the user's own, in their AI app's prompt."""
    user = No()
    with host(service, tmp_path / "host") as engines:
        async with device(service, tmp_path / "laptop", "hosted-no@gmail.example", elicitation=user) as laptop:
            await hosted_by_hand(laptop, service)
            put = await laptop.h.call("web_route", source="warehouse", route="hosted")
            listed = await laptop.h.call("web_routes")
    (asked,) = user.asked
    assert "db.zq-hosted-host.example" in asked and "zq_hosted_user" in asked and "password" in asked
    assert "Portward's hosted engine" in asked and LOGIN not in asked
    assert put.is_error and "nothing was sent" in put.ai.text
    assert [c["on"] for c in listed.ai.structured["connections"] if c["source"] == "warehouse"] == ["direct"]
    assert LOGIN.encode() not in service.stored_bytes()


async def test_the_yes_is_asked_once_per_connection_not_at_every_change(service: Service, tmp_path: Path) -> None:
    user = Yes()
    with host(service, tmp_path / "host"):
        async with device(service, tmp_path / "laptop", "hosted-once@gmail.example", elicitation=user) as laptop:
            await hosted_by_hand(laptop, service)
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
            assert not (await laptop.h.call("web_route", source="warehouse", route="hosted")).is_error
            assert not (await laptop.h.call("web_routes")).is_error
    assert len([a for a in user.asked if "hosted engine" in a]) == 1
