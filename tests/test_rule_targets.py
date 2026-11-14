"""Block rules belong to what a source points at (a file, a database), not to the name it was added under: a second
copy of the source, under any name or path, is blocked the same way, and so is the source added again after it was
removed."""

import os
from pathlib import Path

from harness import Harness, connected
from mcp.types import ElicitResult
from conftest import SECRETS
from test_rules import leaked

SSN = "SELECT name, ssn FROM customers"
STAR = "SELECT * FROM customers"


async def yes(context, params) -> ElicitResult:
    """The user says yes to whatever Portward asks (here: letting the AI read a file it adds)."""
    return ElicitResult(action="accept", content={field: True for field in params.requested_schema["properties"]})


async def _probe(h: Harness, name: str) -> list:
    return [await h.call("query", source=name, question="?", sql=sql) for sql in (SSN, STAR)]


async def test_a_second_copy_of_a_file_under_another_name_is_blocked_too(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people", elicitation=yes) as h:
        assert not (await h.call("block", source="people", table="customers", column="ssn")).is_error
        added = await h.add_file("copy", str(people_db))
        assert not added.is_error, added.ai.text
        replies = await _probe(h, "copy")
        listed = await h.call("list_sources")
    assert not any(leaked(r) for r in replies)
    copy = next(s for s in listed.ai.structured["sources"] if s["name"] == "copy")
    assert "ssn" not in str(copy["tables"])


async def test_another_path_to_the_same_file_is_blocked_too(home: Path, people_db: Path, tmp_path: Path) -> None:
    link = tmp_path / "elsewhere" / "linked.sqlite"
    link.parent.mkdir()
    os.symlink(people_db, link)
    hard = tmp_path / "elsewhere" / "hard.sqlite"
    os.link(people_db, hard)
    async with connected(home, people_db, name="people", elicitation=yes) as h:
        await h.call("block", source="people", table="customers", column="ssn")
        await h.add_file("linked", str(link))
        await h.add_file("hard", str(hard))
        replies = [*await _probe(h, "linked"), *await _probe(h, "hard")]
    assert not any(leaked(r) for r in replies)


async def test_rules_kept_after_a_source_is_removed_apply_when_it_comes_back_under_a_new_name(
        home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people", elicitation=yes) as h:
        await h.call("block", source="people", table="customers", column="ssn")
        await h.call("remove_source", name="people")
        await h.add_file("again", str(people_db))
        replies = await _probe(h, "again")
        rules = await h.call("list_rules", source="again")
    assert not any(leaked(r) for r in replies)
    assert [r["column"] for r in rules.ai.structured["rules"]] == ["ssn"]


async def test_unblocking_through_the_copy_unblocks_the_file_for_every_name(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people", elicitation=yes) as h:
        await h.call("block", source="people", table="customers", column="ssn")
        await h.add_file("copy", str(people_db))
        await h.call("block", source="copy", table="customers", column="ssn")  # already blocked through people
        removed = await h.call("unblock", source="copy", table="customers", column="ssn")
        rules = await h.call("list_rules")
        read = await h.call("query", source="people", question="?", sql=SSN)
    assert not removed.is_error, removed.ai.text
    assert rules.ai.structured["rules"] == []
    assert not read.is_error and SECRETS[0] in read.ai.text


async def test_a_different_file_with_the_same_tables_is_not_blocked(home: Path, people_db: Path,
                                                                    tmp_path: Path) -> None:
    other = tmp_path / "other.sqlite"
    other.write_bytes(people_db.read_bytes())  # a separate copy the user made: its own file
    async with connected(home, people_db, name="people", elicitation=yes) as h:
        await h.call("block", source="people", table="customers", column="ssn")
        await h.add_file("other", str(other))
        read = await h.call("query", source="other", question="?", sql=SSN)
    assert not read.is_error, read.ai.text
