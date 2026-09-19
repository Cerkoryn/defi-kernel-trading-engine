"""Native minting and burning use the ordinary authorization/signing gate."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pycardano import (
    Address,
    Asset,
    AssetName,
    MultiAsset,
    Network,
    PaymentSigningKey,
    ScriptAll,
    ScriptHash,
    ScriptPubkey,
    TransactionOutput,
    Value,
    VerificationKeyHash,
)
from test_composition import StructuralContext
from test_transactions import as_row

from defi_kernel.chain_context import to_utxo
from defi_kernel.coordinator import Coordinator
from defi_kernel.domain import KernelError
from defi_kernel.execution import build_transaction, evaluate_final
from defi_kernel.journal import Journal
from defi_kernel.signing import LocalSigner
from defi_kernel.transactions import CompositionBuilder


def native_candidate(quantity=-10):
    key = PaymentSigningKey.generate()
    owner = Address(key.to_verification_key().hash(), network=Network.TESTNET)
    policy = ScriptPubkey(owner.payment_part)
    name = AssetName(b"test")
    row = as_row(
        TransactionOutput(
            owner, Value(20_000_000, MultiAsset({policy.hash(): Asset({name: 10})}))
        )
    )
    ctx = StructuralContext([row], 1788800000)
    ctx.provider = SimpleNamespace(
        profile=ctx.profile,
        verify_identity=lambda: None,
        clock=lambda: 1788800000,
        tip=lambda: {"abs_slot": ctx.last_block_slot},
        recheck_dependencies=lambda rows: rows,
    )
    builder = CompositionBuilder(ctx)
    builder.add_input(to_utxo(row, ctx.profile))
    builder.native_scripts = [policy]
    builder.mint = MultiAsset({policy.hash(): Asset({name: quantity})})
    builder.validity_start, builder.ttl = (
        ctx.last_block_slot - 30,
        ctx.last_block_slot + 300,
    )
    unit = str(policy.hash()) + bytes(name).hex()
    tx, auth, metadata = build_transaction(
        builder,
        owner,
        [(unit, quantity, quantity), ("lovelace", -2_000_000, 0)],
        {"action": "controlled-test"},
        max_fee=2_000_000,
        max_collateral=0,
    )
    return key, tx, auth, metadata, ctx, row


@pytest.mark.parametrize("quantity", [-10, 10])
def test_native_mint_and_burn_pass_exact_authorization_and_signing(tmp_path, quantity):
    key, tx, auth, metadata, ctx, row = native_candidate(quantity)
    key_path = tmp_path / "payment.skey"
    key.save(str(key_path))
    key_path.chmod(0o600)
    journal = Journal(ctx.profile.state_path(tmp_path), ctx.profile)
    c = Coordinator(ctx.provider, journal)
    signer = LocalSigner(key_path)
    # Changing the authorized amount must still fail before any durable candidate.
    with pytest.raises(KernelError, match="Unauthorized mint or burn"):
        c.prepare("bad", tx, replace(auth, mint=()), ctx, [row], signer, metadata)
    assert not journal.db.execute("SELECT 1 FROM outbox").fetchone()
    c.prepare("native", tx, auth, ctx, [row], signer, metadata)
    entry = journal.outbox_entry("native")
    assert entry["status"] == "prepared" and entry["attempts"] == 0
    assert entry["signed"] is not None
    assert not tx.transaction_body.collateral
    assert evaluate_final(tx, ctx).budgets == ()
    journal.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "wrong_policy",
        "unsigned_key",
        "compound",
        "script_input",
        "reference",
    ],
)
def test_native_gate_rejects_unproven_authorization(mutation):
    _, tx, _, _, ctx, _ = native_candidate()
    body, witness = tx.transaction_body, tx.transaction_witness_set
    if mutation == "missing":
        witness.native_scripts = []
    elif mutation == "extra":
        witness.native_scripts = [
            *witness.native_scripts,
            ScriptPubkey(VerificationKeyHash(b"x" * 28)),
        ]
    elif mutation == "wrong_policy":
        witness.native_scripts = [ScriptPubkey(VerificationKeyHash(b"x" * 28))]
    elif mutation == "unsigned_key":
        body.required_signers = []
    elif mutation == "compound":
        witness.native_scripts = [ScriptAll(list(witness.native_scripts))]
    elif mutation == "reference":
        body.reference_inputs = list(body.inputs)
    else:
        original = ctx.utxo_by_tx_id

        def script_input(*args):
            utxo = original(*args)
            utxo.output.address = Address(
                ScriptHash(b"x" * 28), network=Network.TESTNET
            )
            return utxo

        ctx.utxo_by_tx_id = script_input
    with pytest.raises(KernelError):
        evaluate_final(tx, ctx)
