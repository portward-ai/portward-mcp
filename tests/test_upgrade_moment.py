"""The upgrade moment in the panel (#120): a Free user reaches for anonymised or totals from a column's menu, and the
panel shows that column's values as the AI would get them on Personal (stand-ins or totals) beside the one offer.

Driven through the MCP tools as a panel does (`call_from_panel`, the app-only `level_preview`). The preview never
reads a column the user keeps from the AI, and nothing it shows reaches the AI: not its reply, history or AI log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

from harness import Harness, MemoryKeychain, connected
from test_panel_page import TINY_DOM, UI_JS, needs_node

from portward_free import stand_ins
from portward_free.results_panel import PANEL_URI

NAMES = ("Ada", "Grace", "Linus")  # customers.name in the shop


def expected_stand_in(key: bytes, table: str, column: str, value: str) -> str:
    """The stand-in Personal gives an unlinked column's value (glossary: Stand-in; #121's scope), worked from the
    formula rather than through the code under test."""
    group = hashlib.sha256(f"column:{table}.{column}".encode()).hexdigest()[:32]
    return "anon_" + hmac.new(key, f"anon|{group}|{value}".encode(), hashlib.sha256).hexdigest()[:16]


async def pulled(h: Harness, sql: str) -> str:
    reply = await h.call("query", source="shop", question="Customers", sql=sql)
    assert not reply.is_error, reply.ai.text
    return reply.ai.structured["pull_id"]


def preview_of(reply: Any) -> dict[str, Any]:
    assert not reply.is_error, reply.ai.text
    return reply.ai.structured


async def test_anonymised_shows_the_users_own_values_beside_the_stand_ins_the_ai_would_get(
        home: Path, shop_db: Path) -> None:
    keychain = MemoryKeychain()
    async with connected(home, shop_db, panel=True, keychain=keychain) as h:
        pull_id = await pulled(h, "SELECT id, region FROM customers ORDER BY id")
        preview = preview_of(await h.call_from_panel("level_preview", pull_id=pull_id, column="region",
                                                     level="anonymised"))
    key = base64.b64decode(keychain.get(stand_ins.KEY_ACCOUNT) or "")
    assert preview["kept"] is False
    assert preview["rows"] == [["north", expected_stand_in(key, "customers", "region", "north")],
                               ["south", expected_stand_in(key, "customers", "region", "south")]]


def mentions_a_name(found: Any) -> bool:
    """Whether a kept name appears as a whole word ("ada" alone, not the hex inside a pull id or a hash)."""
    return any(re.search(rf"(?<![0-9a-z]){n.lower()}(?![0-9a-z])", repr(found).lower()) for n in NAMES)


async def test_a_kept_column_is_never_read_for_the_preview_in_any_query_shape(home: Path, shop_db: Path) -> None:
    shapes = {"SELECT * FROM customers ORDER BY id": "name",
              "SELECT id, name AS who FROM customers": "who",
              "SELECT c.id, upper(c.name) AS loud FROM customers c": "loud",
              "SELECT o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id LIMIT 9": "name",
              "WITH n AS (SELECT name FROM customers) SELECT name FROM n": "name"}
    async with connected(home, shop_db, panel=True, mode="results") as h:
        assert not (await h.call("block", source="shop", table="customers", column="name")).is_error
        for sql, column in shapes.items():
            pull_id = await pulled(h, sql)
            for level in ("anonymised", "totals"):
                found = await h.call_from_panel("level_preview", pull_id=pull_id, column=column, level=level)
                assert not mentions_a_name(found.ai), (sql, level)
                assert found.is_error or preview_of(found)["kept"] is True, (sql, level, found.ai)


async def test_a_column_kept_after_the_pull_gets_no_preview(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        pull_id = await pulled(h, "SELECT id, name FROM customers")
        assert not (await h.call("block", source="shop", table="customers", column="name")).is_error
        found = await h.call_from_panel("level_preview", pull_id=pull_id, column="name", level="anonymised")
    assert found.is_error and not mentions_a_name(found.ai)


async def test_totals_shows_what_the_ai_would_get_in_place_of_the_rows(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        ten = await pulled(h, "SELECT id, total FROM orders ORDER BY id LIMIT 10")
        three = await pulled(h, "SELECT id, total FROM orders ORDER BY id LIMIT 3")
        enough = preview_of(await h.call_from_panel("level_preview", pull_id=ten, column="total", level="totals"))
        few = preview_of(await h.call_from_panel("level_preview", pull_id=three, column="total", level="totals"))
    assert enough["totals"] == {"rows": 10, "minimum": 5, "small": False, "sum": 55.0, "average": 5.5}
    assert few["totals"] == {"rows": 3, "minimum": 5, "small": True}  # too few rows: the AI would get nothing


async def test_no_preview_value_reaches_the_ai(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        pull_id = await pulled(h, "SELECT id, region FROM customers ORDER BY id")
        shown = preview_of(await h.call_from_panel("level_preview", pull_id=pull_id, column="region",
                                                   level="anonymised"))
        ai_tools = await h.ai_tools()
        afterwards = [await h.call(tool) for tool in ("history", "sent_to_ai", "ai_log")]
    stand_in = shown["rows"][0][1]
    assert "level_preview" not in ai_tools  # app-only: the host keeps the tool and its result from the model
    for told in afterwards:
        assert stand_in not in f"{told.ai.text}{told.ai.structured}"
        assert "level_preview" not in f"{told.ai.text}{told.ai.structured}"


# --- the panel's side: the column menu's Personal entries (panel/levels.js) ------------------------------------------

LEVELS_JS = files("portward_free").joinpath("panel/levels.js").read_text()


def run_upgrade(script: str) -> Any:
    """Run `script` (async; it sets `out`) with ui.js, levels.js and the tiny DOM stand-in."""
    program = (f"{TINY_DOM}\n{UI_JS}\n{LEVELS_JS}\n(async () => {{ let out = null; {script}\n"
               "console.log(JSON.stringify(out)); })();")
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


# A panel's calls: level_preview answers `preview` (or fails), personal_offer gives the offer the first time only.
PANEL = r"""
const calls = [];
let offered = false;
const callTool = async (tool, args) => {
  calls.push([tool, args]);
  if (tool === "level_preview") {
    if (preview.error) throw new Error(preview.error);
    return { structuredContent: preview };
  }
  if (tool === "personal_offer") {
    const offer = offered ? null : { kind: "anonymised_or_totals", text: "OFFER-TEXT" };
    offered = true;
    return { structuredContent: { offer } };
  }
  return {};
};
"""


@needs_node
def test_the_column_menu_offers_anonymise_and_totals_marked_personal() -> None:
    out = run_upgrade("out = PortwardLevels.menuEntries('p1', 'email', async () => ({}), () => {})"
                      ".map((e) => [e.label, e.level]);")
    assert out == [["Anonymise (Personal)", "anonymised"], ["Totals only (Personal)", "totals"]]


@needs_node
def test_choosing_anonymise_shows_stand_ins_beside_the_users_values_and_the_offer_once() -> None:
    preview = {"column": "region", "level": "anonymised", "kept": False,
               "rows": [["north", "anon_1111111111111111"], ["south", "anon_2222222222222222"]]}
    out = run_upgrade(f"const preview = {json.dumps(preview)};\n{PANEL}" + """
      const first = await PortwardLevels.show("p1", "region", "anonymised", callTool, "Claude");
      const again = await PortwardLevels.show("p1", "region", "anonymised", callTool, "Claude");
      out = { first: first.textContent, again: again.textContent, calls };
    """)
    assert "north" in out["first"] and "anon_1111111111111111" in out["first"] and "anon_2222" in out["first"]
    assert "OFFER-TEXT" in out["first"] and "OFFER-TEXT" not in out["again"]  # once per moment, from offers.py
    assert out["calls"][0] == ["level_preview", {"pull_id": "p1", "column": "region", "level": "anonymised"}]
    assert ["personal_offer", {"moment": "anonymised_or_totals"}] in out["calls"]


@needs_node
def test_a_kept_column_shows_no_values_only_that_it_is_kept() -> None:
    preview = {"column": "email", "level": "anonymised", "kept": True}
    out = run_upgrade(f"const preview = {json.dumps(preview)};\n{PANEL}" + """
      const shown = await PortwardLevels.show("p1", "email", "anonymised", callTool, "Claude");
      out = shown.textContent;
    """)
    assert "keep email from Claude" in out and "not even for this preview" in out


@needs_node
def test_totals_say_what_the_ai_would_get_or_that_too_few_rows_give_nothing() -> None:
    enough = {"column": "total", "level": "totals", "kept": False,
              "totals": {"rows": 10, "minimum": 5, "small": False, "sum": 55, "average": 5.5}}
    few = {"column": "total", "level": "totals", "kept": False, "totals": {"rows": 3, "minimum": 5, "small": True}}
    out = run_upgrade(f"let preview = {json.dumps(enough)};\n{PANEL}" + f"""
      const a = await PortwardLevels.show("p1", "total", "totals", callTool, "Claude");
      preview = {json.dumps(few)};
      const b = await PortwardLevels.show("p1", "total", "totals", callTool, "Claude");
      out = [a.textContent, b.textContent];
    """)
    assert "55" in out[0] and "5.5" in out[0] and "never one row" in out[0]
    assert "fewer than 5" in out[1] and "nothing" in out[1]


@needs_node
def test_a_preview_portward_cant_make_says_why_and_still_offers() -> None:
    out = run_upgrade(f"const preview = {{ error: \"Portward can't tell which column it comes from\" }};\n{PANEL}"
                      + """
      const shown = await PortwardLevels.show("p1", "loud", "anonymised", callTool, "Claude");
      out = shown.textContent;
    """)
    assert "can't tell which column" in out and "OFFER-TEXT" in out


async def test_the_served_results_panel_has_the_personal_entries_in_its_column_menus(home: Path,
                                                                                       shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        page = (await h.client.read_resource(PANEL_URI)).contents[0].text
    assert "const PortwardLevels" in page and "Anonymise (Personal)" in page
    assert "PortwardLevels.menuEntries(" in page  # the results page puts them in each column's menu
