"""Asking the user for a secret out of band: where the client lets them type it without the AI seeing it.

In order: the panel (a form in `panel/password.html`, answered through the app-only `save_secret` tool), URL-mode
elicitation (a one-time page on this computer, `password_page`), else a terminal command that prompts with hidden
input. The tool's reply tells the AI only where the user was asked and what to do next; the panel's single-use ask
goes in `_meta` (with its public `request_id` in the visible result, for hosts that don't pass `_meta` to the panel:
the panel then reads the ask with the app-only `secret_request` tool), the page's address only to the client's prompt.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from mcp.server.apps import Apps, Visibility
from mcp.server.extension import ToolBinding
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ElicitRequest, ElicitRequestURLParams, ElicitResult, InputRequiredResult

from portward_free.asks import REQUEST_ID, Ask, Builder, Outcome, Request, Requests
from portward_free.clients import ClientSupport, client_support
from portward_free.password_page import PasswordPage
from portward_free.replies import reply
from portward_free.storage import LocalStore

PANEL_URI = "ui://portward/password.html"
_Fn = TypeVar("_Fn", bound=Callable[..., Any])
PAGE_KEY = "secret_page"  # the URL prompt's key in an input_required round
PAGE_STATE = "secret-page:"  # request_state prefix: the page's elicitation id follows


class LocalOnlyApps(Apps):
    """The panels for MCP over HTTP (#102): the password panel isn't served, its app-only tools (where a typed secret
    arrives) aren't registered, and the AI's tools bound to it are plain tools that ask in a terminal instead."""

    def __init__(self) -> None:
        super().__init__()
        self._plain: list[ToolBinding] = []

    def add_html_resource(self, uri: str, html: str, **kwargs: Any) -> None:
        if uri != PANEL_URI:
            super().add_html_resource(uri, html, **kwargs)

    def tool(self, *, resource_uri: str, visibility: Sequence[Visibility] | None = None,
             meta: dict[str, Any] | None = None, **tool_kwargs: Any) -> Callable[[_Fn], _Fn]:
        if resource_uri != PANEL_URI:
            return super().tool(resource_uri=resource_uri, visibility=visibility, meta=meta, **tool_kwargs)

        def decorator(fn: _Fn) -> _Fn:
            if visibility is None or "model" in visibility:
                self._plain.append(ToolBinding(fn=fn, meta=meta or None, kwargs=tool_kwargs))
            return fn

        return decorator

    def tools(self) -> Sequence[ToolBinding]:
        return [*super().tools(), *self._plain]


@dataclass(frozen=True)
class Texts:
    """What the AI is told: `what` is asked for ("the password for 'shop'"), what to do `then`, and the terminal
    `command` that takes it (it `does` something: "tests the connection and saves the password in the keychain")."""

    what: str
    then: str
    command: str
    does: str
    lead: str = ""  # first sentence, e.g. "Saved the settings for 'shop'."
    button: str = ""  # an allow ask (asks.Ask.allow): the user presses this button instead of typing `what`

    def in_panel(self) -> str:
        if self.button:
            return f"The user presses {self.button} in the Portward panel shown with this reply, for {self.what}."
        return f"The user types {self.what} in the Portward panel shown with this reply; you never see it."

    def in_page(self) -> str:
        if self.button:
            return f"The user presses {self.button} in a page on this computer, for {self.what}."
        return f"The user is typing {self.what} in a page on this computer; you never see it."

    def in_terminal(self) -> str:
        if self.button:
            return (f"For {self.what}, ask the user to run this in a terminal on this computer; it asks them to type "
                    f"the source's name and {self.does}")
        return (f"You never handle {self.what}. Ask the user to run this in a terminal on this computer; it asks with "
                f"hidden input and {self.does}")

    def prompt(self) -> str:
        if self.button:
            return f"Press {self.button} in a page on this computer, for {self.what}."
        return f"Type {self.what} in a page on this computer. The AI never sees it."


class OutOfBand:
    def __init__(self, store: LocalStore | None = None, *, local_only: bool = False) -> None:
        """`local_only` (MCP over HTTP, #102): the client is far from this computer, so a secret is asked for only
        in a terminal here; never in its panel (the secret would cross the web) or a local page (its address
        would)."""
        self.requests = Requests(store)  # in the store: the panel's calls can reach another server process
        self._pages: dict[str, PasswordPage] = {}  # open local pages, by elicitation id
        self.local_only = local_only

    def panel_meta(self) -> dict[str, Any] | None:
        """A secret-asking tool's `_meta`: bound to the password panel, except when the panel isn't served."""
        return None if self.local_only else {"ui": {"resourceUri": PANEL_URI}}

    def close(self, key: str) -> None:
        """Close every open ask for `key`: its panel form and its local page."""
        self.requests.cancel(key)
        for elicitation_id, page in list(self._pages.items()):
            if page.key == key:
                self._pages.pop(elicitation_id).close()

    def answer(self, token: str, secret: str) -> Outcome:
        return self.requests.answer(token, secret)

    def register(self, kind: str, builder: Builder) -> None:
        """How to rebuild an ask of `kind` from its params, for an ask another server process opened."""
        self.requests.kinds[kind] = builder

    def panel_form(self, request_id: str) -> Request:
        """The open panel ask `request_id` names (Refused if none): its form, for the panel."""
        return self.requests.by_id(request_id)

    def is_retry(self, ctx: Context) -> bool:
        """This call is a client's retry with the answer to the URL prompt (2026-07-28 input_required round)."""
        state = ctx.request_state or ""
        return state.startswith(PAGE_STATE) and state.removeprefix(PAGE_STATE) in self._pages

    async def ask(self, ctx: Context, ask: Ask, payload: dict[str, Any], texts: Texts, entry_field: str
                  ) -> CallToolResult | InputRequiredResult:
        """Ask for `ask` the best way the client allows. `payload` (never secret) goes to the AI, with
        `entry_field` saying where the user was asked: "panel", "page" or "terminal"."""
        lead = f"{texts.lead} " if texts.lead else ""
        support = client_support(ctx)
        if self.local_only:
            support = ClientSupport(panel=False, elicitation=support.elicitation, url_elicitation=False)
        if support.panel:
            self.close(ask.key)
            request = self.requests.open(ask)
            result = reply({**payload, entry_field: "panel", REQUEST_ID: request.id}, (
                f"{lead}{texts.in_panel()} When they say it's done, {texts.then}."))
            return result.model_copy(update={"meta": request.panel_meta()})
        if support.url_elicitation:
            if not self.is_retry(ctx):
                self.close(ask.key)
            opened = await self._page_opened(ctx, ask, texts)
            if isinstance(opened, InputRequiredResult):
                return opened
            if opened:
                return reply({**payload, entry_field: "page"}, (
                    f"{lead}{texts.in_page()} When they say it's done, {texts.then}."))
        return reply({**payload, entry_field: "terminal", "command": texts.command}, (
            f"{lead}{texts.in_terminal()}:\n\n    {texts.command}\n\nWhen they say it's done, {texts.then}."))

    def _open_page(self, ask: Ask) -> tuple[str, PasswordPage]:
        request = self.requests.open(ask)
        page = PasswordPage(request, self.requests.answer, self.requests.is_open,
                            self.requests.seconds_left(request)).start()
        elicitation_id = secrets.token_urlsafe(16)
        self._pages[elicitation_id] = page
        return elicitation_id, page

    async def _page_opened(self, ctx: Context, ask: Ask, texts: Texts) -> bool | InputRequiredResult:
        """Send the user to a one-time local page (URL-mode elicitation). True when they accepted; on a decline the
        page is closed. On 2026-07-28 connections this is an input_required round."""
        message = texts.prompt()
        if ctx.session.can_send_request:
            elicitation_id, page = self._open_page(ask)
            answer = await ctx.elicit_url(message, page.url, elicitation_id)
            accepted = answer.action == "accept"
        else:
            response = (ctx.input_responses or {}).get(PAGE_KEY)
            elicitation_id = (ctx.request_state or "").removeprefix(PAGE_STATE)
            if response is None or not self.is_retry(ctx):
                elicitation_id, page = self._open_page(ask)
                params = ElicitRequestURLParams(message=message, url=page.url, elicitation_id=elicitation_id)
                return InputRequiredResult(input_requests={PAGE_KEY: ElicitRequest(params=params)},
                                           request_state=PAGE_STATE + elicitation_id)
            accepted = isinstance(response, ElicitResult) and response.action == "accept"
        if not accepted:
            page = self._pages.pop(elicitation_id, None)
            if page is not None:
                page.close()
        return accepted
