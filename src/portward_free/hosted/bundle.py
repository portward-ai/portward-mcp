"""What the computer hands the hosted engine (#104), and how the engine takes it.

A bundle is one JSON object, sealed to the engine's key (hosted/seal.py BUNDLE) before it leaves the computer:
- the connections on the hosted route: name, kind, settings and the login (from the keychain);
- what governs them, exactly as on the computer: block rules and notes for a connection on Free's rules; for one on
  profiles, the profiles applied to it (their latest version, context included) and where they're applied;
- the account's anonymising key (#93), so a stand-in is the same on the web as on the computer;
- the public half of the account's inbox key (hosted/inbox.py), which the engine seals its AI log summary to.

The engine holds only what the latest bundle says: landing one first forgets every connection, login, rule, note
and application it held, then takes the bundle's. So taking a connection off the hosted route, or a profile off a
connection, reaches the engine with the next bundle. Profiles themselves stay in the engine's file (unused) as they
do on a computer; nothing lets the AI see them there."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from typing import Any

from portward_free import stand_ins
from portward_free.keychain import Keychain, account_for
from portward_free.remote.routes import HOSTED_LABEL, offered_routes
from portward_free.route_names import HOSTED
from portward_free.rules import Rule
from portward_free.storage import LocalStore
from portward_free.sync.profile_records import valid_terms, _valid_profile, profile_content, profile_from

FORMAT = 1
LABEL = HOSTED_LABEL


class NotHostable(Exception):
    """A connection that can't go on the hosted route; the message is safe to show."""


class BadBundle(Exception):
    """A bundle this engine won't land (a shape it doesn't know); nothing was changed."""


def hostable(store: LocalStore, name: str) -> None:
    """Raises NotHostable unless `name` may go on the hosted route: Portward's own connectors to databases on the
    internet. Files, databases only this computer can reach and wrapped servers stay on the direct route."""
    connection = store.connection(name)
    if connection is None:
        raise NotHostable(f"there's no source named {name!r}")
    if HOSTED not in offered_routes(connection.kind, connection.settings):
        raise NotHostable(f"{name} can only use the direct route: files, databases only this computer can reach "
                          "and wrapped servers are never sent to Portward's hosted engine")


def build(store: LocalStore, keychain: Keychain, names: Iterable[str], inbox_public: bytes) -> dict[str, Any]:
    """The bundle for these connections (each checked with `hostable`)."""
    connections, rules, notes, profiles, applied, on_profiles = [], [], [], {}, [], []
    for name in sorted(set(names)):
        hostable(store, name)
        connection = store.connection(name)
        assert connection is not None
        login = keychain.get(account_for(name))
        if not login:
            raise NotHostable(f"{name} has no saved password on this computer; set it first (update_password)")
        connections.append({"name": name, "kind": connection.kind, "settings": connection.settings, "login": login})
        if store.profiles.on_profiles(name):
            on_profiles.append(name)
            now = store.profiles.seen_now()
            for used in store.profiles.held(name):  # every AI app's (#134): the engine keeps each to its apps
                if used.ended(now):
                    continue
                profiles[used.profile.id] = profile_content(used.profile)
                applied.append({"source": name, "profile_id": used.profile.id, "until": used.until,
                                "apps": list(used.apps)})
        else:
            rules.extend({"source": name, "table": r.table, "column": r.column} for r in store.rules(name))
            notes.extend({"source": name, "table": t, "column": c, "note": n}
                         for (t, c), n in sorted(store.notes(name).items()))
    return {"format": FORMAT, "connections": connections, "rules": rules, "notes": notes,
            "profiles": list(profiles.values()), "on_profiles": on_profiles, "applied": applied,
            "anon_key": base64.b64encode(stand_ins.account_key(keychain)).decode(),
            "inbox_key": base64.b64encode(inbox_public).decode()}


def fingerprint(bundle: dict[str, Any]) -> str:
    """Whether anything in it changed since the last one sent (kept on the computer only)."""
    return hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()


# --- on the engine ---------------------------------------------------------------------------------------------------
def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _key(value: Any, size: int) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != size:
        raise BadBundle("a key in the bundle isn't the right length")
    return raw


def check(bundle: dict[str, Any]) -> None:
    """Raises BadBundle unless every part has the shape this Portward writes and stays on the hosted route."""
    if bundle.get("format") != FORMAT:
        raise BadBundle("the bundle is in a format this engine doesn't know; update the engine")
    connections = bundle.get("connections")
    if not isinstance(connections, list):
        raise BadBundle("the bundle has no connections list")
    names = set()
    for c in connections:
        if not (isinstance(c, dict) and _text(c.get("name")) and _text(c.get("kind")) and _text(c.get("login"))
                and isinstance(c.get("settings"), dict)):
            raise BadBundle("a connection in the bundle isn't complete")
        if HOSTED not in offered_routes(c["kind"], c["settings"]):  # the computer checked; the engine does too
            raise BadBundle(f"{c['name']} can't be on the hosted route")
        names.add(c["name"])
    for part in ("rules", "notes", "profiles", "on_profiles", "applied"):
        if not isinstance(bundle.get(part), list):
            raise BadBundle(f"the bundle's {part} isn't a list")
    for r in bundle["rules"]:
        if not (isinstance(r, dict) and r.get("source") in names and _text(r.get("table"))
                and (r.get("column") is None or _text(r.get("column")))):
            raise BadBundle("a rule in the bundle isn't complete")
    for n in bundle["notes"]:
        if not (isinstance(n, dict) and n.get("source") in names and _text(n.get("table"))
                and isinstance(n.get("column"), str) and _text(n.get("note"))):
            raise BadBundle("a note in the bundle isn't complete")
    if not all(isinstance(p, dict) and _valid_profile(p) for p in bundle["profiles"]):
        raise BadBundle("a profile in the bundle isn't complete")
    ids = {p["id"] for p in bundle["profiles"]}
    if not all(s in names for s in bundle["on_profiles"]):
        raise BadBundle("the bundle puts an unknown connection on profiles")
    if not all(isinstance(a, dict) and a.get("source") in bundle["on_profiles"] and a.get("profile_id") in ids
               for a in bundle["applied"]):
        raise BadBundle("the bundle applies a profile it doesn't hold")
    if not all(valid_terms(a) for a in bundle["applied"]):
        raise BadBundle("an application in the bundle has an end date or AI apps this engine can't read")
    _key(bundle.get("anon_key"), stand_ins.KEY_BYTES)
    _key(bundle.get("inbox_key"), 32)


def forget_everything(store: LocalStore, keychain: Keychain) -> None:
    """The engine holds nothing: no connection, login, rule, note or application."""
    for connection in store.connections():
        store.profiles.switch_to_block_rules(connection.name)
        keychain.delete(account_for(connection.name))
        store.remove_connection(connection.name)
    for source in store.profiles.connections():
        store.profiles.switch_to_block_rules(source)
    for rule in store.rules():
        store.remove_rule(rule)
    for source, table, column, _ in store.all_notes():
        store.set_note(source, table, column, "")


def land(store: LocalStore, keychain: Keychain, bundle: dict[str, Any]) -> bytes:
    """Make the engine hold exactly what `bundle` says (checked first: BadBundle and nothing changes). Returns the
    inbox public key to seal the AI log summary to."""
    check(bundle)
    forget_everything(store, keychain)
    for c in bundle["connections"]:
        store.add_connection(c["name"], c["kind"], c["settings"])  # opened public-only on this route (egress.py)
        keychain.set(account_for(c["name"]), c["login"])
    for r in bundle["rules"]:
        store.add_rule(Rule(r["source"], r["table"], r["column"]))
    for n in bundle["notes"]:
        store.set_note(n["source"], n["table"], n["column"], n["note"])
    for p in bundle["profiles"]:
        store.profiles.land(profile_from(p))
    for source in bundle["on_profiles"]:
        store.profiles.switch_to_profiles(source)
    for a in bundle["applied"]:
        profile = store.profiles.by_id(a["profile_id"])
        assert profile is not None
        store.profiles.apply(a["source"], profile, (), until=a.get("until"), apps=tuple(a.get("apps", ())))
    stand_ins.adopt(keychain, _key(bundle["anon_key"], stand_ins.KEY_BYTES))
    return _key(bundle["inbox_key"], 32)
