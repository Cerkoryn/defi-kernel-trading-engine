"""Regressions for the audit's fund-control and input-boundary findings."""

import json
import os
import stat

import pytest
from pycardano import PaymentSigningKey
from test_coordinator import Provider as RecoveryProvider
from test_coordinator import prepared
from test_engine import MARKET, PROFILE, engine

from defi_kernel.coordinator import Coordinator
from defi_kernel.domain import KernelError
from defi_kernel.journal import Journal
from defi_kernel.strategy import Settings
from defi_kernel.wallet import create_test_wallet, load_private_key, load_wallet


def test_key_permissions_symlinks_and_parser_errors_fail_without_secret_output(
    tmp_path,
):
    key = tmp_path / "payment.skey"
    PaymentSigningKey.generate().save(str(key))
    key.chmod(0o644)
    with pytest.raises(KernelError, match="0600"):
        load_private_key(key, PaymentSigningKey)
    key.chmod(0o600)
    assert load_private_key(key, PaymentSigningKey).payload
    link = tmp_path / "link.skey"
    link.symlink_to(key)
    with pytest.raises(KernelError, match="Cannot read"):
        load_private_key(link, PaymentSigningKey)
    key.write_text("SECRET_SENTINEL not valid key JSON")
    with pytest.raises(KernelError) as error:
        load_private_key(key, PaymentSigningKey)
    assert "SECRET_SENTINEL" not in str(error.value)


def test_wallet_rejects_key_path_escape_and_journal_is_private(tmp_path):
    path, wallet = create_test_wallet(PROFILE, tmp_path)
    wallet["payment_key"] = "../other/payment.skey"
    path.write_text(json.dumps(wallet))
    with pytest.raises(KernelError, match="local .skey"):
        load_wallet(PROFILE, path)
    db = PROFILE.state_path(tmp_path)
    journal = Journal(db, PROFILE)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    journal.close()
    alias = db.parent / "alias.sqlite3"
    alias.symlink_to(db)
    with pytest.raises(OSError):
        Journal(alias, PROFILE)


@pytest.mark.parametrize(
    "kind",
    [
        "datum_hash",
        "inline_datum",
        "reference_script",
        "unconfirmed",
        "foreign",
        "spent",
    ],
)
def test_unspendable_and_unverified_wallet_outputs_cannot_hide_exposure(tmp_path, kind):
    runner, provider, journal, _ = engine(tmp_path, base=1600000)
    original = provider.scan

    def scan(endpoint, body):
        observed = original(endpoint, body)
        row = observed.rows[0]
        if kind in ("datum_hash", "inline_datum", "reference_script"):
            row[kind] = {"bytes": "00"} if kind != "datum_hash" else "a" * 64
        elif kind == "unconfirmed":
            row["block_height"] = provider.tip()["block_no"]
        elif kind == "foreign":
            row["address"] = "another wallet"
        else:
            row["is_spent"] = True
        return observed

    provider.scan = scan
    if kind in ("unconfirmed", "foreign", "spent"):
        with pytest.raises(KernelError):
            runner.tick()
    else:
        result = runner.tick()
        assert result["action"] == "paused"
        assert result["inventory"]["free_base"] == 0
        assert result["inventory"]["protected_assets"][MARKET.base.unit] == 1600000
    journal.close()


def test_stop_arriving_during_dependency_checks_prevents_wire_attempt(tmp_path):
    journal, tx, context = prepared(tmp_path)
    provider = RecoveryProvider(context)
    journal.start_run(0)
    provider.recheck_dependencies = lambda rows: journal.request_stop()
    with pytest.raises(KernelError, match="Stop requested"):
        Coordinator(provider, journal).submit("test")
    assert journal.outbox_entry("test")["attempts"] == 0
    assert provider.attempts == 0
    journal.close()


def test_pending_candidate_and_corrupted_signed_bytes_cannot_be_submitted(tmp_path):
    journal, tx, context = prepared(tmp_path)
    tx.transaction_witness_set.vkey_witnesses = None
    tx.transaction_body.fee += 1
    with pytest.raises(KernelError, match="pending work"):
        journal.prepare_candidate("replacement", tx, [], {})
    # Corruption after signature attachment must fail before an attempt is claimed.
    journal.db.execute(
        "UPDATE outbox SET unsigned=? WHERE intent=?", (tx.to_cbor(), "test")
    )
    with pytest.raises(KernelError, match="Stored signed bytes"):
        journal.claim_submission("test")
    assert journal.outbox_entry("test")["attempts"] == 0
    journal.close()


def test_missing_input_state_cannot_retire_signed_candidate(tmp_path):
    journal, tx, context = prepared(tmp_path)
    provider = RecoveryProvider(context)
    provider.slot = tx.transaction_body.ttl + 10
    provider.canonical = {"hash": "a" * 64, "abs_slot": provider.slot}
    provider.input_states = lambda refs: {}
    with pytest.raises(KernelError, match="every input"):
        Coordinator(provider, journal).reconcile("test")
    assert journal.status()["reservations"]
    journal.close()


@pytest.mark.parametrize("value", [True, 1.5, float("nan")])
def test_strategy_rejects_noninteger_economic_limits(value):
    with pytest.raises(ValueError, match="integer"):
        Settings(order_size=200000, target_base=600000, max_base=value)


def test_transaction_signing_does_not_use_ecdsa(monkeypatch):
    import ecdsa
    from nacl.signing import VerifyKey

    def forbidden(*a, **kw):
        raise AssertionError("ECDSA reached from Cardano signing")

    monkeypatch.setattr(ecdsa.SigningKey, "sign", forbidden)
    monkeypatch.setattr(ecdsa.SigningKey, "sign_digest", forbidden)
    monkeypatch.setattr(ecdsa.SigningKey, "generate", forbidden)
    key = PaymentSigningKey.generate()
    message = b"a" * 32
    VerifyKey(key.to_verification_key().payload).verify(message, key.sign(message))


def test_implicit_dotenv_loading_is_disabled(tmp_path, monkeypatch):
    from dotenv import load_dotenv

    monkeypatch.delenv("KERNEL_AUDIT_SENTINEL", raising=False)
    path = tmp_path / ".env"
    path.write_text("KERNEL_AUDIT_SENTINEL=unexpected\n")
    assert not load_dotenv(path)
    assert "KERNEL_AUDIT_SENTINEL" not in os.environ
