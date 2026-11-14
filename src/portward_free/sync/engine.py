"""One sync run (#55): fetch what changed on the service since last time, merge it with what changed here, land it
here, and send this computer's changes, each sealed under the data key with its id and new version.

For every record this computer keeps what it and the service last agreed on (store.sync_rows: the version, the
content there and what this computer held for it). Against that:
- only the service changed it: it lands here;
- only this computer changed it: it is sent with the version it was read at (the service takes it only if nobody
  wrote in between, else hands back the newer copy, which is merged and sent again);
- both changed it: records.merge decides (keep both; nothing silently lost) and the result lands here and is sent.
A record that doesn't open under the data key (changed bytes, a foreign key) is skipped and counted; what this
computer holds for it stays."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from portward_free.account import AccountServiceError
from portward_free.keychain import Keychain
from portward_free.storage import LocalStore
from portward_free.sync.crypto import DataKey, Unopenable
from portward_free.sync.records import (Content, Landing, Place, apply, canonical, deletable, gone, here, key_of,
                                        local_state, merge, order, tombstone, valid)
from portward_free.sync.service import Remote, SyncService

BATCH = 200                       # records per write: the service's MAX_WRITES
BATCH_BYTES = 4 * 1024 * 1024     # sealed bytes per write, well under the service's 8 MiB body (base64 grows it)
MAX_RECORD_BYTES = 256 * 1024     # the service's own cap on one sealed record
MAX_PAGES = 250  # pages of changes in one run: the service caps an account at 100k records, 500 a page
ROUNDS = 5  # rounds of conflict, merge and send again before leaving a record to the next run


@dataclass
class Report:
    sent: int = 0
    too_big: int = 0
    reread: bool = False  # the service was behind this computer and was read again from the start
    received: int = 0
    merged: int = 0
    unopenable: int = 0
    cursor: int = 0
    high: int = 0  # the highest change counter this computer has seen, its own writes included
    landing: Landing = field(default_factory=Landing)


@dataclass
class _Pending:
    content: Content  # what to send
    base: int         # the version it replaces
    local: Content | None  # what this computer holds for it


class Run:
    def __init__(self, store: LocalStore, service: SyncService, token: str, account_id: str, key: DataKey,
                 keychain: Keychain | None = None) -> None:
        self.store, self.service, self.token, self.account_id, self.key = store, service, token, account_id, key
        self.place = Place(store, keychain)
        self.rows = store.sync_rows()
        self.report = Report()

    def _open(self, remote: Remote) -> Content | None:
        try:
            content = self.key.open(self.account_id, remote.id, remote.version, remote.ciphertext)
        except Unopenable:
            content = None
        if content is None or not valid(content) or self.key.record_id(*key_of(content)) != remote.id:
            self.report.unopenable += 1
            return None
        return content

    def _fetch(self, cursor: int, high: int) -> dict[str, tuple[int, Content]]:
        """What changed since `cursor`. A service whose counter is behind this computer's cursor (restored from a
        backup, or lying) is read again from the start: versions already here are skipped, so nothing is lost. Pages
        must move forward and stop: a service that loops is refused instead of followed."""
        changed: dict[str, tuple[int, Content]] = {}
        seen: dict[str, int] = {}
        reread = False
        for page_number in range(MAX_PAGES):
            page, more, head = self.service.changes(self.token, cursor)
            if page_number == 0 and head < max(cursor, high):
                cursor, reread = 0, True
                page, more, head = self.service.changes(self.token, cursor)
            for remote in page:
                if remote.seq <= cursor or remote.seq > head:
                    raise AccountServiceError("the sync service sent changes out of order; nothing was changed")
                cursor = remote.seq
                seen[remote.id] = remote.version
                row = self.rows.get(remote.id)
                if row is not None and remote.version <= row[1]:
                    continue  # this computer's own write coming back, or older
                content = self._open(remote)
                if content is not None:
                    changed[remote.id] = (remote.version, content)
            if not more or not page:
                break
        else:
            raise AccountServiceError("the sync service kept sending more changes than an account can hold")
        if reread:  # what the service lost (or holds older) is sent again from here, as if never synced
            self.rows = {rid: row for rid, row in self.rows.items() if seen.get(rid, 0) >= row[1]}
            self.report.reread = True
        self.report.cursor = cursor
        self.report.high = cursor if reread else max(cursor, high)
        return changed

    def run(self, cursor: int, high: int = 0) -> Report:
        changed = self._fetch(cursor, high)
        here_now = {self.key.record_id(*key_of(c)): c for c in local_state(self.place)}
        landings: list[tuple[str, Content, Content | None, int, bool]] = []  # id, agreed, current, version, from_remote
        sends: dict[str, _Pending] = {}
        for record_id in set(self.rows) | set(here_now) | set(changed):
            row = self.rows.get(record_id)
            agreed_remote = json.loads(row[2]) if row else None
            agreed_local = json.loads(row[3]) if row and row[3] else None
            current = here_now.get(record_id)
            if current is None and agreed_remote is not None and not here(self.place, agreed_remote):
                current = agreed_local  # a note whose source isn't on this computer: not a deletion
            local_changed = canonical(current) != canonical(agreed_local)
            if record_id in changed:
                version, remote = changed[record_id]
                agreed = merge(current, remote) if local_changed else remote
                self.report.merged += int(local_changed and canonical(current) != canonical(remote))
                landings.append((record_id, agreed, current, version, True))
                if canonical(agreed) != canonical(remote):
                    sends[record_id] = _Pending(agreed, version, None)
            elif local_changed:
                assert row is not None or current is not None
                if current is None:
                    if not deletable(agreed_remote["kind"]):  # e.g. history isn't deleted by sync: put it back
                        landings.append((record_id, agreed_remote, None, row[1], False))
                        continue
                    sends[record_id] = _Pending(tombstone(agreed_remote), row[1], None)
                else:
                    sends[record_id] = _Pending(current, row[1] if row else 0, current)
            elif row is not None and agreed_local is None and agreed_remote is not None and \
                    not gone(agreed_remote) and here(self.place, agreed_remote):
                landings.append((record_id, agreed_remote, None, row[1], False))  # e.g. its source was added

        for record_id, agreed, current, version, from_remote in sorted(
                landings, key=lambda t: (order(t[1]["kind"]), t[1].get("saved_at", ""))):
            held = apply(self.place, agreed, current, self.report.landing)
            self.report.received += int(from_remote)
            if record_id in sends:
                sends[record_id].local = held
            else:
                self.store.set_sync_row(record_id, agreed["kind"], version, canonical(agreed), canonical(held))
        self._send(sends)
        return self.report

    def _batches(self, sends: dict[str, _Pending]) -> list[list[tuple[str, _Pending, bytes]]]:
        """Sealed writes in batches the service takes (BATCH records, BATCH_BYTES); a record over MAX_RECORD_BYTES
        sealed (say, a pull with enormous SQL) stays on this computer only and is counted."""
        batches: list[list[tuple[str, _Pending, bytes]]] = [[]]
        size = 0
        for record_id, p in sends.items():
            sealed = self.key.seal(self.account_id, record_id, p.base + 1, p.content)
            if len(sealed) > MAX_RECORD_BYTES:
                self.report.too_big += 1
                continue
            if len(batches[-1]) >= BATCH or size + len(sealed) > BATCH_BYTES:
                batches.append([])
                size = 0
            batches[-1].append((record_id, p, sealed))
            size += len(sealed)
        return [b for b in batches if b]

    def _send(self, sends: dict[str, _Pending]) -> None:
        for _ in range(ROUNDS):
            if not sends:
                return
            again: dict[str, _Pending] = {}
            for batch in self._batches(sends):
                written = self.service.write(self.token, [(record_id, p.base, sealed) for record_id, p, sealed in batch])
                for record_id, p, _ in batch:
                    if record_id in written.missing:  # the service lost it (e.g. restored from a backup): send anew
                        again[record_id] = _Pending(p.content, 0, p.local)
                    if record_id in written.saved:
                        if written.saved[record_id] != p.base + 1:  # it was sealed for base + 1, nothing else
                            raise AccountServiceError("the sync service gave a record a version it wasn't sealed "
                                                      "for; nothing more was sent")
                        self.report.high = max(self.report.high, written.seqs.get(record_id, 0))
                        self.store.set_sync_row(record_id, p.content["kind"], written.saved[record_id],
                                                canonical(p.content), canonical(p.local))
                        self.report.sent += 1
                for conflict in written.conflicts:
                    pending = sends.get(conflict.id)
                    remote = self._open(conflict)
                    if pending is None or remote is None:
                        continue
                    agreed = merge(pending.local, remote)
                    self.report.merged += 1
                    held = apply(self.place, agreed, pending.local, self.report.landing)
                    if canonical(agreed) == canonical(remote):
                        self.store.set_sync_row(conflict.id, remote["kind"], conflict.version, canonical(remote),
                                                canonical(held))
                    else:
                        again[conflict.id] = _Pending(agreed, conflict.version, held)
            sends = again
