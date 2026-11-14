"""The context tools (#95, Personal): the AI drafts what a profile's granted tables and columns mean and proposes
named definitions; the user approves each draft, or their edit of it, in their client's own prompt; and when two
applied profiles describe one thing differently, the user picks which the AI gets.

The AI can draft but never approve: a draft is stored on this computer (`context_drafts`), used for nothing and never
shown to the AI again (context_drafts lists what each draft is about, not its text), until approve_context has asked
the user and they said yes. Approving publishes a new version of the profile, signed with the account's email, so it
needs Personal on like every profile edit. Before saving, the user is warned when the text names a table or column
the profile doesn't grant; that warning is the user's alone and never reaches the AI.

No `from __future__ import annotations` here: the `Resolve(...)` annotations must hold the resolvers themselves.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import CallToolResult
from pydantic import BaseModel, Field, create_model

from portward_free.clients import client_support
from portward_free.context import (ContextError, Draft, Meaning, links_granted, new_definition, new_link,
                                   new_meaning, ungranted)
from portward_free.context_served import clash_named, served
from portward_free.link_counts import Measurer
from portward_free.metric_def import Metric
from portward_free.profile_editor import key_getter
from portward_free.profile_tools import CANT_ASK, NOT_CONFIRMED, ProfileTools, Step
from portward_free.profiles import Profile, ProfileError, about_granted
from portward_free.replies import error, reply
from portward_free.sources import SourceError, Table
from portward_free.storage import LocalStore
from portward_free.verified_def import VerifiedAnswer

DRAFTED_BY_UNKNOWN = "an AI app"


class MeaningDraft(BaseModel):
    table: str = Field(description="The table, as table or schema.table")
    column: str | None = Field(default=None, description="The column; leave out for the table's own meaning")
    meaning: str = Field(description="What it means, in the user's business terms")
    person: str | None = Field(default=None, description=(
        "For a table's own meaning only: the column that says whose row it is (a customer or employee id), so "
        "totals count different people, not rows. Must be a column the profile grants"))
    values: dict[str, str] | None = Field(default=None, description=(
        "For a column: what its coded values mean, value -> meaning ({\"V\": \"void\"})"))
    samples: list[str] | None = Field(default=None, description=(
        "For a column: a few real values as spelled in the data (from rows you pulled), so filters match them. "
        "Only given to the AI where the column is granted in full"))
    synonyms: list[str] | None = Field(default=None, description=(
        "Other words the user uses for this table or column (\"deal\" for opportunity)"))


class Approve(BaseModel):
    approve: bool = Field(description="Approve it (with your edit, if you made one)")


class Pick(BaseModel):
    pick: str


def register_context_tools(server: MCPServer, profiles: ProfileTools) -> None:
    tools = ContextTools(profiles)
    tools.register_drafting(server)
    tools.register_approving(server)
    tools.register_settling(server)


def _drafted_by(ctx: Context) -> str:
    params = getattr(getattr(ctx, "session", None), "client_params", None)
    name = getattr(getattr(params, "client_info", None), "name", None)
    return name if isinstance(name, str) and name else DRAFTED_BY_UNKNOWN


@dataclass
class ContextTools:
    profiles: ProfileTools

    @property
    def store(self) -> LocalStore:
        return self.profiles.store

    @property
    def measurer(self) -> Measurer:
        """Measures links' match counts on save (#131)."""
        return Measurer(self.store, self.profiles.opened, key_getter(self.profiles.keychain))

    # --- drafting ---------------------------------------------------------------------------------------------------
    def register_drafting(self, server: MCPServer) -> None:
        self._register_meanings(server)
        self._register_links(server)
        self._register_draft_list(server)

    def _register_meanings(self, server: MCPServer) -> None:
        @server.tool(description=(
            "Draft what tables and columns a profile grants mean, from their schema and sample rows (pull a few "
            "first). Each draft waits on this computer for the user's approval and is used for nothing until then; "
            "then ask the user to review them with approve_context. Only tables and columns the profile grants can "
            "have a meaning."))
        def draft_meanings(ctx: Context, profile: str, meanings: list[MeaningDraft]) -> CallToolResult:
            def build(chosen: Profile) -> list[Draft]:
                drafts = []
                for item in meanings:
                    meaning = new_meaning(item.table, item.column, item.meaning, item.person, item.values,
                                          item.samples, item.synonyms)
                    for about in filter(None, (meaning, meaning.person_column)):
                        if not about_granted([about], chosen.grants):
                            raise ContextError(f"profile {chosen.name!r} doesn't grant {about.about}, so it can't "
                                               "be used there; grant it first (update_profile)")
                    drafts.append(Draft.of(chosen.id, meaning, _drafted_by(ctx)))
                return drafts
            return self._drafted(profile, build)

        @server.tool(description=(
            "Draft a named definition for a profile (\"active customer\", \"MRR\"): what the term means in the "
            "user's data, naming the tables and columns it uses. It waits on this computer for the user's approval "
            "and is used for nothing until then; ask them to review it with approve_context. Once approved, the "
            "definition is given with the schema and you must use it."))
        def draft_definition(ctx: Context, profile: str, term: str, text: str) -> CallToolResult:
            return self._drafted(profile, lambda chosen: [
                Draft.of(chosen.id, new_definition(term, text), _drafted_by(ctx))])

    def _register_links(self, server: MCPServer) -> None:
        @server.tool(description=(
            "Draft a link for a profile: `column` (table.column) holds the same values as `other_column` of another "
            "table, in the same source or a different one (a buyer email and a CRM contact email, an order's "
            "customer_id and the customers' id). Once the user approves it (approve_context), you're told to join "
            "those tables on exactly these columns. Both columns must be granted by the profile. `text` may say how "
            "they match, in the user's words. `cardinality` (one-to-one, one-to-many, many-to-one or many-to-many, "
            "read from `column` to `other_column`) is what you think it is; Portward measures it when the link is "
            "saved, and yours is used only when it can't be measured."))
        def draft_link(ctx: Context, profile: str, column: str, other_column: str,
                       text: str | None = None, cardinality: str | None = None) -> CallToolResult:
            def build(chosen: Profile) -> list[Draft]:
                link = new_link(column, other_column, text, cardinality)
                if not links_granted([link], chosen.grants):
                    raise ContextError(f"profile {chosen.name!r} must grant both {link.one.name} and "
                                       f"{link.other.name} to link them; grant them first (update_profile)")
                return [Draft.of(chosen.id, link, _drafted_by(ctx))]
            return self._drafted(profile, build)

    def _register_draft_list(self, server: MCPServer) -> None:
        @server.tool(description=(
            "The context drafts waiting for the user's approval: each draft's id, profile and what it's about (not "
            "its text, which isn't used until approved). approve_context asks the user about one."))
        def context_drafts(profile: str | None = None) -> CallToolResult:
            try:
                chosen = self.profiles.profile(profile) if profile else None
            except ProfileError as err:
                return error(str(err))
            names = {p.id: p.name for p in self.store.profiles.all()}
            listed = [{"id": d.id, "profile": names.get(d.profile_id, "?"), "kind": d.kind, "about": d.about,
                       "drafted_by": d.drafted_by}
                      for d in self.store.profiles.drafts(chosen.id if chosen else None)]
            lines = [f"- {d['id']}: {d['kind']} of {d['about']} for profile {d['profile']!r}" for d in listed]
            return reply({"drafts": listed}, "\n".join(lines) or "No drafts are waiting.")

        @server.tool(description="Throw away a context draft the user doesn't want.")
        def discard_draft(draft_id: str) -> CallToolResult:
            if not self.store.profiles.drop_draft(draft_id):
                return error(f"no draft {draft_id!r}; context_drafts lists them")
            return reply({"discarded": draft_id.strip()}, f"Draft {draft_id.strip()} thrown away.")

    def _drafted(self, profile: str, build: Any) -> CallToolResult:
        email, why = self.profiles.personal_email()
        if email is None:
            return error(why or "")
        try:
            drafts = build(self.profiles.profile(profile))
        except (ProfileError, ContextError) as err:
            return error(str(err))
        saved = [self.store.profiles.add_draft(d) for d in drafts]
        ids = [d.id for d in saved]
        return reply({"drafts": ids},
                     f"Saved {len(ids)} draft{'s' * (len(ids) != 1)} ({', '.join(ids)}). None is used until the user "
                     "approves it: call approve_context with each draft id, and the user sees it and can edit it.")

    # --- approving --------------------------------------------------------------------------------------------------
    def approve_step(self, draft_id: str) -> Step:
        email, why = self.profiles.personal_email()
        if email is None:
            return Step(refusal=why)
        draft = self.store.profiles.draft(draft_id)
        if draft is None:
            return Step(refusal=f"no draft {draft_id!r}; context_drafts lists them")
        profile = self.store.profiles.by_id(draft.profile_id)
        if profile is None:
            return Step(refusal="the profile this draft was for is gone")
        shown = (draft.item.summary if isinstance(draft.item, Metric | VerifiedAnswer) else
                 f"{draft.about}: {draft.item.shown}" if isinstance(draft.item, Meaning) else
                 f"{draft.about}: {draft.text}")
        question = (f"Approve this {draft.kind.replace('_', ' ')} for profile {profile.name!r}, drafted by "
                    f"{draft.drafted_by}?\n"
                    f"{shown}")
        person = getattr(draft.item, "person", None)
        if person:
            question += (f"\nPerson column: {person}. Totals of {draft.about} will count different people by "
                         f"{person}, not rows.")
        words = draft.item.words if isinstance(draft.item, Meaning) else draft.text
        hidden = ungranted(words, profile.grants, self._catalogs(profile))
        if hidden:
            question += (f"\n\nWarning: it names {', '.join(hidden)}, which profile {profile.name!r} doesn't grant. "
                         "If you save it as it is, the AI won't be given it; edit those names out to have it used.")
        return Step(question=question, found={"email": email, "draft": draft, "profile": profile})

    def _catalogs(self, profile: Profile) -> list[Sequence[Table]]:
        """The catalogs of the connections `profile` is applied to (those that open), to spot hidden names in."""
        names = tuple(c.name for c in self.store.connections())
        found = []
        for source in self.store.profiles.where_applied(profile, names):
            try:
                found.append(self.profiles.catalog(source)[1])
            except SourceError:
                continue
        return found

    def register_approving(self, server: MCPServer) -> None:
        def ask_approval(ctx: Context, draft_id: str) -> Elicit[Approve] | Approve:
            step = self.approve_step(draft_id)
            if step.refusal or not client_support(ctx).elicitation:
                return Approve(approve=False)
            draft: Draft = step.found["draft"]  # type: ignore[assignment]
            form = create_model("ApproveDraft", __base__=Approve,
                                text=(str, Field(default=draft.text, description="Edit the text if it's not right")))
            return Elicit(step.question or "", form)

        @server.tool(description=(
            "Ask the user to approve a context draft (see context_drafts): they see its text, can edit it, and say "
            "yes or no in their own prompt. Approved, it joins the profile as a new version and is given to the AI "
            "with the schema from then on."))
        def approve_context(ctx: Context, draft_id: str,
                            answer: Annotated[ElicitationResult[Approve], Resolve(ask_approval)]) -> CallToolResult:
            step = self.approve_step(draft_id)
            if step.refusal:
                return error(step.refusal)
            draft: Draft = step.found["draft"]  # type: ignore[assignment]
            if not client_support(ctx).elicitation:
                return error(f"Draft {draft.id} wasn't approved: {CANT_ASK}")
            if not (isinstance(answer, AcceptedElicitation) and answer.data.approve is True):
                return error(f"Draft {draft.id} wasn't approved: {NOT_CONFIRMED}")
            edit = getattr(answer.data, "text", None)
            try:
                final = draft.edited(edit) if isinstance(edit, str) and edit.strip() else draft
            except ContextError as err:
                return error(str(err))
            profile: Profile = step.found["profile"]  # type: ignore[assignment]
            changed = profile.saved(str(step.found["email"]), context=final.onto(profile.context))
            saved = self.store.profiles.save(self.measurer.measured(changed, profile))
            self.store.profiles.drop_draft(draft.id)
            edited = " with the user's edit" if final.text != draft.text else ""
            return reply({"approved": draft.id, "profile": saved.payload()},
                         f"The user approved draft {draft.id}{edited}; profile {saved.name!r} is now "
                         f"{saved.signature}.")

    # --- settling clashes -------------------------------------------------------------------------------------------
    def settle_step(self, source: str, about: str) -> Step:
        try:
            _, catalog = self.profiles.catalog(source)
        except SourceError as err:
            return Step(refusal=str(err))
        clash = clash_named(served(self.store, source, catalog), about)
        if clash is None:
            return Step(refusal=f"nothing to settle about {about!r} in {source}")
        lines = "\n".join(f"- {name}: {text}" for _, name, text in clash.candidates)
        return Step(question=(f"Profiles applied to {source} describe {clash.about} differently. Which should the AI "
                              f"use?\n{lines}"), found={"clash": clash})

    def register_settling(self, server: MCPServer) -> None:
        def ask_pick(ctx: Context, source: str, about: str) -> Elicit[Pick] | Pick:
            step = self.settle_step(source, about)
            if step.refusal or not client_support(ctx).elicitation:
                return Pick(pick="")
            names = tuple(name for _, name, _ in step.found["clash"].candidates)  # type: ignore[attr-defined]
            form = create_model("PickOne", pick=(Literal[names], Field(description="The profile whose words to use")))
            return Elicit(step.question or "", form)

        @server.tool(description=(
            "When list_sources says two profiles describe a column or term differently, ask the user which one the AI "
            "should use. The pick is remembered until either description changes."))
        def settle_context(ctx: Context, source: str, about: str,
                           answer: Annotated[ElicitationResult[Pick], Resolve(ask_pick)]) -> CallToolResult:
            step = self.settle_step(source, about)
            if step.refusal:
                return error(step.refusal)
            if not client_support(ctx).elicitation:
                return error(f"Not settled: {CANT_ASK}")
            clash = step.found["clash"]
            if not isinstance(answer, AcceptedElicitation):
                return error(f"Not settled: {NOT_CONFIRMED}")
            chosen = next((c for c in clash.candidates if c[1] == answer.data.pick), None)  # type: ignore[attr-defined]
            if chosen is None:
                return error(f"Not settled: {NOT_CONFIRMED}")
            self.store.profiles.set_pick(clash.subject, clash.fingerprint, chosen[0])  # type: ignore[attr-defined]
            return reply({"source": source, "about": clash.about, "picked": chosen[1]},  # type: ignore[attr-defined]
                         f"The user picked profile {chosen[1]!r}'s description of {clash.about}; it's used from now "
                         "on, until either description changes.")
