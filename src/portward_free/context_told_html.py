"""The context editor's read view (#127, design "Personal: context editor B"): everything the AI is told on each
connection the profile is applied to, read as one document, table by table, with definitions and links in a glossary
beside it. It shows what is saved and served now (context_served), from every profile applied there, so it says
exactly what the AI gets; context held back (a column it names is gone, or it names something hidden) is listed
first, each with why and the fix (why_hidden).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from portward_free.context import Clash
from portward_free.page_html import _empty, e
from portward_free.profiles import ANONYMISED, FULL, TOTALS, Profile
from portward_free.why_hidden import Hidden, Kind

NONE_SERVED = "<p class=\"small quiet\">None served.</p>"
LEVELS = {FULL: "Full", ANONYMISED: "Anonymised", TOTALS: "Totals only"}


@dataclass(frozen=True)
class ToldTable:
    name: str
    note: str | None
    columns: tuple[tuple[str, str, str | None, tuple[str, int] | None], ...]  # (column, level, meaning, origin)
    origin: tuple[str, int] | None = None


@dataclass(frozen=True)
class Told:
    source: str
    tables: tuple[ToldTable, ...] = ()
    definitions: tuple[dict[str, Any], ...] = ()
    links: tuple[dict[str, Any], ...] = ()
    clashes: tuple[Clash, ...] = ()
    held: tuple[Hidden, ...] = ()
    problem: str = ""


def _origin(origin: tuple[str, int] | None) -> str:
    return f"<span class=\"from\">{e(origin[0])} v{origin[1]}</span>" if origin else ""


def _held(found: Told) -> str:
    if not found.held:
        return ""
    gone = [i for i in found.held if i.kind is Kind.COLUMN_GONE]
    other = [i for i in found.held if i.kind is not Kind.COLUMN_GONE]
    rows = "".join(f"<li><p><code class=\"id\">{e(i.about)}</code> <span class=\"tag err\">{e(i.label)}</span></p>"
                   f"<p class=\"small\">{e(i.why)}.</p></li>" for i in (*gone, *other))
    lead = (f"{len(gone)} item{'s' * (len(gone) != 1)} name{'s' * (len(gone) == 1)} a column that's gone, so the AI "
            "isn't told until you reword it. " if gone else "")
    return (f"<div class=\"held card\"><h3>Held back from the AI</h3><p class=\"small\">{e(lead)}Each says why and "
            f"what would fix it.</p><ul class=\"held-list\">{rows}</ul></div>")


def _table(index: int, source_index: int, table: ToldTable) -> str:
    head = f"t-{source_index}-{index}"
    note = (f"<p class=\"tmean\">{e(table.note)} {_origin(table.origin)}</p>" if table.note else
            "<p class=\"tmean none\">No meaning for the table yet.</p>")
    rows = []
    for column, level, meaning, origin in table.columns:
        if not level:
            continue
        text = (f"<p>{e(meaning)} {_origin(origin)}</p>" if meaning else "<p class=\"none\">No meaning yet.</p>")
        rows.append(f"<div class=\"entry-row\"><div class=\"k\"><code class=\"id\"><b>{e(column)}</b></code>"
                    f"<span class=\"tag\">{e(LEVELS.get(level, level))}</span></div><div class=\"v\">{text}</div></div>")
    return (f"<section class=\"tsec\" aria-labelledby=\"{head}\"><h3 id=\"{head}\" class=\"id\">{e(table.name)}</h3>"
            f"{note}<div class=\"entries\">{''.join(rows)}</div></section>")


def _glossary(found: Told) -> str:
    defs = "".join(f"<dt>{e(d['term'])}</dt><dd>{e(d['text'])} {_origin((d['profile'], d['version']))}</dd>"
                   for d in found.definitions)
    links = "".join(f"<li><code class=\"id\">{e(k['columns'][0])}</code> = <code class=\"id\">{e(k['columns'][1])}"
                    f"</code>{' in ' + e(k['source']) if k.get('source') else ''}<p class=\"small\">{e(k['text'])}</p>"
                    "</li>" for k in found.links)
    clashes = "".join(
        f"<li><code class=\"id\">{e(c.about)}</code><ul>" + "".join(
            f"<li>{e(text)} <span class=\"from\">{e(name)}</span></li>" for _, name, text in c.candidates)
        + "</ul></li>" for c in found.clashes)
    parts = [f"<div class=\"sec\"><h3>Definitions</h3>{f'<dl>{defs}</dl>' if defs else NONE_SERVED}</div>",
             f"<div class=\"sec\"><h3>Links</h3>{f'<ul class=\"plain\">{links}</ul>' if links else NONE_SERVED}</div>"]
    if clashes:
        parts.append("<div class=\"sec\"><h3>Pick one meaning</h3><p class=\"small quiet\">Two profiles describe these "
                     "differently, so the AI gets neither until you pick (ask your AI to settle_context)."
                     f"</p><ul class=\"plain\">{clashes}</ul></div>")
    return f"<aside class=\"gloss\" aria-label=\"Definitions and links on {e(found.source)}\">{''.join(parts)}</aside>"


def _source(index: int, found: Told) -> str:
    head = f"told-{index}"
    title = f"<h2 id=\"{head}\">On <span class=\"id\">{e(found.source)}</span></h2>"
    if found.problem:
        return (f"<section class=\"told\" aria-labelledby=\"{head}\">{title}<p class=\"problem\">Can't show what the "
                f"AI is told here: {e(found.problem)}.</p></section>")
    tables = "".join(_table(i, index, t) for i, t in enumerate(found.tables)) or _empty(
        "The AI sees no tables here, so it is told nothing about them.")
    return (f"<section class=\"told\" aria-labelledby=\"{head}\">{title}<p class=\"small quiet\">Exactly what the AI "
            f"is given with the schema here, from every profile applied, as saved.</p>{_held(found)}"
            f"<div class=\"doc-grid\"><div class=\"doc\">{tables}</div>{_glossary(found)}</div></section>")


def told(found: Sequence[Told], profile: Profile) -> str:
    if not found:
        return _empty(f"{profile.name} isn't applied to any connection, so the AI is told none of its context. "
                      "Apply it to a connection to see what the AI gets there.")
    return "".join(_source(i, f) for i, f in enumerate(found))
