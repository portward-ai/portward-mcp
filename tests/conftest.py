import os
import shutil
import sqlite3
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pymysql
import pytest
from harness import MemoryKeychain
from install_facts import FREE

ORDER_COUNT = 250


class _StandInBrowser(webbrowser.BaseBrowser):
    """Takes every page and mailto: address a test opens; nothing reaches a real browser or mail app."""

    def __init__(self, opened: list[str]) -> None:
        super().__init__("portward-test")
        self.opened = opened

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        self.opened.append(url)
        return True


@pytest.fixture(autouse=True)
def no_real_apps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every test: `webbrowser` knows only a stand-in, so open_page, account_sign_in, share's mail draft and the
    sign-in command can never open a tab or a mail draft on the computer running the tests. Returns what was opened.
    Patched at the registry (not just `webbrowser.open`), so defaults bound at import (build_server's `browser`) are
    covered too."""
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "_browsers", {"portward-test": [None, _StandInBrowser(opened)]})
    monkeypatch.setattr(webbrowser, "_tryorder", ["portward-test"])
    monkeypatch.setattr(webbrowser, "_os_preferred_browser", None, raising=False)
    return opened


@pytest.fixture(scope="session")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The package built the way it is published (never uploaded anywhere)."""
    if shutil.which("uv") is None:
        pytest.skip("needs uv on PATH")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out), str(FREE)], check=True, capture_output=True)
    (built,) = out.glob("*.whl")
    return built


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """The app-data folder the server writes its local database to."""
    folder = tmp_path / "app-data"
    folder.mkdir()
    return folder


@pytest.fixture()
def shop_db(tmp_path: Path) -> Path:
    """A user's own SQLite file: 3 customers, 250 orders."""
    path = tmp_path / "shop.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT)")
        db.executemany("INSERT INTO customers VALUES (?, ?, ?)",
                       [(1, "Ada", "north"), (2, "Grace", "south"), (3, "Linus", "north")])
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?)",
                       [(i, i % 3 + 1, float(i)) for i in range(1, ORDER_COUNT + 1)])
    db.close()
    return path


@pytest.fixture()
def keychain() -> MemoryKeychain:
    """The OS keychain stand-in: the server stores passwords through the same `Keychain` interface."""
    return MemoryKeychain()


# Real Postgres and MySQL (the same containers as the gateway's tests):
#   docker run -d --name portward_pg_test -e POSTGRES_PASSWORD=pw -p 127.0.0.1:15432:5432 postgres:16-alpine
#   docker run -d --name portward_mysql_test -e MYSQL_ROOT_PASSWORD=pw -p 127.0.0.1:13306:3306 mysql:8
# Tests that need one skip when it isn't running. Everything they create is named pwfree_*.
PG_PORT = int(os.environ.get("PORTWARD_TEST_PG_PORT", "15432"))
MYSQL_PORT = int(os.environ.get("PORTWARD_TEST_MYSQL_PORT", "13306"))
ADMIN_PASSWORD = "pw"
DB_NAME = "pwfree_shop"
READER, WRITER = "pwfree_reader", "pwfree_writer"
READER_PASSWORD = "reader-Marker-7Qx"  # searched for in the local database file and the logs
WRITER_PASSWORD = "writer-Marker-9Kz"

PG_SEED = (
    "DROP TABLE IF EXISTS orders, customers",
    "CREATE TABLE customers (id int PRIMARY KEY, name text, region text)",
    "INSERT INTO customers VALUES (1, 'Ada', 'north'), (2, 'Grace', 'south'), (3, 'Linus', 'north')",
    "CREATE TABLE orders (id int PRIMARY KEY, customer_id int, total numeric)",
    "INSERT INTO orders VALUES (1, 1, 10), (2, 2, 20)",
    f"GRANT SELECT ON customers, orders TO {READER}, {WRITER}",
    f"GRANT INSERT, UPDATE ON customers TO {WRITER}",
)

MYSQL_SEED = (
    f"CREATE DATABASE IF NOT EXISTS {DB_NAME}",
    f"USE {DB_NAME}",
    "DROP TABLE IF EXISTS orders, customers",
    "CREATE TABLE customers (id int PRIMARY KEY, name varchar(50), region varchar(20))",
    "INSERT INTO customers VALUES (1, 'Ada', 'north'), (2, 'Grace', 'south'), (3, 'Linus', 'north')",
    "CREATE TABLE orders (id int PRIMARY KEY, customer_id int, total decimal(10, 2))",
    "INSERT INTO orders VALUES (1, 1, 10), (2, 2, 20)",
    f"DROP USER IF EXISTS {READER}, {WRITER}",
    f"CREATE USER {READER} IDENTIFIED BY '{READER_PASSWORD}'",
    f"CREATE USER {WRITER} IDENTIFIED BY '{WRITER_PASSWORD}'",
    f"GRANT SELECT ON {DB_NAME}.* TO {READER}",
    f"GRANT SELECT ON {DB_NAME}.* TO {WRITER}",
    f"GRANT INSERT, UPDATE ON {DB_NAME}.customers TO {WRITER}",
)


@dataclass(frozen=True)
class Database:
    """A seeded test database. `fields(...)` gives add_source's arguments for one login, `password(user)` its
    password."""

    kind: str
    port: int
    admin_user: str

    def fields(self, user: str = READER, **overrides: Any) -> dict[str, Any]:
        """add_source's arguments: the settings only, never the password (the user types it out of band)."""
        fields = {"kind": self.kind, "host": "127.0.0.1", "port": self.port, "database": DB_NAME, "user": user}
        return {**fields, **overrides}

    def password(self, user: str = READER) -> str:
        return {READER: READER_PASSWORD, WRITER: WRITER_PASSWORD, self.admin_user: ADMIN_PASSWORD}.get(user, "")

    def scalar(self, sql: str) -> Any:
        """One value, read as the admin."""
        if self.kind == "postgres":
            with psycopg.connect(host="127.0.0.1", port=self.port, user="postgres", password=ADMIN_PASSWORD,
                                 dbname=DB_NAME, autocommit=True) as db:
                return db.execute(sql).fetchone()[0]
        with pymysql.connect(host="127.0.0.1", port=self.port, user="root", password=ADMIN_PASSWORD,
                             database=DB_NAME, autocommit=True) as db, db.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]


def _seed_postgres() -> None:
    try:
        admin = psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                                dbname="postgres", autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("test Postgres not running (see free/tests/conftest.py)")
    with admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,)).fetchone():
            admin.execute(f"CREATE DATABASE {DB_NAME}")
        for role, password in ((READER, READER_PASSWORD), (WRITER, WRITER_PASSWORD)):
            exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            admin.execute(f"{'ALTER' if exists else 'CREATE'} ROLE {role} LOGIN PASSWORD '{password}'")
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="postgres", password=ADMIN_PASSWORD,
                         dbname=DB_NAME, autocommit=True) as db:
        for statement in PG_SEED:
            db.execute(statement)


def _seed_mysql() -> None:
    try:
        admin = pymysql.connect(host="127.0.0.1", port=MYSQL_PORT, user="root", password=ADMIN_PASSWORD,
                                connect_timeout=2, autocommit=True)
    except pymysql.Error:
        pytest.skip("test MySQL not running (see free/tests/conftest.py)")
    with admin, admin.cursor() as cur:
        for statement in MYSQL_SEED:
            cur.execute(statement)


@pytest.fixture(scope="session")
def postgres_db() -> Database:
    _seed_postgres()
    return Database("postgres", PG_PORT, "postgres")


@pytest.fixture(scope="session")
def mysql_db() -> Database:
    _seed_mysql()
    return Database("mysql", MYSQL_PORT, "root")


@pytest.fixture(params=["postgres", "mysql"])
def database(request: pytest.FixtureRequest) -> Database:
    """A test that takes `database` runs once against real Postgres and once against real MySQL."""
    return request.getfixturevalue(f"{request.param}_db")


SECRETS = ("123-45-6789", "987-65-4321", "555-12-3456")  # people_db's customers.ssn values


@pytest.fixture()
def people_db(tmp_path: Path) -> Path:
    """Customers with a sensitive `ssn` column, their orders, and a table of notes."""
    path = tmp_path / "people.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, ssn TEXT)")
        db.executemany("INSERT INTO customers VALUES (?, ?, ?)",
                       [(1, "Ada", SECRETS[0]), (2, "Grace", SECRETS[1]), (3, "Linus", SECRETS[2])])
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(1, 1, 10.0), (2, 2, 20.0), (3, 3, 30.0)])
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO notes VALUES (1, 'top secret plan')")
        # views read the tables under another name: a rule on the table must hold through them too
        db.execute("CREATE VIEW cards AS SELECT name, ssn AS number FROM customers")
        db.execute("CREATE VIEW memo AS SELECT body AS text FROM notes")
        db.execute("CREATE VIEW ids AS SELECT rowid AS r, name FROM customers")
    db.close()
    return path
