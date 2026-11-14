"""The results panel's column menu (#119): 'Keep from Claude' and a note, from a column header.

Driven through the MCP tools as a client does: the AI's `query`, then the panel's app-only tools. `Reply.ai` is
everything the receiver gets.
"""

from __future__ import annotations

import json
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

from harness import Harness, MemoryKeychain, connected
from test_modes import User, everything_the_ai_got, results_mode
from test_panel_page import TINY_DOM, UI_JS, needs_node
from test_profiles import PlanAccounts, apply, make

from portward_free.account import Accounts

NAMES = ("Ada", "Grace", "Linus")  # the shop's customers.name values
CUSTOMERS = "SELECT id, name AS who, region FROM customers ORDER BY id"


async def shown_in_panel(h: Harness, sql: str = CUSTOMERS) -> str:
    reply = await h.call("query", source="shop", question="Customers", sql=sql)
    assert not reply.is_error, reply.ai.text
    return str(reply.ai.structured["pull_id"])


async def test_keep_from_claude_from_a_column_header_takes_effect_on_the_next_result(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True, elicitation=User()) as h:
        await results_mode(h)
        first = await h.call("query", source="shop", question="Customers", sql=CUSTOMERS)
        kept = await h.call_from_panel("keep_from_claude", pull_id=first.ai.structured["pull_id"], column="who")
        after = await h.call("query", source="shop", question="Customers", sql="SELECT * FROM customers ORDER BY id")
        rules = await h.call("list_rules", source="shop")
    assert any(n in everything_the_ai_got(first) for n in NAMES)  # before: the AI got the names
    assert not kept.is_error, kept.ai.text
    assert kept.ai.structured["kept"] == {"source": "shop", "table": "customers", "column": "name"}
    assert not any(n in everything_the_ai_got(after) for n in NAMES)
    assert after.ai.structured["rows"] == [[1, "north"], [2, "south"], [3, "north"]]
    assert rules.ai.structured["rules"] == [{"source": "shop", "table": "customers", "column": "name"}]


REGION_NOTE = "north means the Leeds depot, south the Bristol one"


async def test_a_note_set_from_a_column_header_reaches_the_ai_with_the_schema(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:  # 'I send rows myself': the panel is the only view
        pull_id = await shown_in_panel(h)
        noted = await h.call_from_panel("column_note", pull_id=pull_id, column="region", note=f"  {REGION_NOTE} ")
        listed = await h.call("list_sources")
        described = await h.call("describe_source", source="shop")
    assert not noted.is_error, noted.ai.text
    assert noted.ai.structured == {"target": {"source": "shop", "table": "customers", "column": "region"},
                                   "note": REGION_NOTE}
    customers = next(t for t in listed.ai.structured["sources"][0]["tables"] if t["name"] == "customers")
    assert customers["column_notes"] == {"region": REGION_NOTE}
    shown = next(t for t in described.ai.structured["tables"] if t["name"] == "customers")
    assert next(c for c in shown["columns"] if c["name"] == "region")["note"] == REGION_NOTE


async def test_the_column_menu_names_the_table_column_behind_an_alias_with_its_note(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        pull_id = await shown_in_panel(h)
        await h.call("set_note", source="shop", table="customers", column="name", note="The customer's legal name")
        before = await h.call_from_panel("column_menu", pull_id=pull_id, column="who")
        await h.call_from_panel("keep_from_claude", pull_id=pull_id, column="who")
        after = await h.call_from_panel("column_menu", pull_id=pull_id, column="who")
    assert before.ai.structured == {"column": "who", "target": {"source": "shop", "table": "customers", "column": "name"},
                                    "kept": False, "note": "The customer's legal name", "why": None}
    assert after.ai.structured["kept"] is True


async def test_an_empty_note_from_a_column_header_removes_it(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        pull_id = await shown_in_panel(h)
        await h.call_from_panel("column_note", pull_id=pull_id, column="region", note=REGION_NOTE)
        removed = await h.call_from_panel("column_note", pull_id=pull_id, column="region", note="  ")
        listed = await h.call("list_sources")
    assert removed.ai.structured["note"] is None
    customers = next(t for t in listed.ai.structured["sources"][0]["tables"] if t["name"] == "customers")
    assert "column_notes" not in customers


async def test_a_column_worked_out_from_several_columns_cant_be_kept_or_noted_from_its_header(
        home: Path, shop_db: Path) -> None:
    sql = "SELECT name || ' ' || region AS label, COUNT(*) AS n FROM customers GROUP BY label"
    async with connected(home, shop_db, panel=True) as h:
        pull_id = await shown_in_panel(h, sql)
        menu = await h.call_from_panel("column_menu", pull_id=pull_id, column="label")
        kept = await h.call_from_panel("keep_from_claude", pull_id=pull_id, column="label")
        counted = await h.call_from_panel("column_note", pull_id=pull_id, column="n", note="how many")
        rules = await h.call("list_rules", source="shop")
    assert menu.ai.structured["target"] is None
    assert "customers.name, customers.region" in menu.ai.structured["why"]
    assert kept.is_error and "Portward page" in kept.ai.text
    assert counted.is_error
    assert rules.ai.structured["rules"] == []


MENU_TOOLS = {"column_menu", "keep_from_claude", "column_note"}


async def test_the_column_menu_tools_are_the_panels_only(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        ai_tools = await h.ai_tools()
    async with connected(home, shop_db, panel=False, mode="results") as h:  # a client with no panel
        pull_id = await shown_in_panel(h)
        kept = await h._call("keep_from_claude", {"pull_id": pull_id, "column": "who"})
        noted = await h._call("column_note", {"pull_id": pull_id, "column": "region", "note": "x"})
        rules = await h.call("list_rules", source="shop")
    assert not MENU_TOOLS & set(ai_tools)
    assert kept.is_error and noted.is_error and "results panel only" in kept.ai.text
    assert rules.ai.structured["rules"] == []


async def test_on_profiles_the_column_menu_points_to_the_profile_editor(home: Path, shop_db: Path) -> None:
    keychain = MemoryKeychain()
    keychain.set("account-token", "pwat_fake")
    async with connected(home, shop_db, panel=True, keychain=keychain, accounts=Accounts(PlanAccounts("active")),
                         elicitation=User()) as h:
        pull_id = await shown_in_panel(h)
        await make(h, "Sales", ["customers.region"])
        await apply(h, "Sales", source="shop")
        menu = await h.call_from_panel("column_menu", pull_id=pull_id, column="region")
        kept = await h.call_from_panel("keep_from_claude", pull_id=pull_id, column="region")
    assert menu.ai.structured["target"] is None and "profile editor" in menu.ai.structured["why"]
    assert kept.is_error


# --- the panel's side: panel/column_menu.js, run under node with a tiny DOM -------------------------------------------

MENU_JS = files("portward_free").joinpath("panel/column_menu.js").read_text()
TARGET = {"source": "shop", "table": "customers", "column": "name"}


def run_menu(answers: dict[str, dict], script: str) -> Any:
    """Run `script` with ui.js and column_menu.js; callTool answers `answers[tool]` and records every call."""
    program = (f"{TINY_DOM}\n{UI_JS}\n{MENU_JS}\nconst answers = {json.dumps(answers)}; const calls = [];\n"
               "const callTool = async (tool, args) => { calls.push([tool, args]);\n"
               "  const a = answers[tool]; if (a && a.error) throw new Error(a.error); return { structuredContent: a }; };\n"
               "const mounted = []; const changes = [];\n"
               "const entries = PortwardColumnMenu.menuEntries('p1', 'who', callTool, (s) => mounted.push(s), 'Claude',\n"
               "                                               (c) => changes.push(c));\n"
               "const find = (node, test) => test(node) ? node : (node.childNodes || []).map((c) => find(c, test))"
               ".find(Boolean) || null;\n"
               f"(async () => {{ let out = null; {script}\nconsole.log(JSON.stringify(out)); }})();")
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


@needs_node
def test_the_header_menu_keeps_a_column_from_claude_from_the_next_result() -> None:
    out = run_menu({"column_menu": {"column": "who", "target": TARGET, "kept": False, "note": None, "why": None},
                    "keep_from_claude": {"kept": TARGET, "already_kept": False}}, """
      const keep = entries.find((e) => e.label === "Keep from Claude");
      await keep.run();
      out = { labels: entries.map((e) => e.label), calls, text: mounted[0].textContent, changes };
    """)
    assert out["labels"] == ["Keep from Claude", "Say what this means"]
    assert [c[0] for c in out["calls"]] == ["column_menu", "keep_from_claude"]
    assert out["calls"][1][1] == {"pull_id": "p1", "column": "who"}
    assert "customers.name" in out["text"] and "next result" in out["text"]
    assert out["changes"] == [{"kept": "who"}]


@needs_node
def test_the_header_menu_says_why_a_column_cant_be_kept_and_keeps_nothing() -> None:
    why = "label is worked out from customers.name, customers.region, not read from one column"
    out = run_menu({"column_menu": {"column": "who", "target": None, "kept": False, "note": None, "why": why}}, """
      await entries[0].run();
      out = { calls: calls.map((c) => c[0]), text: mounted[0].textContent, changes };
    """)
    assert out["calls"] == ["column_menu"]
    assert "worked out from customers.name" in out["text"] and out["changes"] == []


@needs_node
def test_the_header_menu_edits_the_columns_note() -> None:
    out = run_menu({"column_menu": {"column": "who", "target": TARGET, "kept": False, "note": "Legal name",
                                    "why": None},
                    "column_note": {"target": TARGET, "note": "Legal name, as on the invoice"}}, """
      await entries.find((e) => e.label === "Say what this means").run();
      const box = mounted[0];
      const area = find(box, (n) => n.tag === "textarea");
      const shown = area.textContent;
      area.value = "Legal name, as on the invoice";
      const save = find(box, (n) => n.tag === "button" && n.textContent === "Save note");
      await save.listeners.click();
      out = { shown, calls, text: box.textContent };
    """)
    assert out["shown"] == "Legal name"
    assert out["calls"][1] == ["column_note", {"pull_id": "p1", "column": "who",
                                               "note": "Legal name, as on the invoice"}]
    assert "Saved" in out["text"]
