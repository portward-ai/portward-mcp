"""The context editor's logic (#127, Personal): one sitting of context work on the local Portward page. Its routes are
in context_editor_page.py and its markup in context_editor_html.py.

Like the profile editor (#126), the editor is one form and nothing is stored until Save. Every decision made in the
sitting (each draft approved, with the user's edit, skipped or discarded; definitions and links added, changed or
removed; meanings taken from a dictionary file; assumptions turned into definitions) is carried in the form as one
JSON field (`Sitting`), and Save publishes exactly one new version of the profile with all of them, signed with the
account's email. Drafts stay unused, on this computer, until that save (or until approve_context approves one by
tool): the AI is never given a draft.

Context that names something the profile doesn't grant, or a column that's gone (context_drift), can't be approved
or added here: the AI wouldn't be given it, so the user is shown why and the fix before it joins the profile.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from portward_free.context import (
    ContextError,
    Definition,
    Draft,
    Link,
    Meaning,
    links_granted,
    new_definition,
    new_link,
    ungranted,
)
from portward_free.context_drift import drifted, gone_in
from portward_free.profiles import Profile, about_granted
from portward_free.sources import Table

APPROVE, SKIP, DISCARD = "approve", "skip", "discard"
DECISIONS = (APPROVE, SKIP, DISCARD)
UNREADABLE = "this page's unsaved changes couldn't be read; open the context editor again"


def link_id(link: Link) -> str:
    """A link's key as text, the same whichever end comes first."""
    return " = ".join(sorted(e.name.lower() for e in (link.one, link.other)))


@dataclass(frozen=True)
class Sitting:
    """Everything decided since the page opened the profile's version, none of it stored yet."""

    decisions: Mapping[str, str] = field(default_factory=dict)  # draft id -> approve, skip or discard
    texts: Mapping[str, str] = field(default_factory=dict)  # draft id -> the user's text, for approved drafts
    definitions: Mapping[str, tuple[str, str] | None] = field(default_factory=dict)  # term key -> (term, text); None: removed
    links: Mapping[str, dict | None] = field(default_factory=dict)  # link_id -> stored link; None: removed
    meanings: tuple[dict, ...] = ()  # stored meanings taken from a dictionary file
    assumptions: Mapping[str, str] = field(default_factory=dict)  # assumption id -> the term it becomes

    def dumps(self) -> str:
        return json.dumps({"d": dict(self.decisions), "t": dict(self.texts),
                           "def": {k: list(v) if v else None for k, v in self.definitions.items()},
                           "lk": dict(self.links), "m": list(self.meanings), "a": dict(self.assumptions)},
                          separators=(",", ":"))

    @classmethod
    def loads(cls, text: str) -> Sitting:
        """The sitting a form carried back; ContextError when it isn't one this page wrote."""
        if not text.strip():
            return cls()
        try:
            data = json.loads(text)
            decisions = _str_map(data.get("d", {}))
            if any(v not in DECISIONS for v in decisions.values()):
                raise ValueError
            definitions = {str(k): (_pair(v) if v is not None else None) for k, v in data.get("def", {}).items()}
            links = {str(k): (Link.from_stored(v).stored() if v is not None else None)
                     for k, v in data.get("lk", {}).items()}
            meanings = tuple(Meaning.from_stored(m).stored() for m in data.get("m", []))
            return cls(decisions, _str_map(data.get("t", {})), definitions, links, meanings,
                       _str_map(data.get("a", {})))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ContextError(UNREADABLE) from None

    # --- changes ------------------------------------------------------------------------------------------------
    def decided(self, draft_id: str, decision: str, text: str | None = None) -> Sitting:
        texts = {k: v for k, v in self.texts.items() if k != draft_id}
        if decision == APPROVE and text is not None:
            texts[draft_id] = text
        return replace(self, decisions={**self.decisions, draft_id: decision}, texts=texts)

    def with_definition(self, key: str, value: tuple[str, str] | None, assumption: str = "") -> Sitting:
        found = replace(self, definitions={**self.definitions, key: value})
        if assumption and value is not None:
            found = replace(found, assumptions={**self.assumptions, assumption: value[0]})
        return found

    def without_definition(self, key: str) -> Sitting:
        """The sitting with its change to `key` undone."""
        return replace(self, definitions={k: v for k, v in self.definitions.items() if k != key},
                       assumptions={a: t for a, t in self.assumptions.items() if t.lower() != key})

    def with_link(self, key: str, value: dict | None) -> Sitting:
        return replace(self, links={**self.links, key: value})

    def without_link(self, key: str) -> Sitting:
        return replace(self, links={k: v for k, v in self.links.items() if k != key})

    def with_meanings(self, meanings: Sequence[Meaning]) -> Sitting:
        keys = {m.key for m in meanings}
        kept = tuple(m for m in self.meanings if Meaning.from_stored(m).key not in keys)
        return replace(self, meanings=(*kept, *(m.stored() for m in meanings)))

    # --- what it comes to ---------------------------------------------------------------------------------------
    def approved(self, drafts: Sequence[Draft]) -> tuple[Draft, ...]:
        return tuple(d.edited(self.texts[d.id]) if d.id in self.texts else d
                     for d in drafts if self.decisions.get(d.id) == APPROVE)

    def discarded(self, drafts: Sequence[Draft]) -> tuple[Draft, ...]:
        return tuple(d for d in drafts if self.decisions.get(d.id) == DISCARD)

    def change_count(self, drafts: Sequence[Draft]) -> int:
        return len(self.approved(drafts)) + len(self.meanings) + len(self.definitions) + len(self.links)

    def edited(self, profile: Profile, drafts: Sequence[Draft], by: str) -> Profile:
        """The next version with every change of the sitting: one version for all of them."""
        return profile.saved(by, context=self.context(profile, drafts))

    def context(self, profile: Profile, drafts: Sequence[Draft]):
        context = profile.context
        for draft in self.approved(drafts):
            context = draft.onto(context)
        for stored in self.meanings:
            context = context.with_meaning(Meaning.from_stored(stored))
        for key, value in self.definitions.items():
            context = (context.with_definition(Definition(*value)) if value else
                       replace(context, definitions=tuple(d for d in context.definitions if d.key != key)))
        for key, stored in self.links.items():
            context = (context.with_link(Link.from_stored(stored)) if stored else
                       replace(context, links=tuple(k for k in context.links if link_id(k) != key)))
        return context


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError
    return dict(value)


def _pair(value: object) -> tuple[str, str]:
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value)):
        raise ValueError
    return value[0], value[1]


def next_draft(drafts: Sequence[Draft], sitting: Sitting, after: str = "") -> Draft | None:
    """The next draft without a decision: after `after` in the queue, else from the start; None when all are done."""
    waiting = [d for d in drafts if d.id not in sitting.decisions]
    ids = [d.id for d in drafts]
    if after in ids:
        later = [d for d in waiting if ids.index(d.id) > ids.index(after)]
        if later:
            return later[0]
    return waiting[0] if waiting else None


# --- checks before anything joins the sitting -----------------------------------------------------------------------

def problem_with(text: str, profile: Profile, catalogs: Sequence[Sequence[Table]]) -> str:
    """Why `text` can't be given to the AI through `profile` (it names something the profile doesn't grant, or a
    column that's gone, with the fix), or "" when it can."""
    hidden = ungranted(text, profile.grants, catalogs)
    if hidden:
        return (f"it names {', '.join(hidden)}, which {profile.name} doesn't grant, so the AI would learn it exists. "
                "Edit the text, or grant it in the profile first")
    for catalog in catalogs:
        gone = gone_in(text, profile.grants, catalog)
        if gone:
            names = ", ".join(g.name for g in gone)
            return f"it names {names}, which is gone from the data: " + "; ".join(g.fix() for g in gone)
    return ""


def approvable(draft: Draft, text: str | None, profile: Profile, catalogs: Sequence[Sequence[Table]]) -> Draft:
    """The draft as the user approves it (with their text); ContextError, safe to show, when it can't be."""
    final = draft.edited(text) if text is not None and text.strip() else draft
    item = final.item
    if isinstance(item, Meaning):
        for about in filter(None, (item, item.person_column)):
            if not about_granted([about], profile.grants):
                raise ContextError(f"{profile.name} doesn't grant {about.about} any more, so it can't have a "
                                   "meaning; grant it in the profile first, or discard the draft")
    if isinstance(item, Link) and not links_granted([item], profile.grants):
        raise ContextError(f"{profile.name} must grant both {item.one.name} and {item.other.name} to link them")
    why = problem_with(final.item.words if isinstance(final.item, Meaning) else final.text, profile, catalogs)
    if why:
        raise ContextError(f"This can't be approved yet: {why}.")
    return final


def checked_definition(term: str, text: str, profile: Profile, catalogs: Sequence[Sequence[Table]]) -> Definition:
    definition = new_definition(term, text)
    why = problem_with(definition.text, profile, catalogs)
    if why:
        raise ContextError(f"{definition.term} can't be added yet: {why}.")
    return definition


def checked_link(one: str, other: str, text: str, profile: Profile,
                 catalogs: Sequence[Sequence[Table]]) -> Link:
    link = new_link(one, other, text.strip() or None)
    if not links_granted([link], profile.grants):
        raise ContextError(f"{profile.name} must grant both {link.one.name} and {link.other.name} to link them; "
                           "grant them in the profile first")
    why = problem_with(link.text, profile, catalogs)
    if why:
        raise ContextError(f"The link can't be added yet: {why}.")
    return link


def held_back(profile: Profile, catalogs: Sequence[Sequence[Table]]) -> dict[str, str]:
    """Each context item of `profile` (as drafted) the AI won't be given, by its why_hidden name ("definition MRR",
    "link a.b = c.d", "meaning of t.c"), with why and the fix."""
    found: dict[str, str] = {}
    for catalog in catalogs:
        for item in drifted([profile], catalog):
            found.setdefault(item.about.lower(), item.why)
    for definition in profile.context.definitions:
        hidden = ungranted(definition.text, profile.grants, catalogs)
        if hidden:
            found.setdefault(f"definition {definition.term}".lower(),
                             f"it names {', '.join(hidden)}, which {profile.name} doesn't grant, so it isn't sent; "
                             "reword it, or grant that in the profile")
    for link in profile.context.links:
        hidden = ungranted(link.text, profile.grants, catalogs)
        if hidden:
            found.setdefault(f"link {link.one.name} = {link.other.name}".lower(),
                             f"its text names {', '.join(hidden)}, which {profile.name} doesn't grant; reword it")
    return found
