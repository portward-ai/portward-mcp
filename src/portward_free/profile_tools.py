"""The profile tools (#91, Personal): write profiles that grant columns in full, apply them to connections, and
switch a connection between block rules and profiles. A connection still on block rules can start from a profile
made of them and its notes (#92, starting_profile.py).

Every tool that writes needs this computer signed in to an account with Personal on (trial, active or grace); a
profile's versions are signed with that account's email. Without it they refuse and nothing changes, and Free's
block rules work as before. Profiles already applied keep hiding whatever the plan: after a lapse they are frozen
profiles (#108), applying exactly as they were: no column loosens. Anything that could show more (editing,
creating, applying, going back to block rules) is refused until Personal is on again; taking a profile off a
connection, which can only show less, still works. The plan last seen is kept (account.remember_plan) only to say
so offline; it never lets a change through. On Free these tools aren't listed at all (#116, plan_tools.py), and the
offers of Personal are shown in Portward's panel, never with a refusal here.

As with `unblock`, a change that may let the AI read more is the user's to make, not the AI's: switching a
connection away from its block rules, applying a profile that shows something not shown now, saving a grant onto
an applied profile, and going back to block rules ask the user in their client's own prompt (elicitation) and
change nothing without their yes. Narrowing (taking a profile off, applying to a connection with no block rules)
runs at once. Taking a profile off runs whatever the plan, after a lapse too, because it only ever shows less.
Going back to block rules may show more, so it needs Personal on now. After a lapse it's refused, also offline or
signed out, so that no column loosens (#90). Connections that never had profiles keep Free's block rules as before.

No `from __future__ import annotations` here: the `Resolve(...)` annotations must hold the resolvers themselves.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Annotated

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from portward_free.account import Accounts, AccountServiceError, account_token, known_plan, plan_of, remember_plan
from portward_free.clients import client_support
from portward_free.application_terms import describe, parse_until, resolve_apps
from portward_free.grants import Grants
from portward_free.keychain import Keychain, KeychainError
from portward_free.profiles import (ANONYMISED, LEVELS, TOTALS, Applied, Grant, Profile, ProfileError, preview,
                                    table_grants)
from portward_free.replies import error, reply
from portward_free.rule_tools import Opener
from portward_free.rules import Rules
from portward_free.sources import SourceError, Table
from portward_free.starting_profile import carried_notes, starting_grants
from portward_free.storage import Connection, LocalStore

NEEDS_PERSONAL = ("Profiles need Portward Personal on this computer's account (signed in, on the trial or an active "
                  "plan). Block rules keep working: block hides a table or column.")
SIGN_IN_FIRST = ("Profiles are part of Portward Personal and need this computer signed in to the user's Portward "
                 "account: call account_sign_in.")
CANT_ASK = ("this app can't ask the user to confirm, so it can't be done from chat; use an app that can ask (Claude "
            "Code does)")
NOT_CONFIRMED = "the user didn't confirm, so nothing changed"
FROZEN = ("Personal has ended on this computer's account, so its profiles are frozen: they keep applying exactly as "
          "they were, but can't be edited, created, applied to new connections or swapped back for block rules. A "
          "profile can still be taken off a connection, which only hides more. They unfreeze when Personal is on "
          "again.")


class Confirm(BaseModel):
    confirm: bool = Field(description="Yes, go ahead")


@dataclass(frozen=True)
class Step:
    """What a tool call would do: refused (`refusal`), or done, after asking `question` first when there is one."""

    refusal: str | None = None
    question: str | None = None
    found: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Standing:
    """This computer's account for Personal's tools: `email` (the signer) when Personal is on, else `why` not.
    `free`: known to be on Free (signed out, or the plan ended), never when the plan couldn't be checked."""

    email: str | None
    why: str | None = None
    free: bool = False


def register_profile_tools(server: MCPServer, store: LocalStore, keychain: Keychain, accounts: Accounts,
                           opened: Opener) -> "ProfileTools":
    tools = ProfileTools(store, keychain, accounts, opened)
    tools.register_writing(server)
    tools.register_starting(server)
    tools.register_applying(server)
    tools.register_list(server)
    return tools


@dataclass
class ProfileTools:
    store: LocalStore
    keychain: Keychain
    accounts: Accounts
    opened: Opener

    # --- the account ------------------------------------------------------------------------------------------------
    def standing(self) -> Standing:
        """Asked of the account service now. The plan it gives is kept (remember_plan) so that, offline, a lapsed
        account still hears its profiles are frozen; an unknown plan never lets anything through."""
        try:
            token = account_token(self.keychain)
            if not token:
                return Standing(None, SIGN_IN_FIRST, free=True)
            me = self.accounts.service.me(token)
        except (AccountServiceError, KeychainError) as err:
            if isinstance(err, AccountServiceError) and err.status == 401:
                return Standing(None, SIGN_IN_FIRST, free=True)
            if self.frozen() and self.store.profiles.all():
                return Standing(None, FROZEN)
            return Standing(None, f"Portward couldn't check this computer's plan ({err}); try again")
        plan = plan_of(me)
        if plan is not None:
            remember_plan(self.store, plan)
        if plan is None or not plan["personal"] or not isinstance(me.get("email"), str):
            frozen = plan is not None and plan["state"] == "ended" and bool(self.store.profiles.all())
            return Standing(None, FROZEN if frozen else NEEDS_PERSONAL, free=True)
        return Standing(me["email"])

    def personal_email(self) -> tuple[str | None, str | None]:
        """(the account's email, None) when Personal is on here, else (None, why not)."""
        found = self.standing()
        return found.email, found.why

    def frozen(self) -> bool:
        """The profiles are frozen: Personal has ended (as last seen; the plan isn't asked for here)."""
        known = known_plan(self.store)
        return known is not None and known["state"] == "ended"

    def catalog(self, source: str) -> tuple[Connection, tuple[Table, ...]]:
        connection = self.store.connection(source)
        if connection is None:
            raise SourceError(f"no source named {source!r}; call list_sources")
        with self.opened(connection) as opened_source:
            return connection, opened_source.tables()

    def grants_from(self, grants: Sequence[str], tables: Sequence[str], source: str | None,
                    anonymised: Sequence[str] = (), totals: Sequence[str] = ()) -> tuple[Grant, ...]:
        found = [Grant.parse(g) for g in grants] + [Grant.parse(g, ANONYMISED) for g in anonymised] + \
            [Grant.parse(g, level=TOTALS) for g in totals]
        if tables:
            if not source:
                raise ProfileError("granting a whole table lists its columns from a source: pass `source`")
            _, catalog = self.catalog(source)
            for table in tables:
                found += table_grants(table, catalog, source)
        return tuple(found)

    def profile(self, name: str) -> Profile:
        found = self.store.profiles.named(name)
        if found is None:
            names = ", ".join(p.name for p in self.store.profiles.all()) or "none yet"
            raise ProfileError(f"no profile named {name!r}; the profiles are: {names}")
        return found

    # --- writing ----------------------------------------------------------------------------------------------------
    def register_writing(self, server: MCPServer) -> None:
        self._register_creating(server)
        self._register_updating(server)

    def _register_creating(self, server: MCPServer) -> None:
        @server.tool(description=(
            "Create a profile: a named bundle of what the AI may see, for one job. `grants` name columns as "
            "schema.table.column or table.column (the AI then sees each in full); `anonymised` names columns the "
            "AI sees only as stand-ins (the same value always gets the same stand-in, so it can still filter, "
            "group and join on them; the user sees the real values in the panel); `totals` names columns the AI may "
            "only sum, average or count, with small groups left out (numbers only: on any other column it falls to "
            "hidden); `tables` grants whole tables, listing their columns now from `source`, so columns added later "
            "stay hidden. Each column gets one level. Anything no applied profile grants is hidden. Apply it to a "
            "connection with apply_profile."))
        def create_profile(name: str, description: str, grants: list[str] | None = None,
                           anonymised: list[str] | None = None, tables: list[str] | None = None,
                           source: str | None = None, totals: list[str] | None = None) -> CallToolResult:
            found = self.standing()
            email = found.email
            if email is None:
                return error(found.why or NEEDS_PERSONAL)
            try:
                made = Profile.new(name, description, email,
                                   _one_level_each(self.grants_from(grants or [], tables or [], source,
                                                                    anonymised or [], totals or [])))
                if self.store.profiles.named(made.name) is not None:
                    raise ProfileError(f"there is already a profile named {made.name!r}; update_profile changes it")
            except (ProfileError, SourceError) as err:
                return error(str(err))
            saved = self.store.profiles.save(made)
            return reply({"profile": saved.payload()},
                         f"Saved profile {saved.name!r} ({saved.signature}) with {len(saved.grants)} grants. "
                         "It isn't applied anywhere yet: apply_profile applies it to a connection.")

    def _register_updating(self, server: MCPServer) -> None:
        def confirm_update(ctx: Context, profile: str, add: list[str] | None = None,
                           anonymised: list[str] | None = None, tables: list[str] | None = None,
                           source: str | None = None, totals: list[str] | None = None) -> Elicit[Confirm] | Confirm:
            step = self.update_step(profile, add or [], tables or [], source, anonymised or [], totals or [])
            return _ask(ctx, step)

        @server.tool(description=(
            "Save a new version of a profile: rename it, change its description, add grants in full (`add`, or "
            "whole `tables` from `source`), anonymised (`anonymised`: the AI sees stand-ins) or totals only "
            "(`totals`), or remove grants (`remove`). Adding a column already granted changes its level. Each save "
            "publishes a whole new version. Letting the AI see more through a profile that's applied somewhere asks "
            "the user first."))
        def update_profile(ctx: Context, profile: str,
                           answer: Annotated[ElicitationResult[Confirm], Resolve(confirm_update)],
                           name: str | None = None, description: str | None = None, add: list[str] | None = None,
                           anonymised: list[str] | None = None, remove: list[str] | None = None,
                           tables: list[str] | None = None, source: str | None = None,
                           totals: list[str] | None = None) -> CallToolResult:
            step = self.update_step(profile, add or [], tables or [], source, anonymised or [], totals or [])
            if refused := _refused(ctx, step, answer):
                return refused
            try:
                current = self.profile(profile)
                gone = {Grant.parse(r).key for r in remove or []}
                new = _one_level_each(self.grants_from(add or [], tables or [], source, anonymised or [],
                                                       totals or []))
                grants = next_grants(current.grants, new, gone)
                changed = current.saved(str(step.found["email"]), name=name, description=description,
                                        grants=grants)
                clash = self.store.profiles.named(changed.name)
                if clash is not None and clash.id != current.id:
                    raise ProfileError(f"there is already a profile named {changed.name!r}")
            except (ProfileError, SourceError) as err:
                return error(str(err))
            saved = self.store.profiles.save(changed)
            return reply({"profile": saved.payload()}, f"Saved profile {saved.name!r}: {saved.signature}.")

    def update_step(self, profile: str, add: Sequence[str], tables: Sequence[str], source: str | None,
                    anonymised: Sequence[str] = (), totals: Sequence[str] = ()) -> Step:
        found = self.standing()
        email = found.email
        if email is None:
            return Step(refusal=found.why)
        try:
            current = self.profile(profile)
            added = shows_more(current.grants,
                               _one_level_each(self.grants_from(add, tables, source, anonymised, totals)))
        except (ProfileError, SourceError) as err:
            return Step(refusal=str(err))
        places = self.store.profiles.where_applied(current, tuple(c.name for c in self.store.connections()))
        return Step(question=more_question(current, added, places), found={"email": email})

    # --- a starting profile from block rules and notes (#92) ---------------------------------------------------------
    def start_step(self, source: str, name: str | None) -> Step:
        email, why = self.personal_email()
        if email is None:
            return Step(refusal=why)
        try:
            _, catalog = self.catalog(source)
            if self.store.profiles.on_profiles(source):
                raise ProfileError(f"{source} already runs on profiles")
            rules = Rules(self.store.rules(source))
            grants = starting_grants(rules, catalog)
            if not grants:
                raise ProfileError(f"the AI sees nothing of {source} now, so there is nothing to grant")
            started = Profile.new(name or source, f"Started from {source}'s block rules and notes", email, grants,
                                  carried_notes(self.store.notes(source), grants))
            if self.store.profiles.named(started.name) is not None:
                raise ProfileError(f"there is already a profile named {started.name!r}; pass another `name`")
        except (ProfileError, SourceError) as err:
            return Step(refusal=str(err))
        notes = len(self.store.notes(source))
        question = (f"Turn {source}'s {len(rules.rules)} block rules and {notes} notes into profile "
                    f"{started.name!r} and switch {source} to it? It grants in full exactly what the AI sees there "
                    f"now: {_listed(g.name for g in grants)}. {len(started.context.meanings)} notes on those carry over as "
                    "context; notes on anything blocked are dropped.")
        return Step(question=question, found={"profile": started, "preview": preview(grants, catalog)})

    def register_starting(self, server: MCPServer) -> None:
        def confirm_start(ctx: Context, source: str, name: str | None = None) -> Elicit[Confirm] | Confirm:
            return _ask(ctx, self.start_step(source, name))

        @server.tool(description=(
            "Turn a connection's Free block rules and notes into a starting profile and switch the connection to it: "
            "the profile grants in full, by name, every column not blocked now, and the notes on those become its "
            "context (notes on blocked tables and columns are dropped). `name` defaults to the connection's name. "
            "Asks the user first; if they decline, the connection stays on block rules and nothing is saved."))
        def start_profile_from_block_rules(ctx: Context, source: str,
                                           answer: Annotated[ElicitationResult[Confirm], Resolve(confirm_start)],
                                           name: str | None = None) -> CallToolResult:
            step = self.start_step(source, name)
            if refused := _refused(ctx, step, answer):
                return refused
            started, matched = step.found["profile"], step.found["preview"]
            saved = self.store.profiles.save(started)
            self.store.profiles.switch_to_profiles(source)
            self.store.profiles.apply(source, saved, tuple(g.name for g in matched.not_in_your_data))
            return reply({"profile": saved.payload(), "source": source, "preview": matched.payload()},
                         f"Saved profile {saved.name!r} ({saved.signature}) with {len(saved.grants)} grants and "
                         f"{len(saved.context.meanings)} meanings, and applied it to {source}; it now runs on profiles "
                         f"instead of block rules.\n{matched.text()}")

    def starting_offers(self) -> tuple[str, ...]:
        """The connections a Personal user could start a profile from: those still on block rules."""
        if self.personal_email()[0] is None:
            return ()
        return tuple(c.name for c in self.store.connections() if not self.store.profiles.on_profiles(c.name))

    # --- applying ---------------------------------------------------------------------------------------------------
    def apply_step(self, profile: str, source: str, until: str | None = None,
                   for_apps: Sequence[str] | None = None) -> Step:
        email, why = self.personal_email()
        if email is None:
            return Step(refusal=why)
        try:
            chosen = self.profile(profile)
            _, catalog = self.catalog(source)
            ends = parse_until(until, self.store.profiles.seen_now())
            apps = resolve_apps(for_apps, self.store.profiles.web_apps())
        except (ProfileError, SourceError) as err:
            return Step(refusal=str(err))
        matched = preview(chosen.grants, catalog)
        found: dict[str, object] = {"profile": chosen, "preview": matched, "catalog": catalog, "until": ends,
                                    "apps": apps}
        if matched.nothing_matches:
            return Step(refusal=f"Nothing in profile {chosen.name!r} matches {source}, so it wasn't applied.\n"
                                + matched.text(), found=found)
        on_profiles = self.store.profiles.on_profiles(source)
        others = [g for a in self.store.profiles.applied(source) if a.profile.id != chosen.id
                  for g in a.profile.grants] if on_profiles else []
        gained = _shown(Grants.of([*others, *chosen.grants]), catalog) - _shown(self.store.rules_for(source), catalog)
        rules = () if on_profiles else self.store.rules(source)
        if rules:
            return Step(question=(f"Switch {source} from block rules to profiles? Its {len(rules)} block rules stop "
                                  f"applying and the AI sees only what profiles grant: {_listed(sorted(gained))}."),
                        found=found)
        if gained and on_profiles:
            return Step(question=(f"Let the AI see {_listed(sorted(gained))} in {source} through profile "
                                  f"{chosen.name!r}?"), found=found)
        return Step(found=found)

    def register_applying(self, server: MCPServer) -> None:
        self._register_apply(server)
        self._register_unapplying(server)

    def _register_apply(self, server: MCPServer) -> None:
        def confirm_apply(ctx: Context, profile: str, source: str, until: str | None = None,
                          for_apps: list[str] | None = None) -> Elicit[Confirm] | Confirm:
            return _ask(ctx, self.apply_step(profile, source, until, for_apps))

        @server.tool(description=(
            "Apply a profile to a connection. Shows each grant as matched, hidden here instead (with why) or not in "
            "your data, with counts; refused only when nothing matches. From then on the AI sees only what the "
            "applied profiles grant on that connection, and its block rules stop applying. Several profiles on one "
            "connection add up. Switching from block rules, or showing more, asks the user first. `until` (a date, "
            "or an ISO time) ends it by itself; `for_apps` limits it to some AI apps on the web routes (names of web "
            "AI apps seen here, or OAuth client ids): it never applies on desktop. Applying again replaces both."))
        def apply_profile(ctx: Context, profile: str, source: str,
                          answer: Annotated[ElicitationResult[Confirm], Resolve(confirm_apply)],
                          until: str | None = None, for_apps: list[str] | None = None) -> CallToolResult:
            step = self.apply_step(profile, source, until, for_apps)
            if refused := _refused(ctx, step, answer):
                return refused
            chosen, matched = step.found["profile"], step.found["preview"]
            ends, apps = step.found["until"], step.found["apps"]
            switched = not self.store.profiles.on_profiles(source)
            self.store.profiles.switch_to_profiles(source)
            self.store.profiles.apply(source, chosen, tuple(g.name for g in matched.not_in_your_data),
                                      until=ends, apps=apps)
            terms = describe(Applied(chosen, until=ends, apps=apps), self.store.profiles.seen_now(),
                             self.store.profiles.web_apps())
            lead = f"Applied profile {chosen.name!r} ({chosen.signature}) to {source}" + (f", {terms}" if terms else "")
            lead += "; it now runs on profiles instead of block rules." if switched else "."
            return reply({"applied": chosen.name, "source": source, "switched": switched, "until": ends,
                          "apps": list(apps), "preview": matched.payload()}, f"{lead}\n{matched.text()}")

    def _register_unapplying(self, server: MCPServer) -> None:
        @server.tool(description=(
            "Take a profile off a connection. The connection stays on profiles: with none left, the AI sees nothing "
            "of it (use_block_rules goes back to block rules)."))
        def unapply_profile(profile: str, source: str) -> CallToolResult:
            # Taking a profile off only ever shows less, so it runs whatever the plan: after a lapse too.
            try:
                chosen = self.profile(profile)
            except ProfileError as err:
                return error(str(err))
            if not self.store.profiles.unapply(source, chosen):
                return error(f"profile {chosen.name!r} isn't applied to {source}")
            left = len(self.store.profiles.applied(source))
            then = f"{left} profiles still apply." if left else "No profile applies now, so the AI sees nothing of it."
            return reply({"unapplied": chosen.name, "source": source, "profiles_left": left},
                         f"Took profile {chosen.name!r} off {source}. {then}")

        def confirm_back(ctx: Context, source: str) -> Elicit[Confirm] | Confirm:
            return _ask(ctx, self.back_step(source))

        @server.tool(description=(
            "Switch a connection from profiles back to Free's block rules (anything not blocked is seen). Asks the "
            "user first; only call it when they ask."))
        def use_block_rules(ctx: Context, source: str,
                            answer: Annotated[ElicitationResult[Confirm], Resolve(confirm_back)]) -> CallToolResult:
            if refused := _refused(ctx, self.back_step(source), answer):
                return refused
            self.store.profiles.switch_to_block_rules(source)
            rules = len(self.store.rules(source))
            return reply({"source": source, "on": "block rules", "block_rules": rules},
                         f"{source} runs on block rules again ({rules} of them); its profiles no longer apply there.")

    def back_step(self, source: str) -> Step:
        if self.store.connection(source) is None:
            return Step(refusal=f"no source named {source!r}; call list_sources")
        if not self.store.profiles.on_profiles(source):
            return Step(refusal=f"{source} already runs on block rules")
        # Block rules show everything not blocked, so this may show more: only with Personal on now. After a lapse,
        # offline or signed out, nothing loosens (#90: "after lapse no column loosens"; nothing falls back to Free).
        email, why = self.personal_email()
        if email is None:
            return Step(refusal=FROZEN if self.frozen() else (why or NEEDS_PERSONAL))
        return Step(question=(f"Switch {source} back to block rules? The AI would then see everything in it that "
                              "isn't blocked, not just what your profiles grant."))

    # --- listing ----------------------------------------------------------------------------------------------------
    def register_list(self, server: MCPServer) -> None:
        @server.tool(description="List the user's profiles: their grants, version and where each is applied.")
        def list_profiles() -> CallToolResult:
            names = tuple(c.name for c in self.store.connections())
            listed = []
            now, seen = self.store.profiles.seen_now(), self.store.profiles.web_apps()
            terms: list[list[str]] = []
            for profile in self.store.profiles.all():
                held = self.store.profiles.applications_of(profile, names)
                listed.append({**profile.payload(),
                               "applied_to": list(self.store.profiles.where_applied(profile, names)),
                               "applications": [{"source": s, "until": a.until, "apps": list(a.apps),
                                                 "ended": a.ended(now)} for s, a in held]})
                terms.append([f"{s} {t}" for s, a in held if (t := describe(a, now, seen))])
            lines = [f"- {p['name']}: {p['description']} ({len(p['grants'])} grants, "
                     f"{len(p['context']['meanings'])} meanings, {len(p['context']['definitions'])} definitions; "
                     f"applied to "
                     f"{', '.join(p['applied_to']) or 'nothing'}; {_signature(p)})"
                     + "".join(f"\n  {t}" for t in said) for p, said in zip(listed, terms, strict=True)]
            starting = self.starting_offers()  # asks for the plan, so `frozen` below is as of now
            if starting:
                lines.append(f"Still on block rules: {', '.join(starting)}. start_profile_from_block_rules turns a "
                             "connection's block rules and notes into a starting profile, if the user wants one.")
            frozen = self.frozen() and bool(listed)
            if frozen:
                lines.append(FROZEN)
            return reply({"profiles": listed, "starting_profile_offers": list(starting), "frozen": frozen},
                         "\n".join(lines) or "No profiles yet.")


def shows_more(current: Iterable[Grant], wanted: Iterable[Grant]) -> list[Grant]:
    """The grants of `wanted` that let the AI see more than `current` does: a column not granted now, or granted
    now at a lower level. update_profile and the local page's profile editor (#126) ask about these."""
    held = {o.key: o.level for o in current}
    return [g for g in wanted if g.key not in held or LEVELS.index(g.level) < LEVELS.index(held[g.key])]


def more_question(profile: Profile, added: Sequence[Grant], places: Sequence[str]) -> str | None:
    """The question to ask before saving `added` onto `profile`, applied to `places`; None when there is none."""
    if not added or not places:
        return None
    return (f"Let the AI see {_listed(_as_seen(g) for g in added)} through profile {profile.name!r}? It's applied to "
            f"{', '.join(places)}.")


def next_grants(current: Iterable[Grant], new: Iterable[Grant], gone: Iterable[object]) -> list[Grant]:
    """A profile's grants after a change: the `gone` keys removed, `new` grants added or put at their new level
    (listed last). One rule for update_profile and the profile editor, so a change saves the same from either."""
    new = list(new)
    dropped = set(gone) | {n.key for n in new}
    return [g for g in current if g.key not in dropped] + new


def lit_up(store: LocalStore, source: str, catalog: Iterable[Table]) -> tuple[str, ...]:
    """Notices for grants that weren't in `source` when last looked and are now: they light up by themselves, and
    each is said once."""
    catalog = tuple(catalog)
    if not store.profiles.on_profiles(source):
        return ()
    said = []
    for applied in store.profiles.applied(source):
        if not applied.missing:
            continue
        now = {g.name for g in preview(applied.profile.grants, catalog).matched}
        found = [m for m in applied.missing if m in now]
        if found:
            said += [f"{name}, granted by profile {applied.profile.name!r}, is now in {source}: the AI sees it"
                     for name in found]
            store.profiles.set_missing(source, applied.profile, tuple(m for m in applied.missing if m not in now))
    return tuple(said)


def _shown(policy: object, catalog: tuple[Table, ...]) -> set[str]:
    """What the AI sees of `catalog` under `policy`, a column at a time, its level named: so showing a column in
    full where it was shown as stand-ins or totals counts as showing more."""
    level = policy.level if isinstance(policy, Grants) else (lambda table, column: "full")
    return {_seen_as(f"{t.name}.{c}", level(t, c)) for t in policy.visible(catalog)  # type: ignore[attr-defined]
            for c in t.columns}


def _seen_as(name: str, level: str | None) -> str:
    if level == ANONYMISED:
        return f"{name} (as stand-ins)"
    return f"{name} (totals only)" if level == TOTALS else name


def _as_seen(grant: Grant) -> str:
    return _seen_as(grant.name, grant.level)


def _one_level_each(grants: Iterable[Grant]) -> tuple[Grant, ...]:
    """One grant per column; a column named at two levels in one change is refused, not guessed."""
    seen: dict[object, Grant] = {}
    for grant in grants:
        held = seen.get(grant.key)
        if held is not None and held.level != grant.level:
            raise ProfileError(f"{grant.name} is named both {held.level} and {grant.level}; grant each column at "
                               "one level")
        seen.setdefault(grant.key, grant)
    return tuple(seen.values())


def _listed(names: Iterable[str]) -> str:
    names = list(names)
    return ", ".join(names) if names else "nothing new"


def _signature(payload: dict[str, object]) -> str:
    return f"made by {payload['made_by']} · last changed by {payload['changed_by']}, v{payload['version']}"


def _ask(ctx: Context, step: Step) -> Elicit[Confirm] | Confirm:
    """The resolver's answer: ask only when the step has a question and the client can ask."""
    if step.refusal or not step.question or not client_support(ctx).elicitation:
        return Confirm(confirm=False)
    return Elicit(step.question, Confirm)


def _refused(ctx: Context, step: Step, answer: object) -> CallToolResult | None:
    """The refusal for a step, or None when it may go ahead (no question, or the user said yes to it)."""
    if step.refusal:
        return error(step.refusal)
    if step.question is None:
        return None
    if not client_support(ctx).elicitation:
        return error(f"{step.question} Not done: {CANT_ASK}")
    if isinstance(answer, AcceptedElicitation) and answer.data.confirm is True:
        return None
    return error(f"{step.question} Not done: {NOT_CONFIRMED}")
