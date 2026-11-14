"""The sync keys (#55): what the service is given can only be opened with keys it never has, and what it could make
itself doesn't open on a computer."""

from __future__ import annotations

import pytest
from harness import MemoryKeychain

from portward_free.sync import crypto
from portward_free.sync.crypto import Unopenable

ACCOUNT = "acct_one"


def test_the_recovery_key_is_160_random_bits_in_groups_of_four_and_forgives_how_it_is_typed() -> None:
    key = crypto.new_recovery_key()
    groups = key.split("-")
    assert len(groups) == 8 and all(len(g) == 4 for g in groups)
    assert crypto.new_recovery_key() != key
    assert crypto.parse_recovery_key(key) == crypto.parse_recovery_key(f"  {key.lower().replace('-', ' ')} ")
    with pytest.raises(Unopenable):
        crypto.parse_recovery_key(key[:-1])


def test_the_data_key_opens_only_with_the_recovery_key_it_was_wrapped_under() -> None:
    data_key, recovery = crypto.new_data_key(), crypto.new_recovery_key()
    wrapped = crypto.wrap_for_recovery(data_key, recovery, ACCOUNT)
    assert crypto.open_with_recovery_key(wrapped, recovery, ACCOUNT) == data_key
    assert data_key.raw not in wrapped
    with pytest.raises(Unopenable, match="doesn't open"):
        crypto.open_with_recovery_key(wrapped, crypto.new_recovery_key(), ACCOUNT)
    with pytest.raises(Unopenable):  # another account's wrap, handed over by the service
        crypto.open_with_recovery_key(wrapped, recovery, "acct_two")


def test_a_wrap_the_service_made_with_weak_settings_is_refused_before_any_work() -> None:
    data_key, recovery = crypto.new_data_key(), crypto.new_recovery_key()
    wrapped = bytearray(crypto.wrap_for_recovery(data_key, recovery, ACCOUNT))
    wrapped[1:5] = (8).to_bytes(4, "big")  # 8 KiB of memory instead of 64 MiB
    with pytest.raises(Unopenable, match="won't use"):
        crypto.open_with_recovery_key(bytes(wrapped), recovery, ACCOUNT)
    wrapped[1:5] = (1024 * 1024).to_bytes(4, "big")  # or a gigabyte, to make the computer burn memory and time
    with pytest.raises(Unopenable, match="won't use"):
        crypto.open_with_recovery_key(bytes(wrapped), recovery, ACCOUNT)


def test_a_device_wrap_opens_only_under_this_computers_own_key() -> None:
    keychain = MemoryKeychain()
    mine = crypto.device_key(keychain, create=True)
    assert crypto.device_key(keychain, create=False) == mine
    data_key = crypto.new_data_key()
    wrapped = crypto.wrap_for_device(data_key, mine, ACCOUNT)
    assert crypto.open_with_device_key(wrapped, mine, ACCOUNT) == data_key
    the_services_own = crypto.wrap_for_device(crypto.new_data_key(), crypto.device_key(MemoryKeychain(), create=True),
                                              ACCOUNT)
    with pytest.raises(Unopenable):  # a key the service made can't be slipped in as this computer's
        crypto.open_with_device_key(the_services_own, mine, ACCOUNT)


def test_a_record_opens_only_under_its_own_id_and_version() -> None:
    data_key = crypto.new_data_key()
    record_id = data_key.record_id("note", "books", "invoices", "")
    sealed = data_key.seal(ACCOUNT, record_id, 3, {"kind": "note", "note": "secret words"})
    assert b"secret words" not in sealed
    assert data_key.open(ACCOUNT, record_id, 3, sealed) == {"kind": "note", "note": "secret words"}
    for account, rid, version in ((ACCOUNT, record_id, 4), (ACCOUNT, "0" * 64, 3), ("acct_two", record_id, 3)):
        with pytest.raises(Unopenable):  # replayed as a newer version, moved to another id or account
            data_key.open(account, rid, version, sealed)
    with pytest.raises(Unopenable):
        crypto.new_data_key().open(ACCOUNT, record_id, 3, sealed)


def test_record_ids_say_nothing_about_what_they_stand_for() -> None:
    one, two = crypto.new_data_key(), crypto.new_data_key()
    rid = one.record_id("rule", "books", "invoices", "email")
    assert len(rid) == 64 and "books" not in rid
    assert rid == one.record_id("rule", "books", "invoices", "email") != two.record_id("rule", "books", "invoices",
                                                                                       "email")
    assert one.key_id != two.key_id and len(one.key_id) == 32


def test_a_new_data_key_opens_from_the_handover_only_with_the_old_key_for_this_account() -> None:
    old, new, other = crypto.new_data_key(), crypto.new_data_key(), crypto.new_data_key()
    handover = crypto.seal_handover(old, new, ACCOUNT)
    assert crypto.open_handover(handover, old, ACCOUNT, new.key_id) == new
    for key, account, new_id in ((other, ACCOUNT, new.key_id), (old, "acct_two", new.key_id),
                                 (old, ACCOUNT, other.key_id)):
        with pytest.raises(Unopenable):
            crypto.open_handover(handover, key, account, new_id)


def test_the_service_keeps_a_verifier_it_cant_turn_into_the_proof() -> None:
    import hashlib

    key = crypto.new_data_key()
    assert len(key.proof) == 64 and key.proof != key.verifier
    assert hashlib.sha256(bytes.fromhex(key.proof)).hexdigest() == key.verifier
    assert crypto.new_data_key().proof != key.proof


# --- the AI log's own key (#107) ------------------------------------------------------------------------------------
def _sealed_with_ai_log_key(data_key: crypto.DataKey, record_id: str, content: dict) -> bytes:
    """What a holder of only the AI log's key (an approved browser) could seal."""
    import json
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    aad = f"portward-sync-record-v1\n{ACCOUNT}\n{record_id}\n1".encode()
    return b"\x01" + nonce + AESGCM(data_key.ai_log_key).encrypt(nonce, json.dumps(content).encode(), aad)


def test_the_ai_logs_records_and_the_others_both_open_with_the_data_key() -> None:
    data_key = crypto.new_data_key()
    for content in ({"kind": "read", "entries": []}, {"kind": "note", "text": "x"}):
        assert data_key.open(ACCOUNT, "r1", 1, data_key.seal(ACCOUNT, "r1", 1, content)) == content
    assert data_key.ai_log_key != data_key.raw and len(data_key.ai_log_key) == 32


def test_a_record_of_another_kind_sealed_under_the_ai_logs_key_is_refused() -> None:
    data_key = crypto.new_data_key()
    read = {"kind": "read", "entries": []}
    assert data_key.open(ACCOUNT, "r1", 1, _sealed_with_ai_log_key(data_key, "r1", read)) == read
    with pytest.raises(Unopenable):
        data_key.open(ACCOUNT, "r2", 1, _sealed_with_ai_log_key(data_key, "r2", {"kind": "rule", "table": "t"}))
