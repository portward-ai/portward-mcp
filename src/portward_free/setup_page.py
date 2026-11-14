"""The setup page's routes on the local Portward page (#118): GET /connect (blank, or filled in from what the AI
asked for in add_source) and POST /connect with `action` = check | edit | save. See setup for what each step stores
(nothing until save).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from portward_free import modes
from portward_free import setup_html as html
from portward_free.keychain import Keychain
from portward_free.localweb import Response
from portward_free.page_html import Chrome
from portward_free.setup import STALE, Pending, SetupProblem, Where, check, save
from portward_free.storage import LocalStore


class SetupPage:
    def __init__(self, store: LocalStore, keychain: Keychain) -> None:
        self._store, self._keychain = store, keychain
        self._pending = Pending()

    @staticmethod
    def handles(path: str) -> bool:
        return path == html.PATH

    def get(self, chrome: Chrome, query: Mapping[str, str]) -> Response:
        return Response(body=html.page(chrome, html.SetupView(Where.from_fields(query))))

    def post(self, chrome: Chrome, fields: Mapping[str, str], ticked: Sequence[str]) -> Response:
        where = Where.from_fields(fields)
        action = fields.get("action", "")
        if action == "check":
            try:
                checked = check(where, fields.get("password", ""), self._store)
            except SetupProblem as err:
                return self._page(chrome, html.SetupView(where, problem=f"Not connected: {err}."))
            return self._page(chrome, html.SetupView(where, checked, self._pending.hold(checked)))
        if action == "save":
            return self._save(chrome, where, fields, ticked)
        return self._page(chrome, html.SetupView(where))  # edit, or anything else: back to step 1

    def _save(self, chrome: Chrome, where: Where, fields: Mapping[str, str], ticked: Sequence[str]) -> Response:
        token = fields.get("setup", "")
        checked = self._pending.get(token)
        if checked is None:
            return self._page(chrome, html.SetupView(where, problem=STALE))
        mode = fields.get("mode", "")
        try:
            done = save(self._store, self._keychain, checked, mode, ticked)
        except SetupProblem as err:
            return self._page(chrome, html.SetupView(checked.where, checked, token, f"Nothing was saved: {err}.",
                                                     tuple(ticked), mode if mode in modes.MODES else modes.RESULTS))
        self._pending.drop(token)
        return Response(body=html.saved(chrome, done))

    @staticmethod
    def _page(chrome: Chrome, view: html.SetupView) -> Response:
        return Response(body=html.page(chrome, view))
