"""Approving a browser for the proof report (#107), from this computer.

The account page builds the proof report in the browser, from the AI log's synced records. The browser can't read
them until a computer linked to the account approves it, the way a new computer opens sync with the recovery key:

1. The browser makes a P-256 key pair (its private half never leaves it), sends the public half to the account
   service and shows a code made from it (`browser_code`: the first 60 bits of its SHA-256, base32, in groups of four).
2. The user asks Portward here to approve it and types that code out of band (the panel, a one-time local page, or
   `portward-free approve-browser`), never in the chat: the AI can't approve a browser by itself.
3. This computer syncs (so its latest reads are there), takes the waiting public key whose code matches, and seals
   the AI log's key (`DataKey.ai_log_key`, which opens only the AI log's records: not profiles, notes, rules or the
   other kinds) to it: ECDH P-256, HKDF-SHA256, AES-256-GCM, bound to the account and the request
   (`seal_for_browser`). The account service relays the sealed blob and never sees the key.

A key the service slipped in wouldn't match the code the browser showed the user, so it isn't sealed to. The AI is
told only that a browser was approved, never the key."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free import passwords
from portward_free.account import Accounts, AccountServiceError
from portward_free.asks import Ask, Refused
from portward_free.keychain import Keychain
from portward_free.secret_entry import OutOfBand, Texts
from portward_free.storage import LocalStore
from portward_free.sync.service import SyncService
from portward_free.sync.tools import SyncRefused, _service_failed, _signed_in, data_key, sync_once

ASK_KIND = "browser-report-code"
CODE_CHARS = 12  # 60 bits
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_LOOKALIKES = str.maketrans({"0": "O", "1": "I", "8": "B"})
_FORMAT = 1
_NONCE = 12
PUBLIC_KEY_BYTES = 65


def browser_code(public_key: bytes) -> str:
    """The code a browser shows for its public key (report_crypto.js makes the same one)."""
    letters = base64.b32encode(hashlib.sha256(public_key).digest()).decode()[:CODE_CHARS]
    return "-".join(letters[i:i + 4] for i in range(0, CODE_CHARS, 4))


def typed_code(typed: str) -> str:
    """A code as the user typed it, in the browser's form: spaces, dashes and case don't matter, nor 0/1/8."""
    cleaned = "".join(ch for ch in typed.upper() if ch.isalnum()).translate(_LOOKALIKES)
    if len(cleaned) != CODE_CHARS or any(ch not in _ALPHABET for ch in cleaned):
        raise Refused("that isn't a browser's code: it is 12 letters and digits, in groups of four, shown on the "
                      "report page")
    return "-".join(cleaned[i:i + 4] for i in range(0, CODE_CHARS, 4))


def _context(account_id: str, request_id: str) -> bytes:
    return f"portward-browser-report-v1\n{account_id}\n{request_id}".encode()


def seal_for_browser(secret: bytes, public_key: bytes, account_id: str, request_id: str) -> bytes:
    """format | this side's one-time P-256 public key (65) | nonce | `secret` sealed under
    HKDF-SHA256(ECDH, salt = both public keys, info = account and request) with AES-256-GCM (the same as AAD)."""
    if len(public_key) != PUBLIC_KEY_BYTES or public_key[0] != 4:
        raise ValueError("not a P-256 public key")
    browser = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key)
    mine = ec.generate_private_key(ec.SECP256R1())
    mine_public = mine.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)
    context = _context(account_id, request_id)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=mine_public + public_key,
               info=context).derive(mine.exchange(ec.ECDH(), browser))
    nonce = os.urandom(_NONCE)
    return bytes([_FORMAT]) + mine_public + nonce + AESGCM(key).encrypt(nonce, secret, context)


@dataclass(frozen=True)
class Approved:
    def summary(self) -> str:
        return "The browser is approved: the report opens there now. Tell the AI it's done."

    def payload(self) -> dict[str, Any]:
        return {"browser": "approved"}


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def approve(store: LocalStore, keychain: Keychain, service: SyncService, typed: str) -> Approved:
    """Seal the AI log's key to the waiting browser whose code the user typed. Raises Refused (a safe message)."""
    code = typed_code(typed)
    try:
        signed = _signed_in(store, keychain)
        key = data_key(store, keychain, service, signed)
        if key is None:
            raise SyncRefused("This computer can't open the account's synced data yet: ask the AI to turn sync on "
                              "here first.")
        sync_once(store, keychain, service)  # this computer's latest reads, there for the browser
        waiting = service.browsers(signed.token)
    except SyncRefused as err:
        raise Refused(str(err)) from None
    except AccountServiceError as err:
        raise Refused(str(_service_failed(store, err))) from None
    matched = []
    for browser in waiting:
        try:
            public_key = _unb64(browser.get("public_key"))
        except (ValueError, TypeError):
            continue
        if isinstance(browser.get("id"), str) and hmac.compare_digest(browser_code(public_key), code):
            matched.append((browser["id"], public_key))
    if len(matched) != 1:
        raise Refused("no browser is waiting with that code: open the report page again and type the code it shows "
                      "(a code lasts 10 minutes)")
    request_id, public_key = matched[0]
    try:
        sealed = seal_for_browser(key.ai_log_key, public_key, signed.account_id, request_id)
        service.approve_browser(signed.token, request_id, sealed)
    except ValueError:
        raise Refused("that browser's key isn't one this Portward can seal to; open the report page again") from None
    except AccountServiceError as err:
        raise Refused(str(_service_failed(store, err))) from None
    return Approved()


def approve_command() -> str:
    return passwords.command("approve-browser")


@dataclass
class BrowserReportTools:
    store: LocalStore
    keychain: Keychain
    service: SyncService
    out_of_band: OutOfBand

    def code_ask(self, params: Mapping[str, Any] | None = None) -> Ask:
        """The out-of-band ask for the browser's code; also rebuilds one the other server process opened."""
        return Ask(key=ASK_KIND, title="Approve a browser", label="Code shown on the report page",
                   details=(("For", "opening your AI log's report in that browser"),
                            ("Check", "only type a code shown on signup.portward.ai/me/report in your own browser")),
                   done="I've approved the browser.",
                   save=lambda typed: approve(self.store, self.keychain, self.service, typed), kind=ASK_KIND,
                   hidden=False)

    async def ask(self, ctx: Context) -> CallToolResult | InputRequiredResult:
        texts = Texts(what="the code their browser's report page shows", then="tell them the report opens there",
                      command=approve_command(), does="approves that browser to read the AI log for the report",
                      lead="The report page in the user's browser needs this computer's approval.")
        return await self.out_of_band.ask(ctx, self.code_ask(), {"browser": "needs_code"}, texts, "code_entry")


def register_browser_report_tools(server: MCPServer, store: LocalStore, keychain: Keychain, accounts: Accounts,
                                  out_of_band: OutOfBand, service: SyncService | None = None) -> None:
    tools = BrowserReportTools(store, keychain, service if service is not None else SyncService(accounts.service),
                               out_of_band)
    out_of_band.register(ASK_KIND, tools.code_ask)

    @server.tool(meta=out_of_band.panel_meta(), description=(
        "Approve the user's browser to build the proof report (what my AI saw) on their Portward account page "
        "(sync on). The user types the code the page shows where you can't see it; never ask for it in the "
        "chat. Only call it when the user asks to open or export the report in their browser."))
    async def approve_browser(ctx: Context) -> CallToolResult | InputRequiredResult:
        return await tools.ask(ctx)
