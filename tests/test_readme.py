"""The README's claims about what is counted, stored and sent are checked against the code, so they can't drift.

Each list in the README's "What's counted, stored and sent" section names one thing per bullet, in backticks at the
start of the bullet (a usage field, a table in the local file, a keychain entry, a snapshot field). These tests compare
those names with the code's own lists, and the numbers and addresses in the prose with the code's constants.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from install_facts import FREE
from portward_free import account, ai_log, kept, usage
from portward_free.keychain import account_for
from portward_free.pull import Pull
from portward_free.sharing import options, service, snapshot, tools
from portward_free.storage import LocalStore
from portward_free.sync import crypto

README = (FREE / "README.md").read_text()
SUPPORT = "support@portward.ai"
BULLET_NAME = re.compile(r"^- `([^`]+)`")


def section(heading: str) -> str:
    """The text under a `##`/`###` heading, up to the next heading of any level."""
    match = re.search(rf"^#+ {re.escape(heading)}\n(.*?)(?=^#+ |\Z)", README, re.S | re.M)
    assert match, f"the README has no '{heading}' heading"
    return match.group(1)


def named(heading: str) -> list[str]:
    """The backticked names that start each bullet under a heading, in order."""
    return [m.group(1) for line in section(heading).splitlines() if (m := BULLET_NAME.match(line))]


def prose() -> str:
    """The README with line breaks folded, so a phrase may wrap."""
    return re.sub(r"\s+", " ", README)


class Recorder:
    def send(self, payload: dict) -> bool:
        return True


def test_the_usage_fields_listed_are_exactly_the_fields_sent(tmp_path: Path) -> None:
    store = LocalStore.open(tmp_path / "portward.db")
    counts = usage.Usage(store, Recorder(), now=lambda: datetime(2026, 11, 2, tzinfo=UTC))
    counts.seen("page")
    counts.pulled("sqlite")
    (report,) = counts.reports()

    assert named("Usage counts") == list(usage.FIELDS)
    assert list(report) == list(usage.FIELDS)


def test_the_usage_section_names_where_counts_go_and_how_to_turn_them_off() -> None:
    text = re.sub(r"\s+", " ", section("Usage counts"))
    for claim in (usage.USAGE_URL, "PORTWARD_USAGE=off", "DO_NOT_TRACK=1", "usage_counts"):
        assert claim in text
    assert usage.SEND_INTERVAL.total_seconds() == 3600 and "an hour" in text


def test_every_table_in_the_local_file_is_described(tmp_path: Path) -> None:
    store = LocalStore.open(tmp_path / "portward.db")
    tables = {row[0] for row in store._db.execute(  # noqa: SLF001 (the file's real schema, not an API)
        "SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert sorted(named("On your computer")) == sorted(tables)


def test_every_keychain_entry_is_named() -> None:
    assert named("In your keychain") == [account_for("<name>"), kept.KEY_ACCOUNT, tools.TOKEN_ACCOUNT,
                                         account.TOKEN_ACCOUNT, crypto.DEVICE_KEY_ACCOUNT]


def test_the_fields_of_a_shared_table_are_the_snapshot_fields() -> None:
    pull = Pull(source="shop", question="q", sql="SELECT id, n FROM t", tables=("t",), columns_read=("id", "n"),
                columns=("id", "n"), rows=tuple((1, n) for n in range(options.MIN_GROUP_ROWS)), truncated=False)
    shared = options.with_options(snapshot.snapshot_of(pull, title="t", pulled_at="now"),
                                  {"id": "anonymise", "n": "totals"})  # every field an option can add

    assert named("What a share holds") == list(shared)


def test_the_retention_and_expiry_days_match_the_code() -> None:
    text = prose()
    assert f"deleted after {kept.KEEP_DAYS} days" in text
    assert f"deleted after {ai_log.AI_LOG_DAYS} days" in text
    assert f"expire after {tools.SHARE_DAYS} days" in text
    assert f"can last up to {tools.PERSONAL_SHARE_DAYS} days" in text
    if SHARE_STORE.exists():  # the service's own limit (it decides; the tool only describes it)
        assert share_store_constant("PERSONAL_SHARE_DAYS") == tools.PERSONAL_SHARE_DAYS


def test_the_addresses_it_talks_to_are_the_codes() -> None:
    text = prose()
    for address in (usage.USAGE_URL, usage.TEAM_URL, usage.TEAM_URL_COUNTED, service.DEFAULT_SHARE_URL,
                    service.SHARE_URL_ENV, account.DEFAULT_ACCOUNT_URL, account.ACCOUNT_URL_ENV):
        assert address in text, address


def test_it_names_a_support_address_and_the_team_link() -> None:
    support = section("Support")
    assert f"mailto:{SUPPORT}" in support
    assert "Need this for your team?" in support and usage.TEAM_URL in support


SHARE_STORE = FREE.parent / "src" / "portward" / "control" / "share_store.py"
DAY = 86_400


def share_store_constant(name: str) -> int:
    """A constant of the share service's store, read from its source (this package never imports the service)."""
    match = re.search(rf"^{name} = ((?:[0-9_]+|DAY)(?: \* (?:[0-9_]+|DAY))*)\b", SHARE_STORE.read_text(), re.M)
    assert match, name
    product = 1
    for factor in match.group(1).split("*"):
        product *= DAY if factor.strip() == "DAY" else int(factor.strip())
    return product


@pytest.mark.skipif(not SHARE_STORE.exists(), reason="the share service's code isn't in this repo")
def test_the_share_services_code_and_purge_times_match_its_code() -> None:
    text = re.sub(r"\s+", " ", section("What the share service keeps"))
    assert share_store_constant("CODE_TTL") == 10 * 60 and "last 10 minutes" in text
    assert f"allow {share_store_constant('CODE_ATTEMPTS')} tries" in text
    assert share_store_constant("PURGE_AFTER") == DAY and "the rest a day later" in text


@pytest.mark.skipif(not SHARE_STORE.exists(), reason="the share service's code isn't in this repo")
def test_the_share_size_cap_and_token_lifetimes_match_the_code() -> None:
    text = prose()
    assert snapshot.MAX_SEALED_BYTES == 5 * 1024 * 1024 and "up to 5 MB encrypted" in text
    assert share_store_constant("TOKEN_DAYS") == 90 and "after 90 days" in text
    assert tools.ROTATE_BEFORE.days == 30 and "in its last 30 days" in text
    assert share_store_constant("CONFIRMED_DAYS") == 365 and "up to a year after you confirmed" in text
    assert share_store_constant("SENDER_PURGE_AFTER") == 30 * DAY and "deleted 30 days after it expires" in text


def test_the_sync_section_names_the_terminal_command_and_the_key_derivation() -> None:
    text = re.sub(r"\s+", " ", section("Sync (Personal)"))
    assert "portward-free sync-restore" in text and "Argon2id" in text and "AES-256-GCM" in text
    assert crypto.ARGON_MEMORY_KIB == 64 * 1024 and crypto.RECOVERY_BYTES * 8 == 160


def test_totals_only_drops_small_groups_and_has_no_grand_total() -> None:
    text = prose()
    assert f"groups of fewer than {options.MIN_GROUP_ROWS} rows are left out" in text
    assert "there is no grand total" in text


def test_adding_a_file_names_its_terminal_command_and_needs_a_person() -> None:
    text = re.sub(r"\s+", " ", section("Adding a file"))
    assert "portward-free==0.1.0 add-file <name> <kind> <path>" in text
    assert "runs only at a terminal you're using" in text


@pytest.mark.skipif(not SHARE_STORE.exists(), reason="the share service's code isn't in this repo")
def test_the_sharing_code_limits_match_the_code() -> None:
    text = re.sub(r"\s+", " ", section("Sharing"))
    shares = (FREE.parent / "src" / "portward" / "control" / "shares.py").read_text()
    assert share_store_constant("SENDER_GUESSES") == 10 and "at most 10 wrong codes a day" in text
    assert '"share_verify_email": 3' in shares and "at most 3 code emails an hour" in text


SYNC_STORE = FREE.parent / "src" / "portward" / "control" / "sync_store.py"


@pytest.mark.skipif(not SYNC_STORE.exists(), reason="the sync service's code isn't in this repo")
def test_the_sync_limits_match_the_code() -> None:
    code = SYNC_STORE.read_text()
    text = re.sub(r"\s+", " ", section("Sync (Personal)"))
    assert "MAX_RECORDS = 100_000" in code and "MAX_BYTES = 256 * 1024 * 1024" in code
    assert "at most 100,000 records and 256 MB" in text
