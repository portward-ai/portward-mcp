"""Encrypted sync for Personal (#55), against the real control app on 127.0.0.1.

History, saved pulls, notes and rules go to the account's sync service sealed on this computer: a random data key
per account, kept by the service only wrapped (under each device's own key from its keychain, and under the
recovery key the user is shown once). The service holds ciphertext and version numbers, nothing readable. The
recovery key never reaches the AI or the service: it is shown in a one-time page on this computer (or a terminal)
and typed back the same way (#62)."""

from __future__ import annotations

import base64
import html
import json
import re
import sqlite3
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from harness import Harness, MemoryKeychain
from mcp.types import ElicitResult
from test_account import LAUNCHER, ROOT, Opened, back_to_the_tool, sign_in_and_answer

from portward_free import stand_ins
from portward_free.account import Accounts, AccountService
from portward_free.sync import records
from portward_free.sync.kinds import Place, secret_kind

MARKERS = ("zq-question-marker", "zq_sql_marker", "zq saved marker", "zq-note-marker", "zq_table_marker",
           "zq_column_marker")


@dataclass
class Service:
    url: str
    folder: Path

    def stored_bytes(self) -> bytes:
        """Everything the service keeps on disk: its database file and journals."""
        return b"".join(p.read_bytes() for p in self.folder.iterdir() if p.name.startswith("control.db"))

    def log(self) -> str:
        return (self.folder / "server.log").read_text()

    def move_clock(self, seconds: float) -> None:
        (self.folder / "clock").write_text(str(seconds))


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    import shutil
    import socket
    import subprocess
    import time

    if shutil.which("uv") is None or not (ROOT / "src" / "portward" / "control").is_dir():
        pytest.skip("needs uv and the gateway project (sync lives in its control app)")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    folder = tmp_path_factory.mktemp("sync-service")
    log = open(folder / "server.log", "w")  # uvicorn's access lines go to stdout, the rest to stderr
    process = subprocess.Popen(["uv", "run", "--project", str(ROOT), "--frozen", "python", str(LAUNCHER), str(port),
                                str(folder), "--generous", "--clock", str(folder / "clock"), "--debug-log"],
                               stdout=log, stderr=log)
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
            raise AssertionError("the control app didn't start")
        yield Service(url, folder)
    finally:
        process.terminate()
        process.wait(10)


@pytest.fixture()
def marked_db(tmp_path: Path) -> Path:
    """A user's SQLite file whose table and column names are markers too."""
    path = tmp_path / "marked.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE zq_table_marker (id INTEGER PRIMARY KEY, zq_column_marker TEXT, amount REAL)")
        db.executemany("INSERT INTO zq_table_marker VALUES (?, ?, ?)", [(1, "a", 1.5), (2, "b", 2.5)])
    db.close()
    return path


@dataclass
class Device:
    h: Harness
    keychain: MemoryKeychain
    opened: Opened
    home: Path

    def recovery_key_page(self) -> str:
        """The user reading the one-time page the tool opened in their browser: the browser is handed a file only
        this user can read, which sends it on to the page's address."""
        with urllib.request.urlopen(self.recovery_key_address(), timeout=10) as response:
            return response.read().decode()

    def recovery_key_address(self) -> str:
        (opened,) = [u for u in self.opened.urls if u.startswith("file://")]
        path = Path(urllib.request.url2pathname(urlsplit(opened).path))
        assert path.stat().st_mode & 0o777 == 0o600, oct(path.stat().st_mode)
        found = re.search(r'content="0;url=(http://127\.0\.0\.1:[^"]+)"', path.read_text())
        assert found, path.read_text()
        return html.unescape(found.group(1))


def recovery_key_in(page: str) -> str:
    found = re.search(r'<code[^>]*id="recovery-key"[^>]*>([A-Z2-7-]+)</code>', page)
    assert found, page
    return found.group(1)


@asynccontextmanager
async def device(service: Service, home: Path, email: str, **options: Any) -> AsyncIterator[Device]:
    """One computer with the tool, signed in to `email`'s Personal account (a new account starts a trial).
    `options` go to Harness.start (panel, elicitation)."""
    home.mkdir(parents=True, exist_ok=True)
    keychain, opened = MemoryKeychain(), Opened()
    async with Harness.start(home, keychain=keychain, browser=opened,
                             accounts=Accounts(AccountService(service.url)), **options) as h:
        started = await h.call("account_sign_in")
        assert not started.is_error, started.ai.text
        status, _ = back_to_the_tool(sign_in_and_answer(service, opened.urls[-1], email))
        assert status == 200
        opened.urls.clear()
        yield Device(h, keychain, opened, home)


async def test_the_service_never_stores_or_logs_anything_readable(service: Service, tmp_path: Path,
                                                                  marked_db: Path) -> None:
    async with device(service, tmp_path / "laptop", "mara@gmail.example") as laptop:
        h = laptop.h
        assert not (await h.add_file("books", str(marked_db))).is_error
        pulled = await h.call("query", source="books", question="zq-question-marker: amounts?",
                              sql="SELECT zq_column_marker, amount FROM zq_table_marker "
                                  "WHERE zq_column_marker <> 'zq_sql_marker'")
        assert not pulled.is_error, pulled.ai.text
        pull_id = pulled.ai.structured["pull_id"]
        assert not (await h.call("save_pull", pull_id=pull_id, name="zq saved marker")).is_error
        assert not (await h.call("set_note", source="books", table="zq_table_marker",
                                 note="zq-note-marker means money")).is_error
        assert not (await h.call("block", source="books", table="zq_table_marker",
                                 column="zq_column_marker")).is_error

        turned_on = await h.call("sync_turn_on")
        assert not turned_on.is_error, turned_on.ai.text
        key = recovery_key_in(laptop.recovery_key_page())
        assert turned_on.ai.structured["sent"] == 5, turned_on.ai.text  # the pull, its saved name, note, rule, AI log
        synced = await h.call("sync_now")
        assert not synced.is_error, synced.ai.text
        assert synced.ai.structured["sent"] == 0 and synced.ai.structured["received"] == 0  # nothing new

        seen_by_ai = json.dumps([turned_on.ai.text, turned_on.ai.structured, synced.ai.text, synced.ai.structured])
    stored, log = service.stored_bytes(), service.log()
    assert "/v1/sync/" in log  # the access lines are in the log being checked
    for marker in (*MARKERS, key, key.replace("-", "")):
        assert marker.encode() not in stored, marker
        assert marker not in log, marker
    assert key not in seen_by_ai and key.replace("-", "") not in seen_by_ai


async def fill(h: Harness, db: Path, question: str = "amounts by name") -> str:
    """A source, a pull saved by name, a note and a block, as the user makes them on one computer."""
    assert not (await h.add_file("books", str(db))).is_error
    pulled = await h.call("query", source="books", question=question, sql="SELECT id, amount FROM zq_table_marker")
    assert not pulled.is_error, pulled.ai.text
    pull_id = pulled.ai.structured["pull_id"]
    assert not (await h.call("save_pull", pull_id=pull_id, name="Monthly amounts")).is_error
    assert not (await h.call("set_note", source="books", table="zq_table_marker", note="one row per invoice")).is_error
    assert not (await h.call("block", source="books", table="zq_table_marker", column="zq_column_marker")).is_error
    return pull_id


async def described(h: Harness) -> dict:
    reply = await h.call("describe_source", source="books")
    assert not reply.is_error, reply.ai.text
    (table,) = [t for t in reply.ai.structured["tables"] if t["name"] == "zq_table_marker"]
    return table


async def history_ids(h: Harness) -> set[str]:
    return {p["id"] for p in (await h.call("history")).ai.structured["pulls"]}


async def test_a_new_computer_opens_the_synced_data_with_the_recovery_key(service: Service, tmp_path: Path,
                                                                         marked_db: Path) -> None:
    email = "ines@gmail.example"
    async with device(service, tmp_path / "laptop", email) as laptop:
        pull_id = await fill(laptop.h, marked_db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())

        async with device(service, tmp_path / "desktop", email) as desktop:
            h = desktop.h
            asked = await h.call("sync_turn_on")
            assert not asked.is_error, asked.ai.text
            assert asked.ai.structured["recovery_key_entry"] == "terminal"  # no panel or prompt in this client
            assert "sync-restore" in asked.ai.structured["command"]
            assert (await h.call("sync_now")).is_error  # nothing opens before the recovery key is typed

            wrong = h.run_sync_command("sync-restore", "AAAA-" * 7 + "AAAA")
            assert wrong.code == 1 and "doesn't open" in wrong.out
            typed = h.run_sync_command("sync-restore", key.lower().replace("-", " "))  # as people type it
            assert typed.code == 0, typed.out
            assert key not in typed.out

            synced = await h.call("sync_now")
            assert not synced.is_error, synced.ai.text
            assert synced.ai.structured["received"] == 5
            history = (await h.call("history")).ai.structured["pulls"]
            assert [p["id"] for p in history] == [pull_id] and history[0]["question"] == "amounts by name"
            assert "Monthly amounts" in json.dumps((await h.call("saved_pulls")).ai.structured)

            # the note and the block are on 'books', which this computer doesn't have yet
            assert not (await h.add_file("books", str(marked_db))).is_error
            blocked = {c["name"]: c["blocked"] for c in (await described(h))["columns"]}
            assert blocked["zq_column_marker"] is True  # a block applies as soon as its source is here
            assert not (await h.call("sync_now")).is_error
            assert (await described(h))["note"] == "one row per invoice"

            # and back: the desktop's own pull reaches the laptop
            again = await h.call("query", source="books", question="desktop question",
                                 sql="SELECT count(*) AS n FROM zq_table_marker")
            assert not again.is_error, again.ai.text
            assert (await h.call("sync_now")).ai.structured["sent"] == 2  # the pull and its AI log day
            desktop_ids = await history_ids(h)

        back = await laptop.h.call("sync_now")
        assert back.ai.structured["received"] == 2, back.ai.text
        assert await history_ids(laptop.h) == desktop_ids


class Yes:
    """The user answering yes to the client's prompt (removing a block needs it, #39)."""

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content={"remove": True})


@asynccontextmanager
async def two_computers_in_step(service: Service, tmp_path: Path, db: Path,
                                email: str) -> AsyncIterator[tuple[Device, Device]]:
    """The laptop turned sync on; the desktop opened it with the recovery key and has the source too."""
    async with device(service, tmp_path / "laptop", email, elicitation=Yes()) as laptop:
        await fill(laptop.h, db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())
        async with device(service, tmp_path / "desktop", email) as desktop:
            await desktop.h.call("sync_turn_on")
            assert desktop.h.run_sync_command("sync-restore", key).code == 0
            assert not (await desktop.h.add_file("books", str(db))).is_error
            assert (await desktop.h.call("sync_now")).ai.structured["received"] == 5
            yield laptop, desktop


async def test_edits_made_on_two_computers_at_once_are_both_kept(service: Service, tmp_path: Path,
                                                                marked_db: Path) -> None:
    async with two_computers_in_step(service, tmp_path, marked_db, "olu@gmail.example") as (laptop, desktop):
        await laptop.h.call("set_note", source="books", table="zq_table_marker", note="amounts are in euros")
        await desktop.h.call("set_note", source="books", table="zq_table_marker", note="refunds are negative")
        first = await laptop.h.call("query", source="books", question="laptop", sql="SELECT id FROM zq_table_marker")
        second = await desktop.h.call("query", source="books", question="desktop",
                                      sql="SELECT amount FROM zq_table_marker")
        await laptop.h.call("save_pull", pull_id=first.ai.structured["pull_id"], name="Latest")
        await desktop.h.call("save_pull", pull_id=second.ai.structured["pull_id"], name="Latest")

        assert not (await laptop.h.call("sync_now")).is_error
        merged = await desktop.h.call("sync_now")  # the desktop finds the laptop's edits made meanwhile
        assert not merged.is_error and merged.ai.structured["merged"] >= 1, merged.ai.text
        assert not (await laptop.h.call("sync_now")).is_error

        for h in (laptop.h, desktop.h):
            note = (await described(h))["note"]
            assert "amounts are in euros" in note and "refunds are negative" in note  # neither edit is lost
            assert {first.ai.structured["pull_id"], second.ai.structured["pull_id"]} <= await history_ids(h)
        names = [json.dumps((await h.call("saved_pulls")).ai.structured) for h in (laptop.h, desktop.h)]
        assert names[0] == names[1]  # both computers settle on the same pull for "Latest"


async def test_a_block_removed_on_one_computer_stays_on_the_other_until_the_user_says_so_there(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    async with two_computers_in_step(service, tmp_path, marked_db, "kofi@gmail.example") as (laptop, desktop):
        removed = await laptop.h.call("unblock", source="books", table="zq_table_marker", column="zq_column_marker")
        assert not removed.is_error, removed.ai.text
        assert not (await laptop.h.call("sync_now")).is_error

        synced = await desktop.h.call("sync_now")
        assert synced.ai.structured["held_rules"] == 1 and "until the user unblocks" in synced.ai.text
        columns = {c["name"]: c["blocked"] for c in (await described(desktop.h))["columns"]}
        assert columns["zq_column_marker"] is True  # still blocked here: the user didn't say yes here
        assert (await desktop.h.call("sync_status")).ai.structured["held_rules"] == 1

        assert not (await desktop.h.call("sync_now")).is_error
        assert not (await laptop.h.call("sync_now")).is_error
        columns = {c["name"]: c["blocked"] for c in (await described(laptop.h))["columns"]}
        assert columns["zq_column_marker"] is False  # the desktop's hold doesn't block the laptop again


async def test_sync_stops_when_personal_ends_and_everything_here_stays(service: Service, tmp_path: Path,
                                                                       marked_db: Path) -> None:
    async with device(service, tmp_path / "laptop", "wen@gmail.example") as laptop:
        h = laptop.h
        pull_id = await fill(h, marked_db)
        assert not (await h.call("sync_turn_on")).is_error
        try:
            service.move_clock(15 * 86_400)  # past the 14-day trial, and nobody subscribed
            await h.call("query", source="books", question="after the trial", sql="SELECT id FROM zq_table_marker")
            stopped = await h.call("sync_now")
            assert stopped.is_error and "Personal has ended" in stopped.ai.text, stopped.ai.text
            status = (await h.call("sync_status")).ai.structured
            assert status["stopped"] == "Personal has ended"
            assert pull_id in await history_ids(h) and len(await history_ids(h)) == 2  # nothing here is touched
            assert (await described(h))["note"] == "one row per invoice"
        finally:
            service.move_clock(0)
        resumed = await h.call("sync_now")  # subscribing again (here: back inside the trial) turns it back on
        assert not resumed.is_error and resumed.ai.structured["sent"] == 2, resumed.ai.text
        assert (await h.call("sync_status")).ai.structured["stopped"] is None


async def test_the_recovery_key_typed_in_the_panel_never_reaches_the_ai(service: Service, tmp_path: Path,
                                                                        marked_db: Path) -> None:
    email = "pia@gmail.example"
    async with device(service, tmp_path / "laptop", email) as laptop:
        await fill(laptop.h, marked_db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())
        assert laptop.opened.urls and all(key not in u for u in laptop.opened.urls)
    async with device(service, tmp_path / "desktop", email, panel=True) as desktop:
        asked = await desktop.h.call("sync_turn_on")
        assert asked.ai.structured["recovery_key_entry"] == "panel", asked.ai.text
        token = (await desktop.h.form_in_panel(asked))["token"]  # Claude Desktop doesn't pass the panel _meta
        assert token not in asked.ai.text + json.dumps(asked.ai.structured)
        saved = await desktop.h.call_from_panel("save_secret", request=token, secret=key)
        assert not saved.is_error, saved.ai.text
        synced = await desktop.h.call("sync_now")
        assert synced.ai.structured["received"] == 5
        log = await desktop.h.call("sent_to_ai")
        conversation = log.ai.structured["conversations"][0]["id"]
        sent = await desktop.h.call("sent_to_ai", conversation_id=conversation)
        assert key not in json.dumps(sent.ai.structured) and key not in sent.ai.text


async def test_the_recovery_key_asked_for_in_one_process_is_typed_in_the_panel_of_the_other(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    email = "noor@gmail.example"
    async with device(service, tmp_path / "laptop", email) as laptop:
        await fill(laptop.h, marked_db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())
    async with device(service, tmp_path / "desktop", email, panel=True) as desktop:
        async with Harness.start(desktop.home, panel=True, keychain=desktop.keychain,
                                 accounts=Accounts(AccountService(service.url))) as panel:  # Claude Desktop's second
            asked = await desktop.h.call("sync_turn_on")
            token = (await panel.form_in_panel(asked))["token"]
            saved = await panel.call_from_panel("save_secret", request=token, secret=key)
        assert not saved.is_error, saved.ai.text
        assert (await desktop.h.call("sync_now")).ai.structured["received"] == 5


async def test_what_the_service_changes_or_makes_up_doesnt_open_and_nothing_here_is_lost(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    async with device(service, tmp_path / "laptop", "ravi@gmail.example") as laptop:
        h = laptop.h
        await fill(h, marked_db)
        assert not (await h.call("sync_turn_on")).is_error
        before = await history_ids(h), await described(h)
        account = json.loads(next(r["value"] for r in h.stored("settings") if r["key"] == "account"))["account_id"]
        db = sqlite3.connect(service.folder / "control.db")  # the service, turned against its user
        with db:
            rows = db.execute("SELECT id, ciphertext FROM sync_records WHERE account_id = ? ORDER BY id",
                              (account,)).fetchall()
            shifted = [r[1] for r in rows[1:] + rows[:1]]  # every record gets another one's ciphertext
            for (record_id, _), ciphertext in zip(rows, shifted, strict=True):
                db.execute("UPDATE sync_records SET ciphertext = ?, version = version + 1, seq = seq + 100 "
                           "WHERE account_id = ? AND id = ?", (ciphertext, account, record_id))
            db.execute("UPDATE sync_accounts SET seq = seq + 100 WHERE account_id = ?", (account,))
        tampered = await h.call("sync_now")
        assert not tampered.is_error and tampered.ai.structured["unopenable"] == 5, tampered.ai.text
        assert (await history_ids(h), await described(h)) == before

        with db:  # and hands over a key of its own
            db.execute("UPDATE sync_device_keys SET wrapped = ? WHERE account_id = ?", (bytes(60), account))
        db.close()
        refused = await h.call("sync_now")
        assert refused.is_error and "recovery key" in refused.ai.text


async def test_a_service_restored_from_an_older_backup_gets_this_computers_records_again(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    async with device(service, tmp_path / "laptop", "lin@gmail.example") as laptop:
        h = laptop.h
        await fill(h, marked_db)
        assert not (await h.call("sync_turn_on")).is_error
        account = json.loads(next(r["value"] for r in h.stored("settings") if r["key"] == "account"))["account_id"]
        db = sqlite3.connect(service.folder / "control.db")
        with db:  # the service rolled back to before this computer's first sync
            db.execute("DELETE FROM sync_records WHERE account_id = ?", (account,))
            db.execute("UPDATE sync_accounts SET seq = 0 WHERE account_id = ?", (account,))
        resent = await h.call("sync_now")
        assert not resent.is_error and resent.ai.structured["sent"] == 5, resent.ai.text
        count = db.execute("SELECT count(*) FROM sync_records WHERE account_id = ?", (account,)).fetchone()[0]
        db.close()
        assert count == 5


async def test_a_record_too_big_for_the_service_stays_here_and_the_rest_still_sync(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    async with device(service, tmp_path / "laptop", "tom@gmail.example") as laptop:
        h = laptop.h
        await fill(h, marked_db)
        huge = await h.call("query", source="books", question="huge",
                            sql=f"SELECT id FROM zq_table_marker WHERE '{'x' * 300_000}' <> ''")
        assert not huge.is_error, huge.ai.text[:200]
        turned_on = await h.call("sync_turn_on")
        assert not turned_on.is_error, turned_on.ai.text
        assert turned_on.ai.structured["sent"] == 5 and turned_on.ai.structured["too_big"] == 1
        assert "too big" in turned_on.ai.text


async def test_a_new_recovery_key_replaces_the_old_one_in_a_browser_or_a_terminal(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    email = "ana@gmail.example"
    async with device(service, tmp_path / "laptop", email) as laptop:
        await fill(laptop.h, marked_db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        old = recovery_key_in(laptop.recovery_key_page())
        laptop.opened.works = False  # no browser opens from here this time
        failed = await laptop.h.call("sync_new_recovery_key")
        assert failed.is_error and "sync-recovery-key" in failed.ai.text
        captured = laptop.h.run_sync_command("sync-recovery-key", "NEW", at_terminal=False)
        assert captured.code == 2 and not re.search(r"[A-Z2-7]{4}-[A-Z2-7]{4}-", captured.out)  # e.g. the AI's shell
        assert laptop.h.run_sync_command("sync-recovery-key", "no").code == 1
        printed = laptop.h.run_sync_command("sync-recovery-key", "NEW")
        assert printed.code == 0
        new = re.search(r"\b([A-Z2-7]{4}(?:-[A-Z2-7]{4}){7})\b", printed.out).group(1)
        assert new != old
    async with device(service, tmp_path / "desktop", email) as desktop:
        await desktop.h.call("sync_turn_on")
        assert desktop.h.run_sync_command("sync-restore", old).code == 1  # the old key no longer opens anything
        assert desktop.h.run_sync_command("sync-restore", new).code == 0
        assert (await desktop.h.call("sync_now")).ai.structured["received"] == 5


async def test_a_new_recovery_key_rotates_the_data_key_and_the_other_computer_moves_over(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    """Replacing the recovery key makes a new data key: what the service held under the old one is gone (an old
    backup and the old recovery key open nothing synced since), and the desktop moves to the new key by itself
    on its next sync, without the user typing anything there."""
    async with two_computers_in_step(service, tmp_path, marked_db, "rui@gmail.example") as (laptop, desktop):
        old_page = laptop.opened.urls[:]
        before = service.stored_bytes()
        laptop.opened.urls.clear()
        replaced = await laptop.h.call("sync_new_recovery_key")
        assert not replaced.is_error, replaced.ai.text
        assert laptop.opened.urls != old_page
        new = recovery_key_in(laptop.recovery_key_page())

        moved = await desktop.h.call("sync_now")
        assert not moved.is_error, moved.ai.text
        again = await desktop.h.call("query", source="books", question="after the new key",
                                     sql="SELECT count(*) AS n FROM zq_table_marker")
        assert not again.is_error
        assert (await desktop.h.call("sync_now")).ai.structured["sent"] >= 1
        assert (await laptop.h.call("sync_now")).ai.structured["received"] >= 1
        assert await history_ids(laptop.h) == await history_ids(desktop.h)
    after = service.stored_bytes()
    assert after != before
    async with device(service, tmp_path / "third", "rui@gmail.example") as third:
        await third.h.call("sync_turn_on")
        assert third.h.run_sync_command("sync-restore", new).code == 0
        got = await third.h.call("sync_now")
        assert not got.is_error and got.ai.structured["received"] >= 5, got.ai.text


async def test_the_recovery_key_pages_address_never_reaches_the_process_list_and_opens_once(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    """The browser is started with a file's path, not the page's secret address (which any process on the computer
    could read in its arguments); the file is readable by this user only, and the page answers one viewer."""
    async with device(service, tmp_path / "laptop", "kai@gmail.example") as laptop:
        await fill(laptop.h, marked_db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        address = laptop.recovery_key_address()
        assert all(address not in u and "127.0.0.1" not in u for u in laptop.opened.urls)
        assert recovery_key_in(laptop.recovery_key_page())
        with pytest.raises(OSError):
            urllib.request.urlopen(address, timeout=5).read()  # a second viewer gets nothing


# --- profiles and where they're applied (#97) -------------------------------------------------------------------------
class Confirm:
    """The user answering yes to the client's prompt (applying a profile that shows more asks first)."""

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content={"confirm": True})


async def visible_columns(h: Harness) -> dict[str, list[str]]:
    listed = await h.call("list_sources")
    (entry,) = [s for s in listed.ai.structured["sources"] if s["name"] == "books"]
    return {t["name"]: sorted(t["columns"]) for t in entry["tables"]}


async def profiled(h: Harness, db: Path) -> None:
    """'books' with one profile granting one column, applied."""
    assert not (await h.add_file("books", str(db))).is_error
    made = await h.call("create_profile", name="zq profile marker", description="zq description marker",
                        grants=["zq_table_marker.amount"])
    assert not made.is_error, made.ai.text
    applied = await h.call("apply_profile", profile="zq profile marker", source="books")
    assert not applied.is_error, applied.ai.text


async def test_a_profile_made_on_one_computer_applies_on_another_after_sync(service: Service, tmp_path: Path,
                                                                           marked_db: Path) -> None:
    email = "zara@gmail.example"
    async with device(service, tmp_path / "laptop", email, elicitation=Confirm()) as laptop:
        h = laptop.h
        await profiled(h, marked_db)
        assert not (await h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())

        async with device(service, tmp_path / "desktop", email, elicitation=Confirm()) as desktop:
            d = desktop.h
            await d.call("sync_turn_on")
            assert d.run_sync_command("sync-restore", key).code == 0
            assert not (await d.add_file("books", str(marked_db))).is_error
            assert not (await d.call("sync_now")).is_error
            listed = (await d.call("list_profiles")).ai.structured["profiles"]
            assert [(p["name"], p["version"], p["applied_to"]) for p in listed] == [("zq profile marker", 1,
                                                                                     ["books"])]
            assert await visible_columns(d) == {"zq_table_marker": ["amount"]}
            pulled = await d.call("query", source="books", question="all", sql="SELECT * FROM zq_table_marker")
            assert not pulled.is_error and pulled.ai.structured["columns"] == ["amount"], pulled.ai.text

            # a new version saved on the desktop reaches the laptop and applies there as it is
            changed = await d.call("update_profile", profile="zq profile marker", add=["zq_table_marker.id"])
            assert not changed.is_error, changed.ai.text
            assert not (await d.call("sync_now")).is_error
        assert not (await h.call("sync_now")).is_error
        assert await visible_columns(h) == {"zq_table_marker": ["amount", "id"]}
        (profile,) = (await h.call("list_profiles")).ai.structured["profiles"]
        assert profile["version"] == 2
    stored, log = service.stored_bytes(), service.log()
    for marker in ("zq profile marker", "zq description marker", "zq_table_marker", "zq_column_marker"):
        assert marker.encode() not in stored and marker not in log, marker


@asynccontextmanager
async def two_profiled_computers(service: Service, tmp_path: Path, db: Path,
                                 email: str) -> AsyncIterator[tuple[Device, Device]]:
    """The laptop made and applied a profile and turned sync on; the desktop restored and has it too."""
    async with device(service, tmp_path / "laptop", email, elicitation=Confirm()) as laptop:
        await profiled(laptop.h, db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())
        async with device(service, tmp_path / "desktop", email, elicitation=Confirm()) as desktop:
            await desktop.h.call("sync_turn_on")
            assert desktop.h.run_sync_command("sync-restore", key).code == 0
            assert not (await desktop.h.add_file("books", str(db))).is_error
            assert not (await desktop.h.call("sync_now")).is_error
            yield laptop, desktop


async def test_switching_back_to_block_rules_on_one_computer_is_held_on_the_other(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    async with two_profiled_computers(service, tmp_path, marked_db, "ade@gmail.example") as (laptop, desktop):
        back = await laptop.h.call("use_block_rules", source="books")
        assert not back.is_error, back.ai.text
        assert not (await laptop.h.call("sync_now")).is_error

        synced = await desktop.h.call("sync_now")
        assert synced.ai.structured["held_switches"] == 1, synced.ai.text
        assert "until the user switches it back here" in synced.ai.text
        assert await visible_columns(desktop.h) == {}  # still on profiles here, with none applied: nothing shows


async def test_a_profile_saved_on_two_computers_at_once_keeps_both_versions(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    async with two_profiled_computers(service, tmp_path, marked_db, "eli@gmail.example") as (laptop, desktop):
        assert not (await laptop.h.call("update_profile", profile="zq profile marker",
                                        add=["zq_table_marker.id"])).is_error
        assert not (await desktop.h.call("update_profile", profile="zq profile marker",
                                         add=["zq_table_marker.zq_column_marker"])).is_error
        assert not (await laptop.h.call("sync_now")).is_error
        merged = await desktop.h.call("sync_now")
        assert merged.ai.structured["profile_copies"] == 1, merged.ai.text
        assert not (await desktop.h.call("sync_now")).is_error  # the copy is new here: it goes on the next run
        assert not (await laptop.h.call("sync_now")).is_error

        for h in (laptop.h, desktop.h):
            profiles = {p["name"]: (sorted(g["name"] for g in p["grants"]), p["applied_to"])
                        for p in (await h.call("list_profiles")).ai.structured["profiles"]}
            assert profiles == {
                "zq profile marker": (["zq_table_marker.amount", "zq_table_marker.id"], ["books"]),
                "zq profile marker (copy from this computer)": (
                    ["zq_table_marker.amount", "zq_table_marker.zq_column_marker"], []),
            }
            assert await visible_columns(h) == {"zq_table_marker": ["amount", "id"]}  # the copy shows nothing


class SayYes:
    """The user answering yes to every prompt, whatever it asks (applying a profile, approving a draft)."""

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        properties = params.requested_schema.get("properties", {})
        return ElicitResult(action="accept", content={k: True for k, v in properties.items()
                                                      if v.get("type") == "boolean"})


async def test_approved_definitions_sync_and_drafts_stay_on_this_computer(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    """#95 x #97: a profile's approved context (meanings and named definitions) syncs with it; a draft the user
    hasn't approved never leaves this computer."""
    email = "ola@gmail.example"
    async with device(service, tmp_path / "laptop", email, elicitation=SayYes()) as laptop:
        h = laptop.h
        await profiled(h, marked_db)
        approved = await h.call("draft_definition", profile="zq profile marker", term="zq approved term",
                                text="zq-approved-definition: every invoice")
        (approved_id,) = approved.ai.structured["drafts"]
        assert not (await h.call("approve_context", draft_id=approved_id)).is_error
        waiting = await h.call("draft_definition", profile="zq profile marker", term="zq draft term",
                               text="zq-draft-definition: not approved")
        assert not waiting.is_error, waiting.ai.text
        assert not (await h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())

        async with device(service, tmp_path / "desktop", email) as desktop:
            await desktop.h.call("sync_turn_on")
            assert desktop.h.run_sync_command("sync-restore", key).code == 0
            assert not (await desktop.h.call("sync_now")).is_error
            (profile,) = (await desktop.h.call("list_profiles")).ai.structured["profiles"]
            drafts = (await desktop.h.call("context_drafts")).ai.structured["drafts"]
    assert profile["context"]["definitions"] == [{"term": "zq approved term",
                                                  "text": "zq-approved-definition: every invoice"}]
    assert drafts == []
    stored = service.stored_bytes()
    assert b"zq-draft-definition" not in stored and b"zq-approved-definition" not in stored  # sealed, or never sent


async def test_approved_links_sync_and_assumptions_and_link_drafts_never_leave_this_computer(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    """#96 x #97: an approved link syncs with its profile; the AI's assumptions and a link draft the user hasn't
    approved stay on the computer they were made on, and never reach the service, even sealed."""
    email = "noor96@gmail.example"
    async with device(service, tmp_path / "laptop", email, elicitation=SayYes()) as laptop:
        h = laptop.h
        await profiled(h, marked_db)
        assert not (await h.call("update_profile", profile="zq profile marker", add=["zq_other.ref"])).is_error
        drafted = await h.call("draft_link", profile="zq profile marker", column="zq_table_marker.amount",
                               other_column="zq_other.ref", text="zq-approved-link: same amounts")
        assert not drafted.is_error, drafted.ai.text
        assert not (await h.call("approve_context", draft_id=drafted.ai.structured["drafts"][0])).is_error
        waiting = await h.call("draft_link", profile="zq profile marker", column="zq_other.ref",
                               other_column="zq_table_marker.amount", text="zq-link-draft: not approved")
        assert not waiting.is_error, waiting.ai.text
        pulled = await h.call("query", source="books", question="amounts", sql="SELECT amount FROM zq_table_marker",
                              assumptions=["zq-assumption: amounts are in euros"])
        assert not pulled.is_error, pulled.ai.text
        assert not (await h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())

        async with device(service, tmp_path / "desktop", email) as desktop:
            await desktop.h.call("sync_turn_on")
            assert desktop.h.run_sync_command("sync-restore", key).code == 0
            assert not (await desktop.h.call("sync_now")).is_error
            (profile,) = (await desktop.h.call("list_profiles")).ai.structured["profiles"]
            drafts = (await desktop.h.call("context_drafts")).ai.structured["drafts"]
            assumed = (await desktop.h.call("assumptions")).ai.structured["assumptions"]
    assert profile["context"]["links"] == [{"columns": ["zq_table_marker.amount", "zq_other.ref"],
                                            "text": "zq-approved-link: same amounts"}]
    assert drafts == [] and assumed == []
    stored = service.stored_bytes()
    for marker in (b"zq-approved-link", b"zq-link-draft", b"zq-assumption"):
        assert marker not in stored, marker  # sealed, or never sent


SECRET_ACCOUNT = "test-synced-secret"


def keychain_secret(place: Place) -> bytes | None:
    stored = place.keychain.get(SECRET_ACCOUNT) if place.keychain is not None else None
    return base64.b64decode(stored) if stored else None


def keep_secret(place: Place, raw: bytes) -> None:
    assert place.keychain is not None
    place.keychain.set(SECRET_ACCOUNT, base64.b64encode(raw).decode())


@pytest.fixture()
def synced_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret kept in each computer's keychain, synced through the seam the anonymising key uses (#93)."""
    monkeypatch.setitem(records.REGISTERED, "test_secret", secret_kind("test_secret", keychain_secret, keep_secret))


async def test_recovery_restores_profiles_context_and_the_key_after_the_recovery_key_is_replaced(
        service: Service, tmp_path: Path, marked_db: Path, synced_secret: None) -> None:
    email = "ira@gmail.example"
    laptops_key, desktops_key = b"L" * 32, b"D" * 32
    async with device(service, tmp_path / "laptop", email, elicitation=Confirm()) as laptop:
        h = laptop.h
        laptop.keychain.set(SECRET_ACCOUNT, base64.b64encode(laptops_key).decode())
        assert not (await h.add_file("books", str(marked_db))).is_error
        assert not (await h.call("block", source="books", table="zq_table_marker", column="zq_column_marker")).is_error
        assert not (await h.call("set_note", source="books", table="zq_table_marker",
                                 note="zq-context-marker: one row per invoice")).is_error
        started = await h.call("start_profile_from_block_rules", source="books")
        assert not started.is_error, started.ai.text
        context = started.ai.structured["profile"]["context"]
        assert context == {"meanings": [{"about": "zq_table_marker", "meaning": "zq-context-marker: one row per invoice"}],
                           "definitions": [], "links": []}
        assert not (await h.call("sync_turn_on")).is_error
        old = recovery_key_in(laptop.recovery_key_page())

        async with device(service, tmp_path / "desktop", email) as desktop:
            desktop.keychain.set(SECRET_ACCOUNT, base64.b64encode(desktops_key).decode())  # made before syncing
            await desktop.h.call("sync_turn_on")
            assert desktop.h.run_sync_command("sync-restore", old).code == 0
            assert not (await desktop.h.call("sync_now")).is_error
            assert keychain_secret(Place(None, desktop.keychain)) == laptops_key  # the account's key wins

            laptop.opened.urls.clear()
            assert not (await h.call("sync_new_recovery_key")).is_error
            new = recovery_key_in(laptop.recovery_key_page())
            assert not (await desktop.h.call("sync_now")).is_error  # moves over to the new data key
            assert keychain_secret(Place(None, desktop.keychain)) == laptops_key

    async with device(service, tmp_path / "third", email) as third:
        t = third.h
        await t.call("sync_turn_on")
        assert t.run_sync_command("sync-restore", new).code == 0
        assert not (await t.add_file("books", str(marked_db))).is_error
        assert not (await t.call("sync_now")).is_error
        (profile,) = (await t.call("list_profiles")).ai.structured["profiles"]
        assert profile["context"] == context and profile["applied_to"] == ["books"]
        assert await visible_columns(t) == {"zq_table_marker": ["amount", "id"]}
        assert keychain_secret(Place(None, third.keychain)) == laptops_key  # tokens made from it match
    stored = service.stored_bytes()
    for marker in (b"zq-context-marker", base64.b64encode(laptops_key), base64.b64encode(desktops_key)):
        assert marker not in stored, marker


async def test_the_ai_log_summary_syncs_between_computers_as_ciphertext(service: Service, tmp_path: Path,
                                                                         marked_db: Path) -> None:
    """#98: every computer's AI log summary reaches the others, so a proof report made on any of them is complete;
    the service sees none of its table or column names."""
    async with two_computers_in_step(service, tmp_path, marked_db, "ada@gmail.example") as (laptop, desktop):
        on_desktop = await desktop.h.call("query", source="books", question="desktop read",
                                          sql="SELECT amount FROM zq_table_marker")
        assert not on_desktop.is_error, on_desktop.ai.text
        assert not (await desktop.h.call("sync_now")).is_error
        assert not (await laptop.h.call("sync_now")).is_error
        logs = [(await h.call("ai_log")).ai.structured["entries"] for h in (laptop.h, desktop.h)]
        again = [(await h.call("sync_now")).ai.structured for h in (laptop.h, desktop.h)]
    assert logs[0] == logs[1] and len(logs[0]) == 2  # fill's read on the laptop, and the desktop's
    assert {c["column"] for e in logs[0] for c in e["columns"]} == {"id", "amount"}
    assert all(a["sent"] == 0 and a["received"] == 0 for a in again)  # settled: nothing goes back and forth
    stored = service.stored_bytes()
    for marker in ("zq_table_marker", "zq_column_marker"):
        assert marker.encode() not in stored, marker


async def test_the_context_an_answer_relied_on_syncs_with_its_ai_log_entry_as_ciphertext(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    """#125: the AI log entry names the approved context the answer relied on, with versions, on every computer;
    the service sees neither the context nor what it is about."""
    email = "zelda@gmail.example"
    async with device(service, tmp_path / "laptop", email, elicitation=Confirm()) as laptop:
        h = laptop.h
        assert not (await h.add_file("books", str(marked_db))).is_error
        assert not (await h.call("set_note", source="books", table="zq_table_marker",
                                 note="zq-context-marker: one row per invoice")).is_error
        started = await h.call("start_profile_from_block_rules", source="books")
        assert not started.is_error, started.ai.text
        name, version = started.ai.structured["profile"]["name"], started.ai.structured["profile"]["version"]
        pulled = await h.call("query", source="books", question="?", sql="SELECT amount FROM zq_table_marker")
        assert not pulled.is_error, pulled.ai.text
        assert not (await h.call("sync_turn_on")).is_error
        key = recovery_key_in(laptop.recovery_key_page())
        async with device(service, tmp_path / "desktop", email) as desktop:
            await desktop.h.call("sync_turn_on")
            assert desktop.h.run_sync_command("sync-restore", key).code == 0
            assert not (await desktop.h.call("sync_now")).is_error
            logs = [(await d.h.call("ai_log")).ai.structured["entries"] for d in (laptop, desktop)]
    assert logs[0] == logs[1] and len(logs[0]) == 1
    assert logs[1][0]["context"] == [{"kind": "meaning", "about": "zq_table_marker", "profile": name,
                                       "version": version}]
    stored = service.stored_bytes()
    for marker in ("zq-context-marker", "zq_table_marker"):
        assert marker.encode() not in stored, marker


async def stand_ins_seen(h: Harness) -> list[str]:
    pulled = await h.call("query", source="books", question="markers as stand-ins",
                          sql="SELECT zq_column_marker FROM zq_table_marker ORDER BY id")
    assert not pulled.is_error, pulled.ai.text
    seen = [row[0] for row in pulled.ai.structured["rows"]]
    assert all(stand_ins.PATTERN.fullmatch(v) for v in seen), seen
    return seen


async def test_two_computers_give_the_same_stand_ins_after_sync_and_after_a_new_recovery_key(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    """#93 x #97: the anonymising key syncs sealed (the account's copy wins over one made before syncing), so the
    same value gets the same stand-in on every computer, and a new recovery key doesn't change it."""
    email = "uma@gmail.example"
    async with device(service, tmp_path / "laptop", email, elicitation=Confirm()) as laptop:
        h = laptop.h
        assert not (await h.add_file("books", str(marked_db))).is_error
        made = await h.call("create_profile", name="zq profile marker", description="stand-ins",
                            grants=["zq_table_marker.id"], anonymised=["zq_table_marker.zq_column_marker"])
        assert not made.is_error, made.ai.text
        assert not (await h.call("apply_profile", profile="zq profile marker", source="books")).is_error
        on_laptop = await stand_ins_seen(h)
        assert not (await h.call("sync_turn_on")).is_error
        old = recovery_key_in(laptop.recovery_key_page())

        async with device(service, tmp_path / "desktop", email) as desktop:
            stand_ins.account_key(desktop.keychain)  # a key made here before syncing loses to the account's
            await desktop.h.call("sync_turn_on")
            assert desktop.h.run_sync_command("sync-restore", old).code == 0
            assert not (await desktop.h.add_file("books", str(marked_db))).is_error
            assert not (await desktop.h.call("sync_now")).is_error
            assert await stand_ins_seen(desktop.h) == on_laptop

            laptop.opened.urls.clear()
            assert not (await h.call("sync_new_recovery_key")).is_error
            new = recovery_key_in(laptop.recovery_key_page())
            assert not (await desktop.h.call("sync_now")).is_error
            assert await stand_ins_seen(desktop.h) == on_laptop

    async with device(service, tmp_path / "third", email) as third:
        await third.h.call("sync_turn_on")
        assert third.h.run_sync_command("sync-restore", new).code == 0
        assert not (await third.h.add_file("books", str(marked_db))).is_error
        assert not (await third.h.call("sync_now")).is_error
        assert await stand_ins_seen(third.h) == on_laptop
    key = stand_ins.key_for_sync(laptop.keychain)
    assert key is not None and base64.b64encode(key) not in service.stored_bytes()
