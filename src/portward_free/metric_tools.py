"""The metric tools (#129, Personal): the AI drafts a metric for the user to approve (approve_context, like every
context item), fetches a metric's full definition on demand (list_sources and describe_source give names and
one-liners only, C12), and asks Portward for a metric's number: Portward writes the SQL (metrics.compiled) and runs it
through the normal pull path, so the guard, levels, totals and the AI log apply.

A metric held back on a connection (it names something hidden there) is not mentioned: describe_metric and metric
answer as if it didn't exist, so the hold-back itself says nothing.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, InputRequiredResult

from portward_free.context import ContextError, Draft
from portward_free.context_served import served
from portward_free.context_tools import ContextTools, _drafted_by
from portward_free.metric_def import GRAINS, MetricError, new_metric
from portward_free.profile_tools import ProfileTools
from portward_free.profiles import Profile
from portward_free.pulls import Pulls
from portward_free.replies import error, reply
from portward_free.runtime import RESULTS_PANEL_URI, Runtime
from portward_free.sources import SourceError

METRIC_HELP = ("Metrics are the user's approved calculations (\"MRR\"): ask Portward for one with the metric tool "
               "instead of writing its SQL, so the number is the same every time. describe_metric gives a metric's "
               "full definition.")


def register_metric_drafting(server: MCPServer, profiles: ProfileTools) -> None:
    tools = ContextTools(profiles)

    @server.tool(description=(
        "Draft a metric for a profile: a named calculation the user's numbers must follow (\"MRR\"). `measure` is "
        "one SUM, AVG, COUNT, MIN or MAX over a column of `table` (SUM(amount), COUNT(DISTINCT customer_id)); "
        "`filters` are conditions it always applies (status = 'paid'); `time_column` and `grains` "
        f"({', '.join(GRAINS)}) say how it may be cut by time; `group_by` lists the columns it may be grouped by. "
        "Every column must be granted by the profile. It waits on this computer until the user approves it with "
        "approve_context; then Portward writes its SQL whenever you ask for it with the metric tool."))
    def draft_metric(ctx: Context, profile: str, name: str, measure: str, table: str,
                     filters: list[str] | None = None, time_column: str | None = None,
                     grains: list[str] | None = None, group_by: list[str] | None = None,
                     description: str | None = None) -> CallToolResult:
        def build(chosen: Profile) -> list[Draft]:
            try:
                metric = new_metric(name, measure, table, filters or [], time_column, grains or [], group_by or [],
                                    description)
            except MetricError as err:
                raise ContextError(str(err)) from None
            granted = {(g.table.lower(), g.column.lower()) for g in chosen.grants}
            if missing := [c for c in metric.columns if (metric.table.lower(), c.lower()) not in granted]:
                raise ContextError(f"profile {chosen.name!r} doesn't grant "
                                   f"{', '.join(f'{metric.table}.{c}' for c in missing)}, so the metric can't use "
                                   "it; grant it first (update_profile)")
            return [Draft.of(chosen.id, metric, _drafted_by(ctx))]
        return tools._drafted(profile, build)


def register_metric_reading(server: MCPServer, rt: Runtime) -> None:
    @server.tool(description="The full definition of one of a source's metrics (list_sources and describe_source "
                             "list their names): what it adds up, from which table, its filters, time column, grains "
                             "and the columns it may be grouped by, with its version.")
    def describe_metric(source: str, name: str) -> CallToolResult:
        connection = rt.store.connection(source)
        if connection is None:
            return error(f"no source named {source!r}; call list_sources")
        try:
            with rt.opened(connection) as opened:
                catalog = opened.tables()
        except SourceError as err:
            return error(str(err))
        found = served(rt.store, source, catalog) if rt.store.profiles.on_profiles(source) else None
        chosen = found.metric(name) if found is not None else None
        if chosen is None:
            return error(f"no metric named {name!r} on {source}; list_sources lists the metrics")
        metric, profile = chosen
        return reply({**metric.payload(), "profile": profile, "source": source},
                     f"{metric.summary}. Version {metric.version}, profile {profile!r}. Ask for it with the metric "
                     f"tool (name {metric.name!r}).")


def register_metric_runs(rt: Runtime, pulls: Pulls) -> None:
    @rt.apps.tool(resource_uri=RESULTS_PANEL_URI, name="metric",
                  description="Get one of the user's metrics (list_sources lists them): Portward writes the SQL from "
                              "the metric's definition and runs it, so the number is the same every time. `grain` "
                              "cuts it by time (day, week, month, quarter or year, as the metric allows), `group_by` "
                              "by the columns it allows, `start`/`end` (dates, end not included) limit its time "
                              "column. `question` is the user's question.")
    async def metric(source: str, name: str, question: str, ctx: Context, grain: str | None = None,
                     group_by: list[str] | None = None, start: str | None = None,
                     end: str | None = None) -> CallToolResult | InputRequiredResult:
        return await pulls.record_metric(ctx, source, name, question, grain, group_by or [], start, end)
