"""The proof report in the browser (#107): the page's scripts against what this computer seals.

The account page's report is built in the browser by the control app's report_static scripts, from the AI log's
synced records, after a linked computer seals the AI log's key to the browser's public key. Here those scripts run
under node (as the browser runs them, with Web Crypto) against records and a sealed key made by this package's own
Python, and what they build is compared with the computer's own proof report for the same range: the CSV byte for
byte, the PDF by its text. The approve flow itself runs against the real control app at the end."""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from portward_free.asks import Refused
from portward_free.browser_report import browser_code, seal_for_browser, typed_code
from portward_free.proof import AiRead, ShownColumn, UsedContext, UsedProfile, cutoff_day
from portward_free.proof_report import MADE_FROM, NOTE, export_report, proof_report
from portward_free.storage import LocalStore
from portward_free.sync import crypto
from portward_free.sync.read_records import read_records
from pypdf import PdfReader
from test_account import UserBrowser
from test_sync import Device, Service, device, fill, marked_db, service  # noqa: F401 (fixtures)

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "portward" / "control" / "report_static"
RUNNER = Path(__file__).parent / "fixtures" / "report_in_node.js"
ACCOUNT = "acct_7f3a9c"
REQUEST = "req_Q2x9"
needs_node = pytest.mark.skipif(shutil.which("node") is None or not STATIC.is_dir(),
                                reason="needs node and the control app's report scripts")


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64int(n: int) -> str:
    return b64(n.to_bytes(32, "big"))


class BrowserKeys:
    """The browser's key pair, made here so the test can hand node its private half (a real page can't export it)."""

    def __init__(self) -> None:
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.public = self.private.public_key().public_bytes(serialization.Encoding.X962,
                                                             serialization.PublicFormat.UncompressedPoint)

    def jwk(self) -> dict[str, str]:
        numbers = self.private.private_numbers()
        return {"kty": "EC", "crv": "P-256", "x": _b64int(numbers.public_numbers.x),
                "y": _b64int(numbers.public_numbers.y), "d": _b64int(numbers.private_value)}


def in_node(tmp_path: Path, job: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job))
    done = subprocess.run(["node", str(RUNNER), str(STATIC), str(path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def day(offset: int) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


def at(offset: int, clock: str) -> str:
    return f"{day(offset)}T{clock}.000000+00:00"


def shown(*columns: tuple[Any, ...]) -> tuple[ShownColumn, ...]:
    return tuple(ShownColumn(*c) for c in columns)


def without_rules_applied(content: dict[str, Any], entry_id: str) -> dict[str, Any]:
    """An entry as a computer from before the rules applied and the context relied on were kept wrote it: no
    allowed_by and no context at all."""
    entries = [{**{k: v for k, v in e.items() if k != "context"},
                "columns": [{k: v for k, v in c.items() if k != "allowed_by"} for c in e["columns"]]}
               if e["id"] == entry_id else e for e in content.get("entries", [])]
    return {**content, "entries": entries}


# Reads with what a CSV or a PDF could get wrong: commas, quotes and line breaks, letters outside Latin-1, no AI app,
# block rules, two reads at the same moment, a read on each edge of the range and one outside it.
READS = (
    AiRead("Claude desktop", "desktop", "Warehouse", (UsedProfile("Sales pipeline", 4),),
           shown(("deals", "amount", "full", ("Sales pipeline",)),
                 ("deals", "email", "anonymised", ("Sales pipeline",))), 120, "a" * 64, "laptop-1",
           "id-0001", at(9, "08:00:00"), (UsedContext("definition", "revenue, net", "Sales pipeline", 4),
                                          UsedContext("meaning", "deals.amount", "Sales pipeline", 4))),
    AiRead("claude.ai", "hosted", 'Ware,house "main"', (UsedProfile("Finance, totals", 12), UsedProfile("Ops", 1)),
           shown(("ledger", "total", "totals", ("Finance, totals", "Ops"))), 3, "b" * 64, "hosted-engine", "id-0002",
           at(5, "09:30:00"), (UsedContext("metric", "MRR", "Finance, totals", 12),  # a kind from later on
                               UsedContext("verified_answer", "Won deals, last week?", "Ops", 2, True),  # #130
                               UsedContext("verified_answer", "Open deals?", "Ops", 1, False))),
    AiRead(None, "direct", "line one\nline two", (), shown(("t", "c", "full")), 0, "c" * 64, "laptop-1", "id-0003",
           at(5, "09:30:00")),
    AiRead("ChatGPT", "hosted", "東京 sales 📈", (UsedProfile("Café ünïcode", 2),),
           shown(("ventes", "région", "full", ("Café ünïcode",))), 1, "d" * 64, "desktop-2", "id-0004",
           at(3, "23:59:59"), (UsedContext("definition", "chiffre d’affaires 📈", "Café ünïcode", 2),)),
    AiRead("Claude desktop", "desktop", "Warehouse", (UsedProfile("Sales pipeline", 4),),
           shown(("deals", "amount", "full"), ("deals", "stage", "full")), 40, "e" * 64, "desktop-2", "id-0005",
           at(2, "10:00:00")),
    AiRead("Claude desktop", "desktop", "Warehouse", (UsedProfile("Sales pipeline", 4),), (), 1, "f" * 64,
           "laptop-1", "id-0006", at(30, "10:00:00")),  # before the range
)
START, END = day(9), day(2)


def computer_report(tmp_path: Path) -> tuple[LocalStore, bytes, bytes]:
    store = LocalStore.open(tmp_path / "computer" / "portward.db")
    store.reads.add(READS)
    csv_path, pdf_path = export_report(tmp_path / "exports", proof_report(store.reads.between(START, END), START, END))
    return store, csv_path.read_bytes(), pdf_path.read_bytes()


def synced(store: LocalStore, key: crypto.DataKey) -> list[dict[str, Any]]:
    """The account's synced records as the service hands them out: the AI log's, a profile and a note, and a part
    repeating an entry another part has (sync merges by id, so the browser must too)."""
    contents = [without_rules_applied(c, "id-0005") for c in read_records(store.reads.between(cutoff_day()))]
    contents.append({**contents[0], "part": 7})
    others = [{"kind": "profile", "name": "zq_profile_marker", "grants": {}}, {"kind": "note", "text": "zq-note"}]
    records = []
    for i, content in enumerate([*contents, *others]):
        record_id, version = key.record_id(content["kind"], str(i)), i + 1
        records.append({"id": record_id, "version": version, "ciphertext": b64(key.seal(ACCOUNT, record_id, version,
                                                                                        content))})
    return records


def pdf_text(raw: bytes) -> str:
    """The PDF's words, page footers and all whitespace left out (line and page breaks may fall elsewhere)."""
    text = "".join(page.extract_text() for page in PdfReader(io.BytesIO(raw)).pages)
    return re.sub(r"\s+", "", re.sub(r"Page \d+", "", text))


@needs_node
def test_the_browser_builds_the_same_csv_and_pdf_as_the_computer_for_the_same_range(tmp_path: Path) -> None:
    store, csv_here, pdf_here = computer_report(tmp_path)
    key, browser = crypto.new_data_key(), BrowserKeys()
    out = in_node(tmp_path, {"private_jwk": browser.jwk(), "public_key": b64(browser.public),
                             "sealed": b64(seal_for_browser(key.ai_log_key, browser.public, ACCOUNT, REQUEST)),
                             "account_id": ACCOUNT, "request_id": REQUEST, "records": synced(store, key),
                             "start": START, "end": END})
    csv_there, pdf_there = base64.b64decode(out["csv"]), base64.b64decode(out["pdf"])
    assert csv_there == csv_here
    assert (out["reads"], out["rows"]) == (5, 164)
    assert pdf_text(pdf_there) == pdf_text(pdf_here)
    words = " ".join(PdfReader(io.BytesIO(pdf_there)).pages[0].extract_text().split())
    # #135: the browser holds no computer's signing key, so its report says plainly it isn't signed
    assert "It is not signed: only a report exported on one of your computers is." in words
    assert f"{START} to {END}: 5 reads, 164 rows" in words
    assert "Claude desktop under Sales pipeline v4: 2 reads, 160 rows" in words
    every_page = " ".join(" ".join(p.extract_text().split()) for p in PdfReader(io.BytesIO(pdf_there)).pages)
    assert "?? sales ?" in every_page  # outside Latin-1, as fpdf2 writes it; the CSV keeps it
    assert "東京 sales 📈".encode() in csv_there
    assert b"ledger.total: totals by Finance, totals, Ops" in csv_there
    assert b"deals.stage: full (profile not recorded)" in csv_there  # id-0005, synced without allowed_by
    assert b"block rules (nothing read was blocked)" in csv_there
    assert b'"definition revenue, net (Sales pipeline v4); meaning deals.amount (Sales pipeline v4)"' in csv_there
    assert b"metric MRR (Finance, totals v12)" in csv_there  # a kind a later Portward writes still shows
    assert b"verified answer Won deals, last week? (Ops v2); verified answer Open deals? (Ops v1), result changed" \
        in csv_there
    assert "chiffre d’affaires 📈".encode() in csv_there


@needs_node
def test_the_key_a_browser_gets_opens_only_the_ai_logs_records(tmp_path: Path) -> None:
    store, _, _ = computer_report(tmp_path)
    key, browser = crypto.new_data_key(), BrowserKeys()
    records = synced(store, key)
    out = in_node(tmp_path, {"private_jwk": browser.jwk(), "public_key": b64(browser.public),
                             "sealed": b64(seal_for_browser(key.ai_log_key, browser.public, ACCOUNT, REQUEST)),
                             "account_id": ACCOUNT, "request_id": REQUEST, "records": records,
                             "start": START, "end": END})
    assert out["opened"] == len(records) - 2  # the profile and the note stay shut
    sealed = seal_for_browser(key.ai_log_key, browser.public, ACCOUNT, REQUEST)
    assert key.raw not in sealed and key.ai_log_key not in sealed


@needs_node
@pytest.mark.parametrize("change", ["account", "request", "browser"])
def test_a_sealed_key_opens_only_in_the_browser_and_request_it_was_sealed_for(tmp_path: Path, change: str) -> None:
    key, browser, other = crypto.new_data_key(), BrowserKeys(), BrowserKeys()
    sealed = seal_for_browser(key.ai_log_key, (other if change == "browser" else browser).public, ACCOUNT, REQUEST)
    job = {"private_jwk": browser.jwk(), "public_key": b64(browser.public), "sealed": b64(sealed),
           "account_id": "acct_other" if change == "account" else ACCOUNT,
           "request_id": "req_other" if change == "request" else REQUEST, "records": [], "start": START, "end": END}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job))
    done = subprocess.run(["node", str(RUNNER), str(STATIC), str(path)], capture_output=True, text=True, timeout=60)
    assert done.returncode != 0 and "OperationError" in done.stderr


@needs_node
def test_the_browser_shows_the_code_this_computer_checks(tmp_path: Path) -> None:
    browser = BrowserKeys()
    out = in_node(tmp_path, {"public_key": b64(browser.public)})
    assert out["code"] == browser_code(browser.public)
    assert re.fullmatch(r"[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", out["code"])


def test_a_typed_code_forgives_case_spaces_and_lookalikes_but_not_its_length() -> None:
    code = browser_code(BrowserKeys().public)
    assert typed_code(f" {code.lower().replace('-', ' ')} ") == code
    assert typed_code("ABCD EFGH 2345") == "ABCD-EFGH-2345"
    assert typed_code("abcd-efgh-i0b8") == "ABCD-EFGH-IOBB"
    for wrong in ("ABCD-EFGH", "ABCD-EFGH-23456", "ABCD-EFGH-234!"):
        with pytest.raises(Refused):
            typed_code(wrong)


def test_sealing_refuses_what_isnt_a_p256_public_key() -> None:
    with pytest.raises(ValueError):
        seal_for_browser(b"k" * 32, b"\x04" + b"\x00" * 64, ACCOUNT, REQUEST)  # not on the curve
    with pytest.raises(ValueError):
        seal_for_browser(b"k" * 32, b"\x02" + b"\x01" * 32, ACCOUNT, REQUEST)


# --- against the real control app -----------------------------------------------------------------------------------
class SignedInBrowser:
    """The user's browser on the report page, doing what report_page.js does over the network."""

    def __init__(self, service: Service, email: str) -> None:
        self.base, self.web = service.url, UserBrowser(service.url)
        _, headers, _ = self.web.go("/me/signin")
        asked = {k: v[0] for k, v in parse_qs(urlsplit(headers["location"]).query).items()}
        status, _, _ = self.web.go(asked["redirect_uri"] + "?" + urlencode(
            {"state": asked["state"], "code": f"{email}|{asked['code_challenge']}"}))
        assert status == 303
        _, _, page = self.web.go("/me/report")
        found = re.search(r'name="csrf" content="([^"]+)"', page)
        assert found, page
        self.csrf = found.group(1)
        self.keys = BrowserKeys()

    def call(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.web.cookies.items()),
                   "Content-Type": "application/json", "X-CSRF-Token": self.csrf, "Sec-Fetch-Site": "same-origin"}
        request = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None,
                                         headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def ask(self) -> str:
        status, body = self.call("/me/report/unlock", {"public_key": b64(self.keys.public)})
        assert status == 201, body
        return body["id"]

    @property
    def code(self) -> str:
        return browser_code(self.keys.public)


def opened_key(browser: SignedInBrowser, sealed: bytes, account_id: str, request_id: str) -> bytes:
    """The AI log's key as the page opens it (the same steps as report_crypto.openUnlock), to look for it."""
    theirs = sealed[1:66]
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), theirs)
    context = f"portward-browser-report-v1\n{account_id}\n{request_id}".encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=theirs + browser.keys.public, info=context).derive(
        browser.keys.private.exchange(ec.ECDH(), peer))
    return AESGCM(key).decrypt(sealed[66:78], sealed[78:], context)


async def approved_from(laptop: Device, code: str) -> Any:
    asked = await laptop.h.call("approve_browser")
    assert asked.ai.structured["code_entry"] == "panel", asked.ai.text
    token = (await laptop.h.form_in_panel(asked))["token"]
    return await laptop.h.call_from_panel("save_secret", request=token, secret=code)


@needs_node
async def test_a_browser_approved_from_a_linked_computer_builds_the_computers_report(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    email = "rae@gmail.example"
    async with device(service, tmp_path / "laptop", email, panel=True) as laptop:
        await fill(laptop.h, marked_db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        browser = SignedInBrowser(service, email)
        request_id = browser.ask()
        assert browser.call("/me/report/records")[0] == 403  # not approved: no history

        wrong = await approved_from(laptop, "AAAA-BBBB-CCCC")
        assert wrong.is_error and browser.call(f"/me/report/unlock/{request_id}")[1] == {"state": "waiting"}
        done = await approved_from(laptop, browser.code.lower())
        assert not done.is_error, done.ai.text

        status, answer = browser.call(f"/me/report/unlock/{request_id}")
        assert status == 200 and answer["state"] == "approved"
        status, listed = browser.call("/me/report/records")
        assert status == 200
        today = datetime.now(UTC).date().isoformat()  # the AI log keeps UTC days
        exported = await laptop.h.call("export_proof_report", start=today, end=today)
        assert not exported.is_error, exported.ai.text
        csv_here = Path(exported.ai.structured["csv"]).read_bytes()
        seen_by_ai = json.dumps([done.ai.text, done.ai.structured])
    out = in_node(tmp_path, {"private_jwk": browser.keys.jwk(), "public_key": b64(browser.keys.public),
                             "sealed": answer["sealed"], "account_id": answer["account_id"], "request_id": request_id,
                             "records": listed["records"], "start": today, "end": today})
    assert (out["opened"], len(listed["records"])) == (1, 5)
    assert base64.b64decode(out["csv"]) == csv_here and out["reads"] == 1
    assert b"zq_table_marker" in csv_here

    ai_log_key = opened_key(browser, base64.urlsafe_b64decode(answer["sealed"] + "=="), answer["account_id"],
                            request_id)
    stored, log = service.stored_bytes(), service.log()
    assert "/me/report/records" in log and "/v1/me/browsers/approve" in log
    for marker in (b"zq_table_marker", b"zq_column_marker", ai_log_key, ai_log_key.hex().encode(),
                   b64(ai_log_key).encode(), base64.b64encode(ai_log_key)):
        assert marker not in stored, marker
        assert marker.decode("latin-1") not in log, marker
    assert ai_log_key.hex() not in seen_by_ai and b64(ai_log_key) not in seen_by_ai


async def test_the_code_can_be_typed_in_a_terminal_and_a_computer_without_sync_refuses(
        service: Service, tmp_path: Path, marked_db: Path) -> None:
    email = "sol@gmail.example"
    async with device(service, tmp_path / "laptop", email) as laptop:
        browser = SignedInBrowser(service, email)
        request_id = browser.ask()
        refused = laptop.h.run_sync_command("approve-browser", browser.code)
        assert refused.code == 1 and "Sync isn't set up" in refused.out
        await fill(laptop.h, marked_db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        approved = laptop.h.run_sync_command("approve-browser", browser.code)
        assert approved.code == 0, approved.out
    assert browser.call(f"/me/report/unlock/{request_id}")[1]["state"] == "approved"


# --- the page in a real browser (Playwright, when installed: uv run --with playwright pytest ...) ---------------------
@pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
async def test_the_report_page_in_a_real_browser_matches_the_computers_report(
        service: Service, tmp_path: Path, marked_db: Path, engine: str) -> None:
    playwright_api = pytest.importorskip("playwright.async_api")
    email = f"tia-{engine}@gmail.example"
    async with device(service, tmp_path / "laptop", email, panel=True) as laptop:
        await fill(laptop.h, marked_db)
        assert not (await laptop.h.call("sync_turn_on")).is_error
        async with playwright_api.async_playwright() as p:
            try:
                chromium = await getattr(p, engine).launch()
            except Exception as err:  # noqa: BLE001 (no browser downloaded here)
                pytest.skip(f"no Playwright browser: {err}")
            context = await chromium.new_context(accept_downloads=True)
            # Signed in at the (fake) AuthKit. The test service is plain http, where a browser won't keep its Secure
            # __Host- cookie, so the session cookie is added to this page's requests to the service here instead.
            cookie = "; ".join(f"{k}={v}" for k, v in SignedInBrowser(service, email).web.cookies.items())

            async def with_session(route: Any) -> None:
                answer = await route.fetch(headers={**route.request.headers, "cookie": cookie}, max_redirects=0)
                await route.fulfill(response=answer)

            await context.route(lambda url: url.startswith(service.url + "/"), with_session)
            page = await context.new_page()
            problems: list[str] = []
            page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            await page.goto(service.url + "/me/report")
            start, end = await page.input_value("#start"), await page.input_value("#end")
            await page.click("#unlock")
            await page.wait_for_selector("#code:not(:empty)")
            code = await page.text_content("#code")
            assert await page.locator("#step-files").is_hidden()
            done = await approved_from(laptop, code or "")
            assert not done.is_error, done.ai.text
            await page.wait_for_selector("#step-files:not([hidden])", timeout=20_000)
            totals = await page.text_content("#totals")
            async with page.expect_download() as csv_download:
                await page.click("#csv")
            async with page.expect_download() as pdf_download:
                await page.click("#pdf")
            csv_file, pdf_file = await csv_download.value, await pdf_download.value
            csv_there = Path(await csv_file.path()).read_bytes()
            pdf_there = Path(await pdf_file.path()).read_bytes()
            names = [csv_file.suggested_filename, pdf_file.suggested_filename]
            await chromium.close()
        exported = await laptop.h.call("export_proof_report", start=start, end=end)
        assert not exported.is_error, exported.ai.text
    assert csv_there == Path(exported.ai.structured["csv"]).read_bytes()
    # the same report, but the computer's is signed (#135) and says so where the browser's says it isn't
    pdf_here = pdf_text(Path(exported.ai.structured["pdf"]).read_bytes())
    browser_note, signed_note = re.sub(r"\s+", "", NOTE), re.sub(r"\s+", "", MADE_FROM)
    assert browser_note in pdf_text(pdf_there) and browser_note not in pdf_here
    before, after = pdf_text(pdf_there).split(browser_note)
    assert pdf_here.startswith(before + signed_note) and pdf_here.endswith(after)
    assert totals == f"{start} to {end}: 1 read, 2 rows"
    assert names == [f"proof-report-{start}-to-{end}.csv", f"proof-report-{start}-to-{end}.pdf"]
    # no script error and no CSP refusal (Firefox's own favicon fetch, refused on every control page, aside)
    assert [p for p in problems if "FaviconLoader" not in p] == []
