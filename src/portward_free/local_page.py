"""The Portward page this server serves on this computer: started on the first open_page or add_source, then served
for as long as the server runs, at one address. The address goes to the user's browser only, never to the AI."""

from __future__ import annotations

from portward_free.localweb import LocalWeb
from portward_free.runtime import Runtime


def local_page(rt: Runtime) -> LocalWeb:
    if rt.local_web is None:
        from portward_free.page import PortwardPage  # not at the top: the page imports the tools' modules

        page = PortwardPage(rt.store.path, rt.keychain, rt.opened, rt.usage.on_store, accounts=rt.accounts,
                            profiles_editable=not rt.local_only)  # the web routes can't change profiles
        rt.local_web = LocalWeb(page).start()
    return rt.local_web
