"""A new share's link, for the share panel to show and copy (2026-11-04: the mail app is optional, not the default).

The link carries the share's key in its # fragment, so the AI never gets it: the `share` result names the share by
its public id, and the panel reads the link with the app-only `share_link` tool. Claude Desktop runs the AI's calls
and the panel's in two server processes on one app-data folder (and drops the result's `_meta`), so the link is held
in the local store (`share_links`), sealed with the kept-rows key (`kept`, AES-256-GCM) with the share id as its
associated data, and only for LINK_HOURS (never past the share's own expiry). When the keychain can't seal it, it is
not held at all and the share opens the mail draft instead, as before.

`share_mail` is the panel's "Open in mail app": the local server opens the same draft `share` used to open, through
the user's mail app (`Drafts`), since a sandboxed panel can't open a mailto: address itself in every host.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult

from portward_free import kept
from portward_free.clients import client_support
from portward_free.keychain import Keychain, KeychainError
from portward_free.replies import error, reply
from portward_free.storage import LocalStore

SHARE_PANEL_URI = "ui://portward/share.html"
LINK_HOURS = 24  # how long the panel can still show a new share's link
HELD_LINKS = 100  # recent shares whose links the store keeps for the panel


class LinkGone(LookupError):
    """The panel asked for a share whose link isn't held (never, expired, or can't be opened)."""


@dataclass(frozen=True)
class HeldLink:
    share_id: str
    link: str  # the key is in its # fragment
    recipient: str
    sender_name: str
    expires_at: str  # when the share itself expires (the service's ISO time)


def draft(held: HeldLink) -> tuple[str, str, str]:
    """The mail draft to the teammate: (to, subject, body). The same one `share` opened before the panel had it."""
    said = f"{held.sender_name} shared a table with you"
    return held.recipient, said, f"{said}:\n\n{held.link}"


def _associated(share_id: str) -> str:
    return f"share-link:{share_id}"  # no other sealed blob's: none can pass for another


def _until(expires_at: str, now: float) -> float:
    """LINK_HOURS from now, or when the share expires if that is sooner."""
    held_for = now + LINK_HOURS * 3600
    try:
        share_ends = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return held_for
    return min(held_for, share_ends)


class ShareLinks:
    def __init__(self, store: LocalStore, keychain: Keychain, limit: int = HELD_LINKS,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._keychain = keychain
        self._limit = limit
        self._clock = clock

    def hold(self, held: HeldLink) -> bool:
        """Keep the link sealed for the panel; False when the keychain can't seal it (nothing is then kept)."""
        try:
            secret = kept.key(self._keychain, create=True)
            assert secret is not None  # create=True always gives a key
            sealed = kept.seal_json(secret, _associated(held.share_id), asdict(held))
        except (KeychainError, kept.RowsLocked):
            return False
        now = self._clock()
        self._store.hold_share_link(held.share_id, sealed, now, _until(held.expires_at, now), self._limit)
        return True

    def get(self, share_id: str) -> HeldLink:
        """The share's link, while it is held. Raises LinkGone."""
        sealed = self._store.share_link(share_id, self._clock())
        if sealed is None:
            raise LinkGone(f"the link for share {share_id} isn't held here any more (a new share's link is kept "
                           f"for {LINK_HOURS} hours); share the table again for a new link")
        try:
            data = kept.open_json(kept.stored_key(self._keychain, "the share link"), _associated(share_id), sealed,
                                  "the share link")
        except (KeychainError, kept.RowsLocked) as err:
            raise LinkGone(f"the link for share {share_id} can't be opened ({err}); share the table again") from None
        return HeldLink(**data)


def _not_panel(ctx: Context) -> CallToolResult | None:
    return None if client_support(ctx).panel else error("this is for the Portward share panel only")


def link_for_panel(links: ShareLinks, share_id: str, ctx: Context) -> CallToolResult:
    """`share_link`: the link (and who it is for) for the share panel to show. App-only."""
    if (refused := _not_panel(ctx)) is not None:
        return refused
    try:
        held = links.get(share_id)
    except LinkGone as err:
        return error(str(err))
    return reply({"share_id": held.share_id, "link": held.link, "recipient": held.recipient,
                  "expires_at": held.expires_at}, "the share's link, for the share panel")


def mail_from_panel(links: ShareLinks, open_draft: Callable[[str, str, str], bool], share_id: str,
                    ctx: Context) -> CallToolResult:
    """`share_mail`: open the draft to the teammate in the user's mail app. App-only."""
    if (refused := _not_panel(ctx)) is not None:
        return refused
    try:
        held = links.get(share_id)
    except LinkGone as err:
        return error(str(err))
    opened = open_draft(*draft(held))
    text = (f"A draft to {held.recipient} with the link is open in the mail app." if opened else
            "The mail app couldn't be opened from here. Copy the link and paste it into an email instead.")
    return reply({"share_id": share_id, "opened": opened}, text)
