"""Sharing a pull (#50), as the AI client and the receiver see it, against the real share service on 127.0.0.1.

The tool re-runs the pull under the source's current block rules, seals the snapshot on this machine, uploads only
ciphertext, and opens a mail draft to the teammate holding the link (with the key in its # fragment). The AI is told
who it went to and when it expires, never the link or the key. The service runs from the gateway project
(fixtures/share_service.py) with mail written to a file; nothing leaves this machine."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from harness import Harness, MemoryKeychain, connected

from portward_free.sharing import Sharing, ShareService, open_snapshot

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).parent / "fixtures" / "share_service.py"
MARKER = "PLAINTEXT-MARKER-c41d"
SECRET = "123-45-6789"


@dataclass
class Service:
    url: str
    folder: Path

    def mail(self, to: str) -> list[dict[str, str]]:
        path = self.folder / "mail.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [m for m in map(json.loads, lines) if m["to"] == to]

    def code(self, to: str, after: int) -> str:
        """The code in the first email to `to` after the first `after` (mail goes out after the response)."""
        import re

        for _ in range(100):
            sent = self.mail(to)
            if len(sent) > after:
                match = re.search(r"\b(\d{6})\b", sent[-1]["body"])
                assert match
                return match.group(1)
            time.sleep(0.05)
        raise AssertionError(f"no mail to {to}")

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(self.url + path, json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    if shutil.which("uv") is None or not (ROOT / "src" / "portward" / "control").is_dir():
        pytest.skip("needs uv and the gateway project (the share service lives in its control app)")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    folder = tmp_path_factory.mktemp("share-service")
    process = subprocess.Popen(["uv", "run", "--project", str(ROOT), "--frozen", "python",
                                str(LAUNCHER), str(port), str(folder), "--generous"],  # more sign-ins than 10/h
                               stdout=subprocess.DEVNULL,
                               stderr=open(folder / "server.log", "w"))
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(url + "/health", timeout=1).close()
                break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError((folder / "server.log").read_text()) from None
                time.sleep(0.05)
        else:
            raise AssertionError("the share service didn't start")
        yield Service(url, folder)
    finally:
        process.terminate()
        process.wait(10)


@dataclass
class Drafts:
    """The sender's mail app: the drafts the tool opened, (to, subject, body)."""
    opened: list[tuple[str, str, str]] = field(default_factory=list)
    works: bool = True

    def open(self, to: str, subject: str, body: str) -> bool:
        self.opened.append((to, subject, body))
        return self.works

    def link(self) -> str:
        return next(word for word in self.opened[-1][2].split() if "/s/" in word)


@pytest.fixture()
def team_db(tmp_path: Path) -> Path:
    path = tmp_path / "team.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, ssn TEXT, region TEXT)")
        db.executemany("INSERT INTO people VALUES (?, ?, ?, ?)",
                       [(1, f"Ada {MARKER}", SECRET, "north"), (2, "Grace", "987-65-4321", "south")])
    db.close()
    return path


@pytest.fixture()
def drafts() -> Drafts:
    return Drafts()


@pytest.fixture()
def sharing(service: Service, drafts: Drafts) -> Sharing:
    return Sharing(ShareService(service.url), drafts)


@pytest.fixture()
def sender() -> str:
    """A sender address of each test's own (the service allows a few codes an hour per address)."""
    return f"sam-{uuid.uuid4().hex[:8]}@acme.example"


@pytest.fixture()
def recipient() -> str:
    """A teammate address of each test's own (the service lets one recipient get a few shares an hour)."""
    return f"teammate-{uuid.uuid4().hex[:8]}@gmail.example"


async def sign_in(h: Harness, service: Service, sender: str) -> None:
    """The AI starts the email confirmation; the user types the emailed code in the terminal (never in the chat)."""
    before = len(service.mail(sender))
    started = await h.call("share_sign_in", email=sender, name="Sam Rivera")
    assert not started.is_error, started.ai.text
    typed = h.type_share_code_in_terminal(service.code(sender, before))
    assert typed.code == 0, typed.out


def receive(service: Service, link: str, recipient: str) -> dict[str, Any]:
    """What the receiver's browser does: ask for a code, open with it, decrypt with the key in the fragment."""
    parts = urlsplit(link)
    share_id = parts.path.rsplit("/", 1)[1]
    before = len(service.mail(recipient))
    status, _ = service.post(f"/v1/shares/{share_id}/code", {})
    assert status == 202
    status, opened = service.post(f"/v1/shares/{share_id}/open", {"code": service.code(recipient, before)})
    assert status == 200, opened
    return open_snapshot(opened["ciphertext"], parts.fragment)


async def test_a_shared_pull_opens_for_the_receiver_and_the_ai_never_sees_the_key(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    async with connected(home, team_db, keychain=MemoryKeychain(), sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT name, region FROM people ORDER BY id",
                              question="Who is where?")
        await sign_in(h, service, sender)
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        assert not shared.is_error, shared.ai.text

        to, subject, body = drafts.opened[-1]
        link = drafts.link()
        assert to == recipient and "Sam Rivera" in subject and link.startswith(f"{service.url}/s/")
        key = urlsplit(link).fragment
        seen_by_ai = shared.ai.text + json.dumps(shared.ai.structured)
        assert key not in seen_by_ai and "/s/" not in seen_by_ai
        assert shared.ai.structured["recipient"] == recipient and shared.ai.structured["draft_opened"] is True

        snapshot = receive(service, link, recipient)
        assert snapshot["columns"] == ["name", "region"]
        assert snapshot["rows"] == [[f"Ada {MARKER}", "north"], ["Grace", "south"]]
        assert snapshot["title"] == "Who is where?"

    stored = b"".join(p.read_bytes() for p in service.folder.glob("control.db*"))
    assert MARKER.encode() not in stored and key.encode() not in stored
    assert MARKER not in (service.folder / "mail.jsonl").read_text()
    assert MARKER not in (service.folder / "server.log").read_text()


async def test_blocked_columns_never_appear_in_a_share(home: Path, team_db: Path, service: Service, drafts: Drafts,
                                                       sender: str, sharing: Sharing, recipient: str) -> None:
    async with connected(home, team_db, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT * FROM people", question="Everyone")
        blocked = await h.call("block", source="shop", table="people", column="ssn")
        assert not blocked.is_error, blocked.ai.text
        await sign_in(h, service, sender)
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        assert not shared.is_error, shared.ai.text
        snapshot = receive(service, drafts.link(), recipient)
    assert "ssn" not in snapshot["columns"] and SECRET not in json.dumps(snapshot)
    assert snapshot["columns"] == ["id", "name", "region"]


async def test_the_sender_confirms_their_email_once_per_device(home: Path, team_db: Path, service: Service,
                                                              sender: str, sharing: Sharing, recipient: str) -> None:
    keychain = MemoryKeychain()
    async with connected(home, team_db, keychain=keychain, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT region FROM people", question="Regions")
        refused = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        assert refused.is_error and "share_sign_in" in refused.ai.text
        await sign_in(h, service, sender)
    codes_to_sender = len(service.mail(sender))
    assert "share-token" in keychain.entries and keychain.entries["share-token"].startswith("pwst_")

    async with connected(home, team_db, name="again", keychain=keychain, sharing=sharing) as h:  # a later run
        pulled = await h.call("query", source="again", sql="SELECT region FROM people", question="Regions")
        for teammate in (recipient, "other@acme.example"):
            shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=teammate)
            assert not shared.is_error, shared.ai.text
    assert len(service.mail(sender)) == codes_to_sender  # no new code: the device's token was reused


async def test_each_share_is_in_the_senders_local_history(home: Path, team_db: Path, service: Service,
                                                          sender: str, sharing: Sharing, recipient: str) -> None:
    async with connected(home, team_db, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT region FROM people", question="Regions")
        await sign_in(h, service, sender)
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        listed = await h.call("shares")
        rows = h.stored("shares")
    assert [(r["recipient"], r["pull_id"], r["id"]) for r in rows] == [
        (recipient, pulled.ai.structured["pull_id"], shared.ai.structured["share_id"])]
    assert rows[0]["created_at"] and rows[0]["expires_at"] > rows[0]["created_at"]
    entry = listed.ai.structured["shares"][0]
    assert entry["recipient"] == recipient and entry["created_at"] == rows[0]["created_at"]
    assert recipient in listed.ai.text


async def test_a_wrong_sender_code_confirms_nothing(home: Path, team_db: Path, service: Service,
                                                    sender: str, sharing: Sharing) -> None:
    keychain = MemoryKeychain()
    async with connected(home, team_db, keychain=keychain, sharing=sharing) as h:
        before = len(service.mail(sender))
        await h.call("share_sign_in", email=sender, name="Sam Rivera")
        code = service.code(sender, before)
        wrong = h.type_share_code_in_terminal(f"{(int(code) + 1) % 1_000_000:06d}")
    assert wrong.code != 0 and "Not confirmed" in wrong.out and "share-token" not in keychain.entries


def test_the_share_service_address_must_be_https_except_on_this_machine() -> None:
    with pytest.raises(ValueError):
        ShareService("http://share.example")
    ShareService("https://share.portward.ai")
    ShareService("http://127.0.0.1:8800")


class Counts:
    """Stands in for the usage collection endpoint (#49): keeps what it is handed."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> bool:
        self.sent.append(payload)
        return True


async def test_a_share_is_counted_in_the_usage_counts_and_nothing_about_it_is_kept_there(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing, recipient: str) -> None:
    counts = Counts()
    async with connected(home, team_db, sharing=sharing, usage_sender=counts,
                         clock=lambda: datetime(2026, 11, 2, 9, tzinfo=UTC)) as h:
        pulled = await h.call("query", source="shop", sql="SELECT region FROM people", question="Regions")
        await sign_in(h, service, sender)
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        assert not shared.is_error, shared.ai.text
        failed = await h.call("share", pull_id="no-such-pull", recipient=recipient)
        assert failed.is_error
        week = h.stored("usage_weeks")
    assert [w["shares"] for w in week] == [1]
    assert recipient not in json.dumps(week) and sender not in json.dumps(week)


async def test_the_share_tools_are_in_the_ai_log_without_the_senders_code(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing, recipient: str) -> None:
    async with connected(home, team_db, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT region FROM people", question="Regions")
        await sign_in(h, service, sender)
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        listed = await h.call("sent_to_ai")
        log = await h.call("sent_to_ai", conversation_id=listed.ai.structured["current"])
    code = service.code(sender, 0)
    responses = {r["tool"]: r for r in log.ai.structured["responses"]}
    assert {"share_sign_in", "share"} <= responses.keys()
    assert responses["share"]["structured"] == shared.ai.structured
    assert code not in json.dumps(log.ai.structured)


async def test_share_sign_in_asks_for_the_code_out_of_band_and_no_ai_tool_takes_it(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing) -> None:
    async with connected(home, team_db, sharing=sharing) as h:
        started = await h.call("share_sign_in", email=sender, name="Sam Rivera")
        tools = await h.ai_tools()
    assert started.ai.structured["code_entry"] == "terminal"
    assert "share-code" in started.ai.text
    for name, tool in tools.items():
        assert "code" not in (tool.input_schema or {}).get("properties", {}), name


async def test_the_code_typed_in_the_panel_confirms_the_email(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing) -> None:
    keychain = MemoryKeychain()
    async with connected(home, team_db, panel=True, keychain=keychain, sharing=sharing) as h:
        before = len(service.mail(sender))
        started = await h.call("share_sign_in", email=sender, name="Sam Rivera")
        ask = await h.form_in_panel(started)  # as Claude Desktop does: it doesn't pass the panel _meta
        saved = await h.call_from_panel("save_secret", request=ask["token"], secret=service.code(sender, before))
    assert started.ai.structured["code_entry"] == "panel"
    assert {**ask, "token": None} == {**started.panel.meta["portward/secret_request"], "token": None}
    assert ask["label"] == "Code" and ask["token"] not in started.ai.text + json.dumps(started.ai.structured)
    assert not saved.is_error, saved.ai.text
    assert "share-token" in keychain.entries


async def test_the_code_asked_for_in_one_process_is_typed_in_the_panel_of_the_other(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing) -> None:
    keychain = MemoryKeychain()  # Claude Desktop: the AI's server process and the panel's, one keychain
    async with connected(home, team_db, panel=True, keychain=keychain, sharing=sharing) as h, \
            Harness.start(home, panel=True, keychain=keychain, sharing=sharing) as panel:
        before = len(service.mail(sender))
        started = await h.call("share_sign_in", email=sender, name="Sam Rivera")
        ask = await panel.form_in_panel(started)
        saved = await panel.call_from_panel("save_secret", request=ask["token"],
                                            secret=service.code(sender, before))
    assert ask["label"] == "Code" and ["Email", sender] in ask["details"]
    assert not saved.is_error, saved.ai.text
    assert "share-token" in keychain.entries


async def test_the_code_typed_in_a_local_page_confirms_the_email(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing) -> None:
    from test_password_page import User, browser

    keychain, user = MemoryKeychain(), User()
    async with connected(home, team_db, keychain=keychain, sharing=sharing, elicitation=user,
                         protocol="legacy") as h:
        before = len(service.mail(sender))
        started = await h.call("share_sign_in", email=sender, name="Sam Rivera")
        status, _, body = await browser(user.url, "POST", {"secret": service.code(sender, before)})
    assert started.ai.structured["code_entry"] == "page"
    assert status == 200 and "Confirmed" in body
    assert "share-token" in keychain.entries


async def test_share_confirm_asks_again_and_says_when_nothing_is_in_progress(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing) -> None:
    async with connected(home, team_db, sharing=sharing) as h:
        nothing = await h.call("share_confirm")
        await h.call("share_sign_in", email=sender, name="Sam Rivera")
        again = await h.call("share_confirm")
    assert nothing.is_error and "share_sign_in" in nothing.ai.text
    assert not again.is_error and again.ai.structured["code_entry"] == "terminal"


@pytest.mark.parametrize("way", ["terminal", "panel", "page"])
async def test_the_senders_code_never_reaches_the_ai_the_log_or_the_logs(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing, way: str, recipient: str,
        caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]) -> None:
    import asyncio
    import logging

    from test_password_page import User, request

    caplog.set_level(logging.DEBUG)
    user = User()
    async with connected(home, team_db, sharing=sharing, panel=way == "panel",
                         elicitation=user if way == "page" else None, protocol="legacy", mode="results") as h:
        before = len(service.mail(sender))
        started = await h.call("share_sign_in", email=sender, name="Sam Rivera")
        code = service.code(sender, before)
        wrong = f"{(int(code) + 1) % 1_000_000:06d}"
        if way == "terminal":
            h.type_share_code_in_terminal(wrong)
            h.type_share_code_in_terminal(code)
        elif way == "panel":
            token = started.panel.meta["portward/secret_request"]["token"]
            await h.call_from_panel("save_secret", request=token, secret=wrong)
            await h.call_from_panel("save_secret", request=token, secret=code)
        else:
            await asyncio.to_thread(request, user.url, "POST", {"secret": wrong})
            await asyncio.to_thread(request, user.url, "POST", {"secret": code})
        pulled = await h.call("query", source="shop", sql="SELECT region FROM people", question="Regions")
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        listed = await h.call("sent_to_ai")
        log = await h.call("sent_to_ai", conversation_id=listed.ai.structured["current"])
    assert not shared.is_error, shared.ai.text
    out, err = capfd.readouterr()
    seen = [json.dumps(log.ai.structured), log.ai.text, caplog.text, out, err,
            *(r.ai.text + json.dumps(r.ai.structured) for r in (started, pulled, shared))]
    for text in seen:
        assert code not in text and wrong not in text


# --- limits (#51) ---------------------------------------------------------------------------------------------------

@pytest.fixture()
def wide_db(tmp_path: Path) -> Path:
    """A table whose full pull seals to more than the share size cap (5 MB)."""
    path = tmp_path / "wide.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        db.executemany("INSERT INTO notes VALUES (?, ?)", [(n, f"{n:06d}" + "x" * 600) for n in range(10_000)])
    db.close()
    return path


async def test_a_table_too_big_to_share_is_refused_before_it_is_uploaded(
        home: Path, wide_db: Path, service: Service, sender: str, sharing: Sharing, drafts: Drafts,
        recipient: str) -> None:
    async with connected(home, wide_db, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT * FROM notes", question="All notes")
        await sign_in(h, service, sender)
        refused = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        assert refused.is_error
        assert "too big to share" in refused.ai.text and "5 MB" in refused.ai.text
        assert "fewer rows" in refused.ai.text and "share_sign_in" not in refused.ai.text
        smaller = await h.call("query", source="shop", sql="SELECT id FROM notes", question="Note ids")
        shared = await h.call("share", pull_id=smaller.ai.structured["pull_id"], recipient=recipient)
        assert not shared.is_error, shared.ai.text
        assert len(h.stored("shares")) == 1
    assert len(drafts.opened) == 1


async def test_the_service_limits_reach_the_user_as_a_clear_message(
        home: Path, team_db: Path, service: Service, sender: str, sharing: Sharing) -> None:
    teammate = f"busy-{uuid.uuid4().hex[:8]}@acme.example"  # the service lets one recipient get 5 shares an hour
    keychain = MemoryKeychain()
    async with connected(home, team_db, keychain=keychain, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT region FROM people", question="Regions")
        await sign_in(h, service, sender)
        for _ in range(5):
            shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=teammate)
            assert not shared.is_error, shared.ai.text
        refused = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=teammate)
    assert refused.is_error
    assert "this recipient has been sent 5 shares in the last hour" in refused.ai.text
    assert "try again in" in refused.ai.text and "share_sign_in" not in refused.ai.text
    assert "share-token" in keychain.entries  # a limit isn't a sign-in problem


async def test_the_share_token_is_rotated_before_it_expires_on_the_server(
        home: Path, team_db: Path, service: Service, sender: str, drafts: Drafts, recipient: str) -> None:
    later = [datetime.now(UTC)]
    sharing = Sharing(ShareService(service.url), drafts, now=lambda: later[0])
    keychain = MemoryKeychain()
    async with connected(home, team_db, keychain=keychain, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT region FROM people", question="Regions")
        await sign_in(h, service, sender)
        first = keychain.entries["share-token"]
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        assert not shared.is_error and keychain.entries["share-token"] == first  # far from expiry: kept

        later[0] += timedelta(days=70)  # the token (90 days on the server) is close to running out
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        assert not shared.is_error, shared.ai.text
        rotated = keychain.entries["share-token"]
        assert rotated != first and rotated.startswith("pwst_")
        assert first not in shared.ai.text and rotated not in shared.ai.text

    request = urllib.request.Request(service.url + "/v1/shares", b"{}", method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {first}"})
    with pytest.raises(urllib.error.HTTPError) as old:
        urllib.request.urlopen(request, timeout=5)
    assert old.value.code == 401  # the old token stopped working when it was swapped


async def test_a_refused_rotation_signs_in_again_out_of_band(
        home: Path, team_db: Path, service: Service, sender: str, drafts: Drafts, recipient: str) -> None:
    later = [datetime.now(UTC)]
    sharing = Sharing(ShareService(service.url), drafts, now=lambda: later[0])
    keychain = MemoryKeychain()
    async with connected(home, team_db, keychain=keychain, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT region FROM people", question="Regions")
        await sign_in(h, service, sender)
        ShareService(service.url).rotate_sender(keychain.entries["share-token"])  # elsewhere: this one is now dead
        later[0] += timedelta(days=70)
        refused = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
        assert refused.is_error and "share_sign_in" in refused.ai.text
        assert "share-token" not in keychain.entries
        before = len(service.mail(sender))
        again = await h.call("share_sign_in", email=sender, name="Sam Rivera")
        assert again.ai.structured["code_entry"] == "terminal"
        typed = h.type_share_code_in_terminal(service.code(sender, before))
        assert typed.code == 0, typed.out
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient)
    assert not shared.is_error, shared.ai.text
