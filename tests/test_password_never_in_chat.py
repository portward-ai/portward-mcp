"""A marker password, entered every way there is, is never in a tool result, the AI-sent log, the logs or the file."""

import asyncio
import logging
import re
from pathlib import Path

import pytest
from conftest import READER_PASSWORD, Database
from harness import Harness, MemoryKeychain, Reply
from test_password_page import User, request

SECRET = re.compile(r"pass|secret|pwd|credential|token|key|code", re.IGNORECASE)


async def test_no_tool_the_ai_can_call_takes_a_password(home: Path) -> None:
    for panel in (False, True):
        async with Harness.start(home, panel=panel) as h:
            tools = await h.ai_tools()
        for name, tool in tools.items():
            properties = (tool.input_schema or {}).get("properties", {})
            assert not [p for p in properties if SECRET.search(p)], name


async def _everything_seen(h: Harness) -> str:
    """What the AI was sent in this conversation, from the user's review tool."""
    listed = await h.call("sent_to_ai")
    current = next(c for c in listed.ai.structured["conversations"] if c["current"])
    return (await h.call("sent_to_ai", conversation_id=current["id"])).ai.text


def _files_containing(folder: Path, marker: str) -> list[str]:
    return [p.name for p in folder.rglob("*") if p.is_file() and marker.encode() in p.read_bytes()]


@pytest.mark.integration
@pytest.mark.parametrize("way", ["terminal", "panel", "page"])
async def test_the_marker_password_never_reaches_the_ai_the_log_or_the_file(
        home: Path, postgres_db: Database, keychain: MemoryKeychain, caplog: pytest.LogCaptureFixture,
        capfd: pytest.CaptureFixture[str], way: str) -> None:
    caplog.set_level(logging.DEBUG)
    user = User()
    replies: list[Reply] = []
    async with Harness.start(home, keychain=keychain, panel=way == "panel",
                             elicitation=user if way == "page" else None, protocol="legacy") as h:
        added = await h.call("add_source", name="shop", **postgres_db.fields())
        replies.append(added)
        assert added.ai.structured["password_entry"] == way
        if way == "terminal":
            assert h.type_password_in_terminal("shop", "wrong-" + READER_PASSWORD).code != 0
            assert h.type_password_in_terminal("shop", READER_PASSWORD).code == 0
        elif way == "panel":
            ask = added.panel.meta["portward/secret_request"]["token"]
            await h.call_from_panel("save_secret", request=ask, secret="wrong-" + READER_PASSWORD)
            await h.call_from_panel("save_secret", request=ask, secret=READER_PASSWORD)
        else:
            await asyncio.to_thread(request, user.url, "POST", {"secret": "wrong-" + READER_PASSWORD})
            await asyncio.to_thread(request, user.url, "POST", {"secret": READER_PASSWORD})
        for tool, args in (("test_source", {"name": "shop"}), ("list_sources", {}),
                           ("update_password", {"name": "shop"}),
                           ("query", {"source": "shop", "question": "n", "sql": "SELECT count(*) FROM customers"})):
            replies.append(await h.call(tool, **args))
        seen = await _everything_seen(h)
    assert keychain.passwords == {"connection:shop": READER_PASSWORD}
    assert "add_source" in seen  # the log is being read, not empty
    out, err = capfd.readouterr()
    for text in (seen, caplog.text, out, err, *(r.ai.text + str(r.ai.structured) for r in replies)):
        assert READER_PASSWORD not in text
    assert _files_containing(home, READER_PASSWORD) == []
