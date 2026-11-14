"""A source's mode (#117, modes.py) as a synced record, by the source's name.

'pick' (I send rows myself) only narrows what the AI gets, so it wins a merge and lands at once, like a block.
'results' (Claude gets results) from another computer lands only for a source not set up here yet; a source here that
picks keeps picking until the user switches it here, like a block removed elsewhere ("held")."""

from __future__ import annotations

from portward_free.storage import SEND_MODES, SEND_PICK, SEND_RESULTS
from portward_free.sync.kinds import Content, Kind, Landing, Place


def _local(place: Place) -> list[Content]:
    return [{"source": source, "mode": mode} for source, mode in place.store.send_modes().items()]


def _valid(content: Content) -> bool:
    return isinstance(content.get("source"), str) and content.get("mode") in SEND_MODES


def _merge(local: Content | None, remote: Content) -> Content:
    if local is not None and local["mode"] == SEND_PICK:
        return local
    return remote


def _apply(place: Place, content: Content, current: Content | None, landing: Landing) -> Content | None:
    store, source = place.store, content["source"]
    if content["mode"] == SEND_RESULTS and store.connection(source) is not None \
            and store.send_mode(source) != SEND_RESULTS:
        landing.held_modes += 1
        return current
    store.set_send_mode(source, content["mode"])
    return {"kind": "send_mode", "source": source, "mode": content["mode"]}


SEND_MODE = Kind(name="send_mode", key=lambda c: (c["source"],), valid=_valid, local=_local, apply=_apply,
                 merge=_merge)
