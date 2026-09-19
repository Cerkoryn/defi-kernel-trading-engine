"""Full CBOR construction with recorded dependencies and synthetic funding.

Budgets are placeholders: these tests do NOT evaluate Plutus or prove execution.
"""

import json
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest
from charli3_dendrite.backend import get_backend
from charli3_dendrite.dexs.amm import dano
from pycardano import (
    Address,
    ExecutionUnits,
    Network,
    Transaction,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
    VerificationKeyHash,
)
from test_transactions import PROFILE, Context, as_row

from defi_kernel.chain_context import to_utxo
from defi_kernel.dendrite_bridge import DanoSession
from defi_kernel.domain import Asset, KernelError, OutRef
from defi_kernel.signing import (
    Authorization,
    inspect_transaction,
    ref_text,
    value_units,
)
from defi_kernel.slots import SlotClock
from defi_kernel.transactions import (
    CompositionBuilder,
    close_order,
    create_order,
    fill_order,
)

EVIDENCE = json.loads(
    Path("evidence/preprod-composition-dependencies.json").read_text()
)
MARKET = json.loads(Path("examples/preprod-market.json").read_text())
OWNER = Address(
    VerificationKeyHash(b"p" * 28), VerificationKeyHash(b"s" * 28), Network.TESTNET
)


class StructuralContext(Context):
    """Synthetic budgets and wallet; never used by the live CLI."""

    def __init__(self, rows, now, budgets=None):
        self.budgets = budgets or {}
        self.rows = {OutRef(r["tx_hash"], r["tx_index"]): r for r in rows}
        self.slot_clock = SlotClock(
            PROFILE,
            json.loads(Path("evidence/preprod-era-summaries.json").read_text())["data"],
        )
        self.slot_at_ms = self.slot_clock.slot_at_ms
        self.last_block_slot = self.slot_at_ms(int(now * 1000))
        self.epoch = 1000

    def utxo_by_tx_id(self, tx_hash, index):
        row = self.rows.get(OutRef(tx_hash, index))
        return to_utxo(row, PROFILE) if row else None

    def utxos(self, address):
        return []

    def evaluate_tx(self, transaction):
        self.evaluated_candidate = transaction
        return {
            f"{key.tag.name.lower()}:{key.index}": self.budgets.get(
                f"{key.tag.name.lower()}:{key.index}",
                ExecutionUnits(500_000, 200_000_000),
            )
            for key in transaction.transaction_witness_set.redeemer
        }


def make_candidate(now=None, budgets=None, owner=OWNER):
    now = EVIDENCE["observed_at"] if now is None else now
    rows = deepcopy(EVIDENCE["rows"])
    pool_ref = OutRef(MARKET["pool"]["tx_hash"], MARKET["pool"]["tx_index"])
    context = StructuralContext(rows, now, budgets)
    builder = CompositionBuilder(context)
    funding = UTxO(
        TransactionInput.from_primitive([b"\0" * 32, 0]),
        TransactionOutput(owner, Value(100_000_000)),
    )
    collateral = UTxO(
        TransactionInput.from_primitive([b"\1" * 32, 0]),
        TransactionOutput(owner, Value(10_000_000)),
    )
    builder.add_input(funding)
    builder.collaterals.append(collateral)
    base, ada = Asset.from_unit(PROFILE.name, MARKET["base_unit"]), Asset(PROFILE.name)
    initial = create_order(
        CompositionBuilder(context), PROFILE, owner, base, ada, 1_000_000, Fraction(1)
    )
    order = as_row(initial)  # Controlled synthetic Swaps liquidity for composition.
    fill_order(builder, PROFILE, order, 500_000, 500_000)
    session = DanoSession(PROFILE, rows, now=now, rewards=EVIDENCE["rewards"])
    session.contribute(builder, pool_ref, base.unit, 500_000, ada.unit, 1)
    protocol_outputs = tuple(o.to_cbor() for o in builder.outputs)
    body = builder.build(change_address=owner, collateral_change_address=owner)
    tx = Transaction(body, builder.build_witness_set())
    resolved = {
        ref_text(u.input): u
        for u in [*builder.inputs, *builder.reference_inputs, *builder.collaterals]
    }
    authorization = Authorization(
        PROFILE.chain_id,
        PROFILE.wallet_id,
        frozenset(map(ref_text, body.inputs)),
        frozenset(map(ref_text, body.reference_inputs)),
        frozenset(map(ref_text, body.collateral)),
        frozenset([str(owner)]),
        protocol_outputs,
        (("lovelace", -5_000_000, 50_000_000),),
        (),
        tuple(body.withdraws.items()),
        frozenset(map(str, body.required_signers or [])),
        5_000_000,
        8_000_000,
        body.validity_start,
        body.ttl,
    )
    return tx, authorization, context, resolved, builder


def make_individual_candidate(action, budgets=None):
    """Same recorded preprod context; synthetic wallet and maker liquidity."""
    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.utility import asset_to_value

    now = EVIDENCE["observed_at"]
    rows = deepcopy(EVIDENCE["rows"])
    context = StructuralContext(rows, now, budgets)
    builder = CompositionBuilder(context)
    base, ada = Asset.from_unit(PROFILE.name, MARKET["base_unit"]), Asset(PROFILE.name)
    builder.add_input(
        UTxO(
            TransactionInput.from_primitive([b"\0" * 32, 0]),
            TransactionOutput(
                OWNER,
                asset_to_value(
                    Assets(root={"lovelace": 100_000_000, base.unit: 2_500_000})
                ),
            ),
        )
    )
    builder.collaterals.append(
        UTxO(
            TransactionInput.from_primitive([b"\1" * 32, 0]),
            TransactionOutput(OWNER, Value(10_000_000)),
        )
    )
    builder.validity_start, builder.ttl = (
        context.last_block_slot - 120,
        context.last_block_slot + 240,
    )
    if action == "create":
        create_order(builder, PROFILE, OWNER, base, ada, 500_000, Fraction(4))
        create_order(builder, PROFILE, OWNER, ada, base, 2_000_000, Fraction(1, 4))
    elif action in ("fill", "close"):
        initial = create_order(
            CompositionBuilder(context),
            PROFILE,
            OWNER,
            base,
            ada,
            1_000_000,
            Fraction(1),
        )
        if action == "fill":
            fill_order(builder, PROFILE, as_row(initial), 500_000, 500_000)
        else:
            from defi_kernel.transactions import V1

            references = {
                r["reference_script"]["hash"]: to_utxo(r, PROFILE)
                for r in json.loads(
                    Path("evidence/preprod-swaps-v1-scan.json").read_text()
                )["data"]["rows"]
                if r.get("reference_script")
            }
            close_order(
                builder,
                PROFILE,
                as_row(initial),
                OWNER,
                swap_reference=references[V1["script_hash"]],
                beacon_reference=references[V1["beacon_policy"]],
            )
    elif action == "dano":
        session = DanoSession(PROFILE, rows, now=now, rewards=EVIDENCE["rewards"])
        session.contribute(
            builder,
            OutRef(MARKET["pool"]["tx_hash"], MARKET["pool"]["tx_index"]),
            base.unit,
            500_000,
            ada.unit,
            1,
        )
    else:
        raise ValueError("Unknown individual qualification action")
    body = builder.build(change_address=OWNER, collateral_change_address=OWNER)
    tx = Transaction(body, builder.build_witness_set())
    resolved = {
        ref_text(u.input): u
        for u in [*builder.inputs, *builder.reference_inputs, *builder.collaterals]
    }
    return tx, None, context, resolved, builder


def test_composed_cbor_is_balanced_and_indices_resolve_after_sorting():
    tx, auth, context, resolved, builder = make_candidate()
    delta = inspect_transaction(tx, auth, context, resolved)
    assert set(delta) == {"lovelace"}  # Intermediate token nets to zero.
    assert len(tx.transaction_body.inputs) == 3  # wallet + Swaps + Dano
    redeemers = [
        r.data
        for r in builder._redeemer_list
        if isinstance(r.data, dano.DanoSwapRedeemer)
    ]
    assert (
        len(redeemers) == 3
    )  # pool spend, protocol withdrawal, overdue stake withdrawal
    assert all(r.pool_in_idx == 1 for r in redeemers)  # funding sorts first
    assert all(r._entries == [(1, 0, -500_000)] for r in redeemers)
    withdraw = next(r for r in redeemers if r.is_withdraw)
    ordered = sorted(builder.reference_inputs, key=lambda u: ref_text(u.input))
    assert ordered[withdraw.first_byte].input == withdraw.protocol_config_input
    assert tx.to_cbor() == Transaction.from_cbor(tx.to_cbor()).to_cbor()
    # Placeholder evaluation sees already-resolved indices too.
    assert context.evaluated_candidate.transaction_witness_set.redeemer


def test_preprod_wrapper_does_not_mutate_dendrite_mainnet_or_backend():
    import charli3_dendrite.backend as backend

    previous = backend.BACKEND
    script = dano.DANO_POOL_SCRIPT_HASH_MAINNET
    cache = dano.DanoCLMMState._reference_utxo
    make_candidate()
    assert backend.BACKEND is previous
    assert dano.DANO_POOL_SCRIPT_HASH_MAINNET == script
    assert dano.DanoCLMMState._reference_utxo is cache
    if previous is not None:
        assert get_backend() is previous


def test_dano_validity_is_clipped_at_protocol_epoch_boundary():
    boundary = (
        1_647_899_091 + int((EVIDENCE["observed_at"] - 1_647_899_091) // 1800) * 1800
    )
    tx, _, _, _, _ = make_candidate(boundary + 5)
    assert (
        tx.transaction_body.validity_start
        == boundary - PROFILE.system_start - 1_641_600
    )
    tx, _, _, _, _ = make_candidate(boundary + 1790)
    assert (
        tx.transaction_body.ttl
        == boundary + 1800 - 1 - PROFILE.system_start - 1_641_600
    )


@pytest.mark.parametrize(
    "unit,quantity", [("lovelace", 1), (MARKET["base_unit"], 100_000)]
)
def test_dano_rejects_below_pool_minimum_before_contributing_inputs(unit, quantity):
    rows = deepcopy(EVIDENCE["rows"])
    context = StructuralContext(rows, EVIDENCE["observed_at"], None)
    builder = CompositionBuilder(context)
    session = DanoSession(
        PROFILE, rows, now=EVIDENCE["observed_at"], rewards=EVIDENCE["rewards"]
    )
    other = MARKET["base_unit"] if unit == "lovelace" else "lovelace"
    with pytest.raises(KernelError, match="minimum change"):
        session.contribute(
            builder,
            OutRef(MARKET["pool"]["tx_hash"], MARKET["pool"]["tx_index"]),
            unit,
            quantity,
            other,
            1,
        )
    assert not builder.inputs


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("destination", "Unauthorized destination"),
        ("datum", "Unauthorized destination"),
        ("fee", "fee exceeds"),
        ("mint", "mint or burn"),
        ("withdrawal", "withdrawals"),
        ("signer", "required signers"),
        ("collateral", "Collateral loss"),
        ("expiry", "validity interval"),
        ("input", "spending inputs"),
    ],
)
def test_signing_policy_rejects_mutated_final_body(mutation, reason):
    from pycardano import Asset as NativeAsset
    from pycardano import AssetName, MultiAsset, ScriptHash

    tx, auth, context, resolved, _ = make_candidate()
    b = tx.transaction_body
    if mutation == "destination":
        b.outputs[-1].address = Address(
            VerificationKeyHash(b"x" * 28), network=Network.TESTNET
        )
    elif mutation == "datum":
        b.outputs[0].datum = 42
    elif mutation == "fee":
        b.fee = auth.max_fee + 1
    elif mutation == "mint":
        b.mint = MultiAsset({ScriptHash(b"m" * 28): NativeAsset({AssetName(b"x"): 1})})
    elif mutation == "withdrawal":
        b.withdraws[next(iter(b.withdraws))] += 1
    elif mutation == "signer":
        b.required_signers = [VerificationKeyHash(b"x" * 28)]
    elif mutation == "collateral":
        b.total_collateral = auth.max_collateral + 1
    elif mutation == "expiry":
        b.ttl = context.last_block_slot
    elif mutation == "input":
        b.inputs = list(b.inputs)[:-1]
    with pytest.raises(KernelError, match=reason):
        inspect_transaction(tx, auth, context, resolved)


def test_collateral_without_return_charges_full_input_and_enforces_limit():
    tx, auth, context, resolved, _ = make_candidate()
    tx.transaction_body.total_collateral = None
    tx.transaction_body.collateral_return = None
    with pytest.raises(KernelError, match="Collateral loss"):
        inspect_transaction(tx, auth, context, resolved)
    auth = replace(auth, max_collateral=10_000_000)
    inspect_transaction(tx, auth, context, resolved)
    # Token collateral cannot silently disappear when there is no return.
    from pycardano import Asset as CardanoAsset
    from pycardano import AssetName, MultiAsset, ScriptHash

    resolved[next(iter(auth.collateral))].output.amount.multi_asset = MultiAsset(
        {ScriptHash(b"x" * 28): CardanoAsset({AssetName(b"test"): 1})}
    )
    with pytest.raises(KernelError, match="conserve"):
        inspect_transaction(tx, auth, context, resolved)


def test_signing_policy_rejects_network_and_economic_limit_changes():
    tx, auth, context, resolved, _ = make_candidate()
    with pytest.raises(KernelError, match="chain/wallet"):
        inspect_transaction(tx, replace(auth, chain_id="mainnet"), context, resolved)
    with pytest.raises(KernelError, match="Asset delta"):
        inspect_transaction(
            tx,
            replace(auth, asset_delta_limits=(("lovelace", 0, 0),)),
            context,
            resolved,
        )
    assert all(
        type(q) is int
        for q in value_units(tx.transaction_body.outputs[-1].amount).values()
    )


def test_dano_sdk_validation_failure_is_a_row_rejection():
    from pycardano import IndefiniteList, datum_hash

    from defi_kernel.protocols import DANO_PREPROD_HASH, decode_dano

    row = deepcopy(
        next(
            r
            for r in EVIDENCE["rows"]
            if r["tx_hash"] == MARKET["pool"]["tx_hash"]
            and r["tx_index"] == MARKET["pool"]["tx_index"]
        )
    )
    datum = dano.DanoCLMMState.pool_datum_class().from_cbor(
        row["inline_datum"]["bytes"]
    )
    nft = next(a for a in row["asset_list"] if a["policy_id"] == DANO_PREPROD_HASH)
    # A pool-shaped datum claiming its validity NFT as a traded asset passes the
    # outer shape/NFT checks but is rejected by the SDK's NFT extraction.
    datum = replace(
        datum,
        token_y=IndefiniteList(
            [bytes.fromhex(nft["policy_id"]), bytes.fromhex(nft["asset_name"])]
        ),
    )
    row["inline_datum"]["bytes"] = datum.to_cbor_hex()
    row["datum_hash"] = str(datum_hash(datum))
    with pytest.raises(KernelError, match="Invalid Dano pool: NotAPoolError"):
        decode_dano(row, PROFILE, platform_fee_rate=1000)
