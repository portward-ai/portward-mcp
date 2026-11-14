"""The seam for new kinds of synced record (#97): a kind added to records.REGISTERED is keyed, checked, merged and
landed through the same functions the engine calls for the first four kinds."""

from __future__ import annotations

import base64

import pytest

from portward_free.storage import LocalStore
from portward_free.sync import records
from portward_free.sync.kinds import Landing, Place, secret_kind


class Box:
    def __init__(self, raw: bytes | None = None) -> None:
        self.raw = raw

    def read(self, place: Place) -> bytes | None:
        return self.raw

    def write(self, place: Place, raw: bytes) -> None:
        self.raw = raw


@pytest.fixture()
def box(monkeypatch: pytest.MonkeyPatch) -> Box:
    held = Box(b"m" * 32)
    monkeypatch.setitem(records.REGISTERED, "a_secret", secret_kind("a_secret", held.read, held.write))
    return held


def secret(raw: bytes) -> dict:
    return {"kind": "a_secret", "secret": base64.b64encode(raw).decode()}


def test_a_registered_kind_is_read_from_this_computer_and_keyed_by_its_name(box: Box) -> None:
    place = Place(LocalStore.memory())
    assert secret(b"m" * 32) in records.local_state(place)
    assert records.key_of(secret(b"x" * 32)) == ("a_secret",)  # one per account, whatever it holds
    assert records.order("a_secret") > records.order("applied") > records.order("rule")


@pytest.mark.parametrize("content", [
    {"kind": "a_secret"}, {"kind": "a_secret", "secret": "not base64!"},
    {"kind": "a_secret", "secret": base64.b64encode(b"short").decode()},
    {"kind": "a_secret", "secret": base64.b64encode(b"x" * 65).decode()}])
def test_a_secret_record_of_the_wrong_shape_is_skipped(box: Box, content: dict) -> None:
    assert not records.valid(content)


def test_the_secret_already_on_the_account_wins_and_is_never_deleted_by_sync(box: Box) -> None:
    place, theirs = Place(LocalStore.memory()), secret(b"t" * 32)
    assert records.valid(theirs)
    assert records.merge(secret(b"m" * 32), theirs) == theirs
    assert records.apply(place, theirs, secret(b"m" * 32), Landing()) == theirs
    assert box.raw == b"t" * 32
    assert not records.deletable("a_secret") and not records.gone(theirs)
