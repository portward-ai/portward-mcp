"""`portward-free`: run the local MCP server over stdio, with its database in the app-data folder.

The other commands are the ones people run by hand, never through the AI: `password <name>` asks for a database
connection's password with hidden input, tests the connection and saves it in the keychain (see `passwords`);
`add-file <name> <kind> <path>` lets the AI read a file once the user types its name (see `file_sources`);
`share-code` takes the sharing code; sync (#55) has `sync-restore` (the recovery key, hidden) and `sync-recovery-key`
(makes a new one and prints it here); `approve-browser` takes the code the report page shows (#107); `remove-source`
removes a connection once the user types its name (what the web AI asks for instead of removing, #102); `verify`
checks a signed proof report (#135).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from portward_free import __version__, file_sources, offers, proof, signed_report
from portward_free.account import SIGN_IN_SECONDS, Accounts, AccountService, SignIn, account_token
from portward_free.asks import Refused
from portward_free.browser_report import approve as approve_browser
from portward_free.remote.routes import HOSTED_LABEL as LABEL
from portward_free.hosted.engine import Engine, EngineRefused
from portward_free.keychain import Keychain, KeychainError, OsKeychain, account_for
from portward_free.localweb import LocalWeb
from portward_free.page import PortwardPage
from portward_free.passwords import PasswordRefused, check_and_save, needs_password
from portward_free.paths import database_path
from portward_free.remote import RemoteMcp, RemoteRefused
from portward_free.remote.direct import DIRECT_PORT, Direct
from portward_free.server import build_server, opener_for
from portward_free.sharing import ShareService
from portward_free.sharing.tools import confirm_code, pending
from portward_free.secret_entry import OutOfBand
from portward_free.source_tools import file_settings, remove_connection
from portward_free.sources import SourceError
from portward_free.storage import Connection, LocalStore, NewerFileError
from portward_free.sync.service import SyncService
from portward_free.sync.tools import SyncRefused, new_recovery_key, restore
from portward_free.usage import QUESTION, Usage
from portward_free.wrap.providers import PROVIDERS
from portward_free.wrap.server import WrapRefused, build_wrap_server
from portward_free.wrap.setup import ClientConfigs, add_wrapped

PASSWORD_TRIES = 3
DEFAULT_HTTP_PORT = 8765
# serve-http is served from this computer to an AI app on the web, as serve-direct is (only without the relay), so
# its AI log entries and usage counts say "direct": Portward never sees the data on either.
HTTP_ROUTE = proof.DIRECT
FILE_KINDS = ("sqlite", "csv", "excel")


class Outcome(Protocol):
    def summary(self) -> str: ...


@dataclass(frozen=True)
class Terminal:
    """One run of a hand-run command: where it reads and writes, and the services it may need."""

    store: LocalStore
    keychain: Keychain
    prompt: Callable[[str], str]
    out: TextIO
    is_terminal: Callable[[], bool]
    usage: Usage
    share_service: ShareService | None = None
    account_service: AccountService | None = None

    def say(self, text: str) -> None:
        print(text, file=self.out)

    def sync(self) -> SyncService:
        return SyncService(self.account_service if self.account_service is not None else AccountService.from_env())

    def retry(self, label: str, attempt: Callable[[str], Outcome], refused: str) -> int:
        """Ask for a hidden answer up to PASSWORD_TRIES times; `attempt` raises Refused for a wrong one."""
        for _ in range(PASSWORD_TRIES):
            try:
                done = attempt(self.prompt(label))
            except Refused as err:
                self.say(f"{refused}: {str(err).removeprefix(f'{refused}: ')}")
                continue
            except (EOFError, KeyboardInterrupt):
                break
            self.say(done.summary())
            return 0
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portward-free",
        description="Run Portward's local MCP server over stdio. Your AI app starts it; you don't run it by hand.",
    )
    parser.add_argument("--version", action="version", version=f"portward {__version__}")
    commands = parser.add_subparsers(dest="command")
    password = commands.add_parser("password", help="type a database connection's password (hidden) and save it "
                                                    "in the keychain")
    password.add_argument("name", help="the source's name, as the AI added it")
    add_file = commands.add_parser("add-file", help="let the AI read a file (SQLite, CSV or Excel) as a source")
    add_file.add_argument("name", help="the source's name")
    add_file.add_argument("kind", choices=FILE_KINDS)
    add_file.add_argument("path", help="the file")
    remove = commands.add_parser("remove-source", help="remove a connection and its saved password (what the AI "
                                                       "on the web asks you to run here)")
    remove.add_argument("name", help="the source's name")
    hosted = commands.add_parser("hosted-allow", help="Personal: say yes, on this computer, to sending one database "
                                                      "connection's login to Portward's hosted engine (the hosted "
                                                      "route). Nothing is sent to the engine without it.")
    hosted.add_argument("name", help="the source's name")
    commands.add_parser("share-code", help="type the code Portward emailed you (hidden) to confirm your email for "
                                           "sharing")
    commands.add_parser("page", help="Open your Portward page (history, saved pulls, rules, settings, what the AI "
                                     "was sent) in your browser, served from this computer only, until Ctrl-C.")
    commands.add_parser("sign-in", help="Sign this computer in to your Portward Personal account in your browser "
                                        "(the token is kept in your keychain).")
    serve = commands.add_parser("serve-http", help="Personal: serve Portward's MCP over HTTP for claude.ai and "
                                                   "ChatGPT on the web, open only to your Portward account, until "
                                                   "Ctrl-C or Personal ends. Passwords are typed on this computer only.")
    serve.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="the port on this computer")
    serve.add_argument("--public-url", help="the address connectors are given (default http://127.0.0.1:<port>)")
    wrap_add = commands.add_parser("wrap-add", help="Personal: put Portward in front of a Postgres MCP or Snowflake "
                                                    "MCP server you use, so your profiles apply to it. Run it "
                                                    "yourself in a terminal; the login (DATABASE_URI for Postgres, "
                                                    "SNOWFLAKE_PAT for Snowflake) is read from the environment or "
                                                    "asked for (hidden) and kept in your keychain.")
    wrap_add.add_argument("name", help="a name for it, as your profiles are applied to it")
    wrap_add.add_argument("--provider", choices=sorted(PROVIDERS), default="postgres")
    wrap_add.add_argument("--url", help="Snowflake-managed MCP server: its URL (https://<account>."
                                        "snowflakecomputing.com/api/v2/databases/<db>/schemas/<schema>/mcp-servers/"
                                        "<name>)")
    wrap_add.add_argument("server_command", nargs="*",
                          help="after --, the command that starts the server (for example: -- uvx postgres-mcp, or "
                               "-- uvx snowflake-labs-mcp --account <account> --user <user> --warehouse <warehouse>)")
    wrap = commands.add_parser("wrap", help="Run a wrapped server for your AI app (it starts this, in place of the "
                                            "original server)")
    wrap.add_argument("name", help="the name given to wrap-add")
    direct = commands.add_parser("serve-direct", help="Personal: let claude.ai and ChatGPT on the web reach this "
                                                      "computer through Portward's relay (the direct route), with "
                                                      "TLS ending here, until Ctrl-C or Personal ends.")
    direct.add_argument("--port", type=int, default=DIRECT_PORT, help="the local port the relay client forwards to")
    commands.add_parser("serve-hosted", help="The hosted engine (Portward runs this): the container's command. "
                                            "Its settings come from the environment its host gives it.")
    commands.add_parser("sync-restore", help="type your sync recovery key (hidden) to open your synced history on "
                                             "this computer")
    commands.add_parser("sync-recovery-key", help="make a new sync recovery key and show it here (the old one stops "
                                                  "working)")
    commands.add_parser("approve-browser", help="type the code your browser's report page shows, to let it build "
                                                "the proof report from your AI log (Personal)")
    verify = commands.add_parser("verify", help="check a signed proof report (its PDF, CSV or signature file): "
                                                "that it came from a computer whose key is registered to your "
                                                "account, unedited")
    verify.add_argument("report", help="the report's PDF, CSV or .signature.json file")
    verify.add_argument("--public-key", help="check against this public key (from the account owner) instead of "
                                             "the keys registered to the account this computer is signed in to")
    return parser


def main(argv: list[str] | None = None, *, home: Path | None = None, keychain: Keychain | None = None,
         prompt: Callable[[str], str] = getpass.getpass, out: TextIO | None = None,
         share_service: ShareService | None = None, account_service: AccountService | None = None,
         is_terminal: Callable[[], bool] | None = None,
         usage: Callable[[LocalStore], Usage] = Usage.from_env) -> int:
    arguments = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout carries the MCP protocol
    if arguments.command == "serve-hosted":  # #104: in a container; its own file and in-memory keychain
        return serve_hosted(os.environ, out or sys.stdout)
    try:
        store = LocalStore.open(database_path(home))
    except NewerFileError as err:
        sys.exit(f"portward: {err}")
    if arguments.command == "page":
        store.close()
        return _serve_page()
    try:
        terminal = Terminal(store, keychain if keychain is not None else OsKeychain(), prompt, out or sys.stdout,
                            is_terminal if is_terminal is not None else _is_terminal, usage(store), share_service,
                            account_service)
        return _run(terminal, arguments)
    finally:
        store.close()


def _run(t: Terminal, arguments: argparse.Namespace) -> int:
    commands: dict[str, Callable[[], int]] = {
        "sign-in": lambda: _sign_in(t.store, t.keychain),
        "password": lambda: enter_password(t, arguments.name),
        "add-file": lambda: add_file(t, arguments.name, arguments.kind, arguments.path),
        "remove-source": lambda: remove_source(t, arguments.name),
        "hosted-allow": lambda: hosted_allow(t, arguments.name),
        "sync-restore": lambda: enter_recovery_key(t),
        "sync-recovery-key": lambda: show_new_recovery_key(t),
        "approve-browser": lambda: enter_browser_code(t),
        "share-code": lambda: enter_share_code(t),
        "serve-http": lambda: serve_http(t, arguments.port, arguments.public_url),
        "wrap-add": lambda: wrap_add(t, arguments.name, arguments.provider, arguments.server_command,
                                     url=arguments.url),
        "serve-direct": lambda: serve_direct(t, arguments.port),
        "verify": lambda: verify_report(t, Path(arguments.report), arguments.public_key),
    }
    if arguments.command == "wrap":
        return run_wrap(t, arguments.name)
    command = commands.get(arguments.command)
    if command is not None:
        _ask_about_usage(t)
        return command()
    build_server(t.store, t.keychain, usage=Usage.from_env(t.store)).run("stdio")
    return 0


def _ask_about_usage(t: Terminal) -> None:
    """A person at a terminal sees the usage question here (once, before anyone asked them elsewhere). Output that
    isn't a terminal may be an AI's capture, which doesn't count as the user seeing it."""
    if t.is_terminal() and t.usage.seen("terminal"):
        t.say(f"{QUESTION} To turn them off now: tell your AI app, or run portward-free page.\n")


def _sign_in(store: LocalStore, keychain: Keychain) -> int:
    """The account sign-in from a terminal: the address goes to the user's browser and this terminal only."""
    sign_in = SignIn(Accounts.from_env(), keychain, store)
    url = sign_in.start()
    opened = webbrowser.open(url)
    print(f"{'Opened' if opened else 'Open'} this in your browser to sign in: {url}", flush=True)
    try:
        sign_in.finished.wait(SIGN_IN_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        sign_in.stop()
    print(f"Signed in as {sign_in.email}." if sign_in.email else "Not signed in.", flush=True)
    return 0 if sign_in.email else 1


def enter_password(t: Terminal, name: str) -> int:
    connection = t.store.connection(name)
    if connection is None:
        t.say(f"portward: no source named {name!r}; ask the AI to add it first")
        return 2
    try:
        needs_password(connection)
    except PasswordRefused as err:
        t.say(f"portward: {err}")
        return 2
    settings = connection.settings
    where = f"{settings.get('user')}@{settings.get('host')}/{settings.get('database')}"
    t.say(f"Password for {name!r} ({connection.kind}, {where}). It is saved in this computer's keychain only.")
    return t.retry("Password (hidden): ", lambda typed: check_and_save(connection, typed, t.keychain), "Not saved")


def add_file(t: Terminal, name: str, kind: str, path: str) -> int:
    """Only for a person at a terminal: an AI that can run commands must not add a file this way by itself."""
    if not t.is_terminal():
        t.say("portward: run this yourself in a terminal; it lets the AI read a file, so it won't run with its "
              "input or output redirected")
        return 2
    connection = Connection(name, kind, file_settings(path))
    found = file_sources.open_problem(opener_for(t.keychain), connection, t.store.rules_for(name))
    if isinstance(found, str):
        t.say(f"portward: {found}")
        return 2
    t.say(f"This lets the AI read {found.path} as the {kind} source {name!r}. Your block rules still apply.")

    def confirmed(typed: str) -> file_sources.FileAdded:
        file_sources.typed_name(connection, typed)
        try:
            file_sources.save(t.store, t.keychain, connection)
        except KeychainError as err:
            raise Refused(str(err)) from None
        return found

    return t.retry(f"Type the source's name, {name}, to go on: ", confirmed, "Not added")


@dataclass(frozen=True)
class Removed:
    name: str

    def summary(self) -> str:
        return f"Removed {self.name!r} and its saved password."


def hosted_allow(t: Terminal, name: str) -> int:
    """Only for a person at a terminal: the user's yes to a connection's login going to Portward's hosted engine.
    It records the yes and sends nothing itself; the switch is then made by web_route or the account page's ask."""
    from portward_free.hosted.route import HostedRoute, RouteRefused  # not at the top: only this command needs it
    from portward_free.hosted.service import HostedService

    if not t.is_terminal():
        t.say("portward: run this yourself in a terminal; it lets a database login leave this computer, so it "
              "won't run with its input or output redirected")
        return 2
    connection = t.store.connection(name)
    if connection is None:
        t.say(f"portward: no source named {name!r}")
        return 2
    account = t.account_service if t.account_service is not None else AccountService.from_env()
    route = HostedRoute(t.store, t.keychain, HostedService(account.base_url))
    settings = connection.settings
    t.say(f"This allows {name!r}'s login ({settings.get('user')}@{settings.get('host')}:{settings.get('port')}/"
          f"{settings.get('database')}) and its password to go to Portward's hosted engine, which Portward runs: "
          "Portward can technically see that login and what is read through it. Nothing is sent now; it is sent "
          "when you or your AI app put the connection on the hosted route.")

    def confirmed(typed: str) -> HostedAllowed:
        if typed.strip() != name:
            raise Refused(f"type the source's name, {name}, to allow it")
        try:
            route.allow(name)
        except RouteRefused as err:
            raise Refused(str(err)) from None
        return HostedAllowed(name)

    return t.retry(f"Type {name} to allow it: ", confirmed, "Not allowed")


@dataclass(frozen=True)
class HostedAllowed:
    name: str

    def summary(self) -> str:
        return (f"Allowed. {self.name} can be put on the hosted route now (ask your AI app, or choose it on your "
                "account page). Take it off the route to withdraw this.")


def remove_source(t: Terminal, name: str) -> int:
    """Only for a person at a terminal: the web AI's remove_source names this command instead of removing."""
    if not t.is_terminal():
        t.say("portward: run this yourself in a terminal; it removes a connection, so it won't run with its input "
              "or output redirected")
        return 2
    if t.store.connection(name) is None:
        t.say(f"portward: no source named {name!r}")
        return 2
    t.say(f"This removes the source {name!r} and its saved password from this computer.")

    def confirmed(typed: str) -> Removed:
        if typed.strip() != name:
            raise Refused(f"type the source's name, {name}, to remove it")
        try:
            remove_connection(t.store, t.keychain, name)
        except KeychainError as err:
            raise Refused(str(err)) from None
        OutOfBand(t.store).close(account_for(name))  # an ask for its password still open in the AI app's panel
        return Removed(name)

    return t.retry(f"Type the source's name, {name}, to remove it: ", confirmed, "Not removed")


def _serve_page() -> int:
    """Serve the page on this thread. The address goes to the user's browser and this terminal, never to the AI."""
    keychain = OsKeychain()
    page = PortwardPage(database_path(), keychain, opener_for(keychain), Usage.from_env,
                        accounts=Accounts(AccountService.from_env()))
    web = LocalWeb(page).bind()
    url = web.url()
    opened = webbrowser.open(url)
    print(f"Your Portward page is {'open' if opened else 'ready'} at {url}", flush=True)
    print("It is served from this computer only. Press Ctrl-C to stop.", flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        web.stop()
        page.close()
    return 0


def serve_http(t: Terminal, port: int, public_url: str | None) -> int:
    """#102: MCP over HTTP with Portward sign-in, on this thread. Secrets are still typed only on this computer."""
    accounts = Accounts(t.account_service if t.account_service is not None else AccountService.from_env())
    try:
        remote = RemoteMcp(t.store, t.keychain, accounts, public_url=public_url or f"http://127.0.0.1:{port}",
                           port=port, usage=t.usage.routed(HTTP_ROUTE), route=HTTP_ROUTE)
        t.say(f"Starting Portward over HTTP at {remote.resource}, open only to your Portward account. Ctrl-C "
              "stops it.")
        remote.serve_forever()
    except RemoteRefused as err:
        return _refused_on_the_web(t, accounts, err)
    except ValueError as err:
        t.say(f"portward: {err}")
        return 1
    t.say(remote.stopped_because or "Stopped.")
    return 0 if remote.stopped_because in (None, "stopped") else 1


def _refused_on_the_web(t: Terminal, accounts: Accounts, err: RemoteRefused) -> int:
    """The web route couldn't start; on Free, the first time, #108's offer follows the reason."""
    t.say(f"portward: {err}")
    offer = offers.web(t.store, accounts.service.base_url) if err.free else None
    if offer is not None:
        t.say(offer.text)
    return 1


def wrap_add(t: Terminal, name: str, provider: str, command: list[str], url: str | None = None,
             clients: ClientConfigs | None = None, env: Mapping[str, str] = os.environ) -> int:
    """#100/#101: only for a person at a terminal: it saves a command Portward will run (or a server's URL), and a
    database login."""
    if not t.is_terminal():
        t.say("portward: run this yourself in a terminal; it sets up a command Portward runs, so it won't run with "
              "its input or output redirected")
        return 2
    command = command[1:] if command[:1] == ["--"] else command
    try:
        PROVIDERS[provider].settings(command, url, {})  # before asking for the login
    except SourceError as err:
        t.say(f"portward: {err}")
        return 2
    label = PROVIDERS[provider].secret_env
    secret = env.get(label) or ""
    if not secret:
        try:
            secret = t.prompt(f"{label} for {name!r} (hidden; kept in this computer's keychain): ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    accounts = Accounts(t.account_service if t.account_service is not None else AccountService.from_env())
    try:
        added = add_wrapped(t.store, t.keychain, accounts, name, provider, command, secret,
                            clients=clients if clients is not None else ClientConfigs.on_this_computer(), url=url)
    except WrapRefused as err:
        t.say(f"portward: {err}")
        return 1
    t.say(added.summary())
    return 0


def run_wrap(t: Terminal, name: str) -> int:
    """The wrap server over stdio, for the AI app (stdout carries the MCP protocol: errors go to stderr)."""
    try:
        server = build_wrap_server(t.store, t.keychain, name, usage=t.usage)
    except WrapRefused as err:
        print(f"portward: {err}", file=sys.stderr)
        return 1
    server.run("stdio")
    return 0


def serve_hosted(env: Mapping[str, str], out: TextIO) -> int:
    """#104: the hosted engine, until its host stops it or Personal ends (then it exits, and the host keeps it
    stopped until a request wakes it again)."""
    logging.getLogger("portward").setLevel(logging.INFO)
    try:
        engine = Engine.from_env(dict(env))
        engine.start()
    except (EngineRefused, ValueError) as err:
        print(f"portward: {err}", file=out)
        return 1
    print(f"Portward's hosted engine is serving {engine.resource} ({LABEL}).", file=out)
    try:
        engine.wait()
    except KeyboardInterrupt:
        engine.stop()
    engine.close()
    return 0


def serve_direct(t: Terminal, port: int) -> int:
    """#103: claude.ai and ChatGPT on the web reach this computer through Portward's relay, TLS ending here."""
    accounts = Accounts(t.account_service if t.account_service is not None else AccountService.from_env())
    route = Direct(t.store, t.keychain, accounts, folder=t.store.path.parent / "direct", port=port, usage=t.usage)
    try:
        route.start()
    except RemoteRefused as err:
        return _refused_on_the_web(t, accounts, err)
    assert route.access is not None
    t.say(f"Portward is on the direct route at {route.access.address}/mcp, open only to your Portward account. Add "
          "that address as a connector in claude.ai or ChatGPT. It works while this computer is awake; Ctrl-C stops "
          "it.")
    try:
        route.wait()
    except KeyboardInterrupt:
        route.stop()
        route.wait(10)
    t.say(route.stopped_because or "Stopped.")
    return 0 if route.stopped_because in (None, "stopped") else 1


def enter_share_code(t: Terminal) -> int:
    started = pending(t.store)
    if started is None:
        t.say("portward: no email confirmation is in progress; ask the AI to start sharing first")
        return 2
    service = t.share_service if t.share_service is not None else ShareService.from_env()
    t.say(f"The 6-digit code Portward emailed to {started['email']}. It goes to Portward on this computer only.")
    return t.retry("Code (hidden): ", lambda typed: confirm_code(t.store.path, t.keychain, service, typed),
                   "Not confirmed")


def enter_recovery_key(t: Terminal) -> int:
    t.say("Your Portward recovery key (32 letters and digits). It stays on this computer.")
    sync = t.sync()
    return t.retry("Recovery key (hidden): ", lambda typed: restore(t.store, t.keychain, sync, typed), "Not opened")


def enter_browser_code(t: Terminal) -> int:
    t.say("The code your browser's report page shows (12 letters and digits). Check it is your own browser's.")
    sync = t.sync()
    return t.retry("Code: ", lambda typed: approve_browser(t.store, t.keychain, sync, typed), "Not approved")


def verify_report(t: Terminal, path: Path, public_key: str | None) -> int:
    """`portward-free verify` (#135): 0 when the report verifies, 1 when it doesn't (the reason is printed)."""
    def registered() -> dict[str, str] | None:
        token = account_token(t.keychain)
        if not token:
            return None
        service = t.account_service if t.account_service is not None else AccountService.from_env()
        keys = service.signing_keys(token).get("keys")
        return {str(k["key_id"]): str(k["public_key"]) for k in keys if isinstance(k, dict)} \
            if isinstance(keys, list) else {}

    verdict = signed_report.verify(path, registered, public_key)
    t.say(verdict.text)
    return 0 if verdict.ok else 1


def _is_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def show_new_recovery_key(t: Terminal) -> int:
    """Only for a person at a terminal: an AI that can run commands must not be able to read a new key (or make the
    old one stop working) by running this with its output captured."""
    if not t.is_terminal():
        t.say("portward: run this yourself in a terminal; it prints a new recovery key, so it won't run with its "
              "input or output redirected")
        return 2
    t.say("This makes a new recovery key; the one you have now stops working.")
    try:
        answer = t.prompt("Type NEW to go on (hidden): ")
    except (EOFError, KeyboardInterrupt):
        return 1
    if answer.strip().upper() != "NEW":
        t.say("Nothing was changed.")
        return 1
    try:
        recovery = new_recovery_key(t.store, t.keychain, t.sync())
    except SyncRefused as err:
        t.say(f"portward: {err}")
        return 1
    t.say("Your new Portward recovery key (the old one no longer works). Write it down or keep it in your password "
          "manager; it is the only way to open your synced history on a new computer:")
    t.say(f"\n    {recovery}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
