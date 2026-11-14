"""Tests never open the browser or the mail app on the computer running them (conftest: `no_real_apps`).

Portward opens pages and mail drafts through `webbrowser` (open_page, account_sign_in, share's mail draft, the
sign-in command). The autouse stand-in takes every such call and records it, whatever path reaches `webbrowser`,
including the `browser=webbrowser.open` default bound when `build_server` was defined."""

from __future__ import annotations

import webbrowser

from portward_free import server
from portward_free.sharing.tools import MailApp


def test_opening_a_page_or_mail_draft_in_a_test_reaches_the_stand_in(no_real_apps: list[str]) -> None:
    default_browser = server.build_server.__defaults__[2]  # the `browser` default, bound at import
    assert default_browser("http://127.0.0.1:9/page") is True
    assert webbrowser.open_new_tab("http://127.0.0.1:9/tab") is True
    assert MailApp().open("kim@acme.example", "Rows", "Here they are") is True
    assert no_real_apps[:2] == ["http://127.0.0.1:9/page", "http://127.0.0.1:9/tab"]
    assert no_real_apps[2].startswith("mailto:kim@acme.example")
