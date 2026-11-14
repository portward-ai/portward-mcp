"""What the tool modules share for one server run: the store, the keychain, the open asks, the held rows.

`build_server` makes one `Runtime` and hands it to each area's `register_*` function (pull_tools, panel_tools,
source_tools, history_tools, settings_tools). Nothing here talks to the AI itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.apps import Apps

from portward_free import kept, passwords, proof, results_panel, secret_entry
from portward_free.selections import Selections
from portward_free.sharing.links import ShareLinks
from portward_free.approval import Outbox
from portward_free.keychain import Keychain, KeychainError, account_for
from portward_free.secret_entry import LocalOnlyApps, OutOfBand
from portward_free.sources import KEYCHAIN_KINDS, Source, SourceError, open_source
from portward_free.storage import Connection, LocalStore
from portward_free.usage import Usage

RESULTS_PANEL_URI = results_panel.PANEL_URI
PASSWORD_PANEL_URI = secret_entry.PANEL_URI
Opener = Callable[..., AbstractContextManager[Source]]


def opener_for(keychain: Keychain, route: str = proof.DESKTOP) -> Opener:
    """Opens a stored connection for one call: `with opened(connection) as source:`. A saved database connection's
    password is read from `keychain`, unless one is passed (add_source tests a new one before saving it). On the
    hosted route (the engine Portward runs) only databases on the public internet open (sources/egress.py)."""
    public_only = route == proof.HOSTED

    @contextmanager
    def opened(connection: Connection, password: str | None = None) -> Iterator[Source]:
        if password is None and connection.kind in KEYCHAIN_KINDS:
            try:
                password = keychain.get(account_for(connection.name))
            except KeychainError as err:
                raise SourceError(str(err)) from None
            if password is None:
                raise SourceError(passwords.missing_text(connection.name))
        source = open_source(connection.kind, connection.settings, password, public_only=public_only)
        try:
            yield source
        finally:
            source.close()

    return opened


@dataclass
class Runtime:
    store: LocalStore
    keychain: Keychain
    usage: Usage
    browser: Callable[[str], bool]
    opened: Opener
    apps: Apps = field(default_factory=Apps)
    out_of_band: OutOfBand = field(init=False)  # open asks and held rows are in the store: a client can run two
    outbox: Outbox = field(default_factory=Outbox)  # server processes on it (one for the AI, one for the panel)
    panel_pulls: results_panel.HeldPulls = field(init=False)
    selections: Selections = field(init=False)  # what the user put in the chat, for the AI's get_selection
    share_links: ShareLinks = field(init=False)  # a new share's link, for the share panel (never the AI)
    app_only: set[str] = field(default_factory=set)  # tools only the panel calls; never logged, never the AI's
    local_only: bool = False  # MCP over HTTP (#102): secrets are asked for in a terminal on this computer only
    route: str = proof.DESKTOP  # how the AI app reaches this server, for the AI log (#98; direct/hosted: #103/#104)
    # verified answers already re-run on connect by this server (#130, verified.recheck)
    verified_checked: set[tuple] = field(default_factory=set)
    accounts: Any = None  # account.Accounts: the local page's profile editor checks the plan with it (#126)
    local_web: Any = None  # localweb.LocalWeb serving the Portward page, once open_page or add_source started it

    def __post_init__(self) -> None:
        if self.local_only:
            self.apps = LocalOnlyApps()
        self.out_of_band = OutOfBand(self.store, local_only=self.local_only)
        self.panel_pulls = results_panel.HeldPulls(self.store, self.keychain)
        self.selections = Selections(self.store, self.keychain)
        self.share_links = ShareLinks(self.store, self.keychain)

    def app_tool(self, name: str, description: str, resource_uri: str = RESULTS_PANEL_URI) -> Any:
        """Register a panel-only tool: the host keeps it (and its results) from the model."""
        self.app_only.add(name)
        return self.apps.tool(resource_uri=resource_uri, visibility=["app"], name=name, description=description)

    def prune(self) -> None:
        """Drop kept rows older than KEEP_DAYS, and AI log summary entries older than a year: at start, and before
        rows are kept or read (servers run for days)."""
        self.store.prune_kept_rows(datetime.now(UTC) - timedelta(days=kept.KEEP_DAYS))
        self.store.reads.prune(proof.cutoff_day())
