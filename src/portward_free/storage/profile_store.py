"""Profiles in the local file (#91): every saved version, which connections run on profiles, and what is applied
where. Connections are matched like block rules: by the name a row was made under or by what the connection points
at (targets.py), so the same data added again under another name keeps its profiles and stays hidden by default."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from typing import Any

from portward_free import web_app
from portward_free.context import Context, Draft
from portward_free.profiles import Applied, Grant, Profile, later
from portward_free.row_rules import RowRule

Matching = Callable[[str], tuple[str, tuple[str, ...]]]  # a source name -> a WHERE clause and its parameters
Targets = Callable[[str], tuple[str, ...]]  # a source name -> what it points at
Clock = Callable[[], str]
App = Callable[[], str | None]  # the web AI app asking (web_app.current): None on desktop
SEEN_SETTING = "latest_time_seen"  # #134: for ending applications, this computer's clock only moves forwards


def _grants_json(grants: tuple[Grant, ...]) -> str:
    return json.dumps([{"schema": g.schema, "table": g.table, "column": g.column, "level": g.level} for g in grants])


def _context_json(context: Context) -> str:
    return json.dumps(context.stored())


def _rows_json(rows: tuple[RowRule, ...]) -> str:
    return json.dumps([r.stored() for r in rows])


def _draft(row: sqlite3.Row) -> Draft:
    item = Draft.item_from_stored(json.loads(row["body"]))
    return Draft(row["id"], row["profile_id"], item, row["drafted_by"], row["drafted_at"])


def _profile(row: sqlite3.Row) -> Profile:
    grants = tuple(Grant(g["table"], g["column"], g.get("schema"), g["level"]) for g in json.loads(row["grants"]))
    context = Context.from_stored(json.loads(row["context"]))
    rows = tuple(RowRule.from_stored(r) for r in json.loads(row["rows"]))
    return Profile(row["id"], row["name"], row["description"], row["made_by"], row["changed_by"], row["version"],
                   grants, row["saved_at"], context, rows)


LATEST = ("SELECT p.* FROM profiles p JOIN (SELECT id, max(version) AS version FROM profiles GROUP BY id) l "
          "ON l.id = p.id AND l.version = p.version")


class ProfileStore:
    def __init__(self, db: sqlite3.Connection, matching: Matching, targets: Targets, clock: Clock,
                 app: App = web_app.current) -> None:
        self._db, self._matching, self._targets, self._now, self._app = db, matching, targets, clock, app

    # --- profiles and their versions --------------------------------------------------------------------------------
    def save(self, profile: Profile) -> Profile:
        """Publish `profile` as its version; a version is never changed once saved."""
        saved_at = self._now()
        self._db.execute("INSERT INTO profiles (id, version, name, description, made_by, changed_by, grants, "
                         "saved_at, context, rows) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (profile.id, profile.version, profile.name, profile.description, profile.made_by,
                          profile.changed_by, _grants_json(profile.grants), saved_at,
                          _context_json(profile.context), _rows_json(profile.rows)))
        return dataclasses.replace(profile, saved_at=saved_at)

    def land(self, profile: Profile) -> None:
        """Hold `profile` as another computer saved it (sync, #97), its saved time kept; a version this computer
        holds differently (saved on two computers at once) is replaced by it."""
        self._db.execute("INSERT OR REPLACE INTO profiles (id, version, name, description, made_by, changed_by, "
                         "grants, saved_at, context, rows) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (profile.id, profile.version, profile.name, profile.description, profile.made_by,
                          profile.changed_by, _grants_json(profile.grants), profile.saved_at,
                          _context_json(profile.context), _rows_json(profile.rows)))

    def all(self) -> tuple[Profile, ...]:
        """The latest version of every profile, by name."""
        rows = self._db.execute(f"{LATEST} ORDER BY lower(p.name)").fetchall()
        return tuple(_profile(r) for r in rows)

    def named(self, name: str) -> Profile | None:
        """The latest version of the profile called `name` (any case)."""
        wanted = " ".join(name.split()).lower()
        return next((p for p in self.all() if p.name.lower() == wanted), None)

    def by_id(self, profile_id: str) -> Profile | None:
        row = self._db.execute(f"{LATEST} WHERE p.id = ?", (profile_id,)).fetchone()
        return _profile(row) if row else None

    # --- which connections run on profiles --------------------------------------------------------------------------
    def _stamp(self, source: str) -> tuple[str | None, str | None]:
        keys = self._targets(source)
        return (keys[0] if keys else None, keys[1] if len(keys) > 1 else None)

    def on_profiles(self, source: str) -> bool:
        where, params = self._matching(source)
        return self._db.execute(f"SELECT 1 FROM profile_connections WHERE {where} LIMIT 1",
                                params).fetchone() is not None

    def switch_to_profiles(self, source: str) -> None:
        self._db.execute("INSERT INTO profile_connections VALUES (?, ?, ?, ?) ON CONFLICT (source) DO NOTHING",
                         (source, *self._stamp(source), self._now()))

    def switch_to_block_rules(self, source: str) -> None:
        """Back to block rules: the connection forgets its profiles (the profiles themselves stay)."""
        where, params = self._matching(source)
        self._db.execute(f"DELETE FROM applied_profiles WHERE {where}", params)
        self._db.execute(f"DELETE FROM profile_connections WHERE {where}", params)

    def connections(self) -> tuple[str, ...]:
        """The connection names on profiles, as each row was made (sync, #97)."""
        return tuple(r["source"] for r in self._db.execute("SELECT source FROM profile_connections ORDER BY source"))

    def stamp(self, source: str, keys: tuple[str, ...]) -> None:
        """Give rows made under `source` before its target was known (synced) its target."""
        if keys:
            for table in ("profile_connections", "applied_profiles"):
                self._db.execute(f"UPDATE {table} SET target = ?, target_alt = ? WHERE source = ? AND target IS NULL",
                                 (keys[0], keys[1] if len(keys) > 1 else None, source))

    # --- applied profiles -------------------------------------------------------------------------------------------
    def apply(self, source: str, profile: Profile, missing: tuple[str, ...], *, until: str | None = None,
              apps: tuple[str, ...] = ()) -> None:
        """Apply `profile` to `source`, replacing any earlier application of it there (and its terms): `until` ends
        it (a UTC ISO time), `apps` limits it to those web AI apps (OAuth client ids; empty: every AI app)."""
        self.unapply(source, profile)  # one row per profile, under this name
        self._db.execute("INSERT INTO applied_profiles (source, profile_id, target, target_alt, missing, applied_at, "
                         "until, apps) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (source, profile.id, *self._stamp(source), json.dumps(list(missing)), self._now(), until,
                          json.dumps(sorted(set(apps)))))

    def unapply(self, source: str, profile: Profile) -> bool:
        where, params = self._matching(source)
        cursor = self._db.execute(f"DELETE FROM applied_profiles WHERE {where} AND profile_id = ?",
                                  (*params, profile.id))
        return cursor.rowcount > 0

    def held(self, source: str) -> tuple[Applied, ...]:
        """Every application on `source`, by profile name, whatever its terms: ended ones and ones for other AI apps
        too (listing, the hosted bundle, why-hidden). What applies now is `applied`."""
        where, params = self._matching(source)
        rows = self._db.execute(f"SELECT profile_id, missing, until, apps FROM applied_profiles WHERE {where} "
                                "ORDER BY applied_at", params).fetchall()
        found: dict[str, Applied] = {}
        for row in rows:
            profile = self.by_id(row["profile_id"])
            if profile is not None and profile.id not in found:
                found[profile.id] = Applied(profile, tuple(json.loads(row["missing"])), row["until"],
                                            tuple(json.loads(row["apps"])))
        return tuple(sorted(found.values(), key=lambda a: a.profile.name.lower()))

    def applied(self, source: str) -> tuple[Applied, ...]:
        """The profiles that apply to `source` now (latest versions: an update applies without asking), by name:
        not ended (on `seen_now`, so a clock set back can't bring one back) and for the AI app asking (web_app)."""
        held = self.held(source)
        now = self.seen_now() if any(a.until for a in held) else None
        app = self._app()
        return tuple(a for a in held if not (now and a.ended(now)) and a.for_app(app))

    def seen_now(self) -> str:
        """Now on this computer's clock, but never earlier than the latest time it has seen (kept in the file), so a
        clock set back can't make an ended application apply again. Moves the latest seen time forwards."""
        now = self._now()
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (SEEN_SETTING,)).fetchone()
        if row is not None and later(row["value"], now):
            return row["value"]
        self._db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                         (SEEN_SETTING, now))
        return now

    def applications(self) -> tuple[tuple[str, str], ...]:
        """Every (connection name, profile id) applied, as each row was made (sync, #97)."""
        return tuple((r["source"], r["profile_id"]) for r in self._db.execute(
            "SELECT source, profile_id FROM applied_profiles ORDER BY source, profile_id"))

    def applied_anywhere(self) -> tuple[Profile, ...]:
        """Every profile held applied on any connection, whatever its terms (link_caps: a column one of them
        anonymises lowers its link group on every connection)."""
        found = (self.by_id(pid) for pid in dict.fromkeys(pid for _, pid in self.applications()))
        return tuple(p for p in found if p is not None)

    def terms(self) -> dict[tuple[str, str], tuple[str | None, tuple[str, ...]]]:
        """Every application's (until, apps), by (connection name, profile id) as each row was made (sync)."""
        return {(r["source"], r["profile_id"]): (r["until"], tuple(json.loads(r["apps"]))) for r in self._db.execute(
            "SELECT source, profile_id, until, apps FROM applied_profiles")}

    # --- web AI apps seen (#134) ------------------------------------------------------------------------------------
    def seen_app(self, client_id: str, name: str | None) -> None:
        self._db.execute("INSERT INTO web_apps VALUES (?, ?, ?) ON CONFLICT (client_id) DO UPDATE SET "
                         "name = coalesce(excluded.name, web_apps.name), last_seen = excluded.last_seen",
                         (client_id, name, self._now()))

    def web_apps(self) -> tuple[tuple[str, str | None], ...]:
        """(OAuth client id, the name it gave) of every web AI app seen here, most recent first."""
        return tuple((r["client_id"], r["name"]) for r in self._db.execute(
            "SELECT client_id, name FROM web_apps ORDER BY last_seen DESC, client_id"))

    def set_missing(self, source: str, profile: Profile, missing: tuple[str, ...]) -> None:
        where, params = self._matching(source)
        self._db.execute(f"UPDATE applied_profiles SET missing = ? WHERE {where} AND profile_id = ?",
                         (json.dumps(list(missing)), *params, profile.id))

    def where_applied(self, profile: Profile, sources: tuple[str, ...]) -> tuple[str, ...]:
        """Which of `sources` (connection names) have `profile` applied."""
        return tuple(s for s in sources if any(a.profile.id == profile.id for a in self.applied(s)))

    def applications_of(self, profile: Profile, sources: tuple[str, ...]) -> tuple[tuple[str, Applied], ...]:
        """Where `profile` is held applied among `sources`, with its terms, ended or not."""
        return tuple((s, a) for s in sources for a in self.held(s) if a.profile.id == profile.id)

    def counts(self) -> dict[str, Any]:
        """For the usage counts: how many profiles, and their grants by level."""
        profiles = self.all()
        levels: dict[str, int] = {}
        for profile in profiles:
            for grant in profile.grants:
                levels[grant.level] = levels.get(grant.level, 0) + 1
        return {"profiles": len(profiles), "grants": levels}

    # --- context drafts and picks (#95) -----------------------------------------------------------------------------
    def add_draft(self, draft: Draft) -> Draft:
        drafted_at = self._now()
        self._db.execute("INSERT INTO context_drafts VALUES (?, ?, ?, ?, ?)",
                         (draft.id, draft.profile_id, json.dumps(draft.stored()), draft.drafted_by, drafted_at))
        return dataclasses.replace(draft, drafted_at=drafted_at)

    def draft(self, draft_id: str) -> Draft | None:
        row = self._db.execute("SELECT * FROM context_drafts WHERE id = ?", (draft_id.strip(),)).fetchone()
        return _draft(row) if row else None

    def drafts(self, profile_id: str | None = None) -> tuple[Draft, ...]:
        """Drafts waiting for the user, oldest first: every one, or one profile's."""
        where, params = ("WHERE profile_id = ?", (profile_id,)) if profile_id else ("", ())
        rows = self._db.execute(f"SELECT * FROM context_drafts {where} ORDER BY drafted_at, rowid", params).fetchall()
        return tuple(_draft(r) for r in rows)

    def drop_draft(self, draft_id: str) -> bool:
        return self._db.execute("DELETE FROM context_drafts WHERE id = ?", (draft_id.strip(),)).rowcount > 0

    def pick(self, subject: str, candidates: str) -> str | None:
        """The profile id the user picked between exactly these `candidates`, if they have."""
        row = self._db.execute("SELECT chosen FROM context_picks WHERE subject = ? AND candidates = ?",
                               (subject, candidates)).fetchone()
        return row["chosen"] if row else None

    def set_pick(self, subject: str, candidates: str, chosen: str) -> None:
        self._db.execute("INSERT INTO context_picks VALUES (?, ?, ?, ?) ON CONFLICT (subject, candidates) "
                         "DO UPDATE SET chosen = excluded.chosen, picked_at = excluded.picked_at",
                         (subject, candidates, chosen, self._now()))
