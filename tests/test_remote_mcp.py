"""Serving MCP over HTTP from this computer with Portward sign-in (#102), against the real control app on 127.0.0.1.

With Personal on, the tool serves the same MCP tools over HTTP (as well as stdio), as an OAuth resource server:
claude.ai or ChatGPT sign the user in at Portward's sign-in (WorkOS AuthKit, a fake here) and send a token for this
computer's address; the tool asks the control app whether that token is the account owner's before answering.
Anyone else, or no token, is refused. Pages and panels for entering secrets stay on the computer: over HTTP a
password or code is only ever asked for in a terminal here. When Personal lapses the server stops.

The direct relay (#103) and the hosted route (#104) put this same server behind a public name."""

from __future__ import annotations

import io
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation
from test_sync import Device, Service, device, service  # noqa: F401  (the control app, as a module fixture)

from portward_free import cli
from portward_free.account import Accounts, AccountService
from portward_free.keychain import account_for
from portward_free.paths import database_path
from portward_free.remote import RemoteMcp, RemoteRefused
from portward_free.secret_entry import PANEL_URI as PASSWORD_PANEL_URI
from portward_free.storage import LocalStore
from portward_free.usage import Usage

SECRET_TOOLS = ("save_secret", "secret_request")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fake_authkit_token(email: str, resource: str) -> str:
    """What the connector holds once the user signed in at AuthKit (tests/fixtures/share_service.py: FakeAuthKit)."""
    return f"mcp|{email}|{resource}"


@contextmanager
def serving(laptop: Device, service: Service, plan_seconds: float = 30) -> Iterator[RemoteMcp]:
    store = LocalStore.open(database_path(laptop.home))
    port = free_port()
    remote = RemoteMcp(store, laptop.keychain, Accounts(AccountService(service.url)),
                       public_url=f"http://127.0.0.1:{port}", port=port, plan_seconds=plan_seconds,
                       usage=Usage(store, None, disabled="usage counts are off in tests"))
    try:
        remote.start()
        yield remote
    finally:
        remote.stop()
        store.close()


@asynccontextmanager
async def connector(remote: RemoteMcp, token: str | None, name: str | None = None) -> AsyncIterator[Client]:
    """claude.ai (or ChatGPT) on the web, connected with `token`. It renders panels (MCP Apps), as claude.ai does.
    `name`: what it calls itself (its MCP client info)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    panels = [advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})]
    info = {"client_info": Implementation(name=name, version="1")} if name else {}
    async with httpx2.AsyncClient(headers=headers, timeout=10) as http:
        async with Client(streamable_http_client(remote.resource, http_client=http), extensions=panels,
                          **info) as client:
            yield client


def get(url: str, token: str | None = None) -> tuple[int, dict[str, str], str]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read().decode()


def post_mcp(remote: RemoteMcp, token: str | None) -> int:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude.ai", "version": "1"}}}
    ).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(remote.resource, body, headers), timeout=10) as answer:
            return answer.status
    except urllib.error.HTTPError as err:
        return err.code


async def test_the_account_owner_calls_tools_over_http(service: Service, tmp_path: Path, shop_db: Path) -> None:
    async with device(service, tmp_path / "laptop", "owner@gmail.example") as laptop:
        assert not (await laptop.h.add_file("shop", str(shop_db))).is_error
        with serving(laptop, service) as remote:
            token = fake_authkit_token("owner@gmail.example", remote.resource)
            async with connector(remote, token) as client:
                tools = {t.name for t in (await client.list_tools()).tools}
                assert {"list_sources", "query"} <= tools
                listed = await client.call_tool("list_sources", {})
                assert not listed.is_error and "shop" in json.dumps(listed.structured_content)


async def test_another_account_or_no_token_is_refused(service: Service, tmp_path: Path) -> None:
    async with device(service, tmp_path / "laptop", "keeper@gmail.example") as laptop:
        async with device(service, tmp_path / "other", "intruder@gmail.example"):
            with serving(laptop, service) as remote:
                status, headers, _ = get(remote.resource)
                assert status == 401
                assert "resource_metadata=" in headers["www-authenticate"]  # how the connector finds the sign-in
                assert post_mcp(remote, None) == 401
                assert post_mcp(remote, fake_authkit_token("intruder@gmail.example", remote.resource)) == 401
                assert post_mcp(remote, fake_authkit_token("keeper@gmail.example", "https://elsewhere.example/mcp")
                                ) == 401
                assert post_mcp(remote, "forged") == 401
                assert post_mcp(remote, fake_authkit_token("keeper@gmail.example", remote.resource)) == 200


async def test_the_connector_finds_portwards_sign_in(service: Service, tmp_path: Path) -> None:
    async with device(service, tmp_path / "laptop", "finder@gmail.example") as laptop:
        with serving(laptop, service) as remote:
            status, _, body = get(f"http://127.0.0.1:{remote.port}/.well-known/oauth-protected-resource/mcp")
            assert status == 200, body
            metadata = json.loads(body)
            assert metadata["resource"] == remote.resource
            assert metadata["authorization_servers"] == ["https://authkit.invalid/"] or \
                metadata["authorization_servers"] == ["https://authkit.invalid"]


async def test_secret_entry_is_not_reachable_over_http(service: Service, tmp_path: Path) -> None:
    async with device(service, tmp_path / "laptop", "secrets@gmail.example") as laptop:
        with serving(laptop, service) as remote:
            token = fake_authkit_token("secrets@gmail.example", remote.resource)
            async with connector(remote, token) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                assert not set(SECRET_TOOLS) & set(tools)
                assert all(PASSWORD_PANEL_URI not in json.dumps(t.meta or {}) for t in tools.values())
                resources = {str(r.uri) for r in (await client.list_resources()).resources}
                assert PASSWORD_PANEL_URI not in resources
                with pytest.raises(Exception):
                    await client.read_resource(PASSWORD_PANEL_URI)
                refused = await client.call_tool("save_secret", {"request": "x", "secret": "hunter2"})
                assert refused.is_error

                added = await client.call_tool("add_source", {"name": "warehouse", "kind": "postgres",
                                                              "host": "db.example", "database": "shop",
                                                              "user": "reader"})
                assert not added.is_error, added
                said = json.dumps(added.structured_content)
                assert added.structured_content["password_entry"] == "terminal"
                assert added.structured_content["command"].endswith("password warehouse")
                assert "127.0.0.1" not in said  # no local page's address is handed out over HTTP
            for path in ("/", "/page", "/password", "/done"):
                assert get(f"http://127.0.0.1:{remote.port}{path}", token)[0] == 404, path


# What the web AI may call (sweep open problem 6, #90 story 42): reading, asking, saving its own pulls and notes of
# what it assumed, and setup that the user finishes in a terminal on the computer. Nothing that deletes, changes
# rules, profiles or context, turns sync or kept rows on or off, or signs the computer in or out. A new tool lands
# here only on purpose.
WEB_TOOLS = {
    "query", "rerun", "run_saved", "tweak", "kept_rows", "compare", "panel_rows", "put_in_chat", "value_source",
    "share_link", "share_mail", "add_source", "update_password", "usage_seen", "usage_choice", "more_rows",
    "get_selection", "export", "test_source", "matching_login_sql", "list_sources", "describe_source", "get_started",
    "history", "lineage", "save_pull", "saved_pulls", "sent_to_ai", "ai_log", "export_proof_report", "open_page",
    "dismiss_offer", "list_rules", "list_profiles", "context_drafts", "assumptions", "share_sign_in",
    "share_confirm", "share", "shares", "account_status", "account_shares", "sync_now", "sync_status",
    "metric", "describe_metric", "run_verified_answer", "describe_verified_answer",
    "remove_source",  # served, but it only gives the command to run on the computer
}
COMPUTER_ONLY = {
    "block", "unblock", "create_profile", "update_profile", "start_profile_from_block_rules",
    "apply_profile", "unapply_profile", "use_block_rules", "set_note", "draft_meanings", "draft_definition",
    "draft_link", "draft_metric", "promote_saved_pull", "discard_draft", "approve_context", "settle_context",
    "define_assumption", "import_dictionary",
    "keep_rows", "usage_counts", "set_usage_counts", "account_sign_in", "account_sign_out", "account_delete", "withdraw_share",
    "set_share_expiry", "sync_turn_on", "sync_turn_off", "sync_new_recovery_key", "set_send_mode",
    "personal_offer",  # #116: offers are for Free, and the web routes are Personal's
    "column_menu", "keep_from_claude", "column_note",  # #119: the panel's column menu changes rules and notes
    "level_preview",  # #120: the preview beside that offer
}


async def test_the_web_ai_cant_delete_or_change_settings(service: Service, tmp_path: Path, shop_db: Path) -> None:
    async with device(service, tmp_path / "laptop", "careful@gmail.example") as laptop:
        assert not (await laptop.h.add_file("shop", str(shop_db))).is_error
        with serving(laptop, service) as remote:
            token = fake_authkit_token("careful@gmail.example", remote.resource)
            async with connector(remote, token) as client:
                served = {t.name for t in (await client.list_tools()).tools}
                removed = await client.call_tool("remove_source", {"name": "shop"})
                listed = await client.call_tool("list_sources", {})
    assert served == WEB_TOOLS
    assert removed.is_error and "remove-source shop" in removed.content[0].text
    assert "shop" in json.dumps(listed.structured_content)  # still connected, its password still in the keychain


async def test_the_web_cant_point_a_saved_database_at_another_server_or_drop_its_password(
        service: Service, tmp_path: Path) -> None:
    """add_source replaces a name, and replacing a database forgets its saved password: from the web that would be
    remove_source by another door, and a way to move a trusted name to someone else's server."""
    async with device(service, tmp_path / "repoint", "repointer@gmail.example") as laptop:
        store = LocalStore.open(database_path(laptop.home))
        was = {"host": "db.acme.example", "port": 5432, "database": "app", "user": "reader"}
        store.add_connection("prod", "postgres", was)
        store.close()
        laptop.keychain.set(account_for("prod"), "saved-login")
        with serving(laptop, service) as remote:
            token = fake_authkit_token("repointer@gmail.example", remote.resource)
            async with connector(remote, token) as client:
                moved = await client.call_tool("add_source", {"name": "prod", "kind": "postgres", **was,
                                                              "host": "db.evil.example"})
                same = await client.call_tool("add_source", {"name": "prod", "kind": "postgres", **was})
        kept = LocalStore.open(database_path(laptop.home))
        try:
            assert kept.connection("prod").settings["host"] == "db.acme.example"
        finally:
            kept.close()
        assert moved.is_error
        said = moved.content[0].text
        assert "Nothing was changed" in said and "db.acme.example:5432" in said
        assert "remove-source prod" in said and "terminal on this computer" in said
        assert laptop.keychain.get(account_for("prod")) == "saved-login"
        assert "Nothing was changed" not in same.content[0].text  # the same settings again: only the password ask


async def test_every_tool_is_either_for_the_web_or_for_the_computer_only() -> None:
    from portward_free.remote.serve import COMPUTER_ONLY as SERVED_COMPUTER_ONLY

    assert SERVED_COMPUTER_ONLY == COMPUTER_ONLY


async def test_it_will_not_start_without_personal(service: Service, tmp_path: Path) -> None:
    async with device(service, tmp_path / "lapsed", "ended-lapsed@gmail.example") as laptop:
        with pytest.raises(RemoteRefused, match="Personal"):
            with serving(laptop, service):
                pass


def test_the_command_says_to_sign_in_first(home: Path) -> None:
    from harness import MemoryKeychain

    from portward_free.cli import main

    out = io.StringIO()
    assert main(["serve-http"], home=home, keychain=MemoryKeychain(), out=out, is_terminal=lambda: False) == 1
    assert "sign-in" in out.getvalue()


async def test_it_stops_serving_when_personal_lapses(service: Service, tmp_path: Path) -> None:
    async with device(service, tmp_path / "laptop", "lapsing@gmail.example") as laptop:
        try:
            with serving(laptop, service, plan_seconds=0.2) as remote:
                token = fake_authkit_token("lapsing@gmail.example", remote.resource)
                assert post_mcp(remote, token) == 200
                service.move_clock(15 * 86_400)  # past the 14-day trial, and nobody subscribed
                assert remote.wait(10), "the server kept running after Personal ended"
                assert "Personal" in (remote.stopped_because or "")
                with pytest.raises(OSError):
                    urllib.request.urlopen(remote.resource, timeout=2)
        finally:
            service.move_clock(0)


def test_serve_http_logs_its_reads_as_the_direct_route(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """serve-http serves from this computer to an AI app on the web, so its AI log entries say "direct", as
    serve-direct's do (not "desktop", which means an AI app on this computer)."""
    from harness import MemoryKeychain

    from portward_free import cli, proof

    made: dict[str, object] = {}

    class Recorded:
        stopped_because = None
        resource = "http://127.0.0.1:8765/mcp"

        def __init__(self, *_: object, **options: object) -> None:
            made.update(options)

        def serve_forever(self) -> None:
            return None

    monkeypatch.setattr(cli, "RemoteMcp", Recorded)
    assert cli.main(["serve-http"], home=home, keychain=MemoryKeychain(), out=io.StringIO(),
                    is_terminal=lambda: False) == 0
    assert made["route"] == proof.DIRECT


# --- destructive tools need the user at this computer -----------------------------------------------------------------

def remove_in_terminal(laptop: Device, name: str, typed: str, at_terminal: bool = True) -> tuple[int, str]:
    """The user runs `portward-free remove-source <name>` on the computer and types `typed` to confirm."""
    out = io.StringIO()
    code = cli.main(["remove-source", name], home=laptop.home, keychain=laptop.keychain, prompt=lambda _: typed,
                    out=out, is_terminal=lambda: at_terminal,
                    usage=lambda store: Usage(store, None, disabled="usage counts are off in tests"))
    return code, out.getvalue()


async def test_a_web_connector_cant_remove_a_connection_without_the_user_at_the_computer(
        service: Service, tmp_path: Path, shop_db: Path) -> None:
    async with device(service, tmp_path / "laptop", "remover@gmail.example") as laptop:
        assert not (await laptop.h.add_file("shop", str(shop_db))).is_error
        laptop.keychain.set(account_for("shop"), "saved-login")
        with serving(laptop, service) as remote:
            token = fake_authkit_token("remover@gmail.example", remote.resource)
            async with connector(remote, token) as client:
                removed = await client.call_tool("remove_source", {"name": "shop"})
                listed = await client.call_tool("list_sources", {})
        assert removed.is_error
        said = removed.content[0].text
        assert "remove-source shop" in said and "terminal on this computer" in said
        assert "shop" in json.dumps(listed.structured_content)  # still there
        assert laptop.keychain.get(account_for("shop")) == "saved-login"

        refused, _ = remove_in_terminal(laptop, "shop", typed="shop", at_terminal=False)  # an AI running it
        wrong, _ = remove_in_terminal(laptop, "shop", typed="nope")
        assert refused != 0 and wrong != 0 and laptop.keychain.get(account_for("shop")) == "saved-login"

        code, out = remove_in_terminal(laptop, "shop", typed="shop")
        assert code == 0, out
        listed = await laptop.h.call("list_sources")
        assert "shop" not in json.dumps(listed.ai.structured)
        assert laptop.keychain.get(account_for("shop")) is None
