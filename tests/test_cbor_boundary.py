"""Malformed provider evidence must not crash diagnostics or bypass recovery."""

from hashlib import blake2b

import pytest
from test_coordinator import Provider, prepared

from defi_kernel.coordinator import Coordinator
from defi_kernel.domain import KernelError
from defi_kernel.protocols import checked_datum
from defi_kernel.providers import Koios
from defi_kernel.signing import decode_candidate, decode_transaction

TRUNCATED = "84a0a0d9010281"


@pytest.mark.parametrize("raw", [TRUNCATED, "zz", "00", "80", None, 42])
def test_bad_transaction_evidence_is_a_controlled_error(raw):
    for decode in (decode_transaction, decode_candidate):
        with pytest.raises(KernelError, match="Invalid transaction CBOR"):
            decode(raw)


def test_bad_provider_cbor_keeps_submission_and_reservations_until_verified(tmp_path):
    journal, tx, context = prepared(tmp_path)
    journal.claim_submission("test")
    before = dict(journal.outbox_entry("test"))
    provider = Provider(context)
    koios = Koios.__new__(Koios)
    payload = [TRUNCATED]
    koios.request = lambda *a, **k: [{"tx_hash": str(tx.id), "cbor": payload[0]}]
    provider.transaction_info = lambda txid: koios.transaction_cbor(txid)
    coordinator = Coordinator(provider, journal)
    for _ in range(2):
        with pytest.raises(KernelError, match="CBORDecodeEOF"):
            coordinator.reconcile("test")
        assert dict(journal.outbox_entry("test")) == before
        assert len(journal.status()["reservations"]) == 4
    payload[0] = tx.to_cbor_hex()
    assert str(koios.transaction_cbor(str(tx.id)).id) == str(tx.id)
    provider.transaction_info = lambda _: {
        "valid_contract": True,
        "block_height": 101,
        "block_hash": "a" * 64,
    }
    provider.canonical = {"hash": "a" * 64}
    assert coordinator.reconcile("test") == "confirmed"
    assert journal.outbox_entry("test")["attempts"] == 1
    assert provider.attempts == 0
    journal.close()


@pytest.mark.parametrize("raw", ["d8799f01", "d87901", "zz"])
def test_malformed_liquidity_datum_is_rejected_without_decoder_crash(raw):
    with pytest.raises(KernelError):
        checked_datum({"is_spent": False, "datum_cbor": raw}, 2)


@pytest.mark.parametrize("raw", ["d879820102", "d8799f0102ff"])
def test_valid_definite_and_indefinite_datums_remain_supported(raw):
    row = {
        "is_spent": False,
        "datum_cbor": raw,
        "datum_hash": blake2b(bytes.fromhex(raw), digest_size=32).hexdigest(),
    }
    assert checked_datum(row, 2) == raw
