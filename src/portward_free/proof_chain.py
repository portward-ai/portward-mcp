"""Checking the AI log's chain (#135) before a proof report is signed.

Each computer chains the entries it writes (proof.AiRead.prev/hash, storage/read_store.record). Per computer, the
chained entries here should form one unbroken line: every entry's hash matches what it says, each links to the one
before, exactly one has nothing before it here (its computer's first, or the oldest left after a year's pruning),
and no two link to the same entry. On this computer, its own latest entry must still be here too. What is found
goes into the report as it is, broken or not: the signature then proves the report says what this computer found,
not that the log is whole.

Entries synced from another computer are checked the same way; one missing there can also mean it hasn't synced yet.
Entries written before chaining are counted, not checked."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from portward_free.proof import AiRead


@dataclass(frozen=True)
class ChainCheck:
    linked: int  # chained entries checked
    computers: int  # computers (and hosted engines) they came from
    unlinked: int  # entries written before chaining
    problems: tuple[str, ...]

    @property
    def intact(self) -> bool:
        return not self.problems

    @property
    def said(self) -> str:
        """One paragraph for the report."""
        counted = (f"{self.linked} entr{'y' if self.linked == 1 else 'ies'} from {self.computers} "
                   f"computer{'' if self.computers == 1 else 's'}")
        text = (f"AI log chain: {counted}, linked with no gap." if self.intact
                else f"AI log chain: broken. {counted} checked: " + " ".join(self.problems))
        if self.unlinked:
            text += (f" {self.unlinked} entr{'y was' if self.unlinked == 1 else 'ies were'} written before entries "
                     "were chained and can't be checked.")
        return text

    def as_dict(self) -> dict[str, object]:
        return {"intact": self.intact, "linked": self.linked, "computers": self.computers,
                "unlinked": self.unlinked, "problems": list(self.problems)}


def _problems(origin: str, entries: Sequence[AiRead], head: str | None) -> list[str]:
    name = "this computer" if head is not None else f"computer {origin[:8]}"
    found: list[str] = []
    changed = [e for e in entries if e.entry_hash() != e.hash]
    if changed:
        found.append(f"{len(changed)} entr{'y' if len(changed) == 1 else 'ies'} from {name} changed after "
                     f"{'it was' if len(changed) == 1 else 'they were'} written "
                     f"(first at {changed[0].created_at[:19].replace('T', ' ')}).")
    hashes = {e.hash for e in entries}
    starts = [e for e in entries if e.prev not in hashes]
    if len(starts) > 1:
        later = ", ".join(e.created_at[:19].replace("T", " ") for e in starts[1:3])
        found.append(f"Entries from {name} are missing before {later}"
                     + (" (removed here, or not synced here yet)." if head is None else " (removed here)."))
    prevs = [e.prev for e in entries if e.prev in hashes]
    if len(prevs) != len(set(prevs)):
        found.append(f"Two entries from {name} follow the same entry.")
    if head and head not in hashes:
        found.append("The latest entries written on this computer are missing.")
    return found


def check(reads: Sequence[AiRead], origin_here: str, head_here: str) -> ChainCheck:
    """The chain of every computer's entries in `reads` (the whole AI log kept here, oldest first). `origin_here` and
    `head_here` are this computer's origin and chain head (storage/read_store)."""
    by_origin: dict[str, list[AiRead]] = defaultdict(list)
    for r in reads:
        if r.hash:
            by_origin[r.origin].append(r)
    if head_here and origin_here not in by_origin:
        by_origin[origin_here] = []
    problems = [p for origin, entries in sorted(by_origin.items())
                for p in _problems(origin, entries, head_here if origin == origin_here else None)]
    linked = sum(len(e) for e in by_origin.values())
    return ChainCheck(linked, sum(1 for e in by_origin.values() if e), sum(1 for r in reads if not r.hash),
                      tuple(problems))
