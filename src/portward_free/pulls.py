"""Running a pull and handing its rows to the AI: the one path every result goes through.

`Pulls.run` runs SQL through the guard under the source's block rules. `Pulls.presented` decides what the AI gets,
by the source's mode (modes.py, #117): in results mode the rows minus kept columns, up to modes.AI_ROWS; in pick mode
with a panel only the result's shape (the rows lent to the panel), without one rows only on the user's yes. On
profiles: the shape with a panel, the capped preview without. `Pulls.record` runs, stores the history entry and
presents.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, InputRequiredResult

from portward_free import kept, modes, results_panel, stand_ins, weekly_facts
from portward_free import verified as verified_check
from portward_free.assumptions import cleaned
from portward_free.clients import client_name, client_support
from portward_free.confirm import confirm_in_client
from portward_free.context_served import Served, ServedAnswer, ServedMetric, catalog_of, served
from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.keychain import KeychainError
from portward_free.metric_def import MetricError
from portward_free.metrics import MetricCheck, checked, compiled
from portward_free.profiles import Profile
from portward_free.proof import AiRead, ShownColumn, UsedContext, UsedProfile
from portward_free.pull import Pull, Shown, run_pull
from portward_free.replies import error, reply
from portward_free.runtime import Runtime
from portward_free.sources import SourceError, Table
from portward_free.storage import PullRecord

KEEP_ROWS_SETTING = kept.KEEP_ROWS_SETTING
SEND_KEY = "approve_send_rows"  # the prompt's key in an input_required round (pick mode without a panel)


def sql_diff(old: str, new: str) -> str:
    """A unified diff of two SQL texts, line by line, for showing the user what a tweak changed."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", lineterm="")
    return "\n".join(lines)


def no_pull(pull_id: str) -> CallToolResult:
    return error(f"no pull with id {pull_id!r}; call history for the ids")


class Pulls:
    def __init__(self, rt: Runtime, *, panel: bool = True) -> None:
        """`panel` False: this server has no results panel (the wrap server), so rows always go as the preview."""
        self.rt, self.panel = rt, panel

    def run(self, source: str, question: str, sql: str) -> Pull | CallToolResult:
        """Run the SQL through the guard on the named source, or return the error to send back."""
        return self._opened_run(source, lambda name, opened, policy, key, hidden:
                                run_pull(name, opened, question, sql, policy, key, on_hidden=hidden))

    def run_for_mode(self, source: str, question: str, sql: str, mode: str | None) -> modes.Ran | CallToolResult:
        """Run the SQL as the source's mode wants: in results mode also for the user's own panel (modes.py)."""
        if mode != modes.RESULTS:
            ran = self.run(source, question, sql)
            return ran if isinstance(ran, CallToolResult) else modes.Ran(ran)
        return self._opened_run(source, lambda name, opened, policy, key, hidden:
                                modes.run_results(name, opened, question, sql, policy, key, hidden))

    def _opened_run(self, source: str, run: Any) -> Any:
        store = self.rt.store
        connection = store.connection(source)
        if connection is None:
            return error(f"no source named {source!r}; call list_sources")
        try:
            with self.rt.opened(connection) as opened:
                return run(source, opened, store.rules_for(source), lambda: stand_ins.key_or_none(self.rt.keychain),
                           lambda: weekly_facts.count_hidden_ask(store))
        except (QueryRejected, SourceError) as err:
            return error(str(err))

    def mode_of(self, source: str) -> str | None:
        """The source's mode (modes.py), or None on profiles, where the grants decide and nothing changes."""
        store = self.rt.store
        return None if store.profiles.on_profiles(source) else store.send_mode(source)

    def stricter_mode(self, *sources: str) -> str | None:
        """The mode for a result made from several sources' pulls: pick if any picks."""
        found = {self.mode_of(s) for s in sources}
        return modes.PICK if modes.PICK in found else modes.RESULTS if modes.RESULTS in found else None

    def has_panel(self, ctx: Context) -> bool:
        return self.panel and client_support(ctx).panel

    async def approval(self, ctx: Context, pull: Pull, mode: str | None) -> bool | None | InputRequiredResult:
        """In pick mode without a panel the user says yes in the client's prompt before rows go to the AI: their
        answer, or None when the client can't ask (then the AI gets the shape only). True when no yes is needed."""
        if mode != modes.PICK or self.has_panel(ctx):
            return True
        if not client_support(ctx).elicitation:
            return None
        return await confirm_in_client(ctx, modes.send_question(pull, pull.source), SEND_KEY, modes.send_state(pull))

    def rules_changed(self, pull_id: str) -> CallToolResult | None:
        """An error when the rows held for `pull_id` were pulled under other block rules than the source has now
        (or aren't held any more): they may hold what is now blocked, so they aren't shown or posted."""
        held = self.rt.outbox.get(pull_id)
        if held is not None and self.rt.store.rules_for(held.pull.source) == held.pull.rules:
            return None
        if held is None:  # the panel's rows, maybe held by another server process on this store
            try:
                lent = self.rt.panel_pulls.get(pull_id)
            except results_panel.SelectionError as err:  # not held anywhere: the panel's own message
                return error(str(err))
            if self.rt.store.rules_for(lent.source) == lent.rules:
                return None
        return error(f"the user's block rules changed since pull {pull_id} (or it is no longer held); run the "
                     "query again to see its rows")

    def presented(self, ctx: Context, pull_id: str, pull: Pull, *, mode: str | None = None,
                  ran: modes.Ran | None = None, approved: bool | None = True) -> tuple[str, dict[str, Any]]:
        """What the AI gets for a pull's rows. Every tool that hands the AI result rows goes through here.
        - results mode (modes.py): the first AI_ROWS rows minus kept columns, the rest held for more_rows; with a
          panel, the user's own view and a receipt are lent to it.
        - pick mode without a panel: the first AI_ROWS rows only when `approved`; else the shape only.
        - otherwise (pick with a panel, and profiles): with a panel, the result's shape only, the rows lent to the
          panel (panel_rows); without, the capped preview, the rest held for more_rows (which asks the user)."""
        in_panel = self.has_panel(ctx)
        if mode == modes.RESULTS:
            ran = ran if ran is not None else modes.Ran(pull)
            receipt = modes.receipt_of(ran)
            if ran.ai is not None:
                self.rt.outbox.hold(pull_id, ran.ai, sent=receipt.sent_rows)
            if in_panel:
                self.rt.panel_pulls.hold(pull_id, ran.shown, receipt)
            return modes.for_ai(ran, pull_id, in_panel)
        if mode == modes.PICK and not in_panel:
            if not approved:
                why = ("the user didn't say yes to sending it" if approved is False
                       else "this app can't ask the user before sending rows")
                return modes.shape_only(pull_id, pull, pull.source, why)
            self.rt.outbox.hold(pull_id, pull, sent=min(modes.AI_ROWS, len(pull.rows)))
            return modes.for_ai(modes.Ran(pull), pull_id, False)
        self.rt.outbox.hold(pull_id, pull)
        if in_panel:
            return results_panel.for_ai(self.rt.panel_pulls.hold(pull_id, pull))
        text, payload = pull.for_ai()
        unsent = len(pull.rows) - len(pull.preview)
        if unsent:
            text += (f"\n\nThe other {unsent} rows have not been sent. If the preview isn't enough, call more_rows "
                     f"with pull_id {pull_id!r}; the user is asked before any more are sent.")
        return text, {**payload, "pull_id": pull_id}

    def _sealed_rows(self, pull_id: str, pull: Pull) -> tuple[bytes | None, str]:
        """The pull's rows sealed for keeping, or None and a note for the AI on why they couldn't be kept."""
        try:
            secret = kept.key(self.rt.keychain, create=True)
        except (KeychainError, kept.RowsLocked) as err:
            return None, f"\n\nThe rows weren't kept: {err}."
        assert secret is not None  # create=True always gives a key
        return kept.seal(secret, pull_id, pull.columns, pull.rows, pull.truncated), ""

    def _stored(self, pull: Pull, question: str, sql: str, source: str,
                tweak_of: PullRecord | None, seal: bool = True) -> tuple[PullRecord, str]:
        store = self.rt.store
        entry = PullRecord(question=question, sql=sql, source=source, tables=pull.tables,
                           columns=pull.columns_read, row_count=len(pull.rows), result_hash=pull.result_hash,
                           parent_id=tweak_of.id if tweak_of is not None else None)
        keeping = seal and store.setting(KEEP_ROWS_SETTING) == "on"
        sealed, not_kept = self._sealed_rows(entry.id, pull) if keeping else (None, "")
        return store.record_pull(replace(entry, keep_rows=sealed is not None), sealed), not_kept

    async def record(self, ctx: Context, source: str, question: str, sql: str, rerun_of: PullRecord | None = None,
                     tweak_of: PullRecord | None = None, assumptions: list[str] | None = None,
                     metric: ServedMetric | None = None, verified: ServedAnswer | None = None
                     ) -> CallToolResult | InputRequiredResult:
        """`metric`: the metric (and its profile) Portward wrote `sql` from (#129); without one (or a verified
        answer), the SQL is checked against the metrics whose table it reads. `verified`: the verified answer `sql`
        is (#130); its result is compared with the verified one."""
        mode = self.mode_of(source)
        ran = self.run_for_mode(source, question, sql, mode)
        if isinstance(ran, CallToolResult):
            return ran
        pull = ran.recorded(source, question, sql)
        approved = await self.approval(ctx, pull, mode) if ran.ai is not None else True
        if isinstance(approved, InputRequiredResult):
            return approved
        self.rt.prune()
        entry, not_kept = self._stored(pull, question, sql, source, tweak_of, seal=ran.ai is not None)
        found, dialect = self._served(source)
        relied_on, links = self._relied(found, pull)
        checks = () if metric is not None or verified is not None or found is None else \
            metric_checks(found, pull, dialect)
        matched = None if verified is None else verified_check.remember(
            self.rt.store, source, verified.profile_id, verified.answer, entry.result_hash)
        used = used_items(metric, checks, verified, matched)
        if ran.ai is not None:
            log_read(self.rt, client_name(ctx.session), ran.ai, [*map(UsedItem.of, relied_on), *used])
        connection = self.rt.store.connection(source)
        self.rt.usage.pulled(connection.kind if connection is not None else "other")
        text, payload = self.presented(ctx, entry.id, pull, mode=mode, ran=ran, approved=approved)
        text += not_kept
        notes = (self._assumed(entry.id, source, question, assumptions), rerun(entry, rerun_of),
                 tweaked(sql, tweak_of), definitions(relied_on), approved_links(links), from_metric(metric, sql),
                 from_verified(verified, matched), checked_against(checks, found))
        for patch, more in notes:
            payload, text = {**payload, **patch}, text + more
        return reply(payload, text)

    def _assumed(self, pull_id: str, source: str, question: str, assumptions: list[str] | None) -> Note:
        assumed = self.rt.store.assumptions.record(pull_id, source, question, cleaned(assumptions))
        if not assumed:
            return NOTHING
        return {"assumptions": [a.id for a in assumed]}, (
            f"\n\n{len(assumed)} assumption{'s' * (len(assumed) != 1)} recorded with this answer, for the user to "
            "review (assumptions) and turn into a definition (define_assumption).")

    def _served(self, source: str) -> tuple[Served | None, str]:
        """The context served on `source` and its SQL dialect; None on block rules or when it won't open."""
        store = self.rt.store
        connection = store.connection(source)
        if connection is None or not store.profiles.on_profiles(source):
            return None, ""
        try:
            with self.rt.opened(connection) as opened:
                catalog, dialect = opened.tables(), opened.dialect
        except SourceError:
            return None, ""
        return served(store, source, catalog, catalog_of(self.rt)), dialect

    @staticmethod
    def _relied(found: Served | None, pull: Pull) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if found is None:
            return [], []
        return (found.relied_on(pull.tables, pull.columns_read, pull.columns, pull.question),
                found.links_for(pull.tables))

    def relied_on(self, source: str, pull: Pull) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The approved context (#95, #96) of the profiles applied to `source` that `pull` relies on, and the links
        touching the tables it read; none on block rules."""
        return self._relied(self._served(source)[0], pull)

    async def record_metric(self, ctx: Context, source: str, name: str, question: str, grain: str | None,
                            group_by: Sequence[str], start: str | None, end: str | None
                            ) -> CallToolResult | InputRequiredResult:
        """Write the SQL for one metric question and run it as a pull (#129)."""
        store = self.rt.store
        connection = store.connection(source)
        if connection is None:
            return error(f"no source named {source!r}; call list_sources")
        try:
            with self.rt.opened(connection) as opened:
                catalog, dialect = opened.tables(), opened.dialect
        except SourceError as err:
            return error(str(err))
        found = served(store, source, catalog) if store.profiles.on_profiles(source) else None
        chosen = found.metric(name) if found is not None else None
        if found is None or chosen is None:
            names = ", ".join(m.name for m, _ in found.metrics) if found is not None else ""
            return error(f"no metric named {name!r} on {source}" + (f"; its metrics: {names}" if names else ""))
        visible = store.rules_for(source).visible(catalog)
        table = next(t for t in visible if t.name.lower() == chosen[0].table.lower())
        try:
            sql = compiled(chosen[0], table, dialect, grain, group_by, start, end)
        except MetricError as err:
            return error(str(err))
        return await self.record(ctx, source, question, sql, metric=chosen)


    async def record_verified(self, ctx: Context, source: str, question: str
                              ) -> CallToolResult | InputRequiredResult:
        """Run one verified answer exactly as written, as a pull (#130)."""
        found, _ = self._served(source)
        chosen = found.answer(question) if found is not None else None
        if chosen is None:
            listed = ", ".join(repr(v.answer.question) for v in found.verified) if found is not None else ""
            return error(f"no verified answer to {question!r} on {source}"
                         + (f"; its verified answers: {listed}" if listed else "; list_sources lists them"))
        return await self.record(ctx, source, chosen.answer.question, chosen.answer.sql, verified=chosen)


def metric_checks(found: Served, pull: Pull, dialect: str) -> tuple[MetricCheck, ...]:
    """How the AI's SQL measures up to each metric served on its source whose table it reads (#129)."""
    return tuple(c for metric, profile in found.metrics
                 if (c := checked(pull.sql, dialect, metric, profile, pull.question)) is not None)


Note = tuple[dict[str, Any], str]  # what one part of a pull's answer adds: to the payload, and to the text
NOTHING: Note = ({}, "")


@dataclass(frozen=True)
class UsedItem:
    """One piece of the user's approved context an answer relied on, for the AI log (proof.UsedContext): a
    definition, link, metric or verified answer, by name, profile and version, and whether it was followed."""
    kind: str
    about: str
    profile: str
    version: int
    followed: bool | None = None

    @classmethod
    def of(cls, found: dict[str, Any]) -> UsedItem:
        """From one of Served.relied_on's dicts."""
        return cls(found["kind"], found["about"], found["profile"], found["version"], found.get("followed"))

    def logged(self) -> UsedContext:
        return UsedContext(self.kind, self.about, self.profile, self.version, self.followed)


def used_items(metric: ServedMetric | None, checks: Sequence[MetricCheck], verified: ServedAnswer | None,
               matched: bool | None) -> list[UsedItem]:
    """The metrics and verified answer a pull used (#129, #130), for the AI log."""
    found = [UsedItem("metric", metric.metric.name, metric.profile, metric.metric.version, True)] \
        if metric is not None else [UsedItem("metric", c.metric, c.profile, c.version, c.followed) for c in checks]
    if verified is not None:
        found.append(UsedItem(verified_check.VERIFIED_KIND, verified.answer.question, verified.profile,
                              verified.answer.version, matched))
    return found


def rerun(entry: PullRecord, rerun_of: PullRecord | None) -> Note:
    if rerun_of is None:
        return NOTHING
    same = entry.result_hash == rerun_of.result_hash
    lead = "\n\nSame result as the pull of " if same else "\n\nThe result differs from the pull of "
    return {"rerun_of": rerun_of.id, "same_result": same}, f"{lead}{rerun_of.created_at}."


def tweaked(sql: str, tweak_of: PullRecord | None) -> Note:
    if tweak_of is None:
        return NOTHING
    diff = sql_diff(tweak_of.sql, sql)
    text = f"\n\nWhat changed in the query:\n```diff\n{diff}\n```" if diff else "\n\nThe query is unchanged."
    return {"tweak_of": tweak_of.id, "sql_diff": diff}, text


def definitions(relied_on: Sequence[dict[str, Any]]) -> Note:
    if not relied_on:
        return NOTHING
    return {"definitions": list(relied_on)}, (
        "\n\nThe user's approved definitions this result relies on (use them, and cite the ones your answer relies "
        "on by name and version):\n"
        + "\n".join(f"- {d['about']} ({d['profile']} v{d['version']}): {d['text']}" for d in relied_on))


def approved_links(links: Sequence[dict[str, Any]]) -> Note:
    if not links:
        return NOTHING
    return {"links": list(links)}, ("\n\nThe user's approved links for these tables (join on these columns, not on "
                                    "lookalikes):\n" + "\n".join(f"- {k['join']}" for k in links))


def from_metric(metric: ServedMetric | None, sql: str) -> Note:
    if metric is None:
        return NOTHING
    chosen, profile = metric
    return ({"metric": {"name": chosen.name, "version": chosen.version, "profile": profile, "sql": sql}},
            f"\n\nPortward wrote this SQL from metric {chosen.name} (v{chosen.version}, profile {profile!r}); cite it "
            f"by name and version:\n{sql}")


def from_verified(verified: ServedAnswer | None, matched: bool | None) -> Note:
    if verified is None:
        return NOTHING
    answer = verified.answer
    payload = {"verified_answer": {"question": answer.question, "version": answer.version,
                                   "profile": verified.profile, "checked_at": answer.checked_at,
                                   "same_result": matched}}
    if matched:
        return payload, (f"\n\nThis is the user's verified answer {answer.question!r} (v{answer.version}, profile "
                         f"{verified.profile!r}), run exactly as verified; cite it by question and version.")
    return payload, (f"\n\nThis result differs from the one the user verified for {answer.question!r} on "
                     f"{answer.checked_at[:10]}, so it isn't a verified answer any more: don't present it as "
                     "verified. The user is asked to check it on their computer.")


def checked_against(checks: Sequence[MetricCheck], found: Served | None) -> Note:
    if not checks:
        return NOTHING
    measures = {m.name: m.measure for m, _ in found.metrics} if found is not None else {}
    return ({"metric_checks": [c.payload() for c in checks]},
            "\n\n" + "\n".join(c.notice(measures.get(c.metric, "")) for c in checks))


def log_read(rt: Runtime, app: str | None, pull: Pull, relied_on: Sequence[UsedItem] = ()) -> AiRead:
    """Write one read to the AI log's summary (see proof.py): who read what, at what level, through which
    profiles, and the approved context handed to the AI with it (`relied_on`) with versions; never the rows or the
    context's text. Every tool that hands the AI rows from a fresh read calls this."""
    store = rt.store
    applied = tuple(a.profile for a in store.profiles.applied(pull.source)) \
        if store.profiles.on_profiles(pull.source) else ()
    return store.reads.record(AiRead(
        app=app, route=rt.route, connection=pull.source,
        profiles=tuple(UsedProfile(p.name, p.version) for p in applied),
        columns=tuple(ShownColumn(s.table, s.column, s.level, allowed_by(applied, s)) for s in pull.shown),
        row_count=len(pull.rows), result_hash=pull.result_hash,
        context=tuple(u.logged() for u in relied_on) + row_rules_used(applied, pull)))


def row_rules_used(applied: Sequence[Profile], pull: Pull) -> tuple[UsedContext, ...]:
    """The row rules (#132) of the applied profiles on the tables a read went through: they decided which rows and
    cells it could see; and their standing filters (#133), which every read of the table keeps to."""
    read = set(pull.tables)
    return tuple(UsedContext("standing_filter" if rule.standing else "row_rule", f"{rule.table}: {rule.text}",
                             p.name, p.version)
                 for p in applied for rule in p.rows if rule.table.lower() in read)


def allowed_by(applied: Sequence[Profile], shown: Shown) -> tuple[str, ...]:
    """The rules applied to one column: the applied profiles that grant it at the level it was shown at (one that
    grants it lower didn't allow what the AI saw)."""
    table = Table(shown.table, (shown.column,), schema=shown.schema)
    return tuple(p.name for p in applied if Grants.of(p.grants).level(table, shown.column) == shown.level)
