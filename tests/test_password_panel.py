"""Where the client has panels, a small form in the panel takes the password and sends it through an app-only tool.

The AI sees that the form is shown, never the form's single-use ask (it is in the result's `_meta`, for the panel)
and never what is typed. Hosts like Claude Desktop don't pass `_meta` to the panel: the visible result then carries a
public `request_id`, and the panel reads the ask with the app-only `secret_request` tool.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from conftest import READER_PASSWORD, Database
from harness import Harness, MemoryKeychain
from test_panel_page import needs_node

from portward_free.asks import ALLOW, MAX_ATTEMPTS, Refused, Requests
from portward_free.passwords import password_ask
from portward_free.results_panel import page
from portward_free.storage import Connection

PASSWORD_PANEL = "ui://portward/password.html"
REQUEST = "portward/secret_request"


def _ask(reply) -> str:
    return reply.panel.meta[REQUEST]["token"]


@pytest.mark.integration
async def test_the_panel_form_saves_the_password_through_an_app_only_tool(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, panel=True, keychain=keychain) as h:
        added = await h.call("add_source", name="shop", **database.fields())
        saved = await h.call_from_panel("save_secret", request=_ask(added), secret=READER_PASSWORD)
        tested = await h.call("test_source", name="shop")
        ai_tools = await h.ai_tools()
    assert added.tool_meta["ui"]["resourceUri"] == PASSWORD_PANEL
    assert added.ai.structured["password_entry"] == "panel"
    assert "panel" in added.ai.text
    assert _ask(added) not in added.ai.text + str(added.ai.structured)
    assert not saved.is_error, saved.ai.text
    assert saved.ai.structured["tables"] == ["customers", "orders"]
    assert keychain.passwords == {"connection:shop": READER_PASSWORD}
    assert not tested.is_error
    assert "save_secret" not in ai_tools


@pytest.mark.integration
async def test_a_form_is_single_use(home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, panel=True, keychain=keychain) as h:
        added = await h.call("add_source", name="shop", **database.fields())
        await h.call_from_panel("save_secret", request=_ask(added), secret=READER_PASSWORD)
        again = await h.call_from_panel("save_secret", request=_ask(added), secret="something-else")
    assert again.is_error and "expired or was already used" in again.ai.text
    assert keychain.passwords == {"connection:shop": READER_PASSWORD}


@pytest.mark.integration
async def test_a_wrong_password_in_the_form_says_why_and_the_form_can_be_tried_again(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, panel=True, keychain=keychain) as h:
        added = await h.call("add_source", name="shop", **database.fields())
        wrong = await h.call_from_panel("save_secret", request=_ask(added), secret="not-the-password")
        right = await h.call_from_panel("save_secret", request=_ask(added), secret=READER_PASSWORD)
    assert wrong.is_error and "wrong user or password" in wrong.ai.text
    assert "not-the-password" not in wrong.ai.text
    assert not right.is_error
    assert keychain.passwords == {"connection:shop": READER_PASSWORD}


@pytest.mark.integration
async def test_changing_the_settings_closes_the_form_that_was_open(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, panel=True, keychain=keychain) as h:
        first = await h.call("add_source", name="shop", **database.fields())
        await h.call("add_source", name="shop", **database.fields(host="localhost"))
        late = await h.call_from_panel("save_secret", request=_ask(first), secret=READER_PASSWORD)
    assert late.is_error and keychain.passwords == {}


async def test_the_form_tool_refuses_a_client_without_a_panel_and_an_unknown_ask(home: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        unknown = await h.call_from_panel("save_secret", request="made-up", secret="x")
    async with Harness.start(home, panel=False) as h:
        reply = await h._call("save_secret", {"request": "made-up", "secret": "x"})
    assert unknown.is_error and "expired or was already used" in unknown.ai.text
    assert reply.is_error and "panel only" in reply.ai.text


async def test_the_panel_page_is_a_password_form(home: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        page = (await h.client.read_resource(PASSWORD_PANEL)).contents[0].text
    assert 'ask.hidden === false ? "text" : "password"' in page and 'autocomplete: "off"' in page  # hidden unless the ask says not
    assert "save_secret" in page
    # hosts frame panels sandboxed without allow-forms, so a <form> can't be submitted (found in a real browser)
    assert 'el("form"' not in page and "<form" not in page


async def test_a_file_source_is_added_with_one_allow_button_that_names_the_file(home: Path, shop_db: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        added = await h.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        before = await h.call("list_sources")
        form = added.panel.meta[REQUEST]
        wrong = await h.call_from_panel("save_secret", request=form["token"], secret="something else")
        right = await h.call_from_panel("save_secret", request=form["token"], secret=ALLOW)
        after = await h.call("list_sources")
    assert not added.is_error and added.ai.structured["confirm_entry"] == "panel"
    assert form["token"] not in added.ai.text + str(added.ai.structured)
    assert form["allow"] is True and form["label"] == "Allow"  # one button: nothing to type
    assert shop_db.name in form["title"]
    assert "Source name" not in json.dumps(form)
    assert "types" not in added.ai.text and "Allow" in added.ai.text  # the AI tells the user to press Allow
    assert before.ai.structured["sources"] == []
    assert wrong.is_error and not right.is_error, right.ai.text
    assert right.ai.structured["tables"] == ["customers", "orders"]
    assert "keychain" not in right.ai.text.lower()
    assert [s["name"] for s in after.ai.structured["sources"]] == ["shop"]


def _seen_by_ai(reply) -> str:
    return reply.ai.text + json.dumps(reply.ai.structured, default=str)


async def test_a_host_that_drops_meta_still_gets_the_form_and_the_file_is_added(home: Path, shop_db: Path) -> None:
    async with Harness.start(home, panel=True) as h:
        added = await h.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        request_id = added.ai.structured["request_id"]
        form = await h.call_from_panel("secret_request", request_id=request_id)  # all Claude Desktop's panel has
        before = await h.call("list_sources")
        stale = await h.call_from_panel("save_secret", request=added.panel.meta[REQUEST]["token"], secret=ALLOW)
        right = await h.call_from_panel("save_secret", request=form.ai.structured["token"], secret=ALLOW)
        after = await h.call("list_sources")
        used = await h.call_from_panel("secret_request", request_id=request_id)
        ai_tools = await h.ai_tools()
    assert not form.is_error, form.ai.text
    meta_ask = added.panel.meta[REQUEST]
    assert {**form.ai.structured, "token": None} == {**meta_ask, "token": None}  # the same ask the _meta carries
    # with a new token: the store keeps only a token's hash, so reading the form again replaces it
    assert form.ai.structured["token"] not in (request_id, meta_ask["token"])
    assert stale.is_error and "expired or was already used" in stale.ai.text
    assert form.ai.structured["token"] not in _seen_by_ai(added)
    assert before.ai.structured["sources"] == []
    assert not right.is_error, right.ai.text
    assert [s["name"] for s in after.ai.structured["sources"]] == ["shop"]
    assert used.is_error and "expired or was already used" in used.ai.text  # closed once answered
    assert "secret_request" not in ai_tools and "save_secret" not in ai_tools


async def test_the_form_reader_refuses_an_unknown_or_replaced_ask_and_a_client_without_a_panel(
        home: Path, shop_db: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    other.write_bytes(shop_db.read_bytes())
    async with Harness.start(home, panel=True) as h:
        unknown = await h.call_from_panel("secret_request", request_id="made-up")
        first = await h.call("add_source", name="shop", kind="sqlite", path=str(shop_db))
        await h.call("add_source", name="shop", kind="sqlite", path=str(other))  # replaces the first ask
        replaced = await h.call_from_panel("secret_request", request_id=first.ai.structured["request_id"])
        tool = (await h.tools())["secret_request"]
    async with Harness.start(home, panel=False) as h:
        no_panel = await h._call("secret_request", {"request_id": "made-up"})
    assert unknown.is_error and "expired or was already used" in unknown.ai.text
    assert replaced.is_error and "expired or was already used" in replaced.ai.text
    assert tool.meta["ui"]["visibility"] == ["app"] and tool.meta["ui"]["resourceUri"] == PASSWORD_PANEL
    assert no_panel.is_error and "panel only" in no_panel.ai.text


@pytest.mark.integration
async def test_a_host_that_drops_meta_still_gets_the_password_form(
        home: Path, database: Database, keychain: MemoryKeychain) -> None:
    async with Harness.start(home, panel=True, keychain=keychain) as h:
        added = await h.call("add_source", name="shop", **database.fields())
        ask = await h.form_in_panel(added)
        saved = await h.call_from_panel("save_secret", request=ask["token"], secret=READER_PASSWORD)
        again = await h.call("update_password", name="shop")
        new_ask = await h.form_in_panel(again)
    assert ask["label"] == "Password" and ask["token"] not in _seen_by_ai(added)
    assert not saved.is_error, saved.ai.text
    assert keychain.passwords == {"connection:shop": READER_PASSWORD}
    assert again.ai.structured["password_entry"] == "panel"
    assert new_ask["token"] not in _seen_by_ai(again) and new_ask["token"] != ask["token"]


# The password panel's own script, run under node with a tiny DOM and a stand-in host: what the user is shown.
PANEL_DOM = r"""
class Text { constructor(d) { this.data = String(d); } get textContent() { return this.data; } }
class Element {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.childNodes = []; this.listeners = {}; this.dataset = {}; }
  set className(v) { this.attrs.class = v; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  append(...items) { for (const i of items) this.childNodes.push(typeof i === "object" ? i : new Text(i)); }
  replaceChildren(...items) { this.childNodes = []; this.append(...items.filter((i) => i != null)); }
  focus() {}
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) { this.childNodes = [new Text(v)]; }
  find(tag) { return this.tag === tag ? this : this.childNodes.map((c) => c.find && c.find(tag)).find(Boolean); }
}
globalThis.Node = Element;
const pressed = (n, label) => (n.tag === "button" && n.textContent === label ? n
  : (n.childNodes || []).map((c) => c.childNodes && pressed(c, label)).find(Boolean));
const buttons = (n) => (n.tag === "button" ? [n.textContent] : []).concat(...(n.childNodes || []).map((c) => c.childNodes ? buttons(c) : []));
const nodes = {};
globalThis.document = { createElement: (tag) => new Element(tag),
                        getElementById: (id) => nodes[id] || (nodes[id] = new Element("div")) };
const calls = [];
let handle = null;
globalThis.PortwardHost = {
  start: (fn) => { handle = fn; }, hostName: () => "Claude", canPutInChat: () => true,
  putInChat: async () => "composed",
  callTool: async (name, args) => {
    calls.push([name, args]);
    const answer = answers[name];
    if (!answer || answer.isError) throw new Error(answer ? answer.message : "no such tool");
    return answer;
  },
};
"""
UI_JS = (Path(__file__).parents[1] / "src/portward_free/panel/ui.js").read_text()
ASK = {"token": "tok-123", "title": "Confirm the file", "label": "Name", "details": [["File", "/x/shop.db"]],
       "done": "Done."}


def _panel_script() -> str:
    scripts = re.findall(r"<script>(.*?)</script>", page("password.html"), re.S)
    return scripts[-1]  # the page's own script; the first is the host bridge and ui.js, stood in for here


def show(result: dict, answers: dict | None = None, click: str | None = None) -> dict:
    """Hand the password panel `result` as a host does (then press the button labelled `click`); what it then shows
    and which tools it called."""
    press = (f"await pressed(nodes.body, {json.dumps(click)}).listeners.click();"
             " await new Promise((r) => setTimeout(r, 5));" if click else "")
    program = (f"const answers = {json.dumps(answers or {})};\n{PANEL_DOM}\n{UI_JS}\n{_panel_script()}\n"
               f"(async () => {{ await handle({json.dumps(result)}); await new Promise((r) => setTimeout(r, 5));\n"
               f"{press}\n"
               "console.log(JSON.stringify({ title: nodes.title.textContent, status: nodes.status.textContent,"
               " body: nodes.body.textContent, input: Boolean(nodes.body.find('input')),"
               " buttons: buttons(nodes.body), calls })); })();")
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(done.stdout)


@needs_node
def test_without_meta_the_panel_reads_its_form_with_the_request_id() -> None:
    shown = show({"structuredContent": {"source": "shop", "confirm_entry": "panel", "request_id": "r-1"}},
                 {"secret_request": {"structuredContent": ASK}})
    assert shown["calls"] == [["secret_request", {"request_id": "r-1"}]]
    assert shown["input"] and shown["title"] == "Confirm the file"
    assert "connected" not in shown["body"] + shown["title"]


@needs_node
def test_with_meta_the_panel_shows_the_form_without_asking_again() -> None:
    shown = show({"structuredContent": {"confirm_entry": "panel", "request_id": "r-1"}, "_meta": {REQUEST: ASK}})
    assert shown["calls"] == [] and shown["input"] and shown["title"] == "Confirm the file"


@needs_node
def test_the_panel_never_says_connected_for_an_ask_it_cant_show() -> None:
    gone = show({"structuredContent": {"confirm_entry": "panel", "request_id": "r-1"}},
                {"secret_request": {"isError": True, "message": "this form has expired or was already used"}})
    neither = show({"structuredContent": {"source": "shop", "confirm_entry": "panel"}})
    for shown in (gone, neither):
        assert not shown["input"]
        assert "connected" not in (shown["title"] + shown["body"]).lower()
        assert "Ask again in the chat" in shown["body"]
    assert "expired" in gone["body"]


@needs_node
def test_the_panel_says_connected_only_when_a_source_was_added() -> None:
    added = show({"structuredContent": {"added": "shop", "tables": ["customers"]}})
    nothing = show({"structuredContent": {"sync": "on"}})
    assert added["title"] == "Source connected" and "shop is connected: customers" in added["body"]
    assert "connected" not in (nothing["title"] + nothing["body"]).lower() and not nothing["input"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


SHOP = Connection("shop", "postgres", {"host": "127.0.0.1", "port": 1, "database": "d", "user": "u"})


def test_an_ask_expires() -> None:
    clock = Clock()
    requests = Requests(minutes=10, clock=clock)
    ask = requests.open(password_ask(SHOP, MemoryKeychain()))
    clock.now = 10 * 60 + 1
    with pytest.raises(Refused, match="expired"):
        requests.answer(ask.token, "pw")
    assert not requests.is_open(ask.token)


def test_an_ask_closes_after_too_many_wrong_passwords() -> None:
    requests = Requests()
    ask = requests.open(password_ask(SHOP, MemoryKeychain()))  # port 1: nothing answers, so every try fails
    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(Refused, match="didn't answer"):
            requests.answer(ask.token, "pw")
    assert not requests.is_open(ask.token)


def test_asks_are_unguessable() -> None:
    requests = Requests()
    tokens = {requests.open(password_ask(SHOP, MemoryKeychain())).token for _ in range(50)}
    assert len(tokens) == 50 and all(len(t) >= 40 for t in tokens)


ALLOW_ASK = {"token": "tok-9", "title": "Let Claude read shop.sqlite", "label": "Allow", "allow": True,
             "details": [["File", "shop.sqlite"], ["Where", "/x/shop.sqlite"], ["Source", "shop"]],
             "done": "I've allowed shop."}


@needs_node
def test_a_file_is_allowed_with_one_button_and_no_box_to_type_in() -> None:
    shown = show({"structuredContent": {"confirm_entry": "panel", "request_id": "r-1"}, "_meta": {REQUEST: ALLOW_ASK}})
    assert not shown["input"]
    assert shown["buttons"] == ["Allow"]
    assert "shop.sqlite" in shown["title"] + shown["body"]


@needs_node
def test_allowing_a_file_never_says_keychain() -> None:
    page_text = page("password.html")
    shown = show({"structuredContent": {"confirm_entry": "panel", "request_id": "r-1"}, "_meta": {REQUEST: ALLOW_ASK}},
                 {"save_secret": {"structuredContent": {"added": "shop", "tables": ["customers", "orders"]}}},
                 click="Allow")
    assert shown["calls"] == [["save_secret", {"request": "tok-9", "secret": ALLOW}]]
    assert "keychain" not in (shown["status"] + shown["body"]).lower()
    assert "shop" in shown["status"] + shown["body"]
    assert "keychain" in page_text  # a database password is still saved there, and says so
