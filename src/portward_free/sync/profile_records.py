"""Profiles and where they're applied, as synced records (#97, Personal).

- profile: one record per profile (by its id), holding its latest version whole, its approved context included
  (meanings and named definitions, #95; links, #96, which a record from before links lacks; metrics, #129, and
  verified answers, #130, with their own versions, re-checked on arrival like a draft) and its row rules (#132),
  checked against the row-rule grammar again on arrival: a rule is a structured tree, never SQL text). Drafts the AI wrote and
  the user hasn't approved, and the AI's assumptions, stay on this computer. Each save is a whole new version,
  so the higher version wins; grants are never merged (a union of two versions' grants would show more than either
  computer saved). Two computers saving the same version at once: the other computer's lands as the profile and
  this computer's is kept here as a copy under a new name, applied nowhere, so nothing is lost and nothing shows
  more. Profiles aren't deleted by sync.
- on_profiles: a connection (by name) running on profiles instead of block rules. Switching to profiles (hide by
  default) lands at once; switching back to block rules shows more, so it needs the user's yes on each computer: a
  switch back made elsewhere is held here until the user does it here, like a removed block (#39).
- applied: a profile applied to a connection (by name and profile id), with its end date and AI apps (#134).
  Applying lands as the user said yes to it on the other computer; taking a profile off lands at once. Applied on one
  computer and taken off on another at once: taken off (it shows less); changed on both at once: the narrower terms.
  It waits for its profile to land.

Rows land under the connection name even before that connection is on this computer, and the connection's target
is stamped on them when it's added (storage), as for block rules."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from portward_free.context import CARDINALITIES, ONE_TO_ONE, Context
from portward_free.application_terms import MAX_APPS
from portward_free.metric_def import Metric, MetricError, new_metric
from portward_free.verified_def import VerifiedAnswer, VerifiedError, new_verified
from portward_free.profiles import LEVELS, MAX_NAME, Grant, Profile, ProfileError, later, rows_granted
from portward_free.row_rules import RowRule, RowRuleError
from portward_free.sync.kinds import Content, Kind, Landing, Place
from portward_free.web_app import CLIENT_ID

COPY_SUFFIX = " (copy from this computer)"


# --- profile ---------------------------------------------------------------------------------------------------------
def profile_content(profile: Profile) -> Content:
    return {"kind": "profile", "id": profile.id, "name": profile.name, "description": profile.description,
            "made_by": profile.made_by, "changed_by": profile.changed_by, "version": profile.version,
            "saved_at": profile.saved_at,
            "grants": [{"schema": g.schema, "table": g.table, "column": g.column, "level": g.level}
                       for g in profile.grants],
            "context": profile.context.stored()} | ({"rows": [r.stored() for r in profile.rows]} if profile.rows else {})


def profile_from(content: Content) -> Profile:
    grants = tuple(Grant(g["table"], g["column"], g["schema"], g["level"]) for g in content["grants"])
    context = Context.from_stored(_context_of(content["context"]))
    rows = tuple(RowRule.from_stored(r) for r in content.get("rows", ()))
    return Profile(content["id"], content["name"], content["description"], content["made_by"],
                   content["changed_by"], content["version"], grants, content["saved_at"], context, rows)


def _text(value: Any) -> bool:
    return isinstance(value, str)


def _valid_grant(grant: Any) -> bool:
    return isinstance(grant, dict) and _text(grant.get("table")) and _text(grant.get("column")) and \
        (grant.get("schema") is None or _text(grant.get("schema"))) and grant.get("level") in LEVELS


def _valid_meaning(meaning: Any) -> bool:
    values = meaning.get("values", {}) if isinstance(meaning, dict) else None
    return isinstance(meaning, dict) and _text(meaning.get("table")) and _text(meaning.get("meaning")) and \
        all(meaning.get(k) is None or _text(meaning.get(k)) for k in ("schema", "column")) and \
        isinstance(values, dict) and all(_text(k) and _text(v) for k, v in values.items()) and \
        all(isinstance(meaning.get(k, []), list) and all(map(_text, meaning.get(k, [])))
            for k in ("samples", "synonyms"))


def _valid_definition(definition: Any) -> bool:
    return isinstance(definition, dict) and _text(definition.get("term")) and _text(definition.get("text"))


def _valid_end(end: Any) -> bool:
    return isinstance(end, dict) and _text(end.get("table")) and _text(end.get("column")) and \
        (end.get("schema") is None or _text(end.get("schema")))


def _valid_link(link: Any) -> bool:
    return isinstance(link, dict) and _valid_end(link.get("one")) and _valid_end(link.get("other")) and \
        _text(link.get("text")) and link.get("cardinality", ONE_TO_ONE) in CARDINALITIES and \
        _valid_counts(link.get("counts"))


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_counts(counts: Any) -> bool:
    """A link's match counts (#131), optional: each end's rows and values, and optionally the values matching."""
    if counts is None:
        return True
    side = (lambda s: isinstance(s, dict) and _count(s.get("rows")) and _count(s.get("values")))
    return isinstance(counts, dict) and side(counts.get("one")) and side(counts.get("other")) and \
        (counts.get("matching") is None or _count(counts["matching"]))


def _context_of(context: Any) -> Any:
    """A profile's context as stored: {meanings, definitions} (#95); a record from before definitions is a list."""
    return {"meanings": context, "definitions": []} if isinstance(context, list) else context


def _valid_context(context: Any) -> bool:
    context = _context_of(context)
    return isinstance(context, dict) and isinstance(context.get("meanings"), list) \
        and all(map(_valid_meaning, context["meanings"])) and isinstance(context.get("definitions"), list) \
        and all(map(_valid_definition, context["definitions"])) \
        and isinstance(context.get("links", []), list) and all(map(_valid_link, context.get("links", []))) \
        and isinstance(context.get("metrics", []), list) and all(map(_valid_metric, context.get("metrics", []))) \
        and isinstance(context.get("verified_answers", []), list) \
        and all(map(_valid_verified, context.get("verified_answers", [])))


def _valid_verified(answer: Any) -> bool:
    """A verified answer (#130) another computer sent: re-checked here exactly as a promoted one is (one SELECT, a
    result hash), so a record can't carry SQL a promotion couldn't."""
    if not isinstance(answer, dict) or not isinstance(answer.get("version"), int) or isinstance(
            answer.get("version"), bool) or answer["version"] < 1:
        return False
    try:
        found = VerifiedAnswer.from_stored(answer)
        again = new_verified(found.question, found.sql, found.source, found.dialect, found.result_hash,
                             answer["row_count"], found.checked_at)
    except (VerifiedError, KeyError, TypeError, ValueError, AttributeError):
        return False
    return replace(again, version=found.version) == found


def _valid_metric(metric: Any) -> bool:
    """A metric (#129) another computer sent: its calculation is re-checked here exactly as a draft is, so a record
    can't carry SQL a drafted metric couldn't."""
    if not isinstance(metric, dict) or not isinstance(metric.get("version"), int) or isinstance(
            metric.get("version"), bool) or metric["version"] < 1:
        return False
    try:
        found = Metric.from_stored(metric)
        again = new_metric(found.name, found.measure, found.table, found.filters, found.time, found.grains,
                           found.group_by, found.text)
    except (MetricError, KeyError, TypeError, ValueError, AttributeError):
        return False
    return replace(again, version=found.version) == found


def _valid_profile(content: Content) -> bool:
    version = content.get("version")
    return all(_text(content.get(k)) for k in ("id", "name", "description", "made_by", "changed_by", "saved_at")) \
        and isinstance(version, int) and not isinstance(version, bool) and version >= 1 \
        and isinstance(content.get("grants"), list) and all(map(_valid_grant, content["grants"])) \
        and _valid_context(content.get("context")) and _valid_rows(content.get("rows", []), content["grants"])


def _valid_rows(rows: Any, grants: list) -> bool:
    """Row rules in the grammar (row_rules.RowRule.from_stored), one per table, each on a table the profile grants."""
    if not isinstance(rows, list):
        return False
    try:
        found = tuple(RowRule.from_stored(r) for r in rows)
        kept = rows_granted(found, (Grant(g["table"], g["column"], g["schema"], g["level"]) for g in grants))
    except (RowRuleError, ProfileError):
        return False
    return len(kept) == len(found)


def _merge_profile(local: Content | None, remote: Content) -> Content:
    if local is not None and local["version"] > remote["version"]:
        return local
    return remote  # a tie lands the other computer's; apply keeps this computer's as a copy


def _copy_name(place: Place, name: str) -> str:
    taken = {p.name.lower() for p in place.store.profiles.all()}
    for n in range(1, 100):
        suffix = COPY_SUFFIX if n == 1 else f"{COPY_SUFFIX[:-1]} {n})"
        candidate = name[:MAX_NAME - len(suffix)] + suffix
        if candidate.lower() not in taken:
            return candidate
    raise AssertionError("no free name for a profile copy")


def _apply_profile(place: Place, content: Content, current: Content | None, landing: Landing) -> Content | None:
    profiles = place.store.profiles
    held = profiles.by_id(content["id"])
    if held is not None and held.version > content["version"]:
        return profile_content(held)  # newer here: sent on the next run
    incoming = profile_from(content)
    if held is not None and held.version == incoming.version and profile_content(held) != profile_content(incoming):
        mine = replace(Profile.new(_copy_name(place, held.name), held.description, held.changed_by, held.grants),
                       context=held.context)  # definitions too, not only the meanings
        profiles.save(mine)
        landing.profile_copies += 1
    profiles.land(incoming)
    landed = profiles.by_id(incoming.id)
    assert landed is not None
    return profile_content(landed)


PROFILE = Kind(name="profile", key=lambda c: (c["id"],), valid=_valid_profile,
               local=lambda place: [profile_content(p) for p in place.store.profiles.all()],
               apply=_apply_profile, merge=_merge_profile)


# --- on_profiles ------------------------------------------------------------------------------------------------------
def _apply_switch(place: Place, content: Content, current: Content | None, landing: Landing) -> Content | None:
    if content["on"]:
        place.store.profiles.switch_to_profiles(content["source"])
        return content
    if current is not None:  # switched back elsewhere: stays on profiles here until the user says so here
        landing.held_switches += 1
        return current
    return None


ON_PROFILES = Kind(
    name="on_profiles", key=lambda c: (c["source"],),
    valid=lambda c: _text(c.get("source")) and isinstance(c.get("on"), bool),
    local=lambda place: [{"kind": "on_profiles", "source": s, "on": True} for s in place.store.profiles.connections()],
    apply=_apply_switch,
    merge=lambda local, remote: {**remote, "on": remote["on"] or (local is not None and local["on"])},
    tombstone=lambda remote: {**remote, "on": False}, gone=lambda c: not c["on"])


# --- applied ---------------------------------------------------------------------------------------------------------
def valid_terms(content: Content) -> bool:
    """An application's terms (#134), where present: `until` an ISO time or null, `apps` a list of OAuth client ids.
    A record from before #134 has neither."""
    until, apps = content.get("until"), content.get("apps", [])
    if until is not None:
        try:
            later(until, until)
        except (TypeError, ValueError):
            return False
    return isinstance(apps, list) and len(apps) <= MAX_APPS and all(isinstance(a, str) and CLIENT_ID.fullmatch(a)
                                                                     for a in apps)


def _terms(content: Content) -> tuple[str | None, tuple[str, ...]]:
    return content.get("until"), tuple(sorted(set(content.get("apps", []))))


def _apply_applied(place: Place, content: Content, current: Content | None, landing: Landing) -> Content | None:
    profiles = place.store.profiles
    profile = profiles.by_id(content["profile_id"])
    source = content["source"]
    if not content["applied"]:
        if profile is not None and current is not None:
            profiles.unapply(source, profile)
        return None
    if profile is None:
        return None  # waits for its profile
    profiles.switch_to_profiles(source)
    until, apps = _terms(content)
    if profiles.terms().get((source, profile.id)) != (until, apps):
        profiles.apply(source, profile, (), until=until, apps=apps)
    return content


def _narrower(local: Content, remote: Content) -> Content:
    """Both computers changed one application at once: the terms that show less (the earlier end date; for apps, the
    ones both name, or one side's list when the other applies for every app)."""
    ends = [u for u in (local.get("until"), remote.get("until")) if u]
    until = ends[0] if ends else None
    if len(ends) == 2 and later(ends[0], ends[1]):
        until = ends[1]
    mine, theirs = local.get("apps", []), remote.get("apps", [])
    apps = sorted(set(mine) & set(theirs)) if mine and theirs else sorted(mine or theirs)
    if mine and theirs and not apps:
        return {**remote, "applied": False}  # no AI app both allow: it applies for none
    return {**remote, "until": until, "apps": apps}


def _merge_applied(local: Content | None, remote: Content) -> Content:
    if not (remote["applied"] and local is not None and local["applied"]):
        return {**remote, "applied": False}
    return _narrower(local, remote)


APPLIED = Kind(
    name="applied", key=lambda c: (c["source"], c["profile_id"]),
    valid=lambda c: _text(c.get("source")) and _text(c.get("profile_id")) and isinstance(c.get("applied"), bool)
    and valid_terms(c),
    local=lambda place: [{"kind": "applied", "source": s, "profile_id": p, "applied": True, "until": until,
                          "apps": list(apps)}
                         for (s, p), (until, apps) in sorted(place.store.profiles.terms().items())],
    apply=_apply_applied, merge=_merge_applied,
    tombstone=lambda remote: {**remote, "applied": False}, gone=lambda c: not c["applied"])

KINDS = (PROFILE, ON_PROFILES, APPLIED)  # in the order they land: a profile before where it's applied
