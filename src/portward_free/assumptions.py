"""What the AI assumed when no definition covered a question (#96, Personal).

The AI passes its assumptions with the query that relied on them ("active customer means an order in the last 90
days"). Each is recorded with that answer (the pull) in the local file, and only there: assumptions are never synced
and never part of a profile. The user reviews them and can turn one into a named definition of a profile with one
confirmation; the assumption then records the term it became, so the review shows which gaps are closed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

MAX_ASSUMPTIONS = 10  # per answer
MAX_ASSUMPTION_CHARS = 500


@dataclass(frozen=True)
class Assumption:
    id: str
    pull_id: str
    source: str
    question: str
    text: str
    created_at: str = ""
    defined_as: str | None = None  # the term of the definition it became, once the user turned it into one

    def payload(self) -> dict[str, str | None]:
        return {"id": self.id, "pull_id": self.pull_id, "source": self.source, "question": self.question,
                "text": self.text, "created_at": self.created_at, "defined_as": self.defined_as}


def cleaned(texts: Iterable[str] | None) -> tuple[str, ...]:
    """The assumptions worth keeping, as the AI gave them: blank ones dropped, each and their number capped."""
    kept = (" ".join(str(t).split())[:MAX_ASSUMPTION_CHARS] for t in (texts or ()))
    return tuple(t for t in kept if t)[:MAX_ASSUMPTIONS]
