"""An MCP client test harness for the free tool.

Drives the real server the way an AI client does (in process, or over stdio like Claude Code), with panel support
(MCP Apps) advertised or not, and exposes the three things the tests assert on:

- `Reply.ai`: what reaches the AI (the text content and the structured content);
- `Reply.panel`: what the results panel receives (the whole tool result), or None when there is no panel;
- `Harness.stored(table)`: what is written to the local database file in the app-data folder.

`call` is the AI calling a tool; it can't reach app-only tools (`_meta.ui.visibility == ["app"]`), which a host keeps
from the model. `call_from_panel` is the results panel calling a tool through the host (MCP Apps `tools/call`): its
result goes to the panel, never to the model.

    async with Harness.start(tmp_path, panel=False) as h:
        await h.call("add_source", name="shop", kind="sqlite", path=str(db))
        reply = await h.call("query", source="shop", sql="SELECT ...", question="...")
        assert reply.ai.structured["rows"] == [...]
        assert h.stored("pulls")[0]["sql"] == "SELECT ..."
"""

from __future__ import annotations

import io
import sqlite3
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mcp import Client, StdioServerParameters
from mcp.client.session import ElicitationFnT
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.types import CallToolResult, ElicitResult, TextContent

import setup_form
from memory_keyring import server_env

from portward_free import cli
from portward_free.account import Accounts, AccountService
from portward_free.asks import ALLOW, PANEL_META
from portward_free.keychain import Keychain, KeychainError
from portward_free.paths import HOME_ENV, database_path
from portward_free.server import build_server
from portward_free.setup_html import PATH as SETUP_PATH
from portward_free.sharing import ShareService, Sharing
from portward_free.storage import LocalStore
from portward_free.usage import Sender as UsageSender
from portward_free.usage import Usage

Transport = Literal["inprocess", "stdio"]
FILE_QUESTION = "Add the file "  # how file_sources.question starts
Protocol = Literal["modern", "legacy"]  # 2026-07-28 (input_required rounds) or the initialize handshake


class MemoryKeychain:
    """A stand-in for the OS keychain (satisfies `portward_free.keychain.Keychain`). `entries` is what it holds."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    @property
    def passwords(self) -> dict[str, str]:
        """The connection passwords it holds (`entries` also holds the key that seals stored results)."""
        return {k: v for k, v in self.entries.items() if k.startswith("connection:")}

    def get(self, account: str) -> str | None:
        return self.entries.get(account)

    def set(self, account: str, secret: str) -> None:
        self.entries[account] = secret

    def delete(self, account: str) -> None:
        self.entries.pop(account, None)


class LockedKeychain(MemoryKeychain):
    """A keychain that can't be used (locked, or no keychain on this machine)."""

    def get(self, account: str) -> str | None:
        raise KeychainError("the keychain is locked")

    def set(self, account: str, secret: str) -> None:
        raise KeychainError("the keychain is locked")


def file_bytes(home: Path) -> bytes:
    """The local database file and its journal or write-ahead log, if any, as raw bytes."""
    return b"".join(p.read_bytes() for p in home.iterdir() if p.name.startswith("portward.db"))


@dataclass(frozen=True)
class AiView:
    """What the model is given: text content plus structured content. `_meta` never reaches the model."""

    text: str
    structured: dict[str, Any] | None


@dataclass(frozen=True)
class Terminal:
    code: int  # the command's exit code
    out: str  # everything it showed the user, prompts included


@dataclass(frozen=True)
class Reply:
    is_error: bool
    ai: AiView
    panel: CallToolResult | None  # what an MCP Apps panel is handed; None when the client has no panel
    tool_meta: dict[str, Any] | None  # the called tool's `_meta` (e.g. its bound ui:// resource)


class NoMail:
    """A mail app that opens nothing (tests that don't share)."""

    def open(self, to: str, subject: str, body: str) -> bool:
        return False


# Tests that don't share point at a share service nobody runs (port 9 on this machine): nothing leaves it.
NO_SHARING = Sharing(ShareService("http://127.0.0.1:9"), NoMail())
NO_ACCOUNTS = Accounts(AccountService("http://127.0.0.1:9"))  # likewise for the Personal account


class Harness:
    def __init__(self, client: Client, home: Path, panel: bool, keychain: Keychain | None = None,
                 sharing: Sharing | None = None, accounts: Accounts | None = None,
                 files: FileQuestions | None = None) -> None:
        self.client = client
        self.files = files if files is not None else FileQuestions()
        self.usage: Callable[[LocalStore], Usage] = _no_usage  # the server's usage counts, for terminal commands
        self.accounts = accounts
        self.home = home
        self.panel = panel
        self.keychain = keychain  # the in-process server's keychain; None over stdio
        self.sharing = sharing
        self.setup_pages: list[str] = []  # setup pages (#118) the server opened, newest last
        self._tools: dict[str, Any] | None = None
        self._told: list[str] = []  # the server's notifications to the client, by method (in process only)

    def notifications(self) -> list[str]:
        """The methods of the notifications the server sent this client so far (e.g. tools/list_changed)."""
        return list(self._told)

    @classmethod
    @asynccontextmanager
    async def start(cls, home: Path, *, panel: bool = False, transport: Transport = "inprocess",
                    elicitation: ElicitationFnT | None = None,
                    protocol: Protocol = "modern", keychain: Keychain | None = None,
                    usage_sender: UsageSender | None = None,
                    clock: Callable[[], datetime] | None = None,
                    browser: Callable[[str], bool] | None = None,
                    sharing: Sharing | None = None,
                    accounts: Accounts | None = None, setup_page: bool = False) -> AsyncIterator[Harness]:
        """Start the server with `home` as its app-data folder, and connect a client to it.

        `elicitation` answers the server's questions to the user; passing one advertises elicitation support.
        `protocol="legacy"` connects with the initialize handshake (as clients like Claude Code do today), where the
        server sends its questions as requests of its own instead of input_required results.
        In process, the server's keychain is `keychain` (a fresh empty one if None). Over stdio the server process
        gets an in-memory keyring backend (tests/memory_keyring.py), empty on every start, so no test touches the real
        OS keychain.
        Usage counts are off (as if PORTWARD_USAGE=off) unless `usage_sender` is given: it gets what would be posted
        to the collection endpoint, with `clock` as the time. A stdio server always runs with them off.
        `browser` stands in for the user's web browser (open_page hands it the local page's address); by default
        nothing is opened. `setup_page`: the setup page (#118) opens when add_source asks for it, and the harness
        goes through it as the user (add_file, connect_database); otherwise it doesn't open and the other ways apply.
        """
        mode = "legacy" if protocol == "legacy" else "auto"
        extensions = [advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})] if panel else None
        files = FileQuestions()
        elicitation = files.wrap(elicitation) if elicitation is not None else None
        if transport == "stdio":
            env = server_env(**{HOME_ENV: str(home)})
            server: Any = StdioServerParameters(command=sys.executable, args=["-m", "portward_free"], env=env)
            async with Client(server, extensions=extensions, elicitation_callback=elicitation, mode=mode) as client:
                yield cls(client, home, panel, files=files)
            return
        store = LocalStore.open(database_path(home))
        keychain = keychain if keychain is not None else MemoryKeychain()
        sharing = sharing if sharing is not None else NO_SHARING
        try:
            usage = (Usage(store, usage_sender, now=clock or (lambda: datetime.now(UTC))) if usage_sender is not None
                     else Usage(store, None, disabled="usage counts are off in tests"))
            setup_pages: list[str] = []

            def opening(url: str) -> bool:
                """The setup page (#118) opens only for a test that asks for it (`setup_page`); the harness then
                plays the user there (add_file, connect_database). The test's browser sees every other page."""
                if f"/{SETUP_PATH.strip('/')}?" in url:
                    setup_pages.append(url)
                    return setup_page
                return browser(url) if browser is not None else False

            told: list[str] = []

            async def heard(message: Any) -> None:
                method = getattr(getattr(message, "root", message), "method", None)
                if isinstance(method, str):
                    told.append(method)

            server = build_server(store, keychain, usage=usage, browser=opening,
                                  sharing=sharing,
                                  accounts=accounts if accounts is not None else NO_ACCOUNTS)
            async with Client(server, extensions=extensions, elicitation_callback=elicitation, mode=mode,
                              message_handler=heard) as client:
                harness = cls(client, home, panel, keychain, sharing, accounts, files=files)
                harness._told = told
                harness.usage = usage.factory()
                harness.setup_pages = setup_pages
                yield harness
        finally:
            store.close()

    async def tools(self) -> dict[str, Any]:
        if self._tools is None:
            self._tools = {t.name: t for t in (await self.client.list_tools()).tools}
        return self._tools

    def forget_tools(self) -> None:
        """Read the tool list from the server again next time, as a client does when told the list changed (#116)."""
        self._tools = None

    async def ai_tools(self) -> dict[str, Any]:
        """The tools the model is shown: app-only tools are left out, as a host does."""
        return {name: t for name, t in (await self.tools()).items() if not _app_only(t.meta)}

    async def call(self, tool: str, **arguments: Any) -> Reply:
        """The AI calls a tool. An app-only tool is refused, as a host refuses it. A tool the list doesn't have is
        looked for once more (the list may have changed since it was read, #116), then called anyway: the server
        answers for a tool it doesn't list."""
        if tool not in await self.tools():
            self.forget_tools()
        found = (await self.tools()).get(tool)
        if found is not None and _app_only(found.meta):
            raise AssertionError(f"{tool} is app-only: the model can't call it")
        return await self._call(tool, arguments)

    async def call_from_panel(self, tool: str, **arguments: Any) -> Reply:
        """The results panel calls a tool through the host. Only a client with panel support has a panel."""
        if not self.panel:
            raise AssertionError("no panel: the client didn't advertise MCP Apps support")
        return await self._call(tool, arguments)

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Reply:
        result = await self.client.call_tool(tool, arguments)
        text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
        listed = (await self.tools()).get(tool)
        meta = listed.meta if listed is not None else None
        has_panel = self.panel and bool(meta and meta.get("ui", {}).get("resourceUri"))
        return Reply(
            is_error=bool(result.is_error),
            ai=AiView(text=text, structured=result.structured_content),
            panel=result if has_panel else None,
            tool_meta=meta,
        )

    async def form_in_panel(self, asked: Reply, host_passes_meta: bool = False) -> dict[str, Any]:
        """The panel form's ask for a reply that asked the user in the panel. Hosts like Claude Desktop don't hand
        the panel the result's `_meta`: the panel then reads the ask with the app-only `secret_request` tool and the
        reply's public `request_id`. `host_passes_meta` reads it from `_meta` instead, as other hosts can."""
        if asked.panel is None:
            raise AssertionError("no panel was shown with this reply")
        if host_passes_meta:
            return asked.panel.meta[PANEL_META]
        form = await self.call_from_panel("secret_request", request_id=asked.ai.structured["request_id"])
        if form.is_error:
            raise AssertionError(form.ai.text)
        return form.ai.structured

    def type_password_in_terminal(self, name: str, password: str) -> Terminal:
        """The user runs the command a reply named (`portward-free password <name>`) and types `password` at its
        hidden prompt. Runs in process against the same app-data folder and keychain as the server."""
        if self.keychain is None:
            raise AssertionError("only in process: over stdio the server's keychain lives in its own process")
        out = io.StringIO()
        prompts: list[str] = []

        def typed(prompt: str) -> str:
            prompts.append(prompt)
            return password

        code = cli.main(["password", name], home=self.home, keychain=self.keychain, prompt=typed, out=out)
        return Terminal(code=code, out="".join(prompts) + out.getvalue())

    def run_sync_command(self, command: str, typed: str = "", at_terminal: bool = True) -> Terminal:
        """The user runs `portward-free sync-restore` (typing `typed` at its hidden prompt) or `sync-recovery-key`,
        in process, against the same app-data folder, keychain and account service as the server."""
        if self.keychain is None or self.accounts is None:
            raise AssertionError("only in process, with an account service")
        out = io.StringIO()
        prompts: list[str] = []

        def prompt(text: str) -> str:
            prompts.append(text)
            return typed

        code = cli.main([command], home=self.home, keychain=self.keychain, prompt=prompt, out=out,
                        account_service=self.accounts.service, is_terminal=lambda: at_terminal)
        return Terminal(code=code, out="".join(prompts) + out.getvalue())

    def type_share_code_in_terminal(self, code: str) -> Terminal:
        """The user runs `portward-free share-code` (the command share_sign_in named) and types `code` at its hidden
        prompt. In process, against the same app-data folder, keychain and share service as the server."""
        if self.keychain is None or self.sharing is None:
            raise AssertionError("only in process")
        out = io.StringIO()
        prompts: list[str] = []

        def typed(prompt: str) -> str:
            prompts.append(prompt)
            return code

        result = cli.main(["share-code"], home=self.home, keychain=self.keychain, prompt=typed, out=out,
                          share_service=self.sharing.service)
        return Terminal(code=result, out="".join(prompts) + out.getvalue())

    def add_file_in_terminal(self, name: str, kind: str, path: str, typed: str | None = None,
                             at_terminal: bool = True) -> Terminal:
        """The user runs `portward-free add-file <name> <kind> <path>` (the command add_source named) and types
        `typed` (the source's name unless given) to confirm. In process, against the same app-data folder."""
        out = io.StringIO()
        prompts: list[str] = []

        def prompt(text: str) -> str:
            prompts.append(text)
            return name if typed is None else typed

        code = cli.main(["add-file", name, kind, path], home=self.home, keychain=self.keychain or MemoryKeychain(),
                        prompt=prompt, out=out, is_terminal=lambda: at_terminal, usage=self.usage)
        return Terminal(code=code, out="".join(prompts) + out.getvalue())

    async def add_file(self, name: str, path: Path | str, kind: str = "sqlite") -> Reply:
        """The AI adds a file source and the user says yes where Portward asked them: the client's prompt, the
        panel's form (pressing Allow) or the terminal command. Returns add_source's reply (with the tables only
        when the client's prompt asked: call test_source for them otherwise)."""
        added = await self.call("add_source", name=name, kind=kind, path=str(path))
        if added.is_error or "added" in (added.ai.structured or {}):
            return added
        if added.ai.structured.get("setup_entry") == "page":  # the setup page: saved with no column kept
            setup_form.finish(self.setup_pages[-1])
            return added
        entry = added.ai.structured["confirm_entry"]
        if entry == "panel":
            token = (await self.form_in_panel(added))["token"]
            saved = await self.call_from_panel("save_secret", request=token, secret=ALLOW)
            if saved.is_error:
                raise AssertionError(saved.ai.text)
        elif entry == "terminal":
            ran = self.add_file_in_terminal(name, kind, str(path))
            if ran.code != 0:
                raise AssertionError(ran.out)
        else:
            raise AssertionError(f"can't confirm a file asked for in the {entry}")
        return added

    async def connect_database(self, name: str, fields: dict[str, Any], password: str) -> Reply:
        """Add a database source the way a user does without a panel: the AI calls add_source with its settings,
        the user types the password in the terminal. Returns test_source's reply (the tables and any warnings)."""
        added = await self.call("add_source", name=name, **fields)
        if added.is_error:
            raise AssertionError(added.ai.text)
        if added.ai.structured.get("setup_entry") == "page":
            setup_form.finish(self.setup_pages[-1], password=password)
        else:
            self.type_password_in_terminal(name, password)
        return await self.call("test_source", name=name)

    def choose_mode(self, name: str, mode: str) -> None:
        """The user chooses `name`'s mode (#117: 'results' or 'pick') at setup, on the computer. Written to the same
        local file the server reads."""
        store = LocalStore.open(database_path(self.home))
        try:
            store.set_send_mode(name, mode)
        finally:
            store.close()

    def stored(self, table: str) -> list[dict[str, Any]]:
        """Every row of one table in the local database file, read straight from disk."""
        uri = f"{database_path(self.home).as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def _no_usage(store: LocalStore) -> Usage:
    return Usage(store, None, disabled="usage counts are off in tests")


class FileQuestions:
    """Answers Portward's "add the file?" prompt with yes (the user adding a file the test wants),
    while `answer` is True; every other question goes to the test's own elicitation callback."""

    def __init__(self) -> None:
        self.answer = True

    def wrap(self, elicitation: ElicitationFnT) -> ElicitationFnT:
        async def answering(context: Any, params: Any) -> Any:
            if self.answer and getattr(params, "message", "").startswith(FILE_QUESTION):
                return ElicitResult(action="accept", content={"yes": True})
            return await elicitation(context, params)

        return answering


def _app_only(meta: dict[str, Any] | None) -> bool:
    return (meta or {}).get("ui", {}).get("visibility") == ["app"]


@asynccontextmanager
async def connected(home: Path, sqlite_file: Path, *, name: str = "shop", panel: bool = False,
                    transport: Transport = "inprocess",
                    elicitation: ElicitationFnT | None = None,
                    protocol: Protocol = "modern",
                    keychain: Keychain | None = None,
                    usage_sender: UsageSender | None = None,
                    clock: Callable[[], datetime] | None = None,
                    browser: Callable[[str], bool] | None = None,
                    sharing: Sharing | None = None,
                    accounts: Accounts | None = None,
                    mode: str | None = None, setup_page: bool = False) -> AsyncIterator[Harness]:
    """A harness with one SQLite source already added under `name`, in `mode` (#117: 'results' or 'pick') as the
    user chose it at setup; with none chosen it picks, as every source made before modes does."""
    async with Harness.start(home, panel=panel, transport=transport, elicitation=elicitation,
                             protocol=protocol, keychain=keychain, usage_sender=usage_sender, clock=clock,
                             browser=browser, sharing=sharing, accounts=accounts, setup_page=setup_page) as h:
        added = await h.add_file(name, sqlite_file)
        if added.is_error:
            raise AssertionError(added.ai.text)
        if mode is not None:
            h.choose_mode(name, mode)
        yield h
