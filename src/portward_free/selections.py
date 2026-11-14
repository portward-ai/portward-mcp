"""Selections: what the user put in the chat from the results panel, for the AI to fetch.

'Put in chat' (the app-only `put_in_chat`) records the picked rows and columns of one held pull as a selection and
returns a short note with its id; the user sends that note as their own visible message, and the AI fetches exactly
those rows with `get_selection`. The id is random (`secrets.token_urlsafe`), so the AI can fetch only a selection the
user put in the chat; the store keeps only its SHA-256 hash.

A selection is held in the local store, sealed with the kept-rows key (`kept`) like the panel's rows, because the
panel's put_in_chat and the AI's get_selection can run in two server processes (Claude Desktop runs two on one
app-data folder). It can be read more than once until it expires (with or before the pull it is from), but it is
never widened, and it isn't sent once the source's block rules differ from the ones it was pulled under. When the
keychain can't seal it, it is held in this process's memory only: never in the file unsealed.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from portward_free import kept
from portward_free.keychain import Keychain, KeychainError
from portward_free.pull import typed_value
from portward_free.results_panel import BUTTON, Held, Picked, SelectionError, picked
from portward_free.rules import Policy, policy_from
from portward_free.storage import LocalStore

SELECTION_HOURS = 24  # at most; never longer than the pull it is from is held for the panel
HELD_SELECTIONS = 200  # recent selections the store keeps
PREFIX = "sel_"


@dataclass(frozen=True)
class Selection:
    selection_id: str
    picked: Picked
    source: str
    rules: Policy  # the block rules or grants its pull was pulled under


def _hash(selection_id: str) -> str:
    return hashlib.sha256(selection_id.encode()).hexdigest()


def _associated(id_hash: str) -> str:
    return f"panel-selection:{id_hash}"  # neither a panel pull's nor a kept-rows blob's: none can pass for another


class Selections:
    def __init__(self, store: LocalStore, keychain: Keychain, limit: int = HELD_SELECTIONS,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._keychain = keychain
        self._limit = limit
        self._clock = clock
        self._unsealed: OrderedDict[str, tuple[Selection, float]] = OrderedDict()  # only when sealing failed

    def make(self, held: Held, rows: Sequence[int], columns: Sequence[str]) -> Selection:
        """Record the user's pick from a held pull. Raises SelectionError for a pick outside the result."""
        selection = Selection(PREFIX + secrets.token_urlsafe(18), picked(held, rows, columns), held.source,
                              held.rules)
        now = self._clock()
        expires = min(now + SELECTION_HOURS * 3600, held.expires_at or now + SELECTION_HOURS * 3600)
        id_hash = _hash(selection.selection_id)
        try:
            secret = kept.key(self._keychain, create=True)
            assert secret is not None  # create=True always gives a key
            sealed = kept.seal_json(secret, _associated(id_hash), _sealable(selection))
        except (KeychainError, kept.RowsLocked):
            self._unsealed[id_hash] = (selection, expires)
            while len(self._unsealed) > self._limit:
                self._unsealed.popitem(last=False)
            return selection
        self._store.hold_selection(id_hash, held.pull_id, sealed, now, expires, self._limit)
        return selection

    def get(self, selection_id: str) -> Selection:
        """The selection, if the user put it in the chat, it hasn't expired and the block rules are still the ones
        it was pulled under. Raises SelectionError otherwise."""
        selection = self._found(selection_id)
        if self._store.rules_for(selection.source) != selection.rules:
            raise SelectionError(f"the user's block rules changed since selection {selection_id} was picked; ask "
                                 f"them to run the query again and press '{BUTTON}' in the results panel")
        return selection

    def _found(self, selection_id: str) -> Selection:
        now = self._clock()
        id_hash = _hash(selection_id)
        here = self._unsealed.get(id_hash)
        if here is not None and here[1] > now:
            return here[0]
        found = self._store.selection(id_hash, now)
        if found is None:
            raise SelectionError(f"no selection {selection_id!r}: it expired or was never put in the chat. Use only a "
                                 "selection id from the user's message; to share rows, they pick them in the results "
                                 f"panel and press '{BUTTON}'")
        try:
            data = kept.open_json(kept.stored_key(self._keychain, "the selection"), _associated(id_hash), found[0],
                                  "the selection")
        except (KeychainError, kept.RowsLocked) as err:
            raise SelectionError(f"selection {selection_id} can't be opened ({err}); ask the user to put the rows "
                                 "in the chat again") from None
        return _unsealed(selection_id, data)


def _sealable(selection: Selection) -> dict[str, Any]:
    chosen = selection.picked
    return {"pull_id": chosen.pull_id, "columns": list(chosen.columns),
            "rows": [[typed_value(v) for v in r] for r in chosen.rows], "of_rows": chosen.of_rows,
            "of_columns": chosen.of_columns, "every_row": chosen.every_row, "source": selection.source,
            "rules": selection.rules.payload()}


def _unsealed(selection_id: str, data: dict[str, Any]) -> Selection:
    chosen = Picked(data["pull_id"], tuple(data["columns"]),
                    tuple(tuple(kept.untyped(v) for v in r) for r in data["rows"]), int(data["of_rows"]),
                    int(data["of_columns"]), bool(data["every_row"]))
    return Selection(selection_id, chosen, data["source"], policy_from(data["rules"]))
