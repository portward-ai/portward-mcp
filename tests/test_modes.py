"""Free's two modes per source (#117): 'Claude gets results' and 'I send rows myself'.

Driven through the MCP tools as a client does: with a panel (like Claude Desktop) and without (like Claude Code).
`Reply.ai` is everything the AI receives; the panel's rows come from the app-only panel_rows tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import ORDER_COUNT
from harness import Harness, connected
from mcp.types import ElicitRequestParams, ElicitResult

ALL_ORDERS = "SELECT id, total FROM orders ORDER BY id"


class User:
    """Answers every prompt with `answer` (yes by default) and remembers what was asked."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, context: Any, params: ElicitRequestParams) -> ElicitResult:
        self.asked.append(params.message)
        if not self.answer:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content={"yes": True, "approve": True})


async def results_mode(h: Harness, source: str = "shop") -> None:
    """The user switches `source` to 'Claude gets results' (they confirm it in the client's prompt)."""
    switched = await h.call("set_send_mode", source=source, mode="results")
    assert not switched.is_error, switched.ai.text


def everything_the_ai_got(*replies: Any) -> str:
    return "\n".join(f"{r.ai.text}\n{r.ai.structured}" for r in replies)


async def test_in_results_mode_without_a_panel_the_ai_gets_up_to_200_rows(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False, mode="pick", elicitation=User()) as h:
        await results_mode(h)
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    assert not reply.is_error, reply.ai.text
    assert ORDER_COUNT > 200
    assert reply.ai.structured["rows"] == [[i, float(i)] for i in range(1, 201)]
    assert "| 201 |" not in reply.ai.text
    assert reply.ai.structured["row_count"] == ORDER_COUNT


NAMES = ("Ada", "Grace", "Linus")  # the shop's customers.name values, which the user keeps from the AI


async def keep_names(h: Harness) -> None:
    kept = await h.call("block", source="shop", table="customers", column="name")
    assert not kept.is_error, kept.ai.text


async def test_in_results_mode_with_a_panel_the_ai_gets_rows_and_the_panel_a_receipt(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True, elicitation=User()) as h:
        await results_mode(h)
        await keep_names(h)
        reply = await h.call("query", source="shop", question="Customers", sql="SELECT * FROM customers ORDER BY id")
        panel = await h.call_from_panel("panel_rows", pull_id=reply.ai.structured["pull_id"])
    assert not reply.is_error, reply.ai.text
    assert reply.ai.structured["rows"] == [[1, "north"], [2, "south"], [3, "north"]]
    assert not any(n in everything_the_ai_got(reply) for n in NAMES)
    assert panel.ai.structured["columns"] == ["id", "name", "region"]  # the user still sees the kept column
    assert [r[1] for r in panel.ai.structured["rows"]] == list(NAMES)
    assert panel.ai.structured["receipt"] == {"mode": "results", "row_count": 3, "truncated": False, "sent_rows": 3,
                                              "sent_columns": ["id", "region"], "kept_columns": ["name"]}


async def test_in_results_mode_with_a_panel_at_most_200_rows_reach_the_ai(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True, elicitation=User()) as h:
        await results_mode(h)
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        panel = await h.call_from_panel("panel_rows", pull_id=reply.ai.structured["pull_id"])
    assert len(reply.ai.structured["rows"]) == 200 and "| 201 |" not in reply.ai.text
    assert len(panel.ai.structured["rows"]) == ORDER_COUNT  # the user sees the whole result
    assert panel.ai.structured["receipt"]["sent_rows"] == 200


SHAPES = (
    "SELECT * FROM customers",
    "SELECT id, name FROM customers",
    "SELECT name AS who, region FROM customers",
    "SELECT upper(name) || '!' AS shout, id FROM customers",
    "SELECT c.id, c.name, o.total FROM customers c JOIN orders o ON o.customer_id = c.id",
    "SELECT * FROM (SELECT name, region FROM customers) AS t",
    "WITH t AS (SELECT name FROM customers) SELECT * FROM t",
    "SELECT region FROM customers UNION SELECT name FROM customers",
    "SELECT id FROM customers WHERE name = 'Ada'",
    "SELECT id, row_number() OVER (ORDER BY name) AS n FROM customers",
    "SELECT group_concat(name) AS everyone FROM customers",
    "SELECT (SELECT max(name) FROM customers) AS top, count(*) AS n FROM orders",
)


async def test_a_kept_column_never_reaches_the_ai_by_any_query_shape(home: Path, shop_db: Path) -> None:
    for panel in (True, False):
        async with connected(home / str(panel), shop_db, panel=panel, elicitation=User()) as h:
            await results_mode(h)
            await keep_names(h)
            for sql in SHAPES:
                reply = await h.call("query", source="shop", question="Who", sql=sql)
                assert not any(n in everything_the_ai_got(reply) for n in NAMES), (panel, sql)
                if not reply.is_error:
                    more = await h.call("more_rows", pull_id=reply.ai.structured["pull_id"], rows=500)
                    assert not any(n in everything_the_ai_got(more) for n in NAMES), (panel, sql)


async def test_a_query_naming_a_kept_column_still_shows_the_user_their_result(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True, elicitation=User()) as h:
        await results_mode(h)
        await keep_names(h)
        named = await h.call("query", source="shop", question="Names", sql="SELECT id, name FROM customers ORDER BY id")
        filtered = await h.call("query", source="shop", question="Ada", sql="SELECT id FROM customers WHERE name = 'Ada'")
        named_panel = await h.call_from_panel("panel_rows", pull_id=named.ai.structured["pull_id"])
        filtered_panel = await h.call_from_panel("panel_rows", pull_id=filtered.ai.structured["pull_id"])
    assert not named.is_error and named.ai.structured["rows"] == [[1], [2], [3]]  # the kept column left out
    assert named_panel.ai.structured["rows"] == [[1, "Ada"], [2, "Grace"], [3, "Linus"]]
    assert not filtered.is_error and filtered.ai.structured["rows"] == []  # it filters on a kept column: nothing
    assert "name" in filtered.ai.structured["kept_columns"] and "keeps from you" in filtered.ai.text
    assert filtered_panel.ai.structured["rows"] == [[1]]
    assert filtered_panel.ai.structured["receipt"]["sent_rows"] == 0


async def test_a_kept_column_cant_be_put_in_the_chat_from_the_receipt(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True, elicitation=User()) as h:
        await results_mode(h)
        await keep_names(h)
        reply = await h.call("query", source="shop", question="Customers", sql="SELECT * FROM customers")
        put = await h.call_from_panel("put_in_chat", pull_id=reply.ai.structured["pull_id"], rows=[0],
                                      columns=["id", "name"])
    assert put.is_error and "kept" in put.ai.text


async def test_in_pick_mode_with_a_panel_the_ai_gets_only_the_shape_as_before(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    assert "rows" not in reply.ai.structured and "Put in chat" in reply.ai.text


async def test_in_pick_mode_without_a_panel_rows_go_only_after_the_users_yes(home: Path, shop_db: Path) -> None:
    yes, no = User(), User(answer=False)
    async with connected(home / "yes", shop_db, panel=False, mode="pick", elicitation=yes) as h:
        approved = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    async with connected(home / "no", shop_db, panel=False, mode="pick", elicitation=no) as h:
        declined = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        more = await h.call("more_rows", pull_id=declined.ai.structured["pull_id"], rows=10)
    assert approved.ai.structured["rows"] == [[i, float(i)] for i in range(1, 201)]
    assert any("Send the first 200" in q for q in yes.asked)
    assert declined.ai.structured["rows"] == [] and declined.ai.structured["row_count"] == ORDER_COUNT
    assert "| 1 |" not in declined.ai.text and "didn't say yes" in declined.ai.text
    assert more.is_error and "[1, 1.0]" not in everything_the_ai_got(more)


async def test_in_pick_mode_without_a_panel_or_a_prompt_the_ai_gets_the_shape_only(home: Path,
                                                                                   shop_db: Path) -> None:
    async with connected(home, shop_db, panel=False, mode="pick") as h:
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
        more = await h.call("more_rows", pull_id=reply.ai.structured["pull_id"], rows=10, approved=True)
    assert not reply.is_error
    assert reply.ai.structured["rows"] == [] and reply.ai.structured["columns"] == ["id", "total"]
    assert "I send rows myself" in reply.ai.text and "| 1 |" not in reply.ai.text
    assert more.is_error  # never a fallback to sending rows


async def test_sources_pick_until_the_user_confirms_results(home: Path, shop_db: Path) -> None:
    async with connected(home / "a", shop_db, panel=False, mode="pick") as h:  # this app can't ask the user
        refused = await h.call("set_send_mode", source="shop", mode="results")
        pick = await h.call("set_send_mode", source="shop", mode="pick")
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    async with connected(home / "b", shop_db, panel=False, mode="pick", elicitation=User(answer=False)) as h:
        declined = await h.call("set_send_mode", source="shop", mode="results")
    assert refused.is_error and "confirm" in refused.ai.text
    assert not pick.is_error
    assert reply.ai.structured["rows"] == []
    assert declined.is_error


async def test_a_source_pointed_at_other_data_goes_back_to_picking(home: Path, shop_db: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.sqlite"
    other.write_bytes(shop_db.read_bytes())
    user = User()
    async with connected(home, shop_db, panel=False, mode="pick", elicitation=user) as h:
        await results_mode(h)
        await h.add_file("shop", other)
        await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    assert any(q.startswith("Send the first 200") for q in user.asked)  # picking again: the user is asked


async def test_a_source_nobody_chose_a_mode_for_picks(home: Path, shop_db: Path) -> None:
    """Every source made before modes (and any added without choosing) keeps picking: nothing loosens on upgrade."""
    async with Harness.start(home, panel=False) as h:
        await h.add_file("shop", shop_db)
        reply = await h.call("query", source="shop", question="All orders", sql=ALL_ORDERS)
    assert reply.ai.structured["rows"] == [] and "I send rows myself" in reply.ai.text


# The user's own run of the AI's SQL (kept columns in) errors on a kept value: SQLite's abs() overflows on the
# smallest integer, as Postgres errors on a division by zero. Whether it errors must not change what the AI gets.
ERRS_ON_ADA = ("SELECT id, abs(CASE WHEN name = '{}' THEN -9223372036854775808 ELSE 1 END) AS x "
               "FROM customers WHERE id = 1")
PG_SHAPED = "SELECT id, 1 / (CASE WHEN name = '{}' THEN 0 ELSE 1 END) AS x FROM customers WHERE id = 1"


def _without_ids(reply: Any) -> tuple[bool, str, Any]:
    structured = dict(reply.ai.structured or {})
    pull_id = structured.pop("pull_id", "")
    text = reply.ai.text.replace(pull_id, "<id>") if pull_id else reply.ai.text
    return reply.is_error, text, structured


async def test_whether_the_users_own_run_fails_never_changes_what_the_ai_gets(home: Path, shop_db: Path) -> None:
    for panel in (True, False):
        async with connected(home / str(panel), shop_db, panel=panel, elicitation=User()) as h:
            await results_mode(h)
            await keep_names(h)
            for shape in (ERRS_ON_ADA, PG_SHAPED):
                right = await h.call("query", source="shop", question="Is it Ada?", sql=shape.format("Ada"))
                wrong = await h.call("query", source="shop", question="Is it Ada?", sql=shape.format("Zed"))
                assert _without_ids(right) == _without_ids(wrong), (panel, shape)


# SQLite reads a double-quoted name the table lacks as a string: the AI's run (kept column gone) runs, the user's
# run (kept column in) overflows only when the kept value matches
QUOTED_ERRS_ON = ("SELECT * FROM customers WHERE id = 1 AND "
                  "abs(CASE WHEN \"name\" = '{}' THEN -9223372036854775808 ELSE 1 END) > 0")


async def test_the_kept_columns_the_ai_is_told_of_come_from_its_sql_not_the_users_result(home: Path,
                                                                                        shop_db: Path) -> None:
    for panel in (True, False):
        async with connected(home / str(panel), shop_db, panel=panel, elicitation=User()) as h:
            await results_mode(h)
            await keep_names(h)
            right = await h.call("query", source="shop", question="Who", sql=QUOTED_ERRS_ON.format("Ada"))
            wrong = await h.call("query", source="shop", question="Who", sql=QUOTED_ERRS_ON.format("Zed"))
        assert _without_ids(right) == _without_ids(wrong), panel
