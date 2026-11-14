"""The setup page (#118) as a browser drives it: read its one form, submit it with a button pressed."""

from __future__ import annotations

import http.client
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: str


class SetupForm(HTMLParser):
    """The fields a browser would send from the setup page's form: hidden, text, password and number inputs, the
    checked radios and checkboxes (disabled ones aren't sent), and the form's address."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.ticked: list[str] = []
        self.action = ""
        self._in_form = False
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form" and a.get("action", "").endswith("/connect"):
            self._in_form, self.action = True, a["action"]
        if not self._in_form or tag != "input" or not a.get("name") or "disabled" in a:
            return
        kind = a.get("type", "text")
        if kind in ("hidden", "text", "password", "number"):
            self.fields[a["name"]] = a.get("value", "")
        elif kind == "radio" and "checked" in a:
            self.fields[a["name"]] = a.get("value", "")
        elif kind == "checkbox" and "checked" in a:
            self.ticked.append(a.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def request(url: str, pairs: list[tuple[str, str]] | None = None) -> Answer:
    """GET `url`, or POST `pairs` to it (a form whose fields can repeat) with the page's own Origin."""
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    if pairs is None:
        connection.request("GET", path, headers={"Host": parts.netloc})
    else:
        connection.request("POST", path, body=urllib.parse.urlencode(pairs), headers={
            "Host": parts.netloc, "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"{parts.scheme}://{parts.netloc}"})
    response = connection.getresponse()
    answer = Answer(response.status, {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode("utf-8", "replace"))
    connection.close()
    return answer


def submit(url: str, page: Answer, action: str, *, keep: list[str] | None = None, **changes: str) -> Answer:
    """Submit the setup form on `page` as a browser would, with `changes` made and the button `action` pressed.
    `keep` replaces the ticked columns (None: send what the page ticked)."""
    form = SetupForm(page.body)
    assert form.action, "no setup form on the page"
    parts = urllib.parse.urlsplit(url)
    pairs = [*({**form.fields, **changes, "action": action}).items(),
             *(("keep", v) for v in (form.ticked if keep is None else keep))]
    return request(f"{parts.scheme}://{parts.netloc}{form.action}", pairs)


def finish(url: str, *, mode: str = "pick", keep: list[str] | None = None, password: str = "") -> Answer:
    """The user goes through the whole page at `url`: checks, then saves with `mode` and `keep` (none by default)."""
    checked = submit(url, request(url), "check", password=password)
    if "name=\"setup\"" not in checked.body:
        raise AssertionError(f"the setup check failed: {checked.body[:2000]}")
    done = submit(url, checked, "save", mode=mode, keep=keep or [])
    if "is connected" not in done.body:
        raise AssertionError(f"the setup save failed: {done.body[:2000]}")
    return done
