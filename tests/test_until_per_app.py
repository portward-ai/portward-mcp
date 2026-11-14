"""Applying a profile until a date, and for some AI apps on the web routes (#134, spec #114 stories 38-39).

An application can carry an end date (Until): after it, the profile stops applying to that connection by itself,
on this computer's clock, with no account service needed, and a clock set back never makes it apply again. On the
web routes (serve-http, the relay, the hosted engine) the OAuth client that signed in is known, so an application can
be limited to some AI apps; on desktop the AI app names itself, so a limited application never applies there."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from harness import Harness, MemoryKeychain
from test_profiles import PlanAccounts, columns_of, make, seen, tool
from test_remote_mcp import connector, fake_authkit_token, serving
from test_sync import Service, device, marked_db, service, two_profiled_computers  # noqa: F401  (fixtures)

from conftest import SECRETS
from portward_free.account import AccountServiceError
from portward_free.hosted import bundle
from portward_free.keychain import account_for
from portward_free.profiles import Grant, Profile
from portward_free.remote import RemoteMcp
from portward_free.storage import LocalStore

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


class Clock:
    """This computer's clock, as the store reads it; a test moves it forwards and back."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, at: datetime = T0) -> None:
        self.at = at
        monkeypatch.setattr("portward_free.storage.store.now", lambda: self.at.isoformat(timespec="seconds"))

    def set(self, at: datetime) -> None:
        self.at = at


def offline(*args: object, **kwargs: object) -> dict:
    raise AccountServiceError("the account service wasn't reached", None)


async def names_seen(h: Harness) -> str:
    whole = await h.call("query", source="people", question="?", sql="SELECT name FROM customers")
    return seen(whole)


# --- until ----------------------------------------------------------------------------------------------------------

async def test_an_application_stops_applying_after_its_end_date(home: Path, people_db: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    accounts = PlanAccounts("active")
    async with tool(home, people_db, accounts=accounts) as h:
        await make(h, "Sales", ["customers.name"])
        applied = await h.call("apply_profile", profile="Sales", source="people",
                               until=(T0 + timedelta(days=2)).isoformat())
        assert not applied.is_error, applied.ai.text
        assert "until" in applied.ai.text
        before = await names_seen(h)
        monkeypatch.setattr(accounts, "_call", offline)  # offline from here on: expiry needs no account service
        clock.set(T0 + timedelta(days=3))
        after = await h.call("query", source="people", question="?", sql="SELECT name FROM customers")
        listed = await h.call("list_sources")
    assert "Ada" in before
    assert after.is_error and "Ada" not in seen(after)
    assert columns_of(listed).get("customers") is None


async def test_a_date_alone_applies_through_that_day(home: Path, people_db: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        applied = await h.call("apply_profile", profile="Sales", source="people", until="2026-11-10")
        assert not applied.is_error, applied.ai.text
        clock.set(datetime(2026, 11, 10, 12, 0).astimezone(UTC))  # midday that day, on this computer's clock
        during = await names_seen(h)
        clock.set(datetime(2026, 11, 11, 0, 30).astimezone(UTC))
        after = await names_seen(h)
    assert "Ada" in during and "Ada" not in after


async def test_an_end_date_already_past_is_refused(home: Path, people_db: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    Clock(monkeypatch)
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        past = await h.call("apply_profile", profile="Sales", source="people",
                            until=(T0 - timedelta(hours=1)).isoformat())
        garbled = await h.call("apply_profile", profile="Sales", source="people", until="next friday")
    assert past.is_error and "past" in past.ai.text
    assert garbled.is_error and "date" in garbled.ai.text


async def test_a_clock_set_back_never_applies_an_ended_profile_again(home: Path, people_db: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        await h.call("apply_profile", profile="Sales", source="people", until=(T0 + timedelta(days=1)).isoformat())
        clock.set(T0 + timedelta(days=2))
        ended = await names_seen(h)
        clock.set(T0)  # someone sets the clock back to before the end date
        back = await names_seen(h)
        listed = await h.call("list_sources")
    assert "Ada" not in ended
    assert "Ada" not in back
    assert columns_of(listed).get("customers") is None


async def test_list_profiles_says_until_when_and_when_it_ended(home: Path, people_db: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        await h.call("apply_profile", profile="Sales", source="people", until=(T0 + timedelta(days=1)).isoformat())
        running = await h.call("list_profiles")
        clock.set(T0 + timedelta(days=2))
        ended = await h.call("list_profiles")
    (profile,) = running.ai.structured["profiles"]
    assert profile["applied_to"] == ["people"]
    assert profile["applications"] == [{"source": "people", "until": "2026-11-10T09:00:00+00:00", "apps": [],
                                        "ended": False}]
    (profile,) = ended.ai.structured["profiles"]
    assert profile["applied_to"] == []
    assert profile["applications"][0]["ended"] is True
    assert "ended" in ended.ai.text


async def test_applying_again_without_a_date_lifts_the_end_date(home: Path, people_db: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock(monkeypatch)
    async with tool(home, people_db) as h:
        await make(h, "Sales", ["customers.name"])
        await h.call("apply_profile", profile="Sales", source="people", until=(T0 + timedelta(days=1)).isoformat())
        clock.set(T0 + timedelta(days=2))
        again = await h.call("apply_profile", profile="Sales", source="people")
        shown = await names_seen(h)
    assert not again.is_error, again.ai.text
    assert "Ada" in shown
    assert SECRETS[0] not in shown


# --- per AI app, on the web routes ----------------------------------------------------------------------------------

CHATGPT, CLAUDE_AI = "client_01CHATGPT", "client_01CLAUDEAI"


async def web_names(remote: RemoteMcp, email: str, client: str | None, name: str = "claude.ai") -> list[str]:
    """The columns of customers the web AI app behind OAuth client `client` sees; it calls itself `name`."""
    token = fake_authkit_token(email, remote.resource) + (f"|{client}" if client else "")
    async with connector(remote, token, name=name) as session:
        listed = await session.call_tool("list_sources", {})
    (entry,) = [s for s in listed.structured_content["sources"] if s["name"] == "people"]
    return next((t["columns"] for t in entry["tables"] if t["name"] == "customers"), [])


async def test_on_the_web_an_application_for_one_ai_app_applies_only_to_that_app(
        service: Service, tmp_path: Path, people_db: Path) -> None:
    email = "apps@gmail.example"
    async with device(service, tmp_path / "laptop", email) as laptop:
        assert not (await laptop.h.add_file("people", str(people_db))).is_error
        await make(laptop.h, "Sales", ["customers.name"])
        applied = await laptop.h.call("apply_profile", profile="Sales", source="people", for_apps=[CHATGPT])
        assert not applied.is_error, applied.ai.text
        on_desktop = await names_seen(laptop.h)
        with serving(laptop, service) as remote:
            chatgpt = await web_names(remote, email, CHATGPT)
            claude = await web_names(remote, email, CLAUDE_AI, name="ChatGPT")  # naming itself changes nothing
            unnamed = await web_names(remote, email, None)  # the account service didn't name the OAuth client
    assert chatgpt == ["name"]
    assert claude == [] and unnamed == []
    assert "Ada" not in on_desktop  # desktop has no per-app option: a limited application never applies there


async def test_a_web_ai_app_seen_here_can_be_named_when_applying(service: Service, tmp_path: Path,
                                                                 people_db: Path) -> None:
    email = "named@gmail.example"
    async with device(service, tmp_path / "laptop", email) as laptop:
        assert not (await laptop.h.add_file("people", str(people_db))).is_error
        await make(laptop.h, "Sales", ["customers.name"])
        with serving(laptop, service) as remote:
            await web_names(remote, email, CHATGPT, name="ChatGPT")  # it connects once: its OAuth client is kept
            applied = await laptop.h.call("apply_profile", profile="Sales", source="people", for_apps=["chatgpt"])
            unknown = await laptop.h.call("apply_profile", profile="Sales", source="people", for_apps=["Some App"])
            chatgpt = await web_names(remote, email, CHATGPT, name="ChatGPT")
            listed = await laptop.h.call("list_profiles")
    assert not applied.is_error, applied.ai.text
    assert applied.ai.structured["apps"] == [CHATGPT] and "ChatGPT" in applied.ai.text
    assert unknown.is_error and "isn't a web AI app" in unknown.ai.text
    assert chatgpt == ["name"]
    assert listed.ai.structured["profiles"][0]["applications"][0]["apps"] == [CHATGPT]


# --- the hosted engine and other computers ---------------------------------------------------------------------------

def test_the_hosted_engine_gets_each_applications_end_date_and_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine runs with no computer on: it must end an application and keep it to its apps by itself."""
    clock = Clock(monkeypatch)
    computer, engine = LocalStore.memory(), LocalStore.memory()
    keychain, engine_keychain = MemoryKeychain(), MemoryKeychain()
    computer.add_connection("warehouse", "postgres", {"host": "db.zq-hosted-host.example", "port": 5432,
                                                      "database": "shop", "user": "reader"})
    keychain.set(account_for("warehouse"), "zq-login")
    sales = computer.profiles.save(Profile.new("Sales", "pipeline", "me@example.com", [Grant("customers", "name")]))
    totals = computer.profiles.save(Profile.new("Totals", "sums", "me@example.com", [Grant("orders", "total")]))
    computer.profiles.switch_to_profiles("warehouse")
    computer.profiles.apply("warehouse", sales, (), apps=(CHATGPT,))
    computer.profiles.apply("warehouse", totals, (), until=(T0 + timedelta(days=1)).isoformat())

    bundle.land(engine, engine_keychain, bundle.build(computer, keychain, ["warehouse"], b"k" * 32))

    held = {a.profile.name: (a.until, a.apps) for a in engine.profiles.held("warehouse")}
    assert held == {"Sales": (None, (CHATGPT,)), "Totals": ("2026-11-10T09:00:00+00:00", ())}
    assert [a.profile.name for a in engine.profiles.applied("warehouse")] == ["Totals"]  # no web app asking here
    clock.set(T0 + timedelta(days=2))
    assert engine.profiles.applied("warehouse") == ()


async def test_an_end_date_and_ai_apps_sync_to_the_users_other_computers(service: Service, tmp_path: Path,
                                                                         marked_db: Path) -> None:
    async with two_profiled_computers(service, tmp_path, marked_db, "terms@gmail.example") as (laptop, desktop):
        changed = await laptop.h.call("apply_profile", profile="zq profile marker", source="books",
                                      until="2099-01-31T00:00:00+00:00", for_apps=[CHATGPT])
        assert not changed.is_error, changed.ai.text
        assert not (await laptop.h.call("sync_now")).is_error
        assert not (await desktop.h.call("sync_now")).is_error
        listed = await desktop.h.call("list_profiles")
    (profile,) = listed.ai.structured["profiles"]
    assert profile["applications"] == [{"source": "books", "until": "2099-01-31T00:00:00+00:00", "apps": [CHATGPT],
                                        "ended": False}]
    assert profile["applied_to"] == []  # for ChatGPT on the web only: not on this desktop
