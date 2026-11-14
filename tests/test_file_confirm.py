"""A file is added as a source only on the user's own yes: the AI writes add_source's arguments, so on its own it
could point a new name at any file, or at a second copy of one the user blocked parts of."""

from pathlib import Path

import pytest
from conftest import SECRETS
from harness import Harness, MemoryKeychain, connected
from mcp.types import ElicitResult
from test_rules import leaked


class User:
    """Answers Portward's questions in the client's prompt, and remembers them."""

    def __init__(self, yes: bool) -> None:
        self.yes = yes
        self.asked: list[str] = []

    async def __call__(self, context, params) -> ElicitResult:
        self.asked.append(params.message)
        if not self.yes:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content={"yes": True})


@pytest.mark.parametrize("protocol", ["modern", "legacy"])
async def test_the_client_asks_the_user_before_a_file_is_added(home: Path, shop_db: Path, protocol: str) -> None:
    user = User(yes=True)
    async with Harness.start(home, elicitation=user, protocol=protocol) as h:
        h.files.answer = False  # the test's user answers, not the harness
        added = await h.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
    assert not added.is_error, added.ai.text
    assert added.ai.structured["tables"] == ["customers", "orders"]
    assert len(user.asked) == 1 and str(shop_db.resolve()) in user.asked[0] and "'shop'" in user.asked[0]


async def test_a_file_the_user_says_no_to_is_not_added(home: Path, shop_db: Path) -> None:
    async with Harness.start(home, elicitation=User(yes=False)) as h:
        h.files.answer = False
        added = await h.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        listed = await h.call("list_sources")
    assert added.is_error and "nothing was added" in added.ai.text
    assert listed.ai.structured["sources"] == []


async def test_the_ai_cant_slip_in_a_second_copy_of_a_blocked_file(home: Path, people_db: Path) -> None:
    user = User(yes=False)
    async with connected(home, people_db, name="people", elicitation=user) as h:
        await h.call("block", source="people", table="customers", column="ssn")
        h.files.answer = False
        copy = await h.call("add_source", name="copy", kind="sqlite", path=str(people_db))
        read = await h.call("query", source="copy", question="?", sql="SELECT ssn FROM customers")
    assert copy.is_error and len(user.asked) == 1
    assert read.is_error and "no source named 'copy'" in read.ai.text and not leaked(read)
    assert SECRETS[0] not in user.asked[0]


async def test_adding_the_same_file_again_under_its_name_asks_nothing(home: Path, shop_db: Path) -> None:
    user = User(yes=False)
    async with connected(home, shop_db, elicitation=user) as h:
        h.files.answer = False
        again = await h.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
    assert not again.is_error and user.asked == []


async def test_without_a_prompt_or_panel_the_user_adds_it_with_a_terminal_command(home: Path, shop_db: Path) -> None:
    async with Harness.start(home) as h:
        added = await h.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        redirected = h.add_file_in_terminal("shop", "sqlite", str(shop_db), at_terminal=False)
        mistyped = h.add_file_in_terminal("shop", "sqlite", str(shop_db), typed="shp")
        before = await h.call("list_sources")
        typed = h.add_file_in_terminal("shop", "sqlite", str(shop_db))
        after = await h.call("list_sources")
    assert not added.is_error and added.ai.structured["confirm_entry"] == "terminal"
    assert "add-file shop sqlite" in added.ai.structured["command"]
    assert redirected.code == 2 and "yourself in a terminal" in redirected.out  # an AI with a shell can't do it
    assert mistyped.code == 1 and "Not added" in mistyped.out
    assert before.ai.structured["sources"] == []
    assert typed.code == 0 and str(shop_db.resolve()) in typed.out
    assert [s["name"] for s in after.ai.structured["sources"]] == ["shop"]


async def test_a_file_that_doesnt_open_is_refused_before_the_user_is_asked(home: Path, tmp_path: Path) -> None:
    user = User(yes=True)
    async with Harness.start(home, elicitation=user) as h:
        h.files.answer = False
        missing = await h.call("add_source", name="x", kind="sqlite", path=str(tmp_path / "missing.sqlite"))
    assert missing.is_error and user.asked == []


def test_the_local_page_allows_a_file_with_one_button_and_nothing_to_type(shop_db: Path) -> None:
    from test_password_page import request

    from portward_free import file_sources
    from portward_free.asks import ALLOW, Requests
    from portward_free.password_page import PasswordPage
    from portward_free.storage import Connection, LocalStore

    store = LocalStore.memory()
    connection = Connection("shop", "sqlite", {"path": str(shop_db)})
    found = file_sources.FileAdded("shop", str(shop_db), ("customers", "orders"))
    requests = Requests(store)
    ask = requests.open(file_sources.file_ask(store, MemoryKeychain(), connection, found))
    page = PasswordPage(ask, requests.answer, requests.is_open, 60).start()
    try:
        _, _, form = request(page.url)
        _, _, allowed = request(page.url, "POST", {"secret": ALLOW})
    finally:
        page.close()
    assert shop_db.name in form and ">Allow</button>" in form
    assert 'name="secret" type="hidden"' in form  # the button sends it: no box to type in
    assert "Source name" not in form and 'type="text"' not in form
    assert "keychain" not in (form + allowed).lower()
    assert "Allowed" in allowed and [c.name for c in store.connections()] == ["shop"]
