"""Per-column share options (#52): include, hide, anonymise, totals only, as the receiver sees the shared table.

The options are applied on the sender's machine before the snapshot is sealed, so a hidden value, a raw value
behind an anonymised code, or a single row behind a total never leaves it, even encrypted."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from harness import connected
from test_share import Drafts, Service, receive, sign_in

from portward_free.sharing import Sharing

pytest_plugins = ["test_share"]  # its fixtures: the share service on 127.0.0.1, drafts, sharing, sender, recipient
SALARY_MARKER = 987_654


@pytest.fixture()
def staff_db(tmp_path: Path) -> Path:
    """12 people: north has 6, south 5, east 1 (a group of one)."""
    path = tmp_path / "staff.sqlite"
    people = ([(i, f"North {i}", "ada@acme.example" if i == 1 else f"n{i}@acme.example", "north", 100 + i)
               for i in range(1, 7)]
              + [(i, f"South {i}", f"s{i}@acme.example", "south", 200 + i) for i in range(7, 12)]
              + [(12, "East 12", "ada@acme.example", "east", SALARY_MARKER)])
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY, name TEXT, email TEXT, region TEXT, salary INT)")
        db.executemany("INSERT INTO staff VALUES (?, ?, ?, ?, ?)", people)
    db.close()
    return path


async def shared_view(home: Path, db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
                      sql: str, options: dict[str, str] | None, recipient: str) -> tuple[Any, dict[str, Any]]:
    """Pull, share with `options`, and open the share as the receiver: (the share tool's reply, the snapshot)."""
    async with connected(home, db, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql=sql, question="Staff")
        assert not pulled.is_error, pulled.ai.text
        await sign_in(h, service, sender)
        args: dict[str, Any] = {"pull_id": pulled.ai.structured["pull_id"], "recipient": recipient}
        if options is not None:
            args["options"] = options
        shared = await h.call("share", **args)
    if shared.is_error:
        return shared, {}
    return shared, receive(service, drafts.link(), recipient)


async def test_a_hidden_column_is_absent_from_the_shared_view(home: Path, staff_db: Path, service: Service,
                                                              drafts: Drafts, sender: str, sharing: Sharing,
                                                              recipient: str) -> None:
    shared, snapshot = await shared_view(home, staff_db, service, drafts, sender, sharing,
                                         "SELECT id, name, email FROM staff ORDER BY id", {"email": "hide"}, recipient)
    assert not shared.is_error, shared.ai.text
    assert snapshot["columns"] == ["id", "name"]
    assert snapshot["rows"][0] == [1, "North 1"]
    assert "acme.example" not in json.dumps(snapshot)
    assert shared.ai.structured["columns"] == ["id", "name"]


async def test_an_anonymised_column_shows_the_same_code_for_the_same_value_within_a_share(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    shared, snapshot = await shared_view(home, staff_db, service, drafts, sender, sharing,
                                         "SELECT id, email FROM staff ORDER BY id", {"email": "anonymise"}, recipient)
    assert not shared.is_error, shared.ai.text
    codes = {row[0]: row[1] for row in snapshot["rows"]}
    assert "acme.example" not in json.dumps(snapshot)
    assert codes[1] == codes[12]  # both ada@acme.example
    assert len(set(codes.values())) == 11  # 12 rows, 11 different addresses
    assert all(isinstance(code, str) and code.startswith("anon-") for code in codes.values())
    assert snapshot["anonymised"] == ["email"]


async def test_the_same_value_gets_a_different_code_in_another_share(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    async with connected(home, staff_db, sharing=sharing) as h:
        pulled = await h.call("query", source="shop", sql="SELECT email FROM staff WHERE id = 1", question="Ada")
        await sign_in(h, service, sender)
        codes = []
        for _ in range(2):
            shared = await h.call("share", pull_id=pulled.ai.structured["pull_id"], recipient=recipient,
                                  options={"email": "anonymise"})
            assert not shared.is_error, shared.ai.text
            codes.append(receive(service, drafts.link(), recipient)["rows"][0][0])
    assert codes[0] != codes[1] and "acme" not in "".join(codes)


async def test_a_totals_only_column_shows_only_totals_per_group_and_leaves_out_small_groups(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    shared, snapshot = await shared_view(home, staff_db, service, drafts, sender, sharing,
                                         "SELECT region, name, salary FROM staff ORDER BY id",
                                         {"name": "hide", "salary": "totals"}, recipient)
    assert not shared.is_error, shared.ai.text
    assert snapshot["columns"] == ["region", "row count", "salary (total)"]
    north = sum(100 + i for i in range(1, 7))
    south = sum(200 + i for i in range(7, 12))
    assert snapshot["rows"] == [["north", 6, north], ["south", 5, south]]
    assert str(SALARY_MARKER) not in json.dumps(snapshot)  # east is a group of one: left out, and no grand total
    assert snapshot["totals"] == {"columns": ["salary"], "min_group_rows": 5, "from_rows": 12, "rows_left_out": 1}


async def test_totals_over_the_whole_table_when_every_other_column_is_hidden(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    shared, snapshot = await shared_view(home, staff_db, service, drafts, sender, sharing,
                                         "SELECT name, salary FROM staff", {"name": "hide", "salary": "totals"},
                                         recipient)
    assert not shared.is_error, shared.ai.text
    total = sum(100 + i for i in range(1, 7)) + sum(200 + i for i in range(7, 12)) + SALARY_MARKER
    assert snapshot["columns"] == ["row count", "salary (total)"]
    assert snapshot["rows"] == [[12, total]]


async def test_totals_grouped_by_a_column_of_single_rows_are_not_shared(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    shared, snapshot = await shared_view(home, staff_db, service, drafts, sender, sharing,
                                         "SELECT id, salary FROM staff", {"salary": "totals"}, recipient)
    assert shared.is_error and "fewer than 5" in shared.ai.text
    assert snapshot == {} and drafts.opened == []


async def test_an_option_for_a_column_the_share_does_not_have_is_refused(
        home: Path, staff_db: Path, service: Service, drafts: Drafts, sender: str, sharing: Sharing,
        recipient: str) -> None:
    shared, _ = await shared_view(home, staff_db, service, drafts, sender, sharing,
                                  "SELECT id, name FROM staff", {"salary": "hide"}, recipient)
    assert shared.is_error and "no column 'salary'" in shared.ai.text and drafts.opened == []
