"""The terms an application of a profile can carry (#134, Personal): Until (an end date) and the web AI apps it is for.

- Until: a date (it applies through that day, on this computer's clock) or an ISO time. After it the profile stops
  applying by itself (storage.profile_store.applied), offline too, and a clock set back never brings it back.
- For apps: on the web routes only, the OAuth client the user signed in to Portward from is known (web_app), so an
  application can be limited to some AI apps, named as seen on this computer's web route or by OAuth client id. On
  desktop the AI app names itself, so there is no per-app option there: a limited application never applies on
  desktop.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

from portward_free.profiles import Applied, ProfileError, later
from portward_free.web_app import CLIENT_ID, UNNAMED

MAX_APPS = 20


def parse_until(text: str | None, now: str) -> str | None:
    """`text` as the UTC ISO time the application ends, or None for no end. A date alone runs through that day in
    this computer's time zone. ProfileError when it isn't a date or time, or isn't after `now`."""
    if text is None or not text.strip():
        return None
    try:
        ends = _ends(text.strip())
    except ValueError:
        raise ProfileError(f"until must be a date like 2026-11-14 or an ISO time, not {text!r}") from None
    until = ends.astimezone(UTC).isoformat(timespec="seconds")
    if not later(until, now):
        raise ProfileError(f"until {text} is already in the past, so the profile would never apply; pick a later date")
    return until


def _ends(text: str) -> datetime:
    if len(text) == 10:
        day = date.fromisoformat(text)
        return datetime.combine(day + timedelta(days=1), time()).astimezone()  # the next midnight, local time
    found = datetime.fromisoformat(text)
    return found if found.tzinfo else found.astimezone()


def resolve_apps(wanted: Sequence[str] | None, seen: Sequence[tuple[str, str | None]]) -> tuple[str, ...]:
    """The OAuth client ids for `wanted`: each a web AI app's name as seen on this computer's web route (any case),
    or an OAuth client id. Empty: every AI app. ProfileError for anything else."""
    found: list[str] = []
    for item in wanted or ():
        name = " ".join(str(item).split())
        by_name = [client for client, label in seen if label and label.lower() == name.lower()]
        if len(by_name) > 1:
            raise ProfileError(f"more than one web AI app is called {name!r}; give its OAuth client id: "
                               + ", ".join(by_name))
        if by_name:
            found.append(by_name[0])
        elif name != UNNAMED and name != "connector" and CLIENT_ID.fullmatch(name):
            found.append(name)
        else:
            raise ProfileError(f"{name!r} isn't a web AI app seen here or an OAuth client id")
    if len(set(found)) > MAX_APPS:
        raise ProfileError(f"an application can name at most {MAX_APPS} AI apps")
    return tuple(sorted(set(found)))


def describe(applied: Applied, now: str, seen: Sequence[tuple[str, str | None]] = ()) -> str:
    """The terms in words, e.g. "until 2026-11-14 00:00 UTC (ended); on the web for ChatGPT only"; "" for none."""
    parts = []
    if applied.until:
        parts.append(f"until {_shown(applied.until)}" + (" (ended)" if applied.ended(now) else ""))
    if applied.apps:
        labels = dict(seen)
        names = [labels.get(client) or client for client in applied.apps]
        parts.append(f"on the web for {', '.join(names)} only (never on desktop)")
    return "; ".join(parts)


def _shown(until: str) -> str:
    return datetime.fromisoformat(until).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
