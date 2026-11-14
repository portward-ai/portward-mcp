"""The verified answer tools (#130, Personal): the user promotes a saved pull to a verified answer of a profile
(approved with approve_context, like every context item); the AI fetches one's full item on demand (list_sources and
describe_source give the questions only, C12) and runs one exactly as written through the normal pull path, so the
guard, levels, totals and the AI log apply.

A verified answer held back on a connection (verified.py) is not mentioned: describe_verified_answer and
run_verified_answer answer as if it didn't exist, so the hold-back itself says nothing.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free.context import ContextError, Draft
from portward_free.context_served import served
from portward_free.context_tools import ContextTools, _drafted_by
from portward_free.profile_tools import ProfileTools
from portward_free.profiles import Profile
from portward_free.proof import now
from portward_free.pull import Pull
from portward_free.pulls import Pulls
from portward_free.replies import error, reply
from portward_free.runtime import RESULTS_PANEL_URI, Runtime
from portward_free.sources import SourceError
from portward_free.verified import hidden_names
from portward_free.verified_def import VerifiedError, new_verified

VERIFIED_HELP = ("Verified answers are questions the user has checked the answer to: when the user asks one of them, "
                 "run it with run_verified_answer instead of writing SQL.")


def register_verified_promoting(server: MCPServer, rt: Runtime, profiles: ProfileTools, pulls: Pulls) -> None:
    tools = ContextTools(profiles)

    @server.tool(description=(
        "Promote one of the user's saved pulls (saved_pulls) to a verified answer of a profile applied to its "
        "source: its question, its SQL exactly as saved, and the result it gives now, which Portward re-checks on "
        "every connect. `question` rewords the saved pull's question. It waits on this computer until the user "
        "approves it with approve_context; then it is offered to you to run as written."))
    def promote_saved_pull(ctx: Context, name: str, profile: str, question: str | None = None) -> CallToolResult:
        saved = rt.store.saved_pull(str(name or "").strip())
        if saved is None:
            return error(f"no saved pull named {name!r}; call saved_pulls for the names")

        def build(chosen: Profile) -> list[Draft]:
            source = saved.source
            if chosen.id not in {a.profile.id for a in rt.store.profiles.applied(source)}:
                raise ContextError(f"profile {chosen.name!r} isn't applied to {source}; apply it there first "
                                   "(apply_profile), so the answer is checked under its grants")
            connection = rt.store.connection(source)
            if connection is None:
                raise ContextError(f"no source named {source!r}")
            try:
                with rt.opened(connection) as opened:
                    catalog, dialect = opened.tables(), opened.dialect
            except SourceError as err:
                raise ContextError(str(err)) from None
            pulled = pulls.run(source, saved.question, saved.sql)
            if not isinstance(pulled, Pull):
                raise ContextError("the saved pull doesn't run now, so it can't be verified: "
                                   + (pulled.content[0].text if pulled.content else "it failed"))  # type: ignore
            try:
                answer = new_verified(question or saved.question, saved.sql, source, dialect, pulled.result_hash,
                                      len(pulled.rows), now())
            except VerifiedError as err:
                raise ContextError(str(err)) from None
            visible = rt.store.rules_for(source).visible(catalog)
            if names := hidden_names(answer, chosen.grants, visible, catalog):
                raise ContextError(f"it names {', '.join(names)}, which profile {chosen.name!r} doesn't let the AI "
                                   "see here, so it can't be a verified answer of it")
            return [Draft.of(chosen.id, answer, _drafted_by(ctx))]
        return tools._drafted(profile, build)


def register_verified_reading(server: MCPServer, rt: Runtime) -> None:
    @server.tool(description="The full item of one of a source's verified answers (list_sources and describe_source "
                             "list their questions): its SQL, when the user checked it and how many rows it gave, "
                             "with its version.")
    def describe_verified_answer(source: str, question: str) -> CallToolResult:
        connection = rt.store.connection(source)
        if connection is None:
            return error(f"no source named {source!r}; call list_sources")
        try:
            with rt.opened(connection) as opened:
                catalog = opened.tables()
        except SourceError as err:
            return error(str(err))
        found = served(rt.store, source, catalog).answer(question) if rt.store.profiles.on_profiles(source) else None
        if found is None:
            return error(f"no verified answer to {question!r} on {source}; list_sources lists them")
        answer = found.answer
        return reply({**answer.payload(), "profile": found.profile},
                     f"{answer.summary}\nVersion {answer.version}, profile {found.profile!r}. Run it with "
                     "run_verified_answer.")


def register_verified_runs(rt: Runtime, pulls: Pulls) -> None:
    @rt.apps.tool(resource_uri=RESULTS_PANEL_URI, name="run_verified_answer",
                  description="Run one of the user's verified answers (list_sources lists their questions) exactly "
                              "as the user verified it. `question` is the verified question as listed.")
    async def run_verified_answer(source: str, question: str, ctx: Context) -> CallToolResult | InputRequiredResult:
        return await pulls.record_verified(ctx, source, question)
