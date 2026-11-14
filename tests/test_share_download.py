"""The sender chooses whether the teammate may download a share (2026-11-04). The default is view only.

The choice is sealed inside the encrypted snapshot (`allow_download`), so the share service never learns it and it
can't be changed once the share is made; a snapshot from before the choice existed (version 1, no field) is view
only. The share panel says which, with the other options; the AI's result says so too (it isn't secret), and still
never has the link or its key. Against the real share service on 127.0.0.1 (test_share's fixtures)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from harness import connected
from test_share import Drafts, Service, receive, sign_in

from portward_free.sharing import Sharing
from portward_free.sharing.links import SHARE_PANEL_URI
from portward_free.sharing.snapshot import SNAPSHOT_VERSION, allows_download, open_snapshot, seal_snapshot
from portward_free.sharing.tools import SHARE_DESCRIPTION

pytest_plugins = ["test_share"]  # its fixtures: the share service on 127.0.0.1, drafts, sharing, sender, recipient
EMAIL_MARKER = "hidden-mail.example"


@pytest.fixture()
def staff_db(tmp_path: Path) -> Path:
    """10 people in two regions of five, so totals keep both groups."""
    path = tmp_path / "staff.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY, email TEXT, region TEXT, salary INT)")
        db.executemany("INSERT INTO staff VALUES (?, ?, ?, ?)",
                       [(i, f"p{i}@{EMAIL_MARKER}", "north" if i <= 5 else "south", 100 * i) for i in range(1, 11)])
    db.close()
    return path


async def share_staff(home: Path, db: Path, service: Service, sender: str, sharing: Sharing, recipient: str,
                      **args: Any) -> Any:
    async with connected(home, db, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT id, email, region, salary FROM staff ORDER BY id",
                              question="Staff")
        assert not pulled.is_error, pulled.ai.text
        await sign_in(h, service, sender)
        shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient, **args)
    assert not shared.is_error, shared.ai.text
    return shared


def seen_by_ai(shared: Any) -> str:
    return shared.ai.text + json.dumps(shared.ai.structured)


async def test_a_share_is_view_only_unless_the_sender_allows_download(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    shared = await share_staff(home, staff_db, service, sender, sharing, recipient)
    snapshot = receive(service, drafts.link(), recipient)
    assert snapshot["v"] == SNAPSHOT_VERSION == 2
    assert snapshot["allow_download"] is False and allows_download(snapshot) is False
    assert shared.ai.structured["allow_download"] is False
    assert "View only" in shared.ai.text and "Download: allowed" not in shared.ai.text


async def test_allowing_download_is_sealed_in_the_snapshot_and_said_to_the_ai(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    shared = await share_staff(home, staff_db, service, sender, sharing, recipient, allow_download=True)
    link = drafts.link()
    snapshot = receive(service, link, recipient)
    assert snapshot["allow_download"] is True and allows_download(snapshot) is True
    assert shared.ai.structured["allow_download"] is True
    assert "Download: allowed" in shared.ai.text
    said = seen_by_ai(shared)
    assert urlsplit(link).fragment not in said and "/s/" not in said and "#" not in said
    stored = b"".join(p.read_bytes() for p in service.folder.glob("control.db*"))
    assert b"allow_download" not in stored  # only inside the ciphertext: the service never learns the choice


@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("options", [None, {"email": "hide"}, {"email": "anonymise"},
                                     {"id": "hide", "email": "hide", "salary": "totals"}])
async def test_every_option_applies_before_download_and_the_choice_rides_along(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str, options: dict[str, str] | None, allow: bool) -> None:
    args: dict[str, Any] = {"allow_download": allow}
    if options is not None:
        args["options"] = options
    shared = await share_staff(home, staff_db, service, sender, sharing, recipient, **args)
    link = drafts.link()
    snapshot = receive(service, link, recipient)  # what the viewer decrypts, and so the only thing a CSV can hold
    assert snapshot["allow_download"] is allow and shared.ai.structured["allow_download"] is allow
    if options and options.get("email") in ("hide", "anonymise"):
        assert EMAIL_MARKER not in json.dumps(snapshot)
    if options and "salary" in options:
        assert "totals" in snapshot and len(snapshot["rows"]) == 2
    assert urlsplit(link).fragment not in seen_by_ai(shared)


def test_a_snapshot_from_before_the_choice_is_view_only() -> None:
    old = {"v": 1, "title": "Revenue", "columns": ["region"], "rows": [["north"]]}
    key, sealed = seal_snapshot(old)
    assert allows_download(open_snapshot(sealed, key)) is False
    assert allows_download({**old, "allow_download": "yes"}) is False  # only a real true allows it


def test_the_ai_is_told_not_to_turn_download_on_unless_the_user_asked() -> None:
    assert "allow_download" in SHARE_DESCRIPTION
    assert "unless the user asks" in SHARE_DESCRIPTION and "it is view-only" not in SHARE_DESCRIPTION


async def test_the_share_panel_says_download_allowed_or_view_only(home: Path, staff_db: Path) -> None:
    async with connected(home, staff_db, panel=True) as h:
        page = (await h.client.read_resource(SHARE_PANEL_URI)).contents[0].text
    assert "allow_download" in page and "Download: allowed" in page and "View only" in page
