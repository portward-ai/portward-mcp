"""The results panel page: what its button promises, and how it talks to the host.

The host bridge (panel/host.js) runs under node with a stand-in for the host window: the panel reaches the local
server only through the host (MCP Apps `tools/call`), puts text in the chat only as a visible message (`ui/message`)
and never uses the silent model-context channel.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
from harness import connected

from portward_free.results_panel import PANEL_URI

HOST_JS = files("portward_free").joinpath("panel/host.js").read_text()
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the panel's scripts")

# A host window: records what the panel posts, and answers requests with `answers[method]`.
FAKE_HOST = r"""
const posted = [];
const listeners = {};
globalThis.window = {
  addEventListener: (type, fn) => { listeners[type] = fn; },
  parent: { postMessage: (message) => {
    posted.push(message);
    if (message.id === undefined) return;
    const answer = answers[message.method];
    setTimeout(() => listeners.message({ source: window.parent, data: answer && answer.error
      ? { jsonrpc: "2.0", id: message.id, error: answer.error }
      : { jsonrpc: "2.0", id: message.id, result: answer || {} } }), 0);
  } },
};
globalThis.document = { documentElement: { dataset: {}, style: { setProperty() {} }, scrollWidth: 400 },
                        body: { getBoundingClientRect: () => ({ height: 100 }) } };
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
"""


def run_host(answers: dict, script: str) -> dict:
    """Run host.js with the fake host, then `script` (async; it sets `out`). Returns {out, posted}."""
    program = (f"const answers = {json.dumps(answers)};\n{FAKE_HOST}\n{HOST_JS}\n"
               f"(async () => {{ let out = null; {script}\n"
               "console.log(JSON.stringify({ out, posted })); })().catch((e) => { console.log(JSON.stringify("
               "{ out: 'threw: ' + e.message, posted })); });")
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


def methods(posted: list[dict]) -> list[str]:
    return [m["method"] for m in posted if "method" in m]


CLAUDE = {"ui/initialize": {"hostInfo": {"name": "Claude"}, "hostCapabilities": {"message": {}}}}


@needs_node
def test_the_panel_calls_the_local_server_through_the_host() -> None:
    answers = {**CLAUDE, "tools/call": {"structuredContent": {"rows": [[1]]}}}
    ran = run_host(answers, "PortwardHost.start(() => {}); await tick();"
                            "out = await PortwardHost.callTool('panel_rows', { pull_id: 'p1' });")
    call = next(m for m in ran["posted"] if m.get("method") == "tools/call")
    assert call["params"] == {"name": "panel_rows", "arguments": {"pull_id": "p1"}}
    assert ran["out"] == {"structuredContent": {"rows": [[1]]}}


@needs_node
def test_a_tool_the_host_refuses_is_an_error_not_an_empty_answer() -> None:
    answers = {**CLAUDE, "tools/call": {"isError": True, "content": [{"type": "text", "text": "pull p1 isn't open"}]}}
    ran = run_host(answers, "PortwardHost.start(() => {}); await tick();"
                            "out = await PortwardHost.callTool('panel_rows', { pull_id: 'p1' });")
    assert ran["out"] == "threw: pull p1 isn't open"


@needs_node
def test_in_claude_put_in_chat_fills_the_message_box_with_a_visible_message() -> None:
    ran = run_host(CLAUDE, "PortwardHost.start(() => {}); await tick();"
                           "out = [PortwardHost.hostName(), await PortwardHost.putInChat('| id |')];")
    assert ran["out"] == ["Claude", "composed"]  # Claude prefills the box; the user presses send
    message = next(m for m in ran["posted"] if m.get("method") == "ui/message")
    assert message["params"] == {"role": "user", "content": [{"type": "text", "text": "| id |"}]}
    assert "ui/update-model-context" not in methods(ran["posted"])


@needs_node
def test_a_host_that_cant_post_messages_gets_nothing_silently() -> None:
    no_message = {"ui/initialize": {"hostInfo": {"name": "Other"}, "hostCapabilities": {}}}
    ran = run_host(no_message, "PortwardHost.start(() => {}); await tick();"
                               "out = [PortwardHost.canPutInChat()];"
                               "try { await PortwardHost.putInChat('| id |'); } catch (e) { out.push(e.message); }")
    assert ran["out"][0] is False
    assert "can't put text in the chat" in ran["out"][1]
    assert not {"ui/message", "ui/update-model-context"} & set(methods(ran["posted"]))


async def test_the_served_panel_says_put_in_chat_and_never_promises_an_automatic_send(
        home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, panel=True) as h:
        page = (await h.client.read_resource(PANEL_URI)).contents[0].text
    assert "Put in chat" in page
    assert "Send to" not in page and "Sent to" not in page
    # the message box gets a short note; the AI fetches the picked rows (get_selection) once the user sends it
    assert "A short note goes into your message box; when you press send" in page and "fetches the" in page
    assert "gets them when you press send" not in page
    assert "ui/update-model-context" not in page
    assert "fetch(" not in page  # no HTTP: a local server has no origin the panel could reach
    assert "/*PORTWARD_" not in page  # every placeholder was filled


UI_JS = files("portward_free").joinpath("panel/ui.js").read_text()


def run_ui(expression: str) -> object:
    """Evaluate `expression` with the panel's ui.js loaded (no DOM needed for its text helpers)."""
    program = f"{UI_JS}\nconsole.log(JSON.stringify({expression}));"
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


@needs_node
def test_where_a_summed_value_came_from_reads_as_plain_sentences() -> None:
    found = {"column": "revenue", "kind": "aggregate", "expression": "SUM(orders.total)",
             "from": [{"table": "orders", "column": "total"}],
             "filters": [{"clause": "join", "sql": "customers.id = orders.customer_id"},
                         {"clause": "where", "sql": "orders.total > 5"},
                         {"clause": "having", "sql": "COUNT(*) > 1"}, {"clause": "limit", "sql": "10"}],
             "group": [{"column": "region", "value": "south"}], "traced": True}
    text = run_ui(f"PortwardUI.sourceText({json.dumps(found)})")
    assert text == {
        "summary": "Revenue is SUM(orders.total), for region south.",
        "from": ["orders.total"],
        "filters": ["Joined where customers.id = orders.customer_id", "Only rows where orders.total > 5",
                    "Only groups where COUNT(*) > 1", "First 10 rows only"],
    }


@needs_node
def test_a_plain_value_and_a_row_count_say_so() -> None:
    plain = {"column": "total", "kind": "column", "expression": "orders.total", "from": [{"table": "orders",
             "column": "total"}], "filters": [], "group": [], "traced": True}
    count = {"column": "n", "kind": "aggregate", "expression": "COUNT(*)", "from": [{"table": "orders",
             "column": None}], "filters": [], "group": [], "traced": True}
    unsure = {**plain, "traced": False, "from": [{"table": "orders", "column": None}]}
    out = run_ui(f"[{json.dumps(plain)}, {json.dumps(count)}, {json.dumps(unsure)}].map(PortwardUI.sourceText)")
    assert out[0] == {"summary": "Total is read as it is from orders.total.", "from": ["orders.total"],
                      "filters": ["No filters: every row of the table"]}
    assert out[1]["summary"] == "N is COUNT(*)." and out[1]["from"] == ["orders (rows counted)"]
    assert out[2]["summary"] == "Total comes from orders; Portward couldn't trace the exact column."
    assert out[2]["from"] == ["orders"]


TINY_DOM = r"""
class Text { constructor(d) { this.data = String(d); } get textContent() { return this.data; } }
class Element {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.childNodes = []; this.listeners = {}; }
  set className(v) { this.attrs.class = v; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  append(...items) { for (const i of items) this.childNodes.push(typeof i === "object" ? i : new Text(i)); }
  remove() { this.removed = true; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) { this.childNodes = [new Text(v)]; }
}
globalThis.Node = Element;
globalThis.document = { createElement: (tag) => new Element(tag) };
"""


def run_panel_ui(script: str) -> object:
    """Run `script` (async; it sets `out`) with ui.js and a tiny DOM stand-in."""
    program = (f"{TINY_DOM}\n{UI_JS}\n(async () => {{ let out = null; {script}\n"
               "console.log(JSON.stringify(out)); })();")
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


@needs_node
def test_the_panel_shows_the_usage_question_then_says_it_was_seen_and_offers_an_easy_no() -> None:
    out = run_panel_ui("""
      const calls = [];
      const callTool = async (tool, args) => { calls.push([tool, args, mount.childNodes.length]); return {}; };
      const mount = new Element("div");
      const none = PortwardUI.showUsage({ _meta: {} }, mount, callTool);
      const shown = PortwardUI.showUsage({ _meta: { "portward/usage_question": { text: "Portward shares..." } } },
                                         mount, callTool);
      const aside = mount.childNodes[0];
      const off = aside.childNodes.find((c) => c.tag === "button");
      await off.listeners.click();
      out = { none, shown, text: aside.textContent, calls };
    """)
    assert out["none"] is False and out["shown"] is True
    assert "Portward shares..." in out["text"] and "Usage counts are off" in out["text"]
    assert out["calls"][0] == ["usage_seen", {}, 1]  # only once the question is on the page
    assert out["calls"][1][:2] == ["usage_choice", {"on": False}]


@needs_node
def test_the_panel_shows_an_offer_of_personal_once_and_can_turn_it_off() -> None:
    """#116: the offer is the panel's to show (the words come from the app-only personal_offer, i.e. offers.py)."""
    out = run_panel_ui("""
      const calls = [];
      const offer = { kind: "anonymised_or_totals", text: "Stand-ins and totals: https://example.test/me" };
      let answer = { structuredContent: { offer } };
      const callTool = async (tool, args) => { calls.push([tool, args]); return answer; };
      const mount = new Element("div");
      const shown = await PortwardUI.showOffer("anonymised_or_totals", mount, callTool);
      const aside = mount.childNodes[0];
      const off = aside.childNodes.find((c) => c.tag === "button");
      await off.listeners.click();
      answer = { structuredContent: { offer: null } };
      const again = await PortwardUI.showOffer("anonymised_or_totals", mount, callTool);
      out = { shown, again, text: aside.textContent, calls, asides: mount.childNodes.length };
    """)
    assert out["shown"] is True and out["again"] is False and out["asides"] == 1
    assert "https://example.test/me" in out["text"] and "won't be shown again" in out["text"]
    assert out["calls"][0] == ["personal_offer", {"moment": "anonymised_or_totals"}]
    assert out["calls"][1] == ["dismiss_offer", {"kind": "anonymised_or_totals"}]
