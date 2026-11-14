"""The assumptions review (#96, Personal): what the AI assumed, answer by answer, and one confirmation to turn an
assumption into a named definition of a profile.

The AI can ask for the definition but never approve it: define_assumption shows the user the assumption as the
definition's text, with the term, both editable, in their client's own prompt, and saves only on yes. Saving publishes
a new version of the profile signed with the account's email, like every profile edit, so it needs Personal on.

No `from __future__ import annotations` here: the `Resolve(...)` annotations must hold the resolvers themselves.
"""

from dataclasses import dataclass
from typing import Annotated

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import CallToolResult
from pydantic import BaseModel, Field, create_model

from portward_free.assumptions import Assumption
from portward_free.clients import client_support
from portward_free.context import ContextError, new_definition, ungranted
from portward_free.profile_tools import CANT_ASK, NOT_CONFIRMED, ProfileTools, Step
from portward_free.profiles import Profile, ProfileError
from portward_free.replies import error, reply
from portward_free.sources import SourceError

REVIEW_LIMIT = 50


class Define(BaseModel):
    approve: bool = Field(description="Save it as a definition (with your edits, if you made any)")


def register_assumption_tools(server: MCPServer, profiles: ProfileTools) -> None:
    tools = AssumptionTools(profiles)
    tools.register_review(server)
    tools.register_define(server)


@dataclass
class AssumptionTools:
    profiles: ProfileTools

    def register_review(self, server: MCPServer) -> None:
        @server.tool(description=(
            "What you (the AI) assumed when no definition covered a question, newest first: each assumption with "
            "the answer (pull) and question it was made for, and the definition it became, if the user turned it "
            "into one. Offer to turn an open one into a definition with define_assumption."))
        def assumptions(source: str | None = None, limit: int = REVIEW_LIMIT) -> CallToolResult:
            found = self.profiles.store.assumptions.recent(max(1, min(limit, 200)), source)
            lines = [f"- {a.id} ({a.source}, {a.created_at}): {a.text}"
                     + (f" -> defined as {a.defined_as!r}" if a.defined_as else "") for a in found]
            return reply({"assumptions": [a.payload() for a in found]},
                         "\n".join(lines) or "No assumptions recorded yet.")

    def define_step(self, assumption_id: str, term: str, profile: str | None) -> Step:
        email, why = self.profiles.personal_email()
        if email is None:
            return Step(refusal=why)
        assumption = self.profiles.store.assumptions.get(assumption_id)
        if assumption is None:
            return Step(refusal=f"no assumption {assumption_id!r}; call assumptions for the ids")
        try:
            chosen = self._profile_for(assumption, profile)
            definition = new_definition(term, assumption.text)
        except (ProfileError, ContextError) as err:
            return Step(refusal=str(err))
        question = (f"Turn what the AI assumed into a definition in profile {chosen.name!r}? From then on the AI is "
                    f"given it instead of guessing.\n{definition.term}: {definition.text}")
        hidden = ungranted(definition.text, chosen.grants, self._catalog(assumption.source))
        if hidden:
            question += (f"\n\nWarning: it names {', '.join(hidden)}, which profile {chosen.name!r} doesn't grant. "
                         "If you save it as it is, the AI won't be given it; edit those names out to have it used.")
        return Step(question=question, found={"email": email, "assumption": assumption, "profile": chosen,
                                              "definition": definition})

    def _profile_for(self, assumption: Assumption, name: str | None) -> Profile:
        if name:
            return self.profiles.profile(name)
        store = self.profiles.store
        applied = [a.profile for a in store.profiles.applied(assumption.source)] \
            if store.profiles.on_profiles(assumption.source) else []
        if len(applied) == 1:
            return applied[0]
        if not applied:
            raise ProfileError(f"no profile is applied to {assumption.source}; name the profile to define it in")
        names = ", ".join(repr(p.name) for p in applied)
        raise ProfileError(f"profiles {names} are applied to {assumption.source}; ask the user which one the "
                           "definition belongs in and pass it as `profile`")

    def _catalog(self, source: str) -> list:
        try:
            return [self.profiles.catalog(source)[1]]
        except SourceError:
            return []

    def register_define(self, server: MCPServer) -> None:
        def ask(ctx: Context, assumption_id: str, term: str, profile: str | None = None) -> Elicit[Define] | Define:
            step = self.define_step(assumption_id, term, profile)
            if step.refusal or not client_support(ctx).elicitation:
                return Define(approve=False)
            definition = step.found["definition"]
            form = create_model("DefineAssumption", __base__=Define,
                                term=(str, Field(default=definition.term, description="The term")),  # type: ignore
                                text=(str, Field(default=definition.text,  # type: ignore[attr-defined]
                                                 description="What it means; edit it if it's not right")))
            return Elicit(step.question or "", form)

        @server.tool(description=(
            "Turn one of your assumptions (see assumptions) into a named definition: the user sees it with `term`, "
            "can edit both, and says yes or no in their own prompt. Saved, it joins the profile applied to that "
            "source as a new version (name `profile` when several are applied) and you're given it from then on."))
        def define_assumption(ctx: Context, assumption_id: str, term: str,
                              answer: Annotated[ElicitationResult[Define], Resolve(ask)],
                              profile: str | None = None) -> CallToolResult:
            step = self.define_step(assumption_id, term, profile)
            if step.refusal:
                return error(step.refusal)
            if not client_support(ctx).elicitation:
                return error(f"Not defined: {CANT_ASK}")
            if not (isinstance(answer, AcceptedElicitation) and answer.data.approve is True):
                return error(f"Not defined: {NOT_CONFIRMED}")
            definition = step.found["definition"]
            try:
                final = new_definition(getattr(answer.data, "term", None) or definition.term,  # type: ignore
                                       getattr(answer.data, "text", None) or definition.text)  # type: ignore
            except ContextError as err:
                return error(str(err))
            chosen: Profile = step.found["profile"]  # type: ignore[assignment]
            assumption: Assumption = step.found["assumption"]  # type: ignore[assignment]
            saved = self.profiles.store.profiles.save(
                chosen.saved(str(step.found["email"]), context=chosen.context.with_definition(final)))
            self.profiles.store.assumptions.defined(assumption.id, final.term)
            return reply({"defined": assumption.id, "term": final.term, "profile": saved.payload()},
                         f"The user saved {final.term!r} as a definition in profile {saved.name!r}, now "
                         f"{saved.signature}.")
