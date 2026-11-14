"""A source's mode (#117) syncs with the source: 'I send rows myself' from any computer lands at once, and
'Claude gets results' from another computer never loosens a source that picks here."""

from __future__ import annotations

from portward_free.storage import LocalStore
from portward_free.sync import records
from portward_free.sync.kinds import Landing, Place


def mode(source: str, value: str) -> dict:
    return {"kind": "send_mode", "source": source, "mode": value}


def test_a_sources_mode_is_synced_as_a_record_keyed_by_the_source() -> None:
    store = LocalStore.memory()
    store.set_send_mode("shop", "results")
    assert mode("shop", "results") in records.local_state(Place(store))
    assert records.key_of(mode("shop", "pick")) == ("send_mode", "shop")
    assert records.valid(mode("shop", "pick"))
    assert not records.valid(mode("shop", "everything")) and not records.valid({"kind": "send_mode", "mode": "pick"})


def test_picking_from_any_computer_wins_a_merge_and_lands_at_once() -> None:
    store = LocalStore.memory()
    store.set_send_mode("shop", "results")
    assert records.merge(mode("shop", "results"), mode("shop", "pick")) == mode("shop", "pick")
    assert records.merge(mode("shop", "pick"), mode("shop", "results")) == mode("shop", "pick")
    records.apply(Place(store), mode("shop", "pick"), mode("shop", "results"), Landing())
    assert store.send_mode("shop") == "pick"


def test_results_from_another_computer_never_loosens_a_source_that_picks_here() -> None:
    store = LocalStore.memory()
    store.add_connection("shop", "sqlite", {"path": "/tmp/shop.sqlite"})  # an existing source: it picks
    landing = Landing()
    kept = records.apply(Place(store), mode("shop", "results"), None, landing)
    assert store.send_mode("shop") == "pick"
    assert kept is None or kept["mode"] == "pick"


def test_results_chosen_elsewhere_lands_for_a_source_not_set_up_here_yet() -> None:
    store = LocalStore.memory()
    records.apply(Place(store), mode("wiki", "results"), None, Landing())
    assert store.send_mode("wiki") == "results"
