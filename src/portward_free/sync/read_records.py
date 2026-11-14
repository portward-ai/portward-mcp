"""The AI log's summary as synced records (#98), through the kinds seam (#97).

- read: per computer, day and part: when, which AI app, route, profiles, the tables and columns at their levels,
  row counts and result hashes, never the values. Entries never change, so two copies merge as their union. Days
  older than the AI log keeps (proof.KEEP_DAYS) are dropped on each computer, not deleted through sync."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from portward_free.proof import AiRead, cutoff_day, valid_entry
from portward_free.sync.kinds import Content, Kind, Landing, Place

READ_PART_BYTES = 96 * 1024  # an AI log record holds entries up to about this much JSON (the service caps a record)


def _size(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def read_records(reads: Iterable[AiRead]) -> list[Content]:
    """The AI log's summary as records: per computer and day, in parts of about READ_PART_BYTES, entries in time
    order. A computer only adds to its own day, so a new entry only ever changes that day's last part."""
    days: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for read in reads:
        days.setdefault((read.origin, read.day), []).append(read.as_dict())
    out: list[Content] = []
    for (origin, day), entries in sorted(days.items()):
        part, size, held = 0, 0, []
        for entry in sorted(entries, key=lambda e: (e["created_at"], e["id"])):
            length = _size(entry)
            if held and size + length > READ_PART_BYTES:
                out.append({"kind": "read", "origin": origin, "day": day, "part": part, "entries": held})
                part, size, held = part + 1, 0, []
            held.append(entry)
            size += length
        out.append({"kind": "read", "origin": origin, "day": day, "part": part, "entries": held})
    return out


def _valid(content: Content) -> bool:
    entries = content.get("entries")
    part = content.get("part")
    return isinstance(content.get("origin"), str) and isinstance(content.get("day"), str) and \
        isinstance(part, int) and not isinstance(part, bool) and isinstance(entries, list) and \
        all(valid_entry(e) for e in entries)


def _here(place: Place, content: Content) -> bool:
    return content["day"] >= cutoff_day()  # a day older than the AI log keeps: dropped here, not deleted everywhere


def _merge(local: Content | None, remote: Content) -> Content:
    entries = {e["id"]: e for e in (local or {}).get("entries", [])} | {e["id"]: e for e in remote["entries"]}
    return {**remote, "entries": sorted(entries.values(), key=lambda e: (e["created_at"], e["id"]))}


def _apply(place: Place, content: Content, current: Content | None, landing: Landing) -> Content | None:
    if not _here(place, content):
        return None
    place.store.reads.add(AiRead.from_dict(e) for e in content["entries"])
    return content


READ = Kind(name="read", key=lambda c: (c["origin"], c["day"], str(c["part"])), valid=_valid,
            local=lambda place: read_records(place.store.reads.between(cutoff_day())),
            apply=_apply, merge=_merge, here=_here)
