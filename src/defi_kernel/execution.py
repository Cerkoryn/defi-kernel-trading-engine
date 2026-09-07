"""Final evaluation gate shared by individual and composed transactions."""

from dataclasses import dataclass
from hashlib import sha256

from .domain import KernelError


@dataclass(frozen=True)
class EvaluationReceipt:
    chain_id: str
    transaction_digest: str
    observed_at: float
    provider: str
    budgets: tuple[tuple[str, int, int], ...]


def evaluate_final(transaction, context):
    """Evaluate the complete finalized candidate, never just its separate legs.

    PyCardano evaluates a provisional body while balancing. This additional pass
    rejects changed costs that exceed the assigned budgets; the caller must make
    a fresh candidate and reauthorize any changed fee/output. It never signs.
    """
    cbor = transaction.to_cbor()
    redeemers = transaction.transaction_witness_set.redeemer
    if not redeemers:
        from pycardano import VerificationKeyHash

        body, witness = (
            transaction.transaction_body,
            transaction.transaction_witness_set,
        )
        if any(
            (
                body.mint,
                body.withdraws,
                body.certificates,
                body.reference_inputs,
                witness.native_scripts,
                witness.plutus_v1_script,
                witness.plutus_v2_script,
                witness.plutus_v3_script,
            )
        ):
            raise KernelError("Unbudgeted script or authorization purpose")
        context.provider.verify_identity()
        for tx_input in body.inputs:
            utxo = context.utxo_by_tx_id(str(tx_input.transaction_id), tx_input.index)
            if utxo is None or not isinstance(
                utxo.output.address.payment_part, VerificationKeyHash
            ):
                raise KernelError(
                    "No-script evaluation path requires verified payment-key inputs"
                )
        return EvaluationReceipt(
            context.profile.chain_id,
            sha256(cbor).hexdigest(),
            context.provider.clock(),
            context.profile.koios_url,
            (),
        )
    if not redeemers or not hasattr(redeemers, "items"):
        raise KernelError("Final evaluation requires indexed Plutus redeemers")
    expected = {
        f"{key.tag.name.lower()}:{key.index}": value.ex_units
        for key, value in redeemers.items()
    }
    budgets = context.evaluate_tx_cbor(cbor)
    if set(budgets) != set(expected):
        raise KernelError(
            "Final evaluation did not cover exactly the planned redeemers"
        )
    for key, units in budgets.items():
        allowed = expected[key]
        if units.mem > allowed.mem or units.steps > allowed.steps:
            raise KernelError(
                "Final evaluation exceeds assigned execution budget; rebuild and reauthorize"
            )
    params = context.protocol_param
    if (
        sum(u.mem for u in expected.values()) > params.max_tx_ex_mem
        or sum(u.steps for u in expected.values()) > params.max_tx_ex_steps
    ):
        raise KernelError("Assigned execution budgets exceed transaction limits")
    if transaction.to_cbor() != cbor:
        raise KernelError("Candidate changed during final evaluation")
    return EvaluationReceipt(
        context.profile.chain_id,
        sha256(cbor).hexdigest(),
        context.provider.clock(),
        context.profile.koios_url,
        tuple(sorted((key, u.mem, u.steps) for key, u in budgets.items())),
    )


def prepare_transaction(
    provider,
    journal,
    builder,
    owner,
    wallet,
    key_dir,
    intent,
    deltas,
    metadata,
    dependencies,
    *,
    max_fee,
    max_collateral,
):
    """One finalization/signing path for strategy actions and the test harness."""
    from pycardano import Transaction, VerificationKeyHash

    from .coordinator import Coordinator
    from .signing import Authorization, LocalSigner, ref_text

    outputs = tuple(o.to_cbor() for o in builder.outputs)
    mint = tuple(
        (str(p) + bytes(n).hex(), q)
        for p, names in (builder.mint or {}).items()
        for n, q in names.items()
        if q
    )
    withdrawals = tuple((builder.withdrawals or {}).items())
    expected_signers = builder.required_signers
    if expected_signers is None and builder.all_scripts:
        # PyCardano adds payment input signers when no explicit list exists.
        # Authorize that known behavior before build, not arbitrary body changes.
        expected_signers = [
            u.output.address.payment_part
            for u in [*builder.inputs, *builder.collaterals]
            if isinstance(u.output.address.payment_part, VerificationKeyHash)
        ]
    required_signers = frozenset(map(str, expected_signers or []))
    start, ttl = builder.validity_start, builder.ttl
    profile = provider.profile
    # Coin selection is complete in both callers. Freeze its input roles before
    # build, so balancing cannot authorize additional or substituted funds.
    authorization = Authorization(
        profile.chain_id,
        profile.wallet_id,
        frozenset(ref_text(u.input) for u in builder.inputs),
        frozenset(ref_text(u.input) for u in builder.reference_inputs),
        frozenset(ref_text(u.input) for u in builder.collaterals),
        frozenset([str(owner)]),
        outputs,
        tuple(deltas),
        mint,
        withdrawals,
        required_signers,
        max_fee,
        max_collateral,
        start,
        ttl,
    )
    body = builder.build(change_address=owner, collateral_change_address=owner)
    transaction = Transaction(body, builder.build_witness_set())
    metadata = {**metadata, "fee_lovelace": body.fee, "expires_slot": body.ttl}
    signer = LocalSigner(key_dir / wallet["payment_key"], key_dir / wallet["stake_key"])
    txid = Coordinator(provider, journal).prepare(
        intent,
        transaction,
        authorization,
        builder.context,
        dependencies,
        signer,
        metadata,
    )
    return {
        "intent": intent,
        "txid": txid,
        "fee_lovelace": body.fee,
        "expires_slot": body.ttl,
    }
