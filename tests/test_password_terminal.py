"""A database password never passes through the chat: without a panel or a page, the user types it in a terminal.

The AI-facing `add_source` takes a database's settings only. With no panel and no URL elicitation, its reply tells
the user which command to run; the command prompts with hidden input, tests the connection and saves the password
in the keychain.
"""

import os
import pty
import select
import sys
import time
from pathlib import Path

import pytest
from conftest import READER_PASSWORD, WRITER, WRITER_PASSWORD, Database
from harness import Harness, MemoryKeychain
from install_facts import FREE, VERSION
from memory_keyring import server_env

from portward_free.passwords import terminal_command
from portward_free.paths import HOME_ENV

pytestmark = pytest.mark.integration


async def test_add_source_saves_the_settings_and_asks_for_the_password_in_a_terminal(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        added = await h.call("add_source", name="shop", **database.fields())
        waiting = await h.call("test_source", name="shop")
    assert not added.is_error, added.ai.text
    assert added.ai.structured["password_needed"] is True
    assert added.ai.structured["password_entry"] == "terminal"
    assert "portward-free" in added.ai.text and "password shop" in added.ai.text
    assert [c["name"] for c in h.stored("connections")] == ["shop"]
    assert keychain.passwords == {}
    assert waiting.is_error and "password" in waiting.ai.text and "password shop" in waiting.ai.text


async def test_the_terminal_command_tests_the_connection_and_saves_the_password(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.call("add_source", name="shop", **database.fields())
        typed = h.type_password_in_terminal("shop", READER_PASSWORD)
        tested = await h.call("test_source", name="shop")
    assert typed.code == 0, typed.out
    assert "customers" in typed.out
    assert READER_PASSWORD not in typed.out
    assert keychain.passwords == {"connection:shop": READER_PASSWORD}
    assert not tested.is_error and tested.ai.structured["tables"] == ["customers", "orders"]


async def test_a_wrong_password_in_the_terminal_says_why_and_saves_nothing(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.call("add_source", name="shop", **database.fields())
        typed = h.type_password_in_terminal("shop", "not-the-password")
    assert typed.code != 0
    assert "wrong user or password" in typed.out
    assert "not-the-password" not in typed.out
    assert keychain.passwords == {}


async def test_the_terminal_command_names_a_source_that_does_not_exist(home: Path, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        typed = h.type_password_in_terminal("nope", "whatever")
    assert typed.code != 0 and "no source named 'nope'" in typed.out


async def test_a_file_source_needs_no_password(home: Path, shop_db: Path, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        added = await h.add_file("shop", str(shop_db))
        typed = h.type_password_in_terminal("shop", "whatever")
    assert "password_needed" not in added.ai.structured
    assert typed.code != 0 and "doesn't use a password" in typed.out


async def test_changing_a_connections_settings_drops_its_old_password(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    """Otherwise the AI could point a saved connection at another server and have the old password sent there."""
    async with Harness.start(home, keychain=keychain) as h:
        await h.call("add_source", name="shop", **database.fields())
        h.type_password_in_terminal("shop", READER_PASSWORD)
        again = await h.call("add_source", name="shop", **database.fields(user=WRITER))
    assert again.ai.structured["password_needed"] is True
    assert keychain.passwords == {}


async def test_update_password_asks_again_and_keeps_the_old_one_until_the_new_one_works(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, keychain=keychain) as h:
        await h.call("add_source", name="shop", **database.fields(user=WRITER))
        h.type_password_in_terminal("shop", WRITER_PASSWORD)
        asked = await h.call("update_password", name="shop")
        wrong = h.type_password_in_terminal("shop", "not-the-password")
        kept_meanwhile = dict(keychain.passwords)
        right = h.type_password_in_terminal("shop", WRITER_PASSWORD)
    assert not asked.is_error and "password shop" in asked.ai.text
    assert wrong.code != 0 and right.code == 0
    assert kept_meanwhile == {"connection:shop": WRITER_PASSWORD}


async def test_update_password_names_an_unknown_or_file_source(home: Path, shop_db: Path) -> None:
    async with Harness.start(home) as h:
        unknown = await h.call("update_password", name="nope")
        await h.add_file("file", str(shop_db))
        file = await h.call("update_password", name="file")
    assert unknown.is_error and "no source named 'nope'" in unknown.ai.text
    assert file.is_error and "doesn't use a password" in file.ai.text


def _read_until(fd: int, wanted: bytes, seconds: float = 20) -> bytes:
    seen = b""
    deadline = time.monotonic() + seconds
    while wanted not in seen and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
    return seen


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")  # the child execs at once
async def test_the_real_command_prompts_with_hidden_input(home: Path, database: Database) -> None:
    """The installed command, in a real (pseudo-)terminal: what is typed is not echoed back."""
    async with Harness.start(home) as h:
        await h.call("add_source", name="shop", **database.fields())
    env = server_env(**{HOME_ENV: str(home)})
    pid, fd = pty.fork()
    if pid == 0:  # the child: the command the reply tells the user to run
        os.execve(sys.executable, [sys.executable, "-m", "portward_free", "password", "shop"], env)
    try:
        prompt = _read_until(fd, b":")
        os.write(fd, READER_PASSWORD.encode() + b"\n")
        rest = _read_until(fd, b"customers")
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    assert b"password" in prompt.lower()
    assert READER_PASSWORD.encode() not in prompt + rest
    assert b"customers" in rest
    assert os.waitstatus_to_exitcode(status) == 0


def test_from_the_desktop_extension_the_command_is_the_bundled_program(monkeypatch: pytest.MonkeyPatch) -> None:
    """The .mcpb (#60) runs a compiled program, not uvx: the command names that program."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["/Applications/Claude ext/server/portward-free"])
    monkeypatch.delenv(HOME_ENV, raising=False)
    assert terminal_command("shop") == "'/Applications/Claude ext/server/portward-free' password shop"


def test_a_name_is_quoted_for_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOME_ENV, raising=False)
    assert terminal_command("x; rm -rf ~").endswith("password 'x; rm -rf ~'")


def test_the_skill_and_readme_describe_the_flow() -> None:
    skill = (FREE / "plugin/skills/portward/SKILL.md").read_text()
    readme = (FREE / "README.md").read_text()
    for words in ("no tool takes a password", "update_password", "test_source", "panel", "terminal"):
        assert words in skill
    assert f"uvx portward-free=={VERSION} password <name>" in readme
    for words in ("one-time page", "127.0.0.1", "keychain", "update_password"):
        assert words in readme
