"""The "Why is this hidden?" page of the local Portward page (#124): every table, column and context item a
connection on profiles keeps from the AI, with the reason, and a box to ask about one. Pure functions from
`why_hidden.WhyHidden` to markup, every value escaped. `reasons` is the part the profile editor (#126) embeds; it
starts with "Needs you": context held back because a column it names is gone (#123), each with its suggested fix,
and verified answers whose result changed (#130).
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote

from portward_free.page_html import Chrome, _empty, _section, e, layout
from portward_free.why_hidden import Hidden, WhyHidden

PATH = "/hidden"
TITLE = "Why is this hidden?"
DATA = ("table", "rows", "column")


def _query(text: str) -> str:
    return quote(text, safe="")


def _item(item: Hidden) -> str:
    return (f"<li class=\"entry\"><div class=\"what\"><p class=\"question\"><span class=\"id\">{e(item.about)}"
            f"</span></p><p class=\"meta\"><span class=\"tag\">{e(item.label)}</span></p>"
            f"<p>{e(item.why)}.</p></div></li>")


def held_back(count: int) -> str:
    return f"{count} context item{'' if count == 1 else 's'} held back"


def _needs_you(found: WhyHidden) -> str:
    needed = found.needs_you
    if not needed:
        return ""
    return (f"<h3>Needs you</h3><p>{e(held_back(len(needed)))}: each names a column that's gone (renamed or dropped) "
            f"or is a verified answer whose result changed, so the AI isn't told it until you fix it.</p>"
            f"<ol class=\"ledger\">{''.join(map(_item, needed))}</ol>")


def reasons(found: WhyHidden) -> str:
    """What needs fixing, then the reasons: data first, then context."""
    data = [i for i in found.items if i.what in DATA]
    context = [i for i in found.items if i.what not in DATA and i not in found.needs_you]
    parts = [_needs_you(found)]
    for title, items, none in (("Tables and columns", data, "The AI sees every table and column here."),
                               ("Context", context, "Every approved context item reaches the AI.")):
        body = f"<ol class=\"ledger\">{''.join(map(_item, items))}</ol>" if items else _empty(none)
        parts.append(f"<h3>{e(title)}</h3>{body}")
    return "".join(parts)


def _ask(chrome: Chrome, source: str, about: str) -> str:
    return (f"<form method=\"get\" action=\"{e(chrome.web.link(PATH))}\" class=\"block\">"
            f"<input type=\"hidden\" name=\"source\" value=\"{e(source)}\">"
            f"<label for=\"about\">Ask about one table, column or context item</label><div class=\"inline\">"
            f"<input id=\"about\" name=\"about\" value=\"{e(about)}\" placeholder=\"customers.email\">"
            f"<button type=\"submit\" class=\"primary\">Why?</button></div></form>")


def _answer(found: WhyHidden, about: str) -> str:
    if not about:
        return ""
    matched = found.about(about)
    if not matched:
        return (f"<p class=\"said\" role=\"status\">{e(f'{about} is not hidden on {found.source}, or is not a '
                f'table, column or context item there.')}</p>")
    return f"<div role=\"status\"><ol class=\"ledger\">{''.join(map(_item, matched))}</ol></div>"


def _connection(chrome: Chrome, found: WhyHidden, about: str) -> str:
    if not found.on_profiles:
        body = _empty(f"{found.source} runs on block rules: what's kept from the AI is listed under Rules on the "
                      "overview.")
    else:
        applied = ", ".join(found.applied) or "none"
        body = (f"<p class=\"quiet\">Profiles applied: {e(applied)}</p>{_ask(chrome, found.source, about)}"
                f"{_answer(found, about)}{reasons(found)}")
    return _section(f"hidden-{_query(found.source)}", found.source,
                    "Only you see this page: the AI is never told why something is hidden.", body)


def page(chrome: Chrome, found: Sequence[WhyHidden], about: str = "", problem: str = "") -> str:
    intro = (f"<div class=\"intro\"><p class=\"eyebrow\">Profiles</p><h1>{e(TITLE)}</h1><p class=\"lede\">Why each "
             "table, column and context item is kept from the AI, so you can fix a profile without guessing."
             "</p></div>")
    if problem:
        intro += f"<p class=\"problem\">{e(problem)}</p>"
    body = "".join(_connection(chrome, f, about) for f in found) or _empty("No connection runs on profiles.")
    return layout(chrome, TITLE, intro + body, home=False)
