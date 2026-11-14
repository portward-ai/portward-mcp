"""One LocalStore used from several threads at once, as the hosted engine uses it: its poll thread lands bundles and
hands on AI log entries while the MCP server answers tool calls on worker threads (anyio runs sync tools in a
thread pool, so two calls from claude.ai at once are two threads too). Every call must keep working, and a
transaction (BEGIN IMMEDIATE ... COMMIT) must never take in another thread's statements."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from portward_free.storage import LocalStore, PullRecord

THREADS = 8
ROUNDS = 150


def hammer(work: Callable[[int, int], None]) -> list[BaseException]:
    """Run `work(thread, round)` on THREADS threads started together; the errors any of them raised."""
    start, errors = threading.Barrier(THREADS), []

    def run(n: int) -> None:
        start.wait()
        try:
            for i in range(ROUNDS):
                work(n, i)
        except BaseException as err:  # noqa: BLE001 (collected and asserted on below)
            errors.append(err)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return errors


@pytest.mark.parametrize("where", ["file", "memory"])
def test_a_store_shared_by_threads_keeps_every_call_and_transaction_whole(tmp_path: Path, where: str) -> None:
    store = LocalStore.open(tmp_path / "portward.db") if where == "file" else LocalStore.memory()
    try:
        def work(n: int, i: int) -> None:
            if n % 2:  # writers: a transaction each, as a pull does
                store.record_pull(PullRecord(f"q{n}-{i}", "SELECT 1", "shop", ("t",), ("c",), 1, "h"))
                store.set_setting(f"s{n}", str(i))
            else:  # readers, as the tools and the poll thread do between writes
                store.add_connection(f"c{n}", "postgres", {"host": "db.example"})
                assert store.connection(f"c{n}") is not None
                store.setting("hosted_bundle")
                store.all_pulls()

        errors = hammer(work)
        assert errors == [], errors[:3]
        assert len(store.all_pulls()) == (THREADS // 2) * ROUNDS
    finally:
        store.close()
