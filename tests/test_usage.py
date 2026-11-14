"""Anonymous usage counts: asked once at first run (on by default, easy to say no), and only the published fields
are ever sent. The sender here records instead of posting; nothing in these tests touches the network."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from harness import Harness, connected
from mcp.types import ElicitResult

from portward_free.usage import USAGE_META

ORDERS = "SELECT id, total FROM orders"
MONDAY = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)  # ISO week 2026-W45


class Clock:
    def __init__(self, at: datetime = MONDAY) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


class Recorder:
    """Stands in for the HTTPS post to the collection endpoint: keeps every payload it is handed."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> bool:
        self.sent.append(payload)
        return True


class Offline:
    """The collection endpoint can't be reached."""

    def __init__(self) -> None:
        self.tried: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> bool:
        self.tried.append(payload)
        return False


async def test_with_usage_counts_declined_nothing_is_ever_sent(home: Path, shop_db: Path) -> None:
    clock, sender = Clock(), Recorder()
    async with connected(home, shop_db, usage_sender=sender, clock=clock) as shop:
        declined = await shop.call("set_usage_counts", on=False)
        assert not declined.is_error and declined.ai.structured["usage_counts"] == "off"
        for _ in range(3):
            await shop.call("query", source="shop", sql=ORDERS, question="orders")
        clock.advance(days=8)
        await shop.call("history")
    async with Harness.start(home, usage_sender=sender, clock=clock) as again:  # a later run, a later week
        await again.call("query", source="shop", sql=ORDERS, question="orders")
        clock.advance(days=8)
        await again.call("history")
    assert sender.sent == []
    assert again.stored("usage_weeks") == []  # nothing is even counted locally


async def test_portward_usage_off_in_the_environment_means_never_asked_and_never_sent(home: Path) -> None:
    async with Harness.start(home, transport="stdio") as h:  # the test environment sets PORTWARD_USAGE=off
        first = await h.call("list_sources")
        status = await h.call("usage_counts")
        refused = await h.call("set_usage_counts", on=True)
    assert "usage counts" not in first.ai.text
    assert status.ai.structured["usage_counts"] == "disabled" and "PORTWARD_USAGE" in status.ai.structured["why_off"]
    assert refused.is_error


async def test_a_question_in_the_ais_reply_alone_never_turns_counts_on(home: Path) -> None:
    clock, sender = Clock(), Recorder()
    async with Harness.start(home, usage_sender=sender, clock=clock) as h:  # no prompt, no panel, no page
        first = await h.call("get_started", use_sample=True)
        second = await h.call("list_sources")
        await h.call("query", **first.ai.structured["try"])
        turned_on = await h.call("set_usage_counts", on=True)  # the AI can't answer for the user
        clock.advance(days=8)
        status = await h.call("usage_counts")
    assert "usage counts" in first.ai.text.lower() and "usage counts" not in second.ai.text.lower()  # told once
    assert turned_on.is_error and "asks the user" in turned_on.ai.text
    assert status.ai.structured["usage_counts"] == "not asked yet"
    assert sender.sent == [] and h.stored("usage_weeks") == []  # nothing sent, nothing even counted


async def test_seen_at_the_terminal_counts_are_on_and_nothing_is_sent_for_an_hour(home: Path, shop_db: Path) -> None:
    clock, sender = Clock(), Recorder()
    async with Harness.start(home, usage_sender=sender, clock=clock) as h:
        typed = h.add_file_in_terminal("shop", "sqlite", str(shop_db))  # the user runs a command and sees the question
        clock.advance(minutes=59)
        await h.call("query", source="shop", sql=ORDERS, question="orders")
        before = list(sender.sent)
        clock.advance(minutes=2)
        await h.call("history")
    assert "anonymous usage counts" in typed.out and "never your data" in typed.out.lower()
    assert before == [] and len(sender.sent) == 1


async def test_a_command_whose_output_the_user_doesnt_see_doesnt_count_as_asking(home: Path, shop_db: Path) -> None:
    clock, sender = Clock(), Recorder()
    async with Harness.start(home, usage_sender=sender, clock=clock) as h:
        ran = h.add_file_in_terminal("shop", "sqlite", str(shop_db), at_terminal=False)
        status = await h.call("usage_counts")
    assert "usage counts" not in ran.out.lower()
    assert status.ai.structured["usage_counts"] == "not asked yet"


class Answer:
    """The user's answer to the usage question in the client's prompt (other questions: yes)."""

    def __init__(self, action: str, keep_on: bool | None = None) -> None:
        self.action, self.keep_on, self.asked = action, keep_on, []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        if "usage counts" not in params.message:
            return ElicitResult(action="accept", content={"yes": True})
        self.asked.append(params.message)
        content = {"keep_on": self.keep_on} if self.keep_on is not None else None
        return ElicitResult(action=self.action, content=content)


@pytest.mark.parametrize(("answer", "state"), [(Answer("accept", True), "on"), (Answer("cancel"), "on"),
                                               (Answer("accept", False), "off"), (Answer("decline"), "off")])
async def test_the_clients_own_prompt_asks_once_and_the_default_stays_on(
        home: Path, answer: Answer, state: str) -> None:
    async with Harness.start(home, usage_sender=Recorder(), clock=Clock(), elicitation=answer,
                             protocol="legacy") as h:
        await h.call("list_sources")
        await h.call("list_sources")
        status = await h.call("usage_counts")
    assert len(answer.asked) == 1 and "never your data" in answer.asked[0].lower()
    assert status.ai.structured["usage_counts"] == state


async def test_the_panel_shows_the_question_and_counts_start_only_once_it_was_shown(
        home: Path, shop_db: Path) -> None:
    clock, sender = Clock(), Recorder()
    async with Harness.start(home, panel=True, usage_sender=sender, clock=clock) as h:
        await h.add_file("shop", shop_db)  # typed in the panel's form
        pulled = await h.call("query", source="shop", sql=ORDERS, question="orders")
        question = (pulled.panel.meta or {}).get(USAGE_META)
        clock.advance(days=1)
        await h.call("history")
        unseen = list(sender.sent)
        await h.call_from_panel("usage_seen")
        clock.advance(minutes=61)
        await h.call("history")
        after = await h.call("query", source="shop", sql=ORDERS, question="orders")
        await h.call_from_panel("usage_choice", on=False)
        status = await h.call("usage_counts")
    assert question and "anonymous usage counts" in question["text"]
    assert "anonymous usage counts" not in pulled.ai.text.lower() + str(pulled.ai.structured).lower()
    assert unseen == [] and len(sender.sent) == 1
    assert USAGE_META not in (after.panel.meta or {})  # asked once
    assert status.ai.structured["usage_counts"] == "off"


async def test_with_usage_counts_accepted_only_the_listed_fields_are_sent(home: Path, shop_db: Path) -> None:
    clock, sender = Clock(), Recorder()
    async with connected(home, shop_db, usage_sender=sender, clock=clock) as shop:  # first reply asks; on by default
        await shop.call("query", source="shop", sql=ORDERS, question="what did Ada order?")
        await shop.call("query", source="shop", sql="SELECT name FROM customers", question="who are they?")
        await shop.call("query", source="shop", sql="SELECT nope FROM nowhere", question="a failing one")
        clock.advance(minutes=61)
        await shop.call("history")
    (report,) = sender.sent
    assert re.fullmatch(r"[0-9a-f]{32}", report.pop("report"))
    assert report == {"week": "2026-W45", "installed_week": "2026-W45", "pulls": 2, "shares": 0,
                      "source_types": ["sqlite"], "ai_clients": ["other"], "profiles": 0, "grants_full": 0,
                      "grants_anonymised": 0, "grants_totals": 0, "routes": []}


async def test_each_week_has_its_own_unlinkable_report_and_a_sent_week_is_deleted(home: Path, shop_db: Path) -> None:
    clock, sender = Clock(), Recorder()
    async with connected(home, shop_db, usage_sender=sender, clock=clock) as shop:
        clock.advance(minutes=61)
        await shop.call("query", source="shop", sql=ORDERS, question="orders")  # posts week 45 so far
        clock.advance(days=7)
        await shop.call("query", source="shop", sql=ORDERS, question="orders")  # posts week 45 again, and week 46
        stored = shop.stored("usage_weeks")
    first, again, later = sender.sent
    assert (first["week"], again["week"], later["week"]) == ("2026-W45", "2026-W45", "2026-W46")
    assert first["report"] == again["report"]  # a resend of the same week replaces it
    assert later["report"] != first["report"]
    assert later["installed_week"] == "2026-W45" and later["pulls"] == 1
    assert [row["week"] for row in stored] == ["2026-W46"]  # week 45 is gone from this machine once it was taken


async def test_usage_counts_shows_exactly_what_would_be_sent_next(home: Path, shop_db: Path) -> None:
    clock, sender = Clock(), Recorder()
    async with connected(home, shop_db, usage_sender=sender, clock=clock) as shop:
        await shop.call("query", source="shop", sql=ORDERS, question="orders")
        shown = await shop.call("usage_counts")
    assert shown.ai.structured["usage_counts"] == "on"
    assert shown.ai.structured["fields"] == ["report", "week", "installed_week", "pulls", "shares", "source_types",
                                             "ai_clients", "profiles", "grants_full", "grants_anonymised",
                                             "grants_totals", "routes"]
    (next_report,) = shown.ai.structured["next_reports"]
    assert next_report["pulls"] == 1 and next_report["source_types"] == ["sqlite"]


async def test_failed_sends_are_kept_and_tried_again_an_hour_later(home: Path, shop_db: Path) -> None:
    clock, sender = Clock(), Offline()
    async with connected(home, shop_db, usage_sender=sender, clock=clock) as shop:
        clock.advance(minutes=61)
        await shop.call("query", source="shop", sql=ORDERS, question="orders")
        clock.advance(days=7)
        await shop.call("history")
        await shop.call("history")  # within the hour of the last try: not tried again
        stored = shop.stored("usage_weeks")
    assert [p["week"] for p in sender.tried] == ["2026-W45", "2026-W45", "2026-W46"]
    assert [row["week"] for row in stored] == ["2026-W45", "2026-W46"]
