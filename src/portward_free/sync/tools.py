"""The sync tools (#55) and the steps the terminal commands share with them.

- `sync_turn_on`: the first computer makes the account's data key and recovery key, gives the service the data key
  wrapped twice (under this computer's own key and under the recovery key) and shows the recovery key once, in a
  one-time page on this computer opened in the user's browser (or `portward-free sync-recovery-key` in a
  terminal). Any later computer asks the user for the recovery key out of band (#62: the panel, a one-time local
  page, or `portward-free sync-restore`), opens the data key with it and adds its own wrap.
- `sync_now`: one run (engine.Run). `sync_status`, `sync_turn_off`, `sync_new_recovery_key` (the old one stops
  working).

The AI never sees the recovery key, the data key, the device key or the token; it is told what happened in counts.
Sync needs Personal (trial, active or grace, #54): when the service answers 402 it stops here with the reason, and
everything this computer holds stays."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from http import HTTPStatus
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free import passwords
from portward_free.account import ACCOUNT_SETTING, AccountServiceError, Accounts, account_token
from portward_free.asks import Ask, Refused
from portward_free.keychain import Keychain, KeychainError
from portward_free.localweb import LocalWeb, Request, Response
from portward_free.replies import error, reply
from portward_free.secret_entry import OutOfBand, Texts
from portward_free.storage import LocalStore
from portward_free.hosted import inbox
from portward_free.hosted.service import HostedService
from portward_free.sync import crypto
from portward_free.sync.crypto import DataKey, Unopenable
from portward_free.sync.engine import Run
from portward_free.sync.service import STATUS_ENDED, NewKeys, RemoteKeys, SyncService

log = logging.getLogger("portward.sync")

SYNC_SETTING = "sync"  # {on, account_id, key_id, cursor, high, last_synced_at, stopped}
RECOVERY_ASK = "sync-recovery-key"
PAGE_SECONDS = 10 * 60


class SyncRefused(Exception):
    """Sync can't go ahead; the message is safe to show the AI."""


def state(store: LocalStore) -> dict[str, Any]:
    stored = store.setting(SYNC_SETTING)
    return json.loads(stored) if stored else {}


def _save_state(store: LocalStore, **changes: Any) -> dict[str, Any]:
    new = {**state(store), **changes}
    store.set_setting(SYNC_SETTING, json.dumps(new))
    return new


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def restore_command() -> str:
    return passwords.command("sync-restore")


def recovery_key_command() -> str:
    return passwords.command("sync-recovery-key")


@dataclass(frozen=True)
class Signed:
    token: str
    account_id: str


def _signed_in(store: LocalStore, keychain: Keychain) -> Signed:
    try:
        token = account_token(keychain)
    except KeychainError as err:
        raise SyncRefused(f"The account token couldn't be read from the keychain: {err}.") from None
    stored = store.setting(ACCOUNT_SETTING)
    account_id = json.loads(stored).get("account_id", "") if stored else ""
    if not token or not account_id:
        raise SyncRefused("This computer isn't signed in to a Portward account: call account_sign_in first.")
    return Signed(token, account_id)


def _service_failed(store: LocalStore, err: AccountServiceError) -> SyncRefused:
    if err.status == STATUS_ENDED:
        _save_state(store, stopped="Personal has ended")
        return SyncRefused("Sync has stopped: Personal has ended for this account. Everything on this computer stays "
                           "as it is.")
    if err.status == 401:
        return SyncRefused("The account no longer knows this computer: call account_sign_in, then sync_turn_on.")
    return SyncRefused(f"The sync service couldn't be used just now: {err}.")


def _begin_on(store: LocalStore, account_id: str, key: DataKey) -> None:
    """Sync on for this account: what was agreed with another account (or another key) is forgotten first."""
    before = state(store)
    if before.get("account_id") != account_id or before.get("key_id") != key.key_id:
        store.clear_sync_rows()
        before = {}
    _save_state(store, on=True, account_id=account_id, key_id=key.key_id, cursor=before.get("cursor", 0), high=before.get("high", 0),
                stopped=None)


def _device_key(keychain: Keychain) -> bytes:
    try:
        key = crypto.device_key(keychain, create=True)
    except KeychainError as err:
        raise SyncRefused(f"This computer's sync key couldn't be kept in the keychain: {err}.") from None
    assert key is not None
    return key


def data_key(store: LocalStore, keychain: Keychain, service: SyncService, signed: Signed) -> DataKey | None:
    """The account's data key, opened with this computer's own key; None when this computer has no wrap yet (it
    needs the recovery key). Raises SyncRefused."""
    try:
        keys = service.keys(signed.token)
    except AccountServiceError as err:
        raise _service_failed(store, err) from None
    if keys is None:
        raise SyncRefused("Sync isn't set up for this account yet: call sync_turn_on.")
    try:
        mine = crypto.device_key(keychain, create=False)
    except KeychainError as err:
        raise SyncRefused(f"This computer's sync key couldn't be read from the keychain: {err}.") from None
    if keys.device is None or mine is None:
        return None
    try:
        key = crypto.open_with_device_key(keys.device, mine, signed.account_id)
    except Unopenable:
        return None
    if key.key_id != keys.key_id:
        return _moved_over(store, service, signed, keys, key, mine)
    return key


def _moved_over(store: LocalStore, service: SyncService, signed: Signed, keys: RemoteKeys, old: DataKey,
                mine: bytes) -> DataKey | None:
    """The recovery key was replaced on another computer, which made a new data key: open it from the handover
    with the key this computer holds, and file this computer's wrap of it. None when there is no handover for it."""
    if keys.handover is None or keys.previous_key_id != old.key_id:
        return None
    try:
        new = crypto.open_handover(keys.handover, old, signed.account_id, keys.key_id)
    except Unopenable:
        return None
    try:
        service.set_device(signed.token, crypto.wrap_for_device(new, mine, signed.account_id), new.key_id, new.proof)
    except AccountServiceError as err:
        raise _service_failed(store, err) from None
    return new


@dataclass(frozen=True)
class Restored:
    def summary(self) -> str:
        return "Sync is on for this computer. Tell the AI it's done; it syncs next."

    def payload(self) -> dict[str, Any]:
        return {"sync": "on"}


def restore(store: LocalStore, keychain: Keychain, service: SyncService, typed: str) -> Restored:
    """Open the account's data key with the recovery key the user typed (out of band) and give this computer its own
    wrap of it. Raises Refused with a safe message."""
    try:
        signed = _signed_in(store, keychain)
        keys = service.keys(signed.token)
    except SyncRefused as err:
        raise Refused(str(err)) from None
    except AccountServiceError as err:
        raise Refused(str(_service_failed(store, err))) from None
    if keys is None:
        raise Refused("sync isn't set up for this account yet; ask the AI to turn it on")
    try:
        key = crypto.open_with_recovery_key(keys.recovery, typed, signed.account_id)
    except Unopenable as err:
        raise Refused(str(err)) from None
    if key.key_id != keys.key_id:
        raise Refused("that recovery key opens a key this account no longer uses")
    try:
        service.set_device(signed.token, crypto.wrap_for_device(key, _device_key(keychain), signed.account_id),
                           key.key_id, key.proof)
    except SyncRefused as err:
        raise Refused(str(err)) from None
    except AccountServiceError as err:
        raise Refused(str(_service_failed(store, err))) from None
    _begin_on(store, signed.account_id, key)
    return Restored()


def new_recovery_key(store: LocalStore, keychain: Keychain, service: SyncService) -> str:
    """A new recovery key for the account (the old one stops working), to show the user, with a new data key: the
    service deletes what it held under the old one, and this computer sends everything again under the new one
    (other computers move over through the handover on their next sync). This computer syncs first, so it holds
    everything the service had. Raises SyncRefused."""
    if not state(store).get("on"):
        raise SyncRefused("Sync is off on this computer: call sync_turn_on first, so it holds everything synced.")
    sync_once(store, keychain, service)
    signed = _signed_in(store, keychain)
    old = data_key(store, keychain, service, signed)
    if old is None:
        raise SyncRefused("This computer can't open the account's synced data yet: call sync_turn_on first.")
    new, recovery = crypto.new_data_key(), crypto.new_recovery_key()
    wrapped = crypto.wrap_for_recovery(new, recovery, signed.account_id)
    if crypto.open_with_recovery_key(wrapped, recovery, signed.account_id) != new:  # never upload a wrap that fails
        raise SyncRefused("The new recovery key didn't check out; nothing was changed.")
    keys = NewKeys(new.key_id, wrapped, crypto.wrap_for_device(new, _device_key(keychain), signed.account_id),
                   crypto.seal_handover(old, new, signed.account_id), new.verifier)
    try:
        service.rotate(signed.token, old.proof, keys)
    except AccountServiceError as err:
        raise _service_failed(store, err) from None
    _begin_on(store, signed.account_id, new)
    sync_once(store, keychain, service)  # everything here, again, under the new key
    return recovery


def sync_once(store: LocalStore, keychain: Keychain, service: SyncService) -> dict[str, Any]:
    """One run, if sync is on here. Raises SyncRefused."""
    current = state(store)
    if not current.get("on"):
        raise SyncRefused("Sync is off on this computer: call sync_turn_on.")
    signed = _signed_in(store, keychain)
    if current.get("account_id") != signed.account_id:
        raise SyncRefused("This computer is signed in to a different account than the one it synced with: call "
                          "sync_turn_on to sync with this one.")
    key = data_key(store, keychain, service, signed)
    if key is None:
        raise SyncRefused("This computer can't open the account's synced data any more (it was signed out or "
                          "removed on the account page): call sync_turn_on and the user types the recovery key.")
    if key.key_id != current.get("key_id"):
        _begin_on(store, signed.account_id, key)
    _collect_hosted(store, keychain, service, signed)
    try:
        now_state = state(store)
        report = Run(store, service, signed.token, signed.account_id, key, keychain).run(int(now_state.get("cursor", 0)),
                                                                                 int(now_state.get("high", 0)))
    except AccountServiceError as err:
        raise _service_failed(store, err) from None
    _save_state(store, cursor=report.cursor, high=report.high, last_synced_at=_now(), stopped=None)
    return {"sent": report.sent, "received": report.received, "merged": report.merged,
            "unopenable": report.unopenable, "too_big": report.too_big, "held_rules": held_rules(store),
            "held_switches": held_switches(store), "profile_copies": report.landing.profile_copies}


def _collect_hosted(store: LocalStore, keychain: Keychain, service: SyncService, signed: Signed) -> None:
    """The hosted engine's AI log summary into this computer's first (#104), so this run syncs it everywhere."""
    account_service = getattr(service, "_http", None)  # the account service sync calls through
    if account_service is None:
        return
    try:
        inbox.collect(store, keychain, HostedService(account_service.base_url), signed.token, signed.account_id)
    except (AccountServiceError, KeychainError) as err:
        log.info("the hosted engine's AI log wasn't collected this run: %s", err)


def held_rules(store: LocalStore) -> int:
    """Blocks another computer removed that stay on here until the user unblocks them here."""
    return sum(1 for kind, _, remote, local in store.sync_rows().values()
               if kind == "rule" and local is not None and not json.loads(remote)["blocked"])


def held_switches(store: LocalStore) -> int:
    """Connections another computer switched back to block rules that stay on profiles here until the user does it
    here (#97)."""
    return sum(1 for kind, _, remote, local in store.sync_rows().values()
               if kind == "on_profiles" and local is not None and not json.loads(remote)["on"])


class RecoveryKeyPage:
    """A one-time page on this computer that shows the recovery key to one viewer, once (localweb: 127.0.0.1, a
    secret path). The browser is never started with that address: any process on the computer can read a program's
    arguments. It is started with a file only this user can read (0600, in a 0700 folder) that sends it on to the
    address; the file goes once the page is shown or closed."""

    def __init__(self, recovery_key: str) -> None:
        self._key = recovery_key
        self._web = LocalWeb(self, expires_in=PAGE_SECONDS)
        self._folder: Path | None = None

    def start(self) -> str:
        """Start the page; the address to hand the browser: the file's, never the page's."""
        self._web.start()
        self._folder = Path(tempfile.mkdtemp(prefix="portward-recovery-"))
        launcher = self._folder / "recovery-key.html"
        address = escape(self._web.url("/"), quote=True)
        descriptor = os.open(launcher, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            out.write(f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={address}">'
                      "<title>Portward: recovery key</title><p>Opening your recovery key...</p>")
        return launcher.as_uri()

    def _forget_launcher(self) -> None:
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)
            self._folder = None

    def close(self) -> None:
        self._web.close()
        self._forget_launcher()

    def __call__(self, request: Request, web: LocalWeb) -> Response:
        if request.method != "GET" or request.path != "/":
            return Response.text(HTTPStatus.NOT_FOUND, "Not found.")
        self._forget_launcher()  # one viewer: the address is no longer written anywhere
        body = ("<h1>Your Portward recovery key</h1>"
                f'<p><code id="recovery-key" style="font-size:1.3rem;letter-spacing:.05em">{escape(self._key)}</code>'
                "</p><p>Write it down or keep it in your password manager. It is the only way to open your synced "
                "history on a new computer if you lose this one. Portward can't show it again or recover it: it was "
                "never sent anywhere.</p><p>This page works once. You can close it now.</p>")
        return Response(HTTPStatus.OK, '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta '
                        'name="viewport" content="width=device-width, initial-scale=1"><title>Portward: recovery '
                        'key</title></head><body><main>' + body + "</main></body></html>", close_after=True)


@dataclass
class SyncTools:
    """What the sync tools share: the store, keychain, sync service, the user's browser and the out-of-band asks,
    and the recovery key page open now (at most one)."""

    store: LocalStore
    keychain: Keychain
    service: SyncService
    browser: Callable[[str], bool]
    out_of_band: OutOfBand
    pages: list[RecoveryKeyPage] = field(default_factory=list)

    def show(self, recovery: str) -> bool:
        """Open the recovery key's one-time page in the user's browser (the address never reaches the AI)."""
        while self.pages:
            self.pages.pop().close()
        page = RecoveryKeyPage(recovery)
        if self.browser(page.start()):
            self.pages.append(page)
            return True
        page.close()
        return False

    def recovery_ask(self, params: Mapping[str, Any] | None = None) -> Ask:
        """The out-of-band ask for the recovery key; also rebuilds one the other server process opened."""
        return Ask(key=RECOVERY_ASK, title="Recovery key", label="Recovery key",
                   details=(("For", "your Portward account's synced history"),),
                   done="I've entered my recovery key.",
                   save=lambda typed: restore(self.store, self.keychain, self.service, typed), kind=RECOVERY_ASK)

    async def ask_for_recovery_key(self, ctx: Context) -> CallToolResult | InputRequiredResult:
        ask = self.recovery_ask()
        texts = Texts(what="their Portward recovery key", then="call sync_now", command=restore_command(),
                      does="opens the account's synced data on this computer",
                      lead="Sync is set up on another computer, so this one needs the recovery key the user was "
                           "shown then.")
        return await self.out_of_band.ask(ctx, ask, {"sync": "needs_recovery_key"}, texts, "recovery_key_entry")

    async def turn_on(self, ctx: Context) -> CallToolResult | InputRequiredResult:
        try:
            signed = _signed_in(self.store, self.keychain)
            keys = self.service.keys(signed.token)
            key = data_key(self.store, self.keychain, self.service, signed) if keys is not None else None
        except SyncRefused as err:
            return error(str(err))
        except AccountServiceError as err:
            return error(str(_service_failed(self.store, err)))
        if keys is not None and key is None:
            return await self.ask_for_recovery_key(ctx)
        if key is not None:
            _begin_on(self.store, signed.account_id, key)
            return self.synced("Sync is on for this computer.")
        return await self._first_computer(ctx, signed)

    async def _first_computer(self, ctx: Context, signed: Signed) -> CallToolResult | InputRequiredResult:
        """The account's first keys: made here, the recovery key shown once in the user's browser."""
        try:
            key, recovery = crypto.new_data_key(), crypto.new_recovery_key()
            self.service.set_up(signed.token, key.key_id, crypto.wrap_for_recovery(key, recovery, signed.account_id),
                                crypto.wrap_for_device(key, _device_key(self.keychain), signed.account_id),
                                key.verifier)
        except SyncRefused as err:
            return error(str(err))
        except AccountServiceError as err:
            if err.status == 409:  # another computer set it up a moment ago
                return await self.ask_for_recovery_key(ctx)
            return error(str(_service_failed(self.store, err)))
        _begin_on(self.store, signed.account_id, key)
        shown = self.show(recovery)
        lead = ("Sync is on. The user's recovery key is open in their browser, once: they should write it down or "
                "keep it in a password manager; it is the only way to open synced data on a new computer. You "
                "never see it." if shown else
                "Sync is on, but the recovery key couldn't be shown in a browser. Ask the user to run this in a "
                f"terminal on this computer to get one (it replaces the unseen one):\n\n    {recovery_key_command()}")
        return self.synced(lead, recovery_key_shown=shown)

    def synced(self, lead: str, **extra: Any) -> CallToolResult:
        try:
            counts = sync_once(self.store, self.keychain, self.service)
        except SyncRefused as err:
            return reply({"sync": "on", **extra}, f"{lead} The first sync didn't run: {err}")
        return reply({"sync": "on", **extra, **counts}, f"{lead} {_counts_text(counts)}")

    def now(self) -> CallToolResult:
        try:
            counts = sync_once(self.store, self.keychain, self.service)
        except SyncRefused as err:
            return error(str(err))
        return reply({"sync": "on", **counts}, _counts_text(counts))

    def status(self) -> CallToolResult:
        current = state(self.store)
        payload = {"sync": "on" if current.get("on") else "off", "last_synced_at": current.get("last_synced_at"),
                   "stopped": current.get("stopped"), "held_rules": held_rules(self.store),
                   "held_switches": held_switches(self.store)}
        text = (f"Sync is {payload['sync']} on this computer" +
                (f", last run {payload['last_synced_at']}" if payload["last_synced_at"] else "") + "." +
                (f" It has stopped: {payload['stopped']}; everything on this computer stays." if payload["stopped"]
                 else "") + _held_text(payload["held_rules"], payload["held_switches"]))
        return reply(payload, text)

    def turn_off(self) -> CallToolResult:
        _save_state(self.store, on=False)
        return reply({"sync": "off"}, "Sync is off on this computer. Everything here stays.")

    def new_recovery_key(self) -> CallToolResult:
        try:
            recovery = new_recovery_key(self.store, self.keychain, self.service)
        except SyncRefused as err:
            return error(str(err))
        if self.show(recovery):
            return reply({"recovery_key_shown": True}, "A new recovery key is open in the user's browser, once; the "
                                                       "old one no longer works. You never see it.")
        return error("A new recovery key was made but no browser could be opened to show it. Ask the user to run "
                     f"this in a terminal on this computer (it makes another and prints it):\n\n    "
                     f"{recovery_key_command()}")


def register_sync_tools(server: MCPServer, store: LocalStore, keychain: Keychain, accounts: Accounts,
                        browser: Callable[[str], bool], out_of_band: OutOfBand,
                        service: SyncService | None = None) -> None:
    tools = SyncTools(store, keychain, service if service is not None else SyncService(accounts.service), browser,
                      out_of_band)
    out_of_band.register(RECOVERY_ASK, tools.recovery_ask)

    @server.tool(meta=out_of_band.panel_meta(), description=(
        "Turn on encrypted sync of history, saved pulls, notes, rules and profiles with the user's other computers. "
        "On the first computer it shows the user a recovery key in their browser; on another it asks the user for "
        "that key where you can't see it (never ask for it in the chat). Only call it when the user asks to sync."))
    async def sync_turn_on(ctx: Context) -> CallToolResult | InputRequiredResult:
        return await tools.turn_on(ctx)

    @server.tool(description="Sync history, saved pulls, notes, rules and profiles with the user's other computers now "
                             "(encrypted).")
    def sync_now() -> CallToolResult:
        return tools.now()

    @server.tool(description="Whether sync is on for this computer, when it last ran, and anything it is holding.")
    def sync_status() -> CallToolResult:
        return tools.status()

    @server.tool(description="Stop syncing on this computer. Nothing is deleted, here or on the sync service.")
    def sync_turn_off() -> CallToolResult:
        return tools.turn_off()

    @server.tool(description=(
        "Make a new recovery key for the user's synced data and show it to them in their browser (the old one stops "
        "working; the data key is replaced too, and the other computers move over on their next sync). Only when the "
        "user asks, e.g. they lost theirs."))
    def sync_new_recovery_key() -> CallToolResult:
        return tools.new_recovery_key()



def _held_text(held: int, switches: int = 0) -> str:
    text = ""
    if held:
        text += (f" {held} block{'s' if held != 1 else ''} removed on another computer stay on here until the user "
                 "unblocks them here.")
    if switches:
        text += (f" {switches} connection{'s' if switches != 1 else ''} switched back to block rules on another "
                 "computer stay on profiles here until the user switches it back here.")
    return text


def _counts_text(counts: dict[str, Any]) -> str:
    text = (f"Synced: {counts['sent']} sent, {counts['received']} received"
            + (f", {counts['merged']} merged (both computers' edits kept)" if counts["merged"] else "") + ".")
    if counts["unopenable"]:
        text += (f" {counts['unopenable']} synced record(s) didn't open with this account's key and were skipped; "
                 "what this computer holds for them stays.")
    if counts["too_big"]:
        text += (f" {counts['too_big']} history entr{'ies are' if counts['too_big'] != 1 else 'y is'} too big to "
                 "sync (over 256 KB encrypted) and stay on this computer only.")
    if counts["profile_copies"]:
        text += (f" {counts['profile_copies']} profile(s) were saved on two computers at once: the other computer's "
                 "version applies, and this computer's is kept as a copy (applied nowhere).")
    return text + _held_text(counts["held_rules"], counts["held_switches"])
