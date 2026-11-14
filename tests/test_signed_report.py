"""The signed proof report (#135).

Each computer has an Ed25519 key in its keychain, its public half registered to the account. Every AI log entry a
computer writes is chained to the one it wrote before. The proof report it exports is signed: a signature file
beside the PDF and CSV holds the files' hashes, the state of the chain and the signature. `portward-free verify`
checks a report against the keys registered to the account (or a public key someone was given). The report says
what the signature proves, and nothing more: that it came from that computer unedited.

Driven through the MCP tools and the command line, against the in-memory account service of test_profiles."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any

from harness import Harness, MemoryKeychain
from portward_free import cli
from portward_free.account import TOKEN_ACCOUNT, Accounts, AccountService
from portward_free.paths import database_path
from portward_free.proof import AiRead, ShownColumn, UsedProfile
from portward_free.proof_report import NOTE, export_report, proof_report
from portward_free.signed_report import KEY_ACCOUNT, key_id, public_key_text, public_raw, signing_key
from portward_free.storage import LocalStore
from pypdf import PdfReader
from test_account import Opened, Service, back_to_the_tool, service, sign_in_and_answer  # noqa: F401
from test_profiles import PlanAccounts, tool

DAY = "2026-11-02"
PROVES = "The signature proves only that this report came from that computer and was not edited after it was made."


def a_read(rows: int, clock: str = "09:30:00") -> AiRead:
    return AiRead(app="Claude", route="desktop", connection="people", profiles=(UsedProfile("Support", 2),),
                  columns=(ShownColumn("customers", "name", "full", ("Support",)),), row_count=rows,
                  result_hash="ab" * 32, created_at=f"{DAY}T{clock}.000001+00:00")


def record(h: Harness, *reads: AiRead) -> None:
    """Reads as this computer writes them (chained), the way pulls.log_read records them."""
    store = LocalStore.open(database_path(h.home))
    for r in reads:
        store.reads.record(r)
    store.close()


async def export(h: Harness) -> dict[str, Any]:
    exported = await h.call("export_proof_report", start=DAY, end=DAY)
    assert not exported.is_error, exported.ai.text
    return exported.ai.structured


def verify(h: Harness, path: Path | str, accounts: PlanAccounts, *extra: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(["verify", str(path), *extra], home=h.home, keychain=h.keychain, out=out,
                    account_service=accounts, is_terminal=lambda: False)
    return code, out.getvalue()


def pdf_text(path: Path) -> str:
    return " ".join(" ".join(page.extract_text().split()) for page in PdfReader(path).pages)


async def test_a_signed_report_verifies_against_the_accounts_registered_key(home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        record(h, a_read(3), a_read(4, "10:00:00"))
        report = await export(h)
        assert len(accounts.registered_keys) == 1  # registered before signing
        for path in (report["pdf"], report["csv"], report["signature"]):
            code, said = verify(h, path, accounts)
            assert code == 0, said
            assert "Verified" in said and PROVES in said


async def test_an_edited_report_fails_verification(home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        record(h, a_read(3))
        report = await export(h)
        csv_path = Path(report["csv"])
        csv_path.write_bytes(csv_path.read_bytes().replace(b",3,", b",30,"))
        code, said = verify(h, csv_path, accounts)
        assert code == 1 and "edited" in said
        code, said = verify(h, report["pdf"], accounts)  # the signature covers both files
        assert code == 1 and "edited" in said


async def test_an_edited_signature_file_fails_verification(home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        record(h, a_read(3))
        report = await export(h)
        signature = Path(report["signature"])
        manifest = json.loads(signature.read_text())
        signature.write_text(json.dumps({**manifest, "end": "2026-12-31"}))
        code, said = verify(h, report["pdf"], accounts)
        assert code == 1 and "signature doesn't match" in said


async def test_a_report_from_another_computers_key_fails_unless_that_key_is_registered(
        home: Path, people_db: Path, tmp_path: Path) -> None:
    mine, theirs = PlanAccounts("active"), PlanAccounts("active")  # two accounts
    async with tool(tmp_path / "other", people_db, accounts=theirs) as other:
        record(other, a_read(3))
        report = await export(other)
    async with tool(home, people_db, accounts=mine) as h:
        record(h, a_read(1))
        await export(h)  # this account has its own key registered
        code, said = verify(h, report["pdf"], mine)
        assert code == 1 and "not registered to this account" in said
        mine.registered_keys.update(theirs.registered_keys)  # once that computer's key is registered to this account
        code, said = verify(h, report["pdf"], mine)
        assert code == 0, said


async def test_someone_without_the_account_checks_it_with_the_public_key_they_were_given(
        home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        record(h, a_read(3))
        report = await export(h)
        (public,) = accounts.registered_keys.values()
        h.keychain.delete("account-token")  # not signed in: the account's keys can't be asked for
        code, said = verify(h, report["pdf"], accounts)
        assert code == 1 and "--public-key" in said
        assert verify(h, report["pdf"], accounts, "--public-key", public)[0] == 0
        other = "A" * 43
        code, said = verify(h, report["pdf"], accounts, "--public-key", other)
        assert code == 1 and "not the key you gave" in said


async def test_the_report_says_what_the_signature_proves_and_nothing_more(home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        record(h, a_read(3))
        report = await export(h)
    text = pdf_text(Path(report["pdf"]))
    (key_id,) = accounts.registered_keys
    assert f"Signed by the key of the computer that made it ({key_id})." in text
    assert PROVES in text
    assert "not signed" not in text.lower()
    manifest = json.loads(Path(report["signature"]).read_text())
    assert manifest["proves"] == PROVES and manifest["key_id"] == key_id


async def test_the_chain_is_intact_when_nothing_was_touched(home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        record(h, a_read(3), a_read(4, "10:00:00"), a_read(5, "11:00:00"))
        report = await export(h)
    manifest = json.loads(Path(report["signature"]).read_text())
    assert manifest["chain"]["intact"] is True and manifest["chain"]["linked"] == 3
    assert "AI log chain: 3 entries from 1 computer, linked with no gap." in pdf_text(Path(report["pdf"]))


async def test_an_ai_log_entry_changed_or_removed_on_this_computer_breaks_the_chain(
        home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        record(h, a_read(3), a_read(4, "10:00:00"), a_read(5, "11:00:00"), a_read(6, "12:00:00"))
        db = sqlite3.connect(database_path(h.home))
        db.execute("UPDATE ai_reads SET row_count = 40 WHERE row_count = 4")
        db.execute("DELETE FROM ai_reads WHERE row_count = 5")
        db.commit()
        db.close()
        report = await export(h)
        manifest = json.loads(Path(report["signature"]).read_text())
        assert manifest["chain"]["intact"] is False
        text = pdf_text(Path(report["pdf"]))
        assert "AI log chain: broken." in text
        assert "changed after it was written" in text and "missing" in text
        assert verify(h, report["pdf"], accounts)[0] == 0  # the report itself is still unedited, and says so


async def test_removing_the_latest_entry_breaks_the_chain(home: Path, people_db: Path) -> None:
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        record(h, a_read(3), a_read(4, "10:00:00"))
        db = sqlite3.connect(database_path(h.home))
        db.execute("DELETE FROM ai_reads WHERE row_count = 4")
        db.commit()
        db.close()
        report = await export(h)
    assert json.loads(Path(report["signature"]).read_text())["chain"]["intact"] is False


def test_entries_written_here_chain_to_the_one_before_and_keep_it_when_synced(home: Path) -> None:
    store = LocalStore.open(database_path(home))
    first, second = store.reads.record(a_read(1)), store.reads.record(a_read(2, "10:00:00"))
    store.close()
    assert first.hash and second.prev == first.hash and second.hash != first.hash
    assert AiRead.from_dict(second.as_dict()) == second  # sync and the hosted inbox carry it as written


def test_an_unsigned_report_says_plainly_it_isnt_signed(tmp_path: Path) -> None:
    """The report built without a computer's key (the browser's, #107, mirrors this one) says it isn't signed."""
    report = proof_report([a_read(3)], DAY, DAY)
    _, pdf_path = export_report(tmp_path, report)
    assert "It is not signed: only a report exported on one of your computers is." in pdf_text(pdf_path)
    assert NOTE.endswith("It is not signed: only a report exported on one of your computers is.")


async def test_signing_in_registers_this_computers_key_with_the_real_account_service(
        home: Path, service: Service) -> None:
    """Against the real control app: sign-in registers the key, and the account lists it to its computers."""
    keychain, opened = MemoryKeychain(), Opened()
    accounts = Accounts(AccountService(service.url))
    async with Harness.start(home, keychain=keychain, browser=opened, accounts=accounts) as h:
        await h.call("account_sign_in")
        (url,) = opened.urls
        assert back_to_the_tool(sign_in_and_answer(service, url, "sam@gmail.example"))[0] == 200
    key = signing_key(keychain)
    listed = accounts.service.signing_keys(keychain.entries[TOKEN_ACCOUNT])["keys"]
    assert [(k["key_id"], k["public_key"]) for k in listed] == [(key_id(public_raw(key)), public_key_text(key))]
    assert KEY_ACCOUNT in keychain.entries  # the private half stays in this computer's keychain
