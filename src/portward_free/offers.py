"""Offers of Portward Personal (#57, moments redrawn by #108): the only place the free tool's upgrade wording lives.

Personal is offered at three moments, each the first time it happens on this computer and never again:

- connecting claude.ai or ChatGPT on the web (WEB): `portward-free serve-http` or `serve-direct` on a computer that
  isn't on Personal;
- reaching for a profile (PROFILE) or for anonymised or totals (LEVELS) in Portward's results panel (#116: the
  panel's app-only `personal_offer`; Personal's tools aren't listed on Free, so the AI can't reach these moments).

The panel and the terminal show offers; no tool reply asks the AI to pass one on (#116). "On Free" means signed out,
or signed in to an account whose plan is ended. When the plan can't be checked (the account service unreachable), no
offer is made. Each offer is one line. The user can turn a kind off in the panel (app-only `dismiss_offer`), and a
dismissed kind never comes back on this computer. When the user already has Personal there is no offer. Nothing else
in the tool may carry upgrade wording:
tests/test_upgrade_prompts.py sweeps every tool and the local pages for it. The earlier moments (a second computer, a
share about to expire, the manage-shares page) are gone."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from portward_free.storage import LocalStore

OFFERS_SETTING = "offers"  # {dismissed: [kind], made: [kind]}
WEB = "web"
PROFILE = "profile"
LEVELS = "anonymised_or_totals"
KINDS = (WEB, PROFILE, LEVELS)
TRIAL = "14 days free, no card"


@dataclass(frozen=True)
class Offer:
    kind: str
    text: str  # one line, for the AI (or the terminal) to pass on as is

    def payload(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}


def _state(store: LocalStore) -> dict[str, Any]:
    stored = store.setting(OFFERS_SETTING)
    try:
        found = json.loads(stored) if stored else {}
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _save(store: LocalStore, **changes: Any) -> None:
    store.set_setting(OFFERS_SETTING, json.dumps({**_state(store), **changes}))


def _listed(store: LocalStore, key: str) -> list[str]:
    found = _state(store).get(key, [])
    return [k for k in found if isinstance(k, str)] if isinstance(found, list) else []


def dismissed(store: LocalStore, kind: str) -> bool:
    return kind in _listed(store, "dismissed")


def dismiss(store: LocalStore, kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"no offer called {kind!r}; the offers are {', '.join(KINDS)}")
    if not dismissed(store, kind):
        _save(store, dismissed=[*_listed(store, "dismissed"), kind])


def _once(store: LocalStore, kind: str, text: str) -> Offer | None:
    """The offer, if this computer hasn't had it or dismissed it (it counts as made now)."""
    if dismissed(store, kind) or kind in _listed(store, "made"):
        return None
    _save(store, made=[*_listed(store, "made"), kind])
    return Offer(kind, text)


def web(store: LocalStore, account_url: str) -> Offer | None:
    """Connecting claude.ai or ChatGPT on the web, on Free."""
    return _once(store, WEB, "claude.ai and ChatGPT on the web come with Portward Personal, with your laptop off or "
                             f"straight to this computer ({TRIAL}): {account_url}/me")


def profile(store: LocalStore, account_url: str) -> Offer | None:
    """Creating a profile, on Free."""
    return _once(store, PROFILE, "Profiles come with Portward Personal: named bundles of what the AI may see, where "
                                 f"anything not granted stays hidden ({TRIAL}): {account_url}/me")


def levels(store: LocalStore, account_url: str) -> Offer | None:
    """Granting a column anonymised or as totals, on Free."""
    return _once(store, LEVELS, "Showing a column to the AI as stand-ins or as totals only comes with Portward "
                                f"Personal ({TRIAL}): {account_url}/me")


PANEL_MOMENTS = (LEVELS, PROFILE)  # the moments Portward's panel shows (#116); WEB is the terminal's


def in_panel(store: LocalStore, moment: str, account_url: str) -> Offer | None:
    """The offer of a panel moment (PANEL_MOMENTS), once."""
    return levels(store, account_url) if moment == LEVELS else profile(store, account_url)
