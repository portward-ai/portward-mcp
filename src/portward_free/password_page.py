"""A one-time page on this machine where the user types a secret (for URL-mode elicitation): a database password
or the sharing code; or, letting the AI read a file, presses one Allow button (an allow ask: nothing to type).

Served by `localweb.LocalWeb` (127.0.0.1 only, a per-page path token, its own Host only, same-origin forms with a
form token, strict headers, no scripts, nothing logged). On top of that the page is one-time: it closes once the
password is saved, when the ask expires (`expires_in`) or is cancelled, or after too many wrong passwords
(`asks.Requests` decides; the ask's own single-use token never leaves the server).
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from http import HTTPStatus

from portward_free.asks import ALLOW, Outcome, Refused
from portward_free.asks import Request as Ask
from portward_free.localweb import FORM_TOKEN_FIELD, LocalWeb, Request, Response

STYLESHEET = "/password.css"
STYLE = """
:root { color-scheme: light dark; --ink: #10243a; --muted: #4f6275; --line: #b8c4cf; --accent: #0b5c8a;
        --bad: #9b1c1c; --ok: #1f6f43; --surface: #f7f9fb; }
@media (prefers-color-scheme: dark) { :root { --ink: #e6edf3; --muted: #a3b3c2; --line: #3b4b5a; --accent: #5fb0e5;
        --bad: #f19999; --ok: #7fd1a1; --surface: #0f1923; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 16px; background: var(--surface); color: var(--ink);
       font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 30rem; margin: 0 auto; display: grid; gap: 16px; }
h1 { font-size: 1.4rem; margin: 0; }
p { margin: 0; }
.muted { color: var(--muted); font-size: 0.9rem; }
dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 4px 16px; margin: 0; }
dt { color: var(--muted); } dd { margin: 0; overflow-wrap: anywhere; font-family: ui-monospace, monospace; }
form { display: grid; gap: 8px; }
label { font-weight: 600; }
input { font: inherit; min-height: 44px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 6px;
        background: transparent; color: inherit; }
button { font: inherit; font-weight: 600; min-height: 44px; padding: 0 20px; border-radius: 6px; border: 0;
         background: var(--accent); color: #fff; cursor: pointer; justify-self: start; }
@media (prefers-color-scheme: dark) { button { color: #0f1923; } }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.error { color: var(--bad); font-weight: 600; } .ok { color: var(--ok); font-weight: 600; }
"""


class PasswordApp:
    """The page's routes: the form (GET /), the secret (POST /) and its stylesheet."""

    def __init__(self, ask: Ask, answer: Callable[[str, str], Outcome], is_open: Callable[[str], bool]) -> None:
        self._ask = ask
        self._answer = answer
        self._is_open = is_open

    def __call__(self, request: Request, web: LocalWeb) -> Response:
        if request.path == STYLESHEET:
            return Response(HTTPStatus.OK, STYLE, "text/css; charset=utf-8")
        if request.path != "/":
            return Response.text(HTTPStatus.NOT_FOUND, "Not found.")
        if not self._is_open(self._ask.token):
            return self._gone(web)
        if request.method == "GET":
            return Response(HTTPStatus.OK, self._form(web))
        try:
            outcome = self._answer(self._ask.token, request.form.get("secret", ""))
        except Refused as err:
            if not self._is_open(self._ask.token):
                return self._gone(web)
            return Response(HTTPStatus.OK, self._form(web, str(err)))
        said = "".join(f"<p>{escape(line)}</p>" for line in outcome.summary().splitlines())
        heading = "Allowed" if self._ask.ask.allow else "Saved"
        return Response(HTTPStatus.OK, self._document(web, f"Portward: {heading.lower()}", (
            f'<h1>{heading}</h1><div class="ok">{said}</div>'
            '<p class="muted">You can close this page and tell the AI it\'s done.</p>')), close_after=True)

    def _gone(self, web: LocalWeb) -> Response:
        return Response(HTTPStatus.GONE, self._document(web, "Page closed", (
            "<h1>This page has closed</h1><p>It works once and for a few minutes only. Ask the AI to try again.</p>")),
            close_after=True)

    def _form(self, web: LocalWeb, error: str = "") -> str:
        ask = self._ask.ask
        where = "<dl>" + "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in ask.details) + "</dl>"
        problem = f'<p class="error" role="alert">Not saved: {escape(error)}</p>' if error else ""
        if ask.allow:
            return self._allow_form(web, where + problem)
        return self._document(web, f"Portward: {ask.title.lower()}", (
            f"<h1>{escape(ask.title)}</h1>{where}{problem}"
            f'<form method="post" action="{escape(web.link("/"))}" autocomplete="off">'
            f'<input type="hidden" name="{FORM_TOKEN_FIELD}" value="{escape(web.form_token)}">'
            f'<label for="pw">{escape(ask.label)}</label>'
            f'<input id="pw" name="secret" type="{"password" if ask.hidden else "text"}" autocomplete="off" '
            'required autofocus>'
            '<p class="muted">It goes from here to Portward on this computer only. The AI never sees it. '
            "This page works once and closes in a few minutes.</p>"
            '<button type="submit">Save</button></form>'))

    def _allow_form(self, web: LocalWeb, shown: str) -> str:
        """An allow ask: what is allowed and one button; the form's hidden field says Allow, nothing is typed."""
        ask = self._ask.ask
        return self._document(web, f"Portward: {ask.title.lower()}", (
            f"<h1>{escape(ask.title)}</h1>{shown}"
            f'<form method="post" action="{escape(web.link("/"))}" autocomplete="off">'
            f'<input type="hidden" name="{FORM_TOKEN_FIELD}" value="{escape(web.form_token)}">'
            f'<input name="secret" type="hidden" value="{ALLOW}">'
            '<p class="muted">Your block rules still apply. This page works once and closes in a few minutes.</p>'
            f'<button type="submit" autofocus>{escape(ask.label)}</button></form>'))

    @staticmethod
    def _document(web: LocalWeb, title: str, body: str) -> str:
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width, initial-scale=1"><title>{escape(title)}</title>'
                f'<link rel="stylesheet" href="{escape(web.link(STYLESHEET))}"></head><body><main>{body}</main>'
                "</body></html>")


class PasswordPage:
    """One ask's page. `answer(token, secret)` saves the secret or raises Refused; `is_open(token)` says
    whether the ask can still be answered."""

    def __init__(self, ask: Ask, answer: Callable[[str, str], Outcome], is_open: Callable[[str], bool],
                 expires_in: float) -> None:
        self.key = ask.ask.key
        self._web = LocalWeb(PasswordApp(ask, answer, is_open), expires_in=expires_in)

    @property
    def url(self) -> str:
        return self._web.url("/")

    def start(self) -> PasswordPage:
        self._web.start()
        return self

    def close(self) -> None:
        self._web.close()
