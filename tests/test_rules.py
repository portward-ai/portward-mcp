"""Block rules: a blocked table or column never reaches the AI, whatever SQL the AI sends."""

import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from harness import Harness, connected
from mcp.types import ElicitResult
from openpyxl import Workbook

from conftest import SECRETS


def leaked(reply) -> bool:
    seen = reply.ai.text + repr(reply.ai.structured)
    return any(s in seen for s in SECRETS)


async def test_a_blocked_column_asked_for_directly_never_reaches_the_ai(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        blocked = await h.call("block", source="people", table="customers", column="ssn")
        assert not blocked.is_error, blocked.ai.text
        reply = await h.call("query", source="people", question="?", sql="SELECT name, ssn FROM customers")
    assert reply.is_error
    assert not leaked(reply)


# Every way we could think of to reach customers.ssn (or learn about its values) once it is blocked.
ROUTES_TO_A_BLOCKED_COLUMN = [
    "SELECT ssn FROM customers",
    "SELECT customers.ssn FROM customers",
    'SELECT "ssn" FROM customers WHERE "ssn" <> \'ssn\'',
    "SELECT c.ssn FROM customers c",
    "SELECT c.ssn AS harmless FROM customers AS c",
    "SELECT SSN FROM CUSTOMERS",
    'SELECT "SSN" FROM "Customers"',
    "SELECT o.total, c.ssn FROM orders o JOIN customers c ON c.id = o.customer_id",
    "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.ssn IS NOT NULL",
    "SELECT x.s FROM (SELECT ssn AS s FROM customers) x",
    "WITH leak AS (SELECT ssn FROM customers) SELECT * FROM leak",
    "WITH leak(s) AS (SELECT * FROM customers) SELECT * FROM leak",
    "SELECT (SELECT ssn FROM customers LIMIT 1) AS v FROM orders",
    "SELECT name FROM customers WHERE id IN (SELECT id FROM customers WHERE ssn LIKE '123%')",
    "SELECT name FROM customers WHERE ssn LIKE '123%'",
    "SELECT name FROM customers ORDER BY ssn",
    "SELECT substr(ssn, 1, 3) || '' AS a FROM customers",
    "SELECT upper(ssn) FROM customers",
    "SELECT CAST(ssn AS TEXT) AS t FROM customers",
    "SELECT CASE WHEN ssn > '5' THEN 1 ELSE 0 END AS hi FROM customers",
    "SELECT max(ssn) FROM customers",
    "SELECT group_concat(ssn) FROM customers",
    "SELECT count(DISTINCT ssn) FROM customers",
    "SELECT ssn, count(*) FROM customers GROUP BY ssn",
    "SELECT name FROM customers GROUP BY name HAVING max(ssn) > '0'",
    "SELECT name FROM customers UNION SELECT ssn FROM customers",
    "SELECT ssn FROM customers UNION ALL SELECT name FROM customers",
    "SELECT * FROM customers JOIN customers c2 USING (ssn)",
    "SELECT json_object('s', ssn) AS j FROM customers",
    "SELECT row_number() OVER (ORDER BY ssn) AS r, name FROM customers",
    "SELECT name FROM customers WHERE EXISTS (SELECT 1 FROM customers c2 WHERE c2.ssn = '123-45-6789')",
    "SELECT rowid, * FROM customers",
    "SELECT main.customers.ssn FROM main.customers",
    "SELECT ssn FROM main.customers",
    "SELECT * FROM pragma_table_info('customers')",
    "SELECT * FROM sqlite_master",
    "SELECT sql FROM sqlite_schema",
    "WITH customers AS (SELECT * FROM notes) SELECT * FROM customers",
]


@pytest.mark.parametrize("sql", ROUTES_TO_A_BLOCKED_COLUMN)
async def test_a_blocked_column_never_reaches_the_ai_by_any_route(home: Path, people_db: Path, sql: str) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        reply = await h.call("query", source="people", question="?", sql=sql)
    # refused outright: filtering, ordering or grouping on a blocked column leaks it too, without its values
    assert reply.is_error, reply.ai.text
    assert reply.ai.structured is None
    assert not leaked(reply), reply.ai.text
    assert h.stored("pulls") == []


@pytest.mark.parametrize("sql", [
    "SELECT number FROM cards",
    "SELECT * FROM cards",
    "SELECT name FROM cards WHERE number LIKE '1%'",
    "SELECT c.name FROM customers c JOIN cards k ON k.name = c.name",
])
async def test_a_blocked_column_never_reaches_the_ai_through_a_view(home: Path, people_db: Path, sql: str) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        reply = await h.call("query", source="people", question="?", sql=sql)
    assert reply.is_error, reply.ai.text
    assert not leaked(reply)


ROUTES_TO_A_BLOCKED_TABLE = [
    "SELECT * FROM notes",
    "SELECT body FROM notes",
    "SELECT n.body FROM notes AS n",
    'SELECT * FROM "NOTES"',
    "SELECT count(*) FROM notes",
    "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM notes WHERE body LIKE 'top%')",
    "SELECT id FROM orders UNION SELECT body FROM notes",
    "WITH n AS (SELECT * FROM notes) SELECT * FROM n",
    "WITH notes AS (SELECT * FROM orders) SELECT * FROM notes",
    "SELECT * FROM memo",
    "SELECT count(*) FROM memo",
]


@pytest.mark.parametrize("sql", ROUTES_TO_A_BLOCKED_TABLE)
async def test_a_blocked_table_cant_be_queried(home: Path, people_db: Path, sql: str) -> None:
    async with connected(home, people_db, name="people") as h:
        blocked = await h.call("block", source="people", table="notes")
        assert not blocked.is_error, blocked.ai.text
        reply = await h.call("query", source="people", question="?", sql=sql)
    assert reply.is_error, reply.ai.text
    assert "top secret" not in reply.ai.text


async def test_a_blocked_primary_key_cant_be_read_back_through_rowid(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("block", source="people", table="customers", column="id")
        via_table = await h.call("query", source="people", question="?", sql="SELECT rowid, _rowid_, oid FROM customers")
        via_view = await h.call("query", source="people", question="?", sql="SELECT r FROM ids")
        allowed = await h.call("query", source="people", question="?", sql="SELECT name FROM customers ORDER BY name")
    assert via_table.is_error, via_table.ai.text
    assert via_view.is_error, via_view.ai.text
    assert not allowed.is_error, allowed.ai.text


async def test_the_ai_is_told_the_user_blocked_it(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        await h.call("block", source="people", table="notes")
        column = await h.call("query", source="people", question="?", sql="SELECT name, ssn FROM customers")
        table = await h.call("query", source="people", question="?", sql="SELECT * FROM notes")
    assert "customers.ssn is blocked by the user" in column.ai.text
    assert "notes is blocked by the user" in table.ai.text


async def test_the_result_is_checked_again_before_it_reaches_the_ai(home: Path, people_db: Path) -> None:
    # the query itself only reads allowed columns; it's what comes back that carries a blocked column's name
    async with connected(home, people_db, name="people") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        reply = await h.call("query", source="people", question="?", sql="SELECT name AS SSN FROM customers")
        renamed = await h.call("query", source="people", question="?", sql="SELECT name AS who FROM customers")
    assert reply.is_error
    assert reply.ai.structured is None and "Ada" not in reply.ai.text
    assert "rules block" in reply.ai.text
    assert not renamed.is_error, renamed.ai.text


async def test_select_star_returns_every_column_but_the_blocked_one(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people", mode="results") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        star = await h.call("query", source="people", question="?", sql="SELECT * FROM customers ORDER BY id")
        table_star = await h.call("query", source="people", question="?",
                                  sql="SELECT c.*, o.total FROM customers c JOIN orders o ON o.customer_id = c.id "
                                      "ORDER BY c.id")
    assert not star.is_error, star.ai.text
    assert star.ai.structured["columns"] == ["id", "name"]
    assert star.ai.structured["rows"] == [[1, "Ada"], [2, "Grace"], [3, "Linus"]]
    assert not table_star.is_error, table_star.ai.text
    assert table_star.ai.structured["columns"] == ["id", "name", "total"]


async def test_queries_that_avoid_the_blocked_column_still_work(home: Path, people_db: Path) -> None:
    sql = ("WITH spend AS (SELECT customer_id, sum(total) AS spent FROM orders GROUP BY customer_id) "
           "SELECT upper(c.name) AS name, s.spent FROM customers c JOIN spend s ON s.customer_id = c.id "
           "WHERE c.id IN (SELECT id FROM customers) ORDER BY c.id")
    async with connected(home, people_db, name="people", mode="results") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        reply = await h.call("query", source="people", question="?", sql=sql)
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["rows"] == [["ADA", 10.0], ["GRACE", 20.0], ["LINUS", 30.0]]


def tables_of(listing, source: str) -> dict[str, list[str]]:
    entry = next(s for s in listing.ai.structured["sources"] if s["name"] == source)
    return {t["name"]: t["columns"] for t in entry["tables"]}


async def test_the_ai_isnt_shown_blocked_tables_or_columns(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        await h.call("block", source="people", table="notes")
        listing = await h.call("list_sources")
        readded = await h.add_file("people", str(people_db))
    tables = tables_of(listing, "people")
    assert tables["customers"] == ["id", "name"]
    assert "notes" not in tables
    assert tables["orders"] == ["id", "customer_id", "total"]
    assert "notes" not in readded.ai.structured["tables"]


async def test_the_user_can_list_rules_per_source_and_they_survive_a_restart(
        home: Path, people_db: Path, shop_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.add_file("shop", str(shop_db))
        await h.call("block", source="people", table="CUSTOMERS", column="SSN")  # stored as the source names them
        await h.call("block", source="people", table="notes")
        again = await h.call("block", source="people", table="customers", column="ssn")
        await h.call("block", source="shop", table="customers", column="region")
    assert again.ai.structured["already_blocked"] is True
    async with Harness.start(home) as h:
        everything = await h.call("list_rules")
        people = await h.call("list_rules", source="people")
        reply = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers")
    assert everything.ai.structured["rules"] == [
        {"source": "people", "table": "customers", "column": "ssn"},
        {"source": "people", "table": "notes", "column": None},
        {"source": "shop", "table": "customers", "column": "region"},
    ]
    assert [r["source"] for r in people.ai.structured["rules"]] == ["people", "people"]
    assert "customers.ssn" in people.ai.text and "notes" in people.ai.text
    assert reply.is_error and not leaked(reply)


class User:
    """Answers the client's prompt (elicitation) the way a person would, and remembers what they were asked."""

    def __init__(self, action: str, content: dict | None = None) -> None:
        self.action, self.content = action, content
        self.asked: list[str] = []

    async def __call__(self, context, params) -> ElicitResult:
        self.asked.append(params.message)
        return ElicitResult(action=self.action, content=self.content if self.action == "accept" else None)


@pytest.mark.parametrize("transport", ["inprocess", "stdio"])
async def test_a_rule_is_removed_only_when_the_user_confirms_it(home: Path, people_db: Path, transport: str) -> None:
    user = User("accept", {"remove": True})
    async with connected(home, people_db, name="people", elicitation=user, transport=transport, mode="results") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        removed = await h.call("unblock", source="people", table="customers", column="ssn")
        rules = await h.call("list_rules")
        reply = await h.call("query", source="people", question="?", sql="SELECT ssn FROM customers ORDER BY id")
    assert not removed.is_error, removed.ai.text
    assert len(user.asked) == 1 and "customers.ssn" in user.asked[0]
    assert rules.ai.structured["rules"] == []
    assert not reply.is_error and reply.ai.structured["rows"][0] == [SECRETS[0]]


@pytest.mark.parametrize("user", [User("decline"), User("cancel"), User("accept", {"remove": False})])
async def test_a_rule_stays_when_the_user_says_no(home: Path, people_db: Path, user: User) -> None:
    async with connected(home, people_db, name="people", elicitation=user) as h:
        await h.call("block", source="people", table="customers", column="ssn")
        refused = await h.call("unblock", source="people", table="customers", column="ssn")
        rules = await h.call("list_rules")
    assert refused.is_error
    assert len(user.asked) == 1
    assert rules.ai.structured["rules"] == [{"source": "people", "table": "customers", "column": "ssn"}]


async def test_a_rule_stays_when_the_client_cant_ask_the_user(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:  # no elicitation support
        await h.call("block", source="people", table="notes")
        refused = await h.call("unblock", source="people", table="notes")
        rules = await h.call("list_rules")
    assert refused.is_error
    assert "can't ask you" in refused.ai.text
    assert len(rules.ai.structured["rules"]) == 1


async def test_the_ai_cant_answer_for_the_user(home: Path, people_db: Path) -> None:
    yes = {"action": "accept", "content": {"remove": True}, "data": {"remove": True}, "remove": True}
    async with connected(home, people_db, name="people") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        schema = (await h.tools())["unblock"].input_schema
        try:
            await h.call("unblock", source="people", table="customers", column="ssn", answer=yes)
        except Exception:  # noqa: S110 - refused by argument validation is fine too
            pass
        rules = await h.call("list_rules")
    assert "answer" not in schema.get("properties", {})
    assert len(rules.ai.structured["rules"]) == 1


async def test_removing_a_rule_that_isnt_there_says_so(home: Path, people_db: Path) -> None:
    user = User("accept", {"remove": True})
    async with connected(home, people_db, name="people", elicitation=user) as h:
        reply = await h.call("unblock", source="people", table="customers", column="ssn")
    assert reply.is_error and "isn't blocked" in reply.ai.text
    assert user.asked == []


async def test_a_rule_added_after_a_pull_holds_when_it_is_rerun_tweaked_compared_or_exported(
        home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people", mode="results") as h:
        before = await h.call("query", source="people", question="?", sql="SELECT name, ssn FROM customers")
        other = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        assert not before.is_error and leaked(before)  # not blocked yet
        await h.call("block", source="people", table="customers", column="ssn")
        pull_id, other_id = before.ai.structured["pull_id"], other.ai.structured["pull_id"]
        saved = await h.call("save_pull", pull_id=pull_id, name="Customer numbers")
        assert not saved.is_error, saved.ai.text
        by_name = await h.call("run_saved", name="Customer numbers")
        rerun = await h.call("rerun", pull_id=pull_id)
        tweak = await h.call("tweak", pull_id=pull_id, sql="SELECT ssn FROM customers WHERE id = 1")
        compared = await h.call("compare", pull_id=pull_id, other_pull_id=other_id)
        exported = await h.call("export", pull_id=other_id, format="csv")
    for reply in (by_name, rerun, tweak, compared):
        assert not reply.is_error or "ssn" in reply.ai.text  # in results mode it runs without ssn (#117)
        assert not leaked(reply)
    assert not exported.is_error, exported.ai.text  # SELECT * still runs, without the blocked column
    written = Path(exported.ai.structured["path"]).read_text()
    assert "ssn" not in written and not any(s in written for s in SECRETS)
    assert "Ada" in written


@pytest.mark.parametrize("sql", [
    "SELECT ssn FROM people", "SELECT COLUMNS('.*') FROM people", "SELECT p FROM people p",
    "SELECT * EXCLUDE (name) FROM people", "SELECT name FROM people WHERE ssn LIKE '1%'",
])
async def test_a_blocked_column_of_a_csv_file_never_reaches_the_ai(home: Path, tmp_path: Path, sql: str) -> None:
    csv = tmp_path / "people.csv"
    csv.write_text("id,name,ssn\n" + "".join(f"{i},n{i},{s}\n" for i, s in enumerate(SECRETS)))
    async with Harness.start(home) as h:
        added = await h.add_file("file", str(csv), kind="csv")
        assert not added.is_error, added.ai.text
        await h.call("block", source="file", table="people", column="ssn")
        reply = await h.call("query", source="file", question="?", sql=sql)
    assert not leaked(reply), reply.ai.text


@pytest.mark.parametrize("ask_in_client", [True, False])
async def test_rows_held_from_before_a_rule_arent_sent_after_it(
        home: Path, shop_db: Path, ask_in_client: bool) -> None:
    user = User("accept", {"approve": True}) if ask_in_client else None
    async with connected(home, shop_db, elicitation=user, mode="results") as h:
        pulled = await h.call("query", source="shop", question="?", sql="SELECT id, total FROM orders ORDER BY id")
        await h.call("block", source="shop", table="orders", column="total")
        pull_id = pulled.ai.structured["pull_id"]
        first = await h.call("more_rows", pull_id=pull_id)
        again = await h.call("more_rows", pull_id=pull_id, approved=True)
    for reply in (first, again):
        assert not reply.ai.structured or not reply.ai.structured.get("rows"), reply.ai.text
        assert "| 21 | 21.0 |" not in reply.ai.text
    assert "rules changed" in again.ai.text


async def test_kept_rows_from_before_a_rule_arent_shown_after_it(
        home: Path, people_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # times are stored to the second and a tie counts as "rule is newer", so let each timestamp be a second apart
    ticks = itertools.count()
    start = datetime(2026, 11, 3, tzinfo=UTC)
    monkeypatch.setattr("portward_free.storage.store.now",
                        lambda: (start + timedelta(seconds=next(ticks))).isoformat(timespec="seconds"))
    async with connected(home, people_db, name="people") as h:
        await h.call("keep_rows", on=True)
        pulled = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        orders = await h.call("query", source="people", question="?", sql="SELECT * FROM orders")
        await h.call("block", source="people", table="customers", column="ssn")
        after = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        stale = await h.call("kept_rows", pull_id=pulled.ai.structured["pull_id"])
        fresh = await h.call("kept_rows", pull_id=after.ai.structured["pull_id"])
        other = await h.call("kept_rows", pull_id=orders.ai.structured["pull_id"])
    assert stale.is_error and not leaked(stale), stale.ai.text
    assert "rules changed" in stale.ai.text
    assert not fresh.is_error, fresh.ai.text  # kept under the current rules: no ssn in it
    assert fresh.ai.structured["columns"] == ["id", "name"]
    assert other.is_error  # fail closed: any rule on the source added since the pull


async def test_the_log_of_what_the_ai_saw_doesnt_replay_a_column_blocked_since(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("query", source="people", question="?", sql="SELECT name, ssn FROM customers")
        await h.call("block", source="people", table="customers", column="ssn")
        current = (await h.call("sent_to_ai")).ai.structured["current"]
        log = await h.call("sent_to_ai", conversation_id=current)
    assert not leaked(log), log.ai.text
    assert "block rules" in log.ai.text


async def test_comparing_kept_rows_from_before_a_rule_doesnt_show_the_blocked_column(
        home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("keep_rows", on=True)
        one = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        two = await h.call("query", source="people", question="?", sql="SELECT * FROM customers WHERE id < 3")
        await h.call("block", source="people", table="customers", column="ssn")
        compared = await h.call("compare", pull_id=one.ai.structured["pull_id"],
                                other_pull_id=two.ai.structured["pull_id"])
    assert not leaked(compared), compared.ai.text
    if not compared.is_error:
        assert "ssn" not in compared.ai.structured["columns"]


async def test_an_excel_sheets_blocked_column_never_reaches_the_ai(home: Path, tmp_path: Path) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "People"
    sheet.append(["id", "name", "ssn"])
    for i, secret in enumerate(SECRETS):
        sheet.append([i, f"n{i}", secret])
    path = tmp_path / "people.xlsx"
    book.save(path)
    async with Harness.start(home) as h:
        added = await h.add_file("book", str(path), kind="excel")
        assert not added.is_error, added.ai.text
        table = (await h.call("test_source", name="book")).ai.structured["tables"][0]
        await h.call("block", source="book", table=table, column="ssn")
        direct = await h.call("query", source="book", question="?", sql=f"SELECT ssn FROM {table}")
        star = await h.call("query", source="book", question="?", sql=f"SELECT * FROM {table}")
    assert direct.is_error and not leaked(direct)
    assert not star.is_error, star.ai.text
    assert star.ai.structured["columns"] == ["id", "name"] and not leaked(star)


async def test_the_panel_doesnt_show_or_post_rows_from_before_a_rule(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people", panel=True) as h:
        pulled = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        pull_id = pulled.ai.structured["pull_id"]
        await h.call("block", source="people", table="customers", column="ssn")
        rows = await h.call_from_panel("panel_rows", pull_id=pull_id)
        posted = await h.call_from_panel("put_in_chat", pull_id=pull_id, rows=[0, 1], columns=["name", "ssn"])
        fresh = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        fresh_rows = await h.call_from_panel("panel_rows", pull_id=fresh.ai.structured["pull_id"])
    assert not leaked(pulled)  # with a panel the AI never got the rows
    for reply in (rows, posted):
        assert reply.is_error and not leaked(reply), reply.ai.text
        assert "rules changed" in reply.ai.text
    assert not fresh_rows.is_error, fresh_rows.ai.text
    assert fresh_rows.ai.structured["columns"] == ["id", "name"]


async def test_a_selection_picked_before_a_rule_isnt_sent_after_it(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people", panel=True) as h:
        pulled = await h.call("query", source="people", question="?", sql="SELECT * FROM customers")
        posted = await h.call_from_panel("put_in_chat", pull_id=pulled.ai.structured["pull_id"], rows=[0, 1],
                                         columns=["name", "ssn"])
        before = await h.call("get_selection", selection_id=posted.ai.structured["selection_id"])
        await h.call("block", source="people", table="customers", column="ssn")
        after = await h.call("get_selection", selection_id=posted.ai.structured["selection_id"])
    assert not posted.is_error and not leaked(posted)  # the note in the chat has no values
    assert not before.is_error and leaked(before)  # picked and fetched before the rule: the user chose to send it
    assert after.is_error and not leaked(after) and "rules changed" in after.ai.text


async def test_notes_on_blocked_tables_and_columns_dont_reach_the_ai(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("set_note", source="people", table="customers", column="ssn", note="social security number")
        await h.call("set_note", source="people", table="notes", note="board meeting plans")
        await h.call("set_note", source="people", table="customers", column="name", note="full name")
        await h.call("block", source="people", table="customers", column="ssn")
        await h.call("block", source="people", table="notes")
        listed = await h.call("list_sources")
        browsed = await h.call("describe_source", source="people")
    for reply in (listed, browsed):
        assert "social security" not in reply.ai.text + repr(reply.ai.structured)
        assert "board meeting" not in reply.ai.text + repr(reply.ai.structured)
    assert "full name" in repr(listed.ai.structured)
    tables = {t["name"]: t for t in browsed.ai.structured["tables"]}
    assert tables["notes"]["blocked"] is True  # still marked, so the user sees what is protected


async def test_browsing_a_source_marks_what_is_blocked(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        await h.call("block", source="people", table="customers", column="ssn")
        await h.call("block", source="people", table="notes")
        browsed = await h.call("describe_source", source="people")
    tables = {t["name"]: t for t in browsed.ai.structured["tables"]}
    assert {c["name"]: c["blocked"] for c in tables["customers"]["columns"]} == {
        "id": False, "name": False, "ssn": True}
    assert tables["notes"]["blocked"] is True
    assert tables["orders"]["blocked"] is False


async def test_a_rule_is_removed_on_the_users_yes_with_the_older_protocol_too(home: Path, people_db: Path) -> None:
    user = User("accept", {"remove": True})
    async with connected(home, people_db, name="people", elicitation=user, protocol="legacy") as h:
        await h.call("block", source="people", table="notes")
        removed = await h.call("unblock", source="people", table="notes")
        rules = await h.call("list_rules")
    assert not removed.is_error, removed.ai.text
    assert len(user.asked) == 1
    assert rules.ai.structured["rules"] == []


async def test_blocking_a_name_the_source_doesnt_have_says_what_it_has(home: Path, people_db: Path) -> None:
    async with connected(home, people_db, name="people") as h:
        table = await h.call("block", source="people", table="custmers")
        column = await h.call("block", source="people", table="customers", column="sn")
        source = await h.call("block", source="nowhere", table="customers")
        rules = await h.call("list_rules")
    assert table.is_error and "customers" in table.ai.text
    assert column.is_error and "ssn" in column.ai.text
    assert source.is_error
    assert rules.ai.structured["rules"] == []
