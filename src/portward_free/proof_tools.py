"""The tools for proof (#98): the AI log's summary for a date range, and the proof report exported from it."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from portward_free.account import (
    ACCOUNT_SETTING,
    Accounts,
    AccountServiceError,
    account_token,
    plan_of,
    register_signing_key,
)
from portward_free.export import EXPORTS_FOLDER
from portward_free.keychain import Keychain, KeychainError
from portward_free.proof import AiRead, cutoff_day
from portward_free.proof_chain import check as check_chain
from portward_free.proof_report import proof_report
from portward_free.replies import error, reply
from portward_free.runtime import Runtime
from portward_free.signed_report import PROVES, export_signed, signing_key

MAX_LISTED = 200  # entries the ai_log tool hands back at most (the report has them all)
NEEDS_PERSONAL = ("The proof report needs Portward Personal on this computer's account (signed in, on the trial or "
                  "an active plan). The AI log itself is kept either way: ai_log shows it.")
SIGN_IN_FIRST = ("The proof report is part of Portward Personal and needs this computer signed in to the user's "
                 "Portward account: call account_sign_in.")


def personal_refusal(keychain: Keychain, accounts: Accounts) -> str | None:
    """None when Personal is on for this computer's account, else why the report can't be made."""
    try:
        token = account_token(keychain)
        if not token:
            return SIGN_IN_FIRST
        plan = plan_of(accounts.service.me(token))
    except (AccountServiceError, KeychainError) as err:
        return f"Portward couldn't check this computer's plan ({err}); try again"
    return None if plan is not None and plan["personal"] else NEEDS_PERSONAL


def day_range(start: str | None, end: str | None) -> tuple[str | None, str | None] | str:
    """`start` and `end` as ISO days, or what is wrong with them."""
    for name, value in (("start", start), ("end", end)):
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError:
                return f"{name} must be a date like 2026-11-06"
            if len(value) != 10:
                return f"{name} must be a date like 2026-11-06"
    if start is not None and end is not None and start > end:
        return "start must be on or before end"
    return start, end


def _line(r: AiRead) -> str:
    profiles = ", ".join(f"{p.name} v{p.version}" for p in r.profiles) or "block rules"
    columns = ", ".join(f"{c.table}.{c.column} ({c.level})" for c in r.columns) or "no columns"
    relied_on = ", ".join(u.said for u in r.context)
    return f"- {r.created_at} {r.app or 'unknown app'} via {r.route}, {r.connection} under {profiles}: " \
           f"{columns}; {r.row_count} rows" + (f"; relied on {relied_on}" if relied_on else "")


def register_proof_tools(server: MCPServer, rt: Runtime, accounts: Accounts,
                         collect: Callable[[], int] | None = None, report_server: MCPServer | None = None) -> None:
    """`collect` brings the hosted engine's entries in first (#104), where there's an engine to collect from. The AI
    log is kept on Free too; the proof report is Personal's, registered on `report_server` (#116: plan_tools)."""
    store = rt.store
    collect = collect or (lambda: 0)

    @server.tool(description="Show the AI log's summary (for 'what did my AI see?'): every read, with when, which AI "
                             "app, the route, the profiles and versions that allowed it, the tables and columns at "
                             "the level each was shown, and how many rows. Never the values. Optional start and end "
                             "are dates (YYYY-MM-DD, both included).")
    def ai_log(start: str | None = None, end: str | None = None) -> CallToolResult:
        days = day_range(start, end)
        if isinstance(days, str):
            return error(days)
        collect()
        reads = store.reads.between(*days)
        listed = reads[-MAX_LISTED:]
        payload = {"start": days[0], "end": days[1], "reads": len(reads),
                   "rows": sum(r.row_count for r in reads), "entries": [r.as_dict() for r in listed]}
        if not reads:
            return reply(payload, "The AI log has no reads in that range.")
        text = f"{len(reads)} reads, {payload['rows']} rows in all"
        text += f" (the last {len(listed)} listed):" if len(listed) < len(reads) else ":"
        return reply(payload, "\n".join([text, *(_line(r) for r in listed)]))

    @(report_server or server).tool(description="Export the proof report, 'what my AI saw', for a date range: start and end are dates "
                             "(YYYY-MM-DD, both included). Writes a PDF and a CSV with the same facts to the user's "
                             "exports folder: per AI app and profile the tables and columns, levels and row counts, "
                             "and every read. Never the values. Signed by this computer's key, with a signature file "
                             "beside them (portward-free verify checks it). You get the files' paths and the totals.")
    def export_proof_report(start: str, end: str) -> CallToolResult:
        days = day_range(start, end)
        if isinstance(days, str):
            return error(days)
        refused = personal_refusal(rt.keychain, accounts)
        if refused is not None:
            return error(refused)
        return exported(rt, accounts, collect, start, end)


def exported(rt: Runtime, accounts: Accounts, collect: Callable[[], int], start: str, end: str) -> CallToolResult:
    """Export the signed proof report for `start` to `end` (export_proof_report, once Personal is checked)."""
    store = rt.store
    try:  # #135: registered again before signing (no change once it is), so the account can check it
        register_signing_key(rt.keychain, accounts.service, account_token(rt.keychain) or "")
        key = signing_key(rt.keychain)
    except (AccountServiceError, KeychainError) as err:
        return error(f"The report wasn't made: this computer's signing key couldn't be registered to the "
                     f"account ({err}). Try again.")
    collect()
    reads = store.reads.between(start, end)
    head, head_at = store.reads.chain_head()
    chain = check_chain(store.reads.between(), store.reads.origin(),
                        head if head_at[:10] >= cutoff_day() else "")  # an entry no longer kept isn't missing
    account = json.loads(store.setting(ACCOUNT_SETTING) or "{}").get("email", "")
    report = proof_report(reads, start, end)
    csv_path, pdf_path, signature = export_signed(store.path.parent / EXPORTS_FOLDER, report, reads, chain, key,
                                                  account)
    payload = {"start": start, "end": end, "reads": report.reads, "rows": report.rows, "pdf": str(pdf_path),
               "csv": str(csv_path), "signature": str(signature), "chain_intact": chain.intact}
    return reply(payload, f"The proof report for {start} to {end} ({report.headline}) is at {pdf_path}, and "
                          f"as CSV at {csv_path}, signed by this computer (signature file {signature}). "
                          f"{PROVES} {chain.said}")
