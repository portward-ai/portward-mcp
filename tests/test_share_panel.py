"""A new share shows its link in the share panel to copy; the mail app is optional (2026-11-04).

With a panel, `share` opens no mail app. The AI's result says who the share is for, what is in it and when it
expires, never the link: the link (its # fragment is the key) is held sealed in the local store under its public
share id, and the panel reads it with the app-only `share_link` tool, from the other server process too (Claude
Desktop runs two and drops the result's `_meta`). The panel's "Open in mail app" calls the app-only `share_mail`, which
opens the same draft today's share opened. A client without a panel still gets the draft, and the AI is told so.
Against the real share service on 127.0.0.1 (test_share's fixtures); nothing leaves this machine."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from harness import Harness, MemoryKeychain, connected, file_bytes
from test_share import Drafts, Service, receive, sign_in

from portward_free.kept import KEY_ACCOUNT
from portward_free.sharing import Sharing
from portward_free.sharing.links import HELD_LINKS, LINK_HOURS, SHARE_PANEL_URI

pytest_plugins = ["test_share"]  # its fixtures: the share service on 127.0.0.1, drafts, sharing, recipient, team_db


def seen_by_ai(*replies) -> str:
    return "\n".join(r.ai.text + json.dumps(r.ai.structured) for r in replies)


async def share_in(h: Harness, service: Service, sender: str, recipient: str, **options):
    pulled = await h.call("query", source="shop", sql="SELECT name, region FROM people ORDER BY id",
                          question="Who is where?")
    await sign_in(h, service, sender)
    shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient, **options)
    assert not shared.is_error, shared.ai.text
    return shared


async def test_a_share_with_a_panel_opens_no_mail_app_and_the_ai_never_gets_the_link(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    async with connected(home, team_db, panel=True, sharing=sharing) as h:
        shared = await share_in(h, service, sender, recipient)
        link = (await h.call_from_panel("share_link", share_id=shared.ai.structured["share_id"])).ai.structured["link"]
    assert drafts.opened == []  # nothing opened by default
    assert shared.panel is not None and shared.tool_meta["ui"]["resourceUri"] == SHARE_PANEL_URI
    said = seen_by_ai(shared)
    assert "#" not in said and urlsplit(link).fragment not in said and "/s/" not in said
    assert shared.ai.structured["link_in"] == "panel" and shared.ai.structured["draft_opened"] is False
    assert recipient in shared.ai.text and "panel" in shared.ai.text and "copy" in shared.ai.text.lower()
    assert shared.ai.structured["expires_at"] in shared.ai.text


async def test_the_panel_in_the_other_process_reads_the_link_and_it_opens_for_the_teammate(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    keychain = MemoryKeychain()  # Claude Desktop: the AI's process and the panel's, one app-data folder and keychain
    async with connected(home, team_db, panel=True, keychain=keychain, sharing=sharing) as h, \
            Harness.start(home, panel=True, keychain=keychain, sharing=sharing) as panel:
        shared = await share_in(h, service, sender, recipient, options={"region": "anonymise"})
        read = await panel.call_from_panel("share_link", share_id=shared.ai.structured["share_id"])
        on_disk = file_bytes(home)
    assert not read.is_error, read.ai.text
    link = read.ai.structured["link"]
    assert link.startswith(f"{service.url}/s/{shared.ai.structured['share_id']}#")
    assert read.ai.structured["recipient"] == recipient
    assert urlsplit(link).fragment.encode() not in on_disk  # held for the panel sealed, never in plain text
    snapshot = receive(service, link, recipient)
    assert snapshot["anonymised"] == ["region"] and snapshot["columns"] == ["name", "region"]
    assert shared.ai.structured["options"] == {"anonymised": ["region"], "hidden": [], "totals": []}


async def test_open_in_mail_app_from_the_panel_opens_the_same_draft_as_before(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    keychain = MemoryKeychain()
    async with connected(home, team_db, panel=True, keychain=keychain, sharing=sharing) as h, \
            Harness.start(home, panel=True, keychain=keychain, sharing=sharing) as panel:
        shared = await share_in(h, service, sender, recipient)
        share_id = shared.ai.structured["share_id"]
        link = (await panel.call_from_panel("share_link", share_id=share_id)).ai.structured["link"]
        mailed = await panel.call_from_panel("share_mail", share_id=share_id)
    assert not mailed.is_error, mailed.ai.text
    assert mailed.ai.structured["opened"] is True
    assert drafts.opened == [(recipient, "Sam Rivera shared a table with you",
                              f"Sam Rivera shared a table with you:\n\n{link}")]
    assert "#" not in mailed.ai.text


async def test_a_mail_app_that_wont_open_is_said_in_the_panel(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    async with connected(home, team_db, panel=True, sharing=sharing) as h:
        shared = await share_in(h, service, sender, recipient)
        drafts.works = False
        mailed = await h.call_from_panel("share_mail", share_id=shared.ai.structured["share_id"])
    assert mailed.ai.structured["opened"] is False and "copy" in mailed.ai.text.lower()


async def test_a_client_without_a_panel_still_gets_the_mail_draft_and_the_ai_is_told(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    async with connected(home, team_db, sharing=sharing) as h:
        shared = await share_in(h, service, sender, recipient)
    assert len(drafts.opened) == 1 and drafts.opened[0][0] == recipient
    assert shared.ai.structured["link_in"] == "mail" and shared.ai.structured["draft_opened"] is True
    assert "mail app" in shared.ai.text and "no Portward panel" in shared.ai.text
    assert "#" not in seen_by_ai(shared) and urlsplit(drafts.link()).fragment not in seen_by_ai(shared)


async def test_when_the_link_cant_be_sealed_for_the_panel_the_mail_app_opens_instead(
        home: Path, team_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    class NoKeptKey(MemoryKeychain):  # the share token is there, the kept-rows key can't be made
        def set(self, account: str, secret: str) -> None:
            if account == KEY_ACCOUNT:
                from portward_free.keychain import KeychainError
                raise KeychainError("the keychain is locked")
            super().set(account, secret)

    async with connected(home, team_db, panel=True, keychain=NoKeptKey(), sharing=sharing) as h:
        shared = await share_in(h, service, sender, recipient)
        read = await h.call_from_panel("share_link", share_id=shared.ai.structured["share_id"])
    assert len(drafts.opened) == 1
    assert shared.ai.structured["link_in"] == "mail" and "mail app" in shared.ai.text
    assert read.is_error


async def test_the_link_tools_are_app_only_and_refuse_an_unknown_share(
        home: Path, team_db: Path, sharing: Sharing) -> None:
    async with connected(home, team_db, panel=True, sharing=sharing) as h:
        ai_tools = await h.ai_tools()
        unknown = await h.call_from_panel("share_link", share_id="no-such-share-0000")
        mailed = await h.call_from_panel("share_mail", share_id="no-such-share-0000")
        resources = {str(r.uri) for r in (await h.client.list_resources()).resources}
    assert "share_link" not in ai_tools and "share_mail" not in ai_tools and "share" in ai_tools
    assert unknown.is_error and mailed.is_error
    assert SHARE_PANEL_URI in resources


async def test_the_link_is_not_in_the_ai_log(home: Path, team_db: Path, service: Service, sender: str,
                                             sharing: Sharing, recipient: str) -> None:
    async with connected(home, team_db, panel=True, sharing=sharing) as h:
        shared = await share_in(h, service, sender, recipient)
        link = (await h.call_from_panel("share_link", share_id=shared.ai.structured["share_id"])).ai.structured["link"]
        listed = await h.call("sent_to_ai")
        log = await h.call("sent_to_ai", conversation_id=listed.ai.structured["current"])
    tools = [r["tool"] for r in log.ai.structured["responses"]]
    assert "share" in tools and "share_link" not in tools
    assert urlsplit(link).fragment not in json.dumps(log.ai.structured)


def test_the_readme_says_how_long_a_link_is_held_and_that_copying_is_the_default() -> None:
    readme = " ".join((Path(__file__).parents[1] / "README.md").read_text().split())
    assert f"Kept for {LINK_HOURS} hours" in readme and f"the last {HELD_LINKS} shares" in readme
    assert "press Copy link" in readme and "Open in mail app" in readme
