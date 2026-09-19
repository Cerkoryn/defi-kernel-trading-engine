"""Direct venue conservation and composition using recorded Preprod scripts.

Structural budgets here are not Plutus execution evidence. The qualification
harness evaluates these same candidates against hosted Ogmios separately.
"""

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import cbor2
import pytest
from pycardano import Address, TransactionInput, TransactionOutput, UTxO
from test_composition import EVIDENCE as OLD
from test_composition import OWNER, StructuralContext
from test_transactions import PROFILE

from defi_kernel.chain_context import protocol_parameters, to_utxo
from defi_kernel.domain import KernelError, OutRef
from defi_kernel.execution import build_transaction
from defi_kernel.protocols import DANO_PREPROD_HASH
from defi_kernel.signing import (
    decode_candidate,
    inspect_transaction,
    ref_text,
    value_units,
)
from defi_kernel.slots import SlotClock
from defi_kernel.transactions import CompositionBuilder
from defi_kernel.venues import _ratio, contribute_direct, decode_direct

EVIDENCE = json.loads(Path("evidence/preprod-direct-venues.json").read_text())
VENUES = {
    "swaps-two-way": "swaps-v1-two-way",
    "splash-0": "splash",
    "splash-1": "splash",
    "genius": "genius-yield",
    "saturn": "saturnswap",
}


def make_direct_candidate(key, *, direction=0, full=False, count=1, budgets=None):
    rows = deepcopy(EVIDENCE["references"]) + [deepcopy(EVIDENCE["config"])]
    selected = deepcopy(EVIDENCE["rows"][key][:count])
    rows += selected
    ctx = StructuralContext(rows, OLD["observed_at"], budgets)
    ctx.slot_clock = SlotClock(PROFILE, EVIDENCE["era_summaries"])
    ctx.slot_at_ms = ctx.slot_clock.slot_at_ms
    ctx.last_block_slot = EVIDENCE["tip"]["abs_slot"]
    ctx.protocol_param = protocol_parameters(EVIDENCE["protocol_parameters"])
    builder = CompositionBuilder(ctx)
    builder.validity_start = ctx.last_block_slot - 30
    builder.ttl = ctx.last_block_slot + 120
    net = Counter()
    for row in selected:
        state = decode_direct(
            VENUES[key], row, PROFILE, ctx, config_row=EVIDENCE["config"]
        )
        edge = state.edges()[direction]
        amount = 10**15 if full else 1_000_000
        if key == "genius" and not full:
            amount = max(
                1,
                (state.datum.offered_amount // 3)
                * state.datum.price.numerator
                // state.datum.price.denominator,
            )
        if key == "saturn" and not full:
            amount = max(state.datum.min_partial_fill, state.datum.amount_buy * 2 // 3)
        quote = edge.quote(amount)
        assert quote is not None
        contribute_direct(
            builder,
            state,
            quote,
            {OutRef(r["tx_hash"], r["tx_index"]): r for r in rows},
        )
    for u in builder.inputs:
        net.update(value_units(u.output.amount))
    for o in builder.outputs:
        net.subtract(value_units(o.amount))
    for p, names in (builder.mint or {}).items():
        for name, q in names.items():
            net[str(p) + bytes(name).hex()] += q
    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.utility import asset_to_value

    funding = UTxO(
        TransactionInput.from_primitive([b"\x11" * 32, 0]),
        TransactionOutput(
            OWNER,
            asset_to_value(
                Assets(
                    root={
                        "lovelace": max(100_000_000, -net["lovelace"] + 10_000_000),
                        **{u: -q for u, q in net.items() if u != "lovelace" and q < 0},
                    }
                )
            ),
        ),
    )
    collateral = UTxO(
        TransactionInput.from_primitive([b"\x12" * 32, 0]),
        TransactionOutput(OWNER, 10_000_000),
    )
    builder.add_input(funding)
    builder.collaterals.append(collateral)
    deltas = [
        (u, q - 3_000_000 if u == "lovelace" else q, q)
        for u, q in net.items()
        if q or u == "lovelace"
    ]
    if "lovelace" not in net:
        deltas.append(("lovelace", -3_000_000, 0))
    tx, auth, meta = build_transaction(
        builder, OWNER, deltas, {}, max_fee=3_000_000, max_collateral=5_000_000
    )
    resolved = {
        ref_text(u.input): u
        for u in [*builder.inputs, *builder.reference_inputs, *builder.collaterals]
    }
    return tx, auth, meta, resolved, ctx, builder


@pytest.mark.parametrize("key", list(VENUES))
@pytest.mark.parametrize("count", [1, 2])
def test_direct_fills_preserve_authorized_value_and_resolve_indices(key, count):
    tx, auth, _, resolved, ctx, builder = make_direct_candidate(key, count=count)
    inspect_transaction(tx, auth, ctx, resolved)
    assert decode_candidate(tx.to_cbor()).to_cbor() == tx.to_cbor()
    for data, utxo, output in getattr(builder, "_kernel_indexed_spends", []):
        index = data.input_index if output is not None else data.self_index
        assert tx.transaction_body.inputs[index] == utxo.input
        if output is not None:
            assert (
                tx.transaction_body.outputs[data.output_index].to_cbor()
                == output.to_cbor()
            )
    poisoned = deepcopy(tx)
    poisoned.transaction_body.outputs[0].amount.coin -= 1
    with pytest.raises(KernelError):
        inspect_transaction(poisoned, auth, ctx, resolved)


@pytest.mark.parametrize("key", ["genius", "saturn"])
def test_complete_fills(key):
    tx, auth, _, resolved, ctx, _ = make_direct_candidate(key, full=True, count=2)
    inspect_transaction(tx, auth, ctx, resolved)
    if key == "genius":
        assert (
            sum(
                q for names in tx.transaction_body.mint.values() for q in names.values()
            )
            == -2
        )


@pytest.mark.parametrize("key", ["swaps-two-way", "splash-0", "splash-1"])
def test_reverse_direction(key):
    tx, auth, _, resolved, ctx, _ = make_direct_candidate(key, direction=1)
    inspect_transaction(tx, auth, ctx, resolved)


def test_saturn_ratio_preserves_double_ceiling_above_float_precision():
    assert _ratio(3, 1, 10**18) == 333333333334000000


def test_direct_deployment_and_datum_identity_fail_closed():
    _, _, _, _, ctx, _ = make_direct_candidate("splash-0")
    row = deepcopy(EVIDENCE["rows"]["splash-0"][0])
    row["datum_hash"] = "00" * 32
    with pytest.raises(KernelError, match="hash"):
        decode_direct("splash", row, PROFILE, ctx)
    row = deepcopy(EVIDENCE["rows"]["splash-0"][0])
    row["address"] = str(OWNER)
    with pytest.raises(KernelError, match="deployment"):
        decode_direct("splash", row, PROFILE, ctx)


def make_mixed_candidate(budgets=None, include_dano=False):
    """Synthetic four-hop ADA cycle using all four real deployed validators."""
    from hashlib import sha256
    from types import SimpleNamespace

    from charli3_dendrite.dataclasses.datums import AssetClass, PlutusNone
    from charli3_dendrite.dataclasses.models import Assets
    from charli3_dendrite.utility import asset_to_value
    from test_transactions import as_row

    from defi_kernel.arbitrage import ArbitrageConfig, RoutePlan
    from defi_kernel.arbitrage_runtime import build_route
    from defi_kernel.venues import GENIUS_POLICY, TWO_WAY_POLICY

    _, _, _, _, ctx, _ = make_direct_candidate("swaps-two-way", budgets=budgets)
    a, b, c = (AssetClass(bytes([i]) * 28, bytes([i])) for i in (10, 11, 12))
    rows = deepcopy(EVIDENCE["references"]) + [deepcopy(EVIDENCE["config"])]
    base = None
    dano_state = None
    if include_dano:
        from pycardano import datum_hash

        from defi_kernel.arbitrage import Edge
        from defi_kernel.dendrite_bridge import protocol_epoch
        from defi_kernel.protocols import DANO_PREPROD_HASH, decode_dano

        now = ctx.slot_clock.time_at_slot(ctx.last_block_slot) / 1000
        rows.extend(deepcopy(OLD["rows"]))
        dano_row = next(
            r
            for r in rows
            if str(Address.from_primitive(r["address"]).payment_part)
            == DANO_PREPROD_HASH
            and r.get("inline_datum")
            and len(cbor2.loads(bytes.fromhex(r["inline_datum"]["bytes"])).value) == 12
        )
        dano_state = decode_dano(dano_row, PROFILE, platform_fee_rate=1000)
        datum = deepcopy(dano_state._datum)
        datum.last_withdraw_epoch = protocol_epoch(PROFILE, int(now * 1000))
        dano_row["datum_hash"] = str(datum_hash(datum))
        dano_row["inline_datum"] = {"bytes": datum.to_cbor_hex()}
        dano_state = decode_dano(dano_row, PROFILE, platform_fee_rate=1000)
        base = AssetClass.from_assets(Assets(root={dano_state.unit_b: 1}))
    synthetic = []
    for i, key in enumerate(("swaps-two-way", "splash-0", "genius", "saturn")):
        original = EVIDENCE["rows"][key][0]
        state = decode_direct(
            VENUES[key], original, PROFILE, ctx, config_row=EVIDENCE["config"]
        )
        d = deepcopy(state.datum)
        if key == "swaps-two-way":
            d.asset1_id = d.asset1_name = b""
            d.asset2_id, d.asset2_name = a.policy, a.asset_name
            d.pair_beacon = sha256(b"\0" + a.policy + a.asset_name).digest()
            d.asset1_beacon = sha256(b"").digest()
            d.asset2_beacon = sha256(a.policy + a.asset_name).digest()
            d.asset1_price.numerator = d.asset1_price.denominator = 1
            d.asset2_price.numerator, d.asset2_price.denominator = 1, 2
            d.prev_input = PlutusNone()
            assets = {
                "lovelace": 20_000_000,
                a.assets.unit(): 100_000_000,
                **{
                    TWO_WAY_POLICY + n.hex(): 1
                    for n in (d.pair_beacon, d.asset1_beacon, d.asset2_beacon)
                },
            }
        elif key == "splash-0":
            d.asset_x, d.asset_y = a, b
            d.treasury_x = d.treasury_y = d.lq_bound = 0
            assets = {
                "lovelace": 3_000_000,
                a.assets.unit(): 1_000_000_000,
                b.assets.unit(): 4_000_000_000,
                d.pool_nft.assets.unit(): 1,
                d.lp_token.assets.unit(): state.assets[d.lp_token.assets.unit()],
            }
        elif key == "genius":
            d.offered_asset, d.asked_asset = c, b
            d.offered_original_amount = d.offered_amount = 1_000_000_000
            d.price.numerator = d.price.denominator = 1
            d.start_time = d.end_time = PlutusNone()
            d.partial_fills = d.contained_payment = 0
            d.contained_fee.lovelaces = d.contained_fee.asked_tokens = (
                d.contained_fee.offered_tokens
            ) = 0
            assets = {
                "lovelace": 5_000_000,
                c.assets.unit(): d.offered_amount,
                GENIUS_POLICY + d.nft.hex(): 1,
            }
        else:
            d.policy_id_sell = d.asset_name_sell = b""
            if include_dano:
                d.policy_id_sell, d.asset_name_sell = base.policy, base.asset_name
            d.policy_id_buy, d.asset_name_buy = c.policy, c.asset_name
            d.amount_sell, d.amount_buy = 200_000_000, 100_000_000
            d.min_partial_fill = 0
            d.valid_before_time = PlutusNone()
            assets = {"lovelace": 201_000_000}
            if include_dano:
                d.amount_sell = 40_000_000
                assets = {"lovelace": 3_000_000, base.assets.unit(): d.amount_sell}
        output = TransactionOutput(
            Address.from_primitive(original["address"]),
            asset_to_value(Assets(root=assets)),
            datum=d,
        )
        row = as_row(output)
        row["tx_hash"] = bytes([30 + i]).hex() * 32
        row["tx_index"] = 0
        row["block_height"] = ctx.last_block_slot
        synthetic.append(row)
        rows.append(row)
    by_ref = {OutRef(r["tx_hash"], r["tx_index"]): r for r in rows}
    budget = 2_000_000
    unit = "lovelace"
    hops = []
    for key, row in zip(("swaps-two-way", "splash-0", "genius", "saturn"), synthetic):
        state = decode_direct(
            VENUES[key], row, PROFILE, ctx, config_row=EVIDENCE["config"]
        )
        edge = next(e for e in state.edges() if e.input_unit == unit)
        quote = edge.quote(budget)
        assert quote is not None and quote.amount_in == budget
        hops.append(quote)
        unit, budget = quote.output_unit, quote.amount_out
    if include_dano:
        quote = Edge(
            OutRef(dano_row["tx_hash"], dano_row["tx_index"]),
            "dano",
            unit,
            "lovelace",
            10**15,
            pool=dano_state,
            fixed_fee=100_000,
        ).quote(budget)
        assert quote is not None and quote.amount_in == budget
        hops.append(quote)
    funding = as_row(TransactionOutput(OWNER, 20_000_000))
    funding["tx_hash"] = "11" * 32
    collateral = as_row(TransactionOutput(OWNER, 5_000_000))
    collateral["tx_hash"] = "12" * 32
    ctx.rows = {
        **by_ref,
        OutRef(funding["tx_hash"], 2): funding,
        OutRef(collateral["tx_hash"], 2): collateral,
    }
    now = ctx.slot_clock.time_at_slot(ctx.last_block_slot) / 1000
    cfg = ArbitrageConfig(
        (
            "lovelace",
            a.assets.unit(),
            b.assets.unit(),
            c.assets.unit(),
            *([base.assets.unit()] if include_dano else []),
        ),
        max_hops=5 if include_dano else 4,
    )
    plan = RoutePlan(tuple(hops), 2_000_000, 1)
    tx, auth, meta, deps = build_route(
        SimpleNamespace(profile=PROFILE, clock=lambda: now),
        cfg,
        OWNER,
        plan,
        by_ref,
        {},
        now,
        [funding],
        collateral,
        ctx,
    )
    resolved = {ref_text(to_utxo(r, PROFILE).input): to_utxo(r, PROFILE) for r in deps}
    return tx, auth, meta, resolved, ctx, None


def test_all_four_venues_settle_in_one_body_without_intermediate_funding():
    tx, auth, meta, resolved, ctx, _ = make_mixed_candidate()
    delta = inspect_transaction(tx, auth, ctx, resolved)
    assert delta["lovelace"] >= 100_000 and len(delta) == 1
    assert len(meta["hops"]) == 4
    assert {h["venue"] for h in meta["hops"]} == set(VENUES.values())
    for u in resolved.values():
        if u.output.address == OWNER:
            assert set(value_units(u.output.amount)) == {"lovelace"}


def test_new_venues_compose_with_dano_leading_outputs():
    tx, auth, meta, resolved, ctx, _ = make_mixed_candidate(include_dano=True)
    assert len(meta["hops"]) == 5
    assert str(tx.transaction_body.outputs[0].address.payment_part) == DANO_PREPROD_HASH
    assert set(inspect_transaction(tx, auth, ctx, resolved)) == {"lovelace"}


@pytest.mark.parametrize(
    "changed",
    [
        {"venue": "dano"},
        {"ref": OutRef("ff" * 32, 0)},
        {"output_unit": "ff" * 28},
        {"amount_out": 1},
    ],
)
def test_contribution_rechecks_quote_identity_before_mutating_builder(changed):
    from dataclasses import replace

    _, _, _, _, ctx, _ = make_direct_candidate("splash-0")
    state = decode_direct("splash", EVIDENCE["rows"]["splash-0"][0], PROFILE, ctx)
    quote = state.edges()[0].quote(1_000_000)
    builder = CompositionBuilder(ctx)
    with pytest.raises(KernelError, match="quote"):
        contribute_direct(builder, state, replace(quote, **changed), {})
    assert not builder.inputs and not builder.outputs


def test_discovery_enables_all_new_venues_and_propagates_provider_failure():
    from types import SimpleNamespace

    from defi_kernel.arbitrage import ArbitrageConfig
    from defi_kernel.arbitrage_runtime import Liquidity
    from defi_kernel.providers import ProviderError
    from defi_kernel.venues import GENIUS_CONFIG_HASH, NEW_VENUES

    _, _, _, _, ctx, _ = make_direct_candidate("genius")
    ctx._tip = EVIDENCE["tip"]
    rows = [r for group in EVIDENCE["rows"].values() for r in group]
    assets = {"lovelace"}
    for key, group in EVIDENCE["rows"].items():
        for row in group:
            state = decode_direct(
                VENUES[key], row, PROFILE, ctx, config_row=EVIDENCE["config"]
            )
            assets.update((state.unit_a, state.unit_b))
    config = ArbitrageConfig(tuple(sorted(assets)), venues=NEW_VENUES)
    now = ctx.slot_clock.time_at_slot(ctx.last_block_slot) / 1000

    def observation(credential):
        selected = (
            [EVIDENCE["config"]]
            if credential == GENIUS_CONFIG_HASH
            else [
                r
                for r in rows
                if str(Address.from_primitive(r["address"]).payment_part) == credential
            ]
        )
        return SimpleNamespace(rows=selected, complete=True, observed_at=now)

    provider = SimpleNamespace(
        profile=PROFILE,
        clock=lambda: now,
        credential_utxos=observation,
        resolve_datums=lambda rows: rows,
        stake_rewards_many=lambda _: {},
        reference_scripts=lambda hashes: {
            r["reference_script"]["hash"]: r
            for r in EVIDENCE["references"]
            if r["reference_script"]["hash"] in hashes
        },
    )
    edges, _, _, _, rejected = Liquidity().observe(provider, config, OWNER, ctx)
    assert {e.venue for e in edges} == set(NEW_VENUES), rejected

    def failed(rows):
        raise ProviderError("Datum service unavailable")

    provider.resolve_datums = failed
    with pytest.raises(ProviderError, match="Datum service"):
        Liquidity().observe(provider, config, OWNER, ctx)


def test_all_example_strategies_enable_four_new_venues_without_changing_assets():
    from defi_kernel.arbitrage import ArbitrageConfig
    from defi_kernel.venues import ALL_VENUES

    for path in Path("examples").glob("preprod-arbitrage*.json"):
        cfg = ArbitrageConfig.load(path, PROFILE)
        assert cfg.venues == ALL_VENUES
    with pytest.raises(KernelError, match="supported deployments"):
        ArbitrageConfig(("lovelace", "ff" * 28), venues=("unsupported",))


def test_saved_plutus_evaluations_still_cover_current_candidates():
    from pycardano import ExecutionUnits, Transaction

    report = json.loads(
        Path("evidence/preprod-direct-venue-evaluation.json").read_text()
    )
    assert len(report["samples"]) == 17
    for sample in report["samples"]:
        assert sample["stage"] == "final_evaluated"
        measured = {
            f"{r['validator']['purpose'].replace('withdraw', 'withdrawal')}:{r['validator']['index']}": ExecutionUnits(
                r["budget"]["memory"], r["budget"]["cpu"]
            )
            for r in sample["measured_budgets"]
        }
        saved = Transaction.from_cbor(sample["unsigned_cbor"])
        assigned = {
            f"{k.tag.name.lower()}:{k.index}": v.ex_units
            for k, v in saved.transaction_witness_set.redeemer.items()
        }
        # Current construction must reproduce the exact body and witnesses that
        # passed hosted evaluation, not merely another structurally valid fill.
        args = {k: sample[k] for k in ("direction", "full", "count") if k in sample}
        if sample["venue"] in ("mixed", "mixed-dano"):
            current, auth, _, resolved, ctx, _ = make_mixed_candidate(
                budgets=measured, include_dano=sample["venue"] == "mixed-dano"
            )
        else:
            current, auth, _, resolved, ctx, _ = make_direct_candidate(
                sample["venue"], budgets=measured, **args
            )
        assert current.to_cbor_hex() == sample["unsigned_cbor"]
        assert set(assigned) == set(measured)
        assert all(
            assigned[k].mem >= v.mem and assigned[k].steps >= v.steps
            for k, v in measured.items()
        )
        assert saved.to_cbor_hex() == sample["request"]["transaction"]["cbor"]
        assert saved.transaction_body.fee == sample["fee_lovelace"]
        inspect_transaction(saved, auth, ctx, resolved)


@pytest.mark.parametrize(
    "case", ["negative_fee", "wrong_nft", "expired", "covered", "fee_owner"]
)
def test_direct_order_boundaries_fail_closed(case):
    from charli3_dendrite.dataclasses.datums import PlutusFullAddress
    from charli3_dendrite.dexs.ob.geniusyield import GeniusTimestamp
    from charli3_dendrite.dexs.ob.saturnswap import (
        SaturnSwapCoverage,
        SaturnSwapSomeCoverage,
    )
    from pycardano import datum_hash

    from defi_kernel.venues import SATURN_FEE_ADDRESS

    key = "genius" if case in ("negative_fee", "wrong_nft", "expired") else "saturn"
    _, _, _, _, ctx, _ = make_direct_candidate(key)
    row = deepcopy(EVIDENCE["rows"][key][0])
    state = decode_direct(VENUES[key], row, PROFILE, ctx, config_row=EVIDENCE["config"])
    d = deepcopy(state.datum)
    if case == "negative_fee":
        d.taker_lovelace_fee = -1
    elif case == "wrong_nft":
        d.nft = b"\xff" * 32
    elif case == "expired":
        d.end_time = GeniusTimestamp(
            ctx.slot_clock.time_at_slot(ctx.last_block_slot - 60)
        )
    elif case == "covered":
        d.coverage = SaturnSwapSomeCoverage(
            SaturnSwapCoverage(d.owner, 100, d.output_reference)
        )
    else:
        d.owner = PlutusFullAddress.from_address(
            Address.from_primitive(SATURN_FEE_ADDRESS)
        )
    row["datum_hash"] = str(datum_hash(d))
    if row.get("inline_datum"):
        row["inline_datum"] = {"bytes": d.to_cbor_hex()}
    else:
        row["datum_cbor"] = d.to_cbor_hex()
    if case != "expired":
        with pytest.raises(KernelError):
            decode_direct(VENUES[key], row, PROFILE, ctx, config_row=EVIDENCE["config"])
    else:
        state = decode_direct(
            VENUES[key], row, PROFILE, ctx, config_row=EVIDENCE["config"]
        )
        builder = CompositionBuilder(ctx)
        builder.validity_start, builder.ttl = (
            ctx.last_block_slot - 30,
            ctx.last_block_slot + 120,
        )
        with pytest.raises(KernelError, match="validity interval"):
            contribute_direct(builder, state, state.edges()[0].quote(1_000_000), {})
        assert not builder.inputs
