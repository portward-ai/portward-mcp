"""A source name pointed at another server: the user is told where it pointed before, where they type the password.

The AI chooses add_source's host. Text it read (a prompt injection) could have it re-add a name the user trusts with
someone else's server, and the user, asked for "the password for 'prod'", would type the real one. The form already
shows the server; when the name pointed somewhere else until now, it says that too.
"""

from pathlib import Path

from harness import Harness, MemoryKeychain

from portward_free.passwords import password_ask
from portward_free.storage import Connection

REQUEST = "portward/secret_request"
FIRST = {"kind": "postgres", "host": "db.acme.example", "port": 5432, "database": "app", "user": "reader"}


def _details(reply) -> dict[str, str]:
    return {k: v for k, v in reply.panel.meta[REQUEST]["details"]}


async def test_a_new_source_has_nothing_it_replaces(home: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        added = await h.call("add_source", name="prod", **FIRST)
    assert _details(added)["Server"] == "db.acme.example:5432"
    assert "Replaces" not in _details(added)


async def test_a_name_moved_to_another_server_says_where_it_pointed_before(home: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        await h.call("add_source", name="prod", **FIRST)
        moved = await h.call("add_source", name="prod", **{**FIRST, "host": "db.evil.example"})
    details = _details(moved)
    assert details["Server"] == "db.evil.example:5432"
    assert details["Replaces"].startswith("db.acme.example:5432")
    assert moved.ai.structured["replaces"] == "db.acme.example:5432"
    assert "db.acme.example:5432" in moved.ai.text  # the AI is told too, so an honest one says so


async def test_another_port_on_the_same_host_counts_as_another_server(home: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        await h.call("add_source", name="prod", **FIRST)
        moved = await h.call("add_source", name="prod", **{**FIRST, "port": 6543})
    assert _details(moved)["Replaces"].startswith("db.acme.example:5432")


async def test_the_same_server_with_another_login_or_database_replaces_nothing(home: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        await h.call("add_source", name="prod", **FIRST)
        again = await h.call("add_source", name="prod", **{**FIRST, "user": "analyst", "database": "sales"})
    assert "Replaces" not in _details(again)


async def test_host_names_that_differ_only_by_case_are_the_same_server(home: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        await h.call("add_source", name="prod", **FIRST)
        again = await h.call("add_source", name="prod", **{**FIRST, "host": "DB.Acme.Example"})
    assert "Replaces" not in _details(again)


async def test_a_file_source_renamed_to_a_database_says_what_it_was(home: Path, shop_db: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        await h.add_file("prod", shop_db)
        moved = await h.call("add_source", name="prod", **FIRST)
    assert "shop.sqlite" in _details(moved)["Replaces"]


def test_the_ask_survives_the_trip_to_the_other_server_process() -> None:
    """Some AI apps run the panel in a second Portward process, which rebuilds the ask from its params."""
    from portward_free.passwords import password_ask_builder
    keychain = MemoryKeychain()
    ask = password_ask(Connection("prod", "postgres", dict(FIRST) | {"kind": "postgres"}), keychain,
                       replaces="db.acme.example:5432")
    rebuilt = password_ask_builder(keychain)(ask.params)
    assert dict(rebuilt.details)["Replaces"] == dict(ask.details)["Replaces"]
