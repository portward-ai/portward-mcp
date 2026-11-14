"""The proof report (#98): the AI log's summary for a date range, as CSV and PDF (signed by signed_report, #135).

Both files are made from one `ProofReport` and carry the same facts: one line per read (time, AI app, route,
connection, profiles and versions, tables and columns at their levels, the rules applied to each column (the
profiles that allowed it at its level), the approved context the answer relied on with versions (#125), row count,
result hash), and in the PDF a
summary per AI app and profile on top, which adds up those lines. It is made from the summary alone, so it never
holds a value the AI was sent.

The PDF uses the standard Helvetica font, which only has Latin-1 letters: a name outside it is written with "?" in
the PDF (the CSV keeps it exactly).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF

from portward_free.export import export_file
from portward_free.proof import AiRead

FIELDS = ("time", "ai_app", "route", "connection", "profiles", "columns", "rules", "context", "rows", "result_hash")
LABELS = {"time": "Time (UTC)", "ai_app": "AI app", "route": "Route", "connection": "Connection", "profiles": "Profiles",
          "columns": "Tables and columns (level)", "rules": "Rules applied",
          "context": "Context relied on (version)", "rows": "Rows", "result_hash": "Result hash"}
BLOCK_RULES = "block rules (nothing read was blocked)"
NOT_RECORDED = "profile not recorded"  # an entry written before the rules applied were kept
TITLE = "What my AI saw"
MADE_FROM = ("Made by Portward from the AI log's summary on this computer. It lists what each AI app read and at what "
             "level, never the values themselves.")
# The browser's report (#107, report_build.js NOTE) says the same: only a computer holds a signing key (#135).
NOTE = MADE_FROM + " It is not signed: only a report exported on one of your computers is."


@dataclass(frozen=True)
class Group:
    """The reads of one AI app under one set of profiles."""
    app: str
    profiles: str
    reads: int
    rows: int
    columns: str


@dataclass(frozen=True)
class ProofReport:
    start: str
    end: str
    lines: tuple[dict[str, str], ...]  # one per read, keyed by FIELDS
    groups: tuple[Group, ...]

    @property
    def reads(self) -> int:
        return len(self.lines)

    @property
    def rows(self) -> int:
        return sum(int(line["rows"]) for line in self.lines)

    @property
    def headline(self) -> str:
        return counted(self.reads, self.rows)


def counted(reads: int, rows: int) -> str:
    return f"{reads} read{'' if reads == 1 else 's'}, {rows} row{'' if rows == 1 else 's'}"


def _profiles(read: AiRead) -> str:
    return ", ".join(f"{p.name} v{p.version}" for p in read.profiles) or "block rules"


def _columns(read: AiRead) -> str:
    return "; ".join(f"{c.table}.{c.column} ({c.level})" for c in read.columns) or "no columns"


def _rules(read: AiRead) -> str:
    """Per column, the profiles that allowed it at its level (report_build.js rulesOf mirrors this)."""
    if not read.profiles:
        return BLOCK_RULES
    said = [f"{c.table}.{c.column}: {c.level} " + (f"by {', '.join(c.allowed_by)}" if c.allowed_by
                                                    else f"({NOT_RECORDED})") for c in read.columns]
    return "; ".join(said) or "no columns"


def _context(read: AiRead) -> str:
    """The approved context the answer relied on, each with its kind and version (report_build.js contextOf
    mirrors this)."""
    return "; ".join(u.said for u in read.context) or "none"


def _line(read: AiRead) -> dict[str, str]:
    return {"time": read.created_at[:19].replace("T", " "), "ai_app": read.app or "unknown", "route": read.route,
            "connection": read.connection, "profiles": _profiles(read), "columns": _columns(read),
            "rules": _rules(read), "context": _context(read),
            "rows": str(read.row_count), "result_hash": read.result_hash}


def proof_report(reads: Sequence[AiRead], start: str, end: str) -> ProofReport:
    """The report for the reads from day `start` to day `end` (the caller picks them from the AI log)."""
    lines = tuple(_line(r) for r in reads)
    grouped: dict[tuple[str, str], list[AiRead]] = {}
    for read, line in zip(reads, lines, strict=True):
        grouped.setdefault((line["ai_app"], line["profiles"]), []).append(read)
    groups = []
    for (app, profiles), members in sorted(grouped.items()):
        seen = {(c.table, c.column, c.level): None for r in members for c in r.columns}
        columns = "; ".join(f"{t}.{c} ({level})" for t, c, level in seen) or "no columns"
        groups.append(Group(app, profiles, len(members), sum(r.row_count for r in members), columns))
    return ProofReport(start, end, lines, tuple(groups))


# --- the files -----------------------------------------------------------------------------------------------------
def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


class _Pdf(FPDF):
    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", size=8)
        self.cell(0, 6, f"Page {self.page_no()}", align="C")


def _write(pdf: FPDF, text: str, size: float = 10, bold: bool = False, gap: float = 1) -> None:
    pdf.set_font("Helvetica", "B" if bold else "", size)
    pdf.multi_cell(0, size * 0.5, _latin1(text), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(gap)


def write_pdf(report: ProofReport, path: Path, signed: Sequence[str] = ()) -> None:
    """`signed`: what the PDF says about its signature (signed_report.signed_note), in place of "It is not signed"."""
    pdf = _Pdf(format="A4")
    pdf.set_title(TITLE)
    pdf.set_creator("Portward")
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_page()
    _write(pdf, TITLE, 18, bold=True, gap=2)
    _write(pdf, f"{report.start} to {report.end}: {report.headline}", 12, gap=2)
    if signed:
        _write(pdf, MADE_FROM, 9, gap=1)
        for line in signed:
            _write(pdf, line, 9, gap=1)
        pdf.ln(3)
    else:
        _write(pdf, NOTE, 9, gap=4)
    _write(pdf, "By AI app and profile", 13, bold=True, gap=2)
    for group in report.groups:
        _write(pdf, f"{group.app} under {group.profiles}: {counted(group.reads, group.rows)}", 10, bold=True)
        _write(pdf, group.columns, 9, gap=3)
    if not report.groups:
        _write(pdf, "No reads in this range.", 10)
    _write(pdf, "Every read", 13, bold=True, gap=2)
    for line in report.lines:
        for field in FIELDS:
            _write(pdf, f"{LABELS[field]}: {line[field]}", 9, bold=field == "time", gap=0)
        pdf.ln(3)
    with path.open("xb") as f:
        f.write(pdf.output())


def export_report(folder: Path, report: ProofReport, signed: Sequence[str] = ()) -> tuple[Path, Path]:
    """Write the report to a new CSV file and a PDF beside it in `folder` (exports); returns both paths. `signed` as
    for write_pdf (the signature file itself is signed_report's)."""
    title = f"proof-report-{report.start}-to-{report.end}"
    csv_path = export_file(folder, title, "ai-log", "csv", FIELDS, [[line[f] for f in FIELDS] for line in report.lines])
    pdf_path = csv_path.with_suffix(".pdf")
    write_pdf(report, pdf_path, signed)
    return csv_path, pdf_path
