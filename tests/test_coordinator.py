import json
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pycardano import PaymentSigningKey, VerificationKeyWitness
from test_composition import PROFILE, make_candidate

from defi_kernel.coordinator import Coordinator
from defi_kernel.domain import KernelError
from defi_kernel.journal import Journal
from defi_kernel.providers import UnknownSubmission
from defi_kernel.wallet import create_test_wallet, load_wallet


def prepared(tmp_path):
    tx, _, context, _, _ = make_candidate()
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    journal.prepare_candidate("test", tx, [], {"action": "controlled-test"})
    key = PaymentSigningKey.generate()
    tx.transaction_witness_set.vkey_witnesses = [
        VerificationKeyWitness(
            key.to_verification_key(), key.sign(tx.transaction_body.hash())
        )
    ]
    journal.attach_signature("test", tx)
    return journal, tx, context


class Provider:
    profile = PROFILE
    info = None
    canonical = None
    attempts = 0

    def __init__(self, context):
        self.slot = context.last_block_slot

    def verify_identity(self):
        pass

    def tip(self):
        return {"abs_slot": self.slot, "block_no": 103}

    def recheck_dependencies(self, rows):
        return rows

    def submit(self, encoded):
        self.attempts += 1
        raise UnknownSubmission("Test transport timeout")

    def transaction_info(self, txid):
        return self.info

    def block_at_height(self, height):
        return self.canonical


def test_restart_after_unknown_never_resubmits_or_rebuilds(tmp_path):
    journal, tx, context = prepared(tmp_path)
    p = Provider(context)
    with pytest.raises(UnknownSubmission):
        Coordinator(p, journal).submit("test")
    journal.close()
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    assert Coordinator(p, journal).reconcile("test") == "unknown"
    with pytest.raises(KernelError, match="reconciliation"):
        Coordinator(p, journal).submit("test")
    assert p.attempts == 1
    assert len(journal.status()["reservations"]) == 4
    tx.transaction_body.fee += 1
    with pytest.raises(KernelError, match="already prepared"):
        journal.prepare_candidate("test", tx, [], {"action": "controlled-test"})
    journal.close()


def test_confirmation_releases_collateral_and_requires_positive_rollback_evidence(
    tmp_path,
):
    journal, tx, context = prepared(tmp_path)
    p = Provider(context)
    journal.claim_submission("test")
    p.info = {
        "tx_hash": str(tx.transaction_body.id),
        "valid_contract": True,
        "block_height": 101,
        "block_hash": "a" * 64,
    }
    p.canonical = {"hash": "a" * 64}
    c = Coordinator(p, journal)
    assert c.reconcile("test") == "confirmed"
    assert len(journal.status()["reservations"]) == 3
    p.info = None
    assert c.reconcile("test") == "confirmed"  # Absence is not rollback evidence.
    p.canonical = {"hash": "b" * 64}
    assert c.reconcile("test") == "rolled_back"
    journal.close()


def test_claim_is_exclusive_and_candidate_is_durable(tmp_path):
    journal, _, _ = prepared(tmp_path)
    second = Journal(PROFILE.state_path(tmp_path), PROFILE)
    assert journal.claim_submission("test")
    with pytest.raises(KernelError, match="not repeatable"):
        second.claim_submission("test")
    assert second.outbox_entry("test")["attempts"] == 1
    assert (
        json.loads(second.outbox_entry("test")["metadata"])["action"]
        == "controlled-test"
    )
    journal.close()
    second.close()


def test_wallet_keys_are_private_not_overwritten_and_network_bound(tmp_path):
    path, manifest = create_test_wallet(PROFILE, tmp_path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for name in ("payment.skey", "stake.skey"):
        assert stat.S_IMODE((path.parent / name).stat().st_mode) == 0o600
    assert load_wallet(PROFILE, path)[0] == manifest
    with pytest.raises(FileExistsError):
        create_test_wallet(PROFILE, tmp_path)
    with pytest.raises(KernelError, match="mismatch"):
        load_wallet(replace(PROFILE, wallet_id="another"), path)
    with pytest.raises(KernelError, match="restricted"):
        create_test_wallet(SimpleNamespace(name="mainnet"), tmp_path)


def test_abandon_only_unsigned_never_attempted_candidates(tmp_path):
    tx, _, _, _, _ = make_candidate()
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    journal.prepare_candidate("unsigned", tx, [], {})
    journal.abandon_unsigned("unsigned", "Failed pre-signing check")
    assert journal.outbox_entry("unsigned")["status"] == "aborted"
    assert journal.status()["reservations"] == []
    with pytest.raises(KernelError, match="already prepared"):
        journal.prepare_candidate("unsigned", tx, [], {})
    with pytest.raises(KernelError, match="differs"):
        journal.attach_signature("unsigned", tx)
    with pytest.raises(KernelError, match="not repeatable"):
        journal.claim_submission("unsigned")
    journal.close()
    signed, _, _ = prepared(tmp_path / "signed")
    with pytest.raises(KernelError, match="unsigned"):
        signed.abandon_unsigned("test", "No")
    signed.claim_submission("test")
    with pytest.raises(KernelError, match="unsigned"):
        signed.abandon_unsigned("test", "No")
    assert signed.status()["reservations"]
    signed.close()


def test_expiry_requires_mature_anchor_and_explicit_unspent_inputs(tmp_path):
    from defi_kernel.signing import ref_text

    journal, tx, context = prepared(tmp_path)
    p = Provider(context)
    p.slot = tx.transaction_body.ttl + 10
    refs = {
        ref_text(i)
        for i in [*tx.transaction_body.inputs, *tx.transaction_body.collateral]
    }
    p.input_states = lambda _: {r: {"is_spent": False} for r in refs}
    p.canonical = {"hash": "a" * 64, "abs_slot": tx.transaction_body.ttl - 1}
    assert Coordinator(p, journal).reconcile("test") == "prepared"
    assert journal.status()["reservations"]
    p.canonical["abs_slot"] += 1
    assert Coordinator(p, journal).reconcile("test") == "expired"
    assert not journal.status()["reservations"]
    with pytest.raises(KernelError, match="not repeatable"):
        journal.claim_submission("test")
    journal.close()


def test_expired_cancellation_race_needs_confirmed_different_spender(tmp_path):
    from defi_kernel.signing import ref_text

    journal, tx, context = prepared(tmp_path)
    p = Provider(context)
    journal.claim_submission("test")
    journal.mark_transaction("test", "unknown")
    p.slot = tx.transaction_body.ttl + 10
    p.canonical = {"hash": "a" * 64, "abs_slot": p.slot}
    spent = ref_text(tx.transaction_body.inputs[0])
    refs = {
        ref_text(i)
        for i in [*tx.transaction_body.inputs, *tx.transaction_body.collateral]
    }
    p.input_states = lambda _: {r: {"is_spent": r == spent} for r in refs}
    p.confirmed_spender = lambda *a, **kw: None
    c = Coordinator(p, journal)
    assert c.reconcile("test") == "unknown"
    assert len(journal.status()["reservations"]) == len(refs)
    p.confirmed_spender = lambda *a, **kw: {
        "txid": "f" * 64,
        "consumed": spent,
        "block_hash": "a" * 64,
    }
    assert c.reconcile("test") == "conflicted"
    assert [r["ref"] for r in journal.status()["reservations"]] == [spent]
    assert journal.outbox_entry("test")["attempts"] == 1
    journal.close()


def test_failed_script_only_releases_regular_inputs_and_rollback_recovers_collateral(
    tmp_path,
):
    from copy import deepcopy

    from defi_kernel.signing import ref_text

    journal, tx, context = prepared(tmp_path)
    p = Provider(context)
    journal.claim_submission("test")
    p.info = {
        "tx_hash": str(tx.transaction_body.id),
        "valid_contract": False,
        "block_height": 101,
        "block_hash": "a" * 64,
    }
    p.canonical = {"hash": "a" * 64, "abs_slot": p.slot}
    failed = deepcopy(tx)
    failed.valid = False
    p.transaction_cbor = lambda txid: failed
    c = Coordinator(p, journal)
    assert c.reconcile("test") == "failed"
    assert c.reconcile("test") == "failed"
    assert {r["ref"] for r in journal.status()["reservations"]} == set(
        map(ref_text, tx.transaction_body.collateral)
    )
    # A canonical rollback removes the failed transaction; mature expiry plus
    # positive unspent states releases its collateral without resubmission.
    p.info = None
    p.slot = tx.transaction_body.ttl + 100
    p.canonical = {"hash": "b" * 64, "abs_slot": p.slot}
    p.input_states = lambda refs: {str(r): {"is_spent": False} for r in refs}
    assert c.reconcile("test") == "expired"
    assert not journal.status()["reservations"]
    assert journal.outbox_entry("test")["attempts"] == 1
    journal.close()


def test_rollback_of_expiry_anchor_reopens_original_candidate(tmp_path):
    journal, tx, context = prepared(tmp_path)
    p = Provider(context)
    p.slot = tx.transaction_body.ttl + 10
    p.canonical = {"hash": "a" * 64, "abs_slot": p.slot}
    p.input_states = lambda refs: {str(r): {"is_spent": False} for r in refs}
    coordinator = Coordinator(p, journal)
    assert coordinator.reconcile("test") == "expired"
    assert coordinator.reconcile("test") == "expired"
    # The expiry proof's branch disappears and the original bytes are now
    # included on the replacement branch. They must be accounted, not resent.
    p.canonical = {"hash": "b" * 64, "abs_slot": p.slot}
    p.info = {
        "tx_hash": str(tx.transaction_body.id),
        "valid_contract": True,
        "block_height": 101,
        "block_hash": "b" * 64,
    }
    assert coordinator.reconcile("test") == "confirmed"
    assert journal.outbox_entry("test")["attempts"] == 0
    assert len(journal.status()["reservations"]) == len(tx.transaction_body.inputs)
    journal.close()


def test_confirmed_fast_path_still_requires_canonical_block(tmp_path):
    journal, tx, context = prepared(tmp_path)
    provider = Provider(context)
    journal.record_inclusion("test", "a" * 64, 101, 3, confirmed=True)
    provider.canonical = {"hash": "a" * 64}
    provider.transaction_info = lambda _: pytest.fail(
        "Unchanged block should not refetch transaction"
    )
    assert Coordinator(provider, journal).reconcile("test") == "confirmed"
    provider.canonical = None
    with pytest.raises(KernelError, match="block unavailable"):
        Coordinator(provider, journal).reconcile("test")
    journal.close()
