"""The first-run guide: from a fresh install to a first pull, with the user's own file or the bundled sample."""

from __future__ import annotations

import json
from pathlib import Path

from harness import Harness, connected

from portward_free.usage import TEAM_URL, TEAM_URL_COUNTED, client_kind, disabled_by


async def test_a_new_user_is_told_how_to_connect_a_first_source(home: Path) -> None:
    async with Harness.start(home) as h:
        guide = await h.call("get_started")
    steps = {s["step"]: s["done"] for s in guide.ai.structured["steps"]}
    assert steps == {"connect a source": False, "first pull": False}
    assert guide.ai.structured["next"] == "connect a source"
    for hint in ("add_source", "sqlite", "csv", "excel", "use_sample"):
        assert hint in guide.ai.text
    assert "password" not in guide.ai.text.lower()  # a database's password never goes through the chat (#62)


async def test_the_sample_gets_a_new_user_from_install_to_a_first_pull(home: Path) -> None:
    async with Harness.start(home) as h:
        started = await h.call("get_started", use_sample=True)
        assert not started.is_error and started.ai.structured["next"] == "first pull"
        suggestion = started.ai.structured["try"]
        pulled = await h.call("query", **suggestion)
        done = await h.call("get_started")
        listed = await h.call("list_sources")
    assert suggestion["source"] == "sample"
    assert not pulled.is_error and pulled.ai.structured["row_count"] > 0
    assert {s["step"]: s["done"] for s in done.ai.structured["steps"]} == {"connect a source": True,
                                                                          "first pull": True}
    assert done.ai.structured["next"] is None
    assert "history" in done.ai.text and "save_pull" in done.ai.text
    assert [s["name"] for s in listed.ai.structured["sources"]] == ["sample"]
    assert (home / "sample.sqlite").exists()  # in the app-data folder, next to the local database


async def test_with_their_own_file_the_guide_suggests_a_first_pull_on_it(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as shop:
        guide = await shop.call("get_started")
        pulled = await shop.call("query", **guide.ai.structured["try"])
    assert guide.ai.structured["try"]["source"] == "shop"
    assert not pulled.is_error


async def test_the_suggested_first_pull_leaves_out_what_the_user_blocked(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as shop:
        assert not (await shop.call("block", source="shop", table="customers", column="name")).is_error
        guide = await shop.call("get_started")
        pulled = await shop.call("query", **guide.ai.structured["try"])
    assert "*" not in guide.ai.structured["try"]["sql"] and "name" not in guide.ai.structured["try"]["sql"]
    assert not pulled.is_error, pulled.ai.text


async def test_the_guide_says_which_source_would_not_open_and_why(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as shop:
        shop_db.unlink()  # the user moved the file away
        guide = await shop.call("get_started")
    assert not guide.is_error
    failed = guide.ai.structured["failed_sources"]
    assert [f["source"] for f in failed] == ["shop"]
    assert failed[0]["error"]
    assert "shop" in guide.ai.text and failed[0]["error"] in guide.ai.text


async def test_the_first_run_guide_describes_free_only_never_teams_or_governance(home: Path, shop_db: Path) -> None:
    async with Harness.start(home) as h:
        fresh = await h.call("get_started")
        sample = await h.call("get_started", use_sample=True)
        pulled = await h.call("query", **sample.ai.structured["try"])
        done = await h.call("get_started")
    assert not pulled.is_error
    for guide in (fresh, sample, done):
        seen = (guide.ai.text + json.dumps(guide.ai.structured)).lower()
        for word in ("team", "governance", TEAM_URL, TEAM_URL_COUNTED):
            assert word not in seen, (word, guide.ai.text)


def test_the_ai_client_is_reported_as_a_kind_never_its_name() -> None:
    assert client_kind("claude-code") == "claude-code"
    assert client_kind("claude-ai") == "claude-desktop"
    assert client_kind("codex-mcp-client") == "chatgpt"
    assert client_kind("acme-internal-bot 1.2") == "other"
    assert client_kind(None) == "other"


def test_the_environment_can_turn_usage_counts_off_for_good() -> None:
    assert disabled_by({"PORTWARD_USAGE": "off"})
    assert disabled_by({"DO_NOT_TRACK": "1"})
    assert disabled_by({"DO_NOT_TRACK": "0"}) is None
    assert disabled_by({}) is None
