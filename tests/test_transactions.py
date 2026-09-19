import json
from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import pytest
from pycardano import Address, Network, VerificationKeyHash, datum_hash

from defi_kernel.chain_context import protocol_parameters, to_utxo
from defi_kernel.config import load_profile
from defi_kernel.domain import Asset, KernelError
from defi_kernel.protocols import SwapsV1Datum, decode_swaps
from defi_kernel.transactions import (
    V1,
    CompositionBuilder,
    close_order,
    create_order,
    fill_order,
)

PROFILE = load_profile(Path("config.example.toml"), "preprod")
PARAMS = json.loads(Path("evidence/preprod-ogmios-parameters.json").read_text())[
    "result"
]


class Context:
    profile = PROFILE
    network = Network.TESTNET
    protocol_param = protocol_parameters(PARAMS)


OWNER = Address(
    VerificationKeyHash(b"p" * 28), VerificationKeyHash(b"s" * 28), Network.TESTNET
)
TOKEN = Asset("preprod", b"t" * 28, b"T")
ADA = Asset("preprod")


def as_row(output):
    from charli3_dendrite.dexs.amm.dano import _value_to_assets

    assets = _value_to_assets(output.amount).root
    return {
        "tx_hash": "f" * 64,
        "tx_index": 2,
        "address": str(output.address),
        "value": str(assets["lovelace"]),
        "is_spent": False,
        "datum_hash": str(datum_hash(output.datum))
        if output.datum is not None
        else None,
        "inline_datum": {"bytes": output.datum.to_cbor_hex()}
        if output.datum is not None
        else None,
        "asset_list": [
            {"policy_id": u[:56], "asset_name": u[56:], "quantity": str(q)}
            for u, q in assets.items()
            if u != "lovelace"
        ],
    }


def test_current_cost_models_and_reference_fee_schedule_not_truncated():
    p = protocol_parameters(PARAMS)
    for language in ("v1", "v2", "v3"):
        assert (
            list(p.cost_models["Plutus" + language.upper()].values())
            == PARAMS["plutusCostModels"]["plutus:" + language]
        )
    assert p.price_step == Fraction(721, 10_000_000)
    assert p.min_fee_reference_scripts["range"] == 25600
    assert p.min_fee_reference_scripts["multiplier"] == 1.2


def test_create_both_sides_accumulates_one_policy_and_preserves_stake():
    b = CompositionBuilder(Context())
    a = create_order(b, PROFILE, OWNER, ADA, TOKEN, 5_000_000, Fraction(2))
    z = create_order(b, PROFILE, OWNER, TOKEN, ADA, 10_000_000, Fraction(3, 5))
    assert a.address.staking_part == OWNER.staking_part == z.address.staking_part
    assert (
        a.address.network == Network.TESTNET
    )  # Network.TESTNET is falsey: never use `or MAINNET`.
    assert a.amount.coin > 5_000_000
    assert len(b._minting_script_to_redeemers) == 1
    assert b._minting_script_to_redeemers[0][1].data.CONSTR_ID == 0
    assert b.required_signers == [OWNER.staking_part]
    assert isinstance(a.datum, SwapsV1Datum)
    assert decode_swaps(as_row(a), PROFILE).offer == ADA


def test_partial_fill_uses_exact_rounding_and_preserves_order_lineage():
    b = CompositionBuilder(Context())
    initial = create_order(b, PROFILE, OWNER, TOKEN, ADA, 10_000_000, Fraction(2, 3))
    row = as_row(initial)
    f = CompositionBuilder(Context())
    output, paid = fill_order(f, PROFILE, row, 1_000_001, 666_668)
    assert paid == 666_668
    parsed = decode_swaps(as_row(output), PROFILE)
    assert parsed.held_offer == 8_999_999
    assert str(parsed.previous) == "f" * 64 + "#2"
    assert output.address == initial.address
    assert f.required_signers is None  # Public fills need no maker signature.
    assert f.mint is None
    assert len(f.inputs) == 1


def test_full_ada_fill_leaves_valid_carrier_and_slippage_is_bounded():
    b = CompositionBuilder(Context())
    initial = create_order(b, PROFILE, OWNER, ADA, TOKEN, 5_000_000, Fraction(2, 3))
    row = as_row(initial)
    f = CompositionBuilder(Context())
    out, paid = fill_order(f, PROFILE, row, 5_000_000, 3_333_334)
    assert out.amount.coin == initial.amount.coin - 5_000_000
    with pytest.raises(KernelError, match="maximum payment"):
        fill_order(CompositionBuilder(Context()), PROFILE, row, 5_000_000, 3_333_333)
    with pytest.raises(KernelError, match="carrier"):
        fill_order(
            CompositionBuilder(Context()), PROFILE, row, initial.amount.coin, 10_000_000
        )


def test_close_needs_correct_stake_authority_and_burns_all_beacons():
    initial = create_order(
        CompositionBuilder(Context()),
        PROFILE,
        OWNER,
        TOKEN,
        ADA,
        10_000_000,
        Fraction(1),
    )
    row = as_row(initial)
    b = CompositionBuilder(Context())
    wrong = Address(OWNER.payment_part, VerificationKeyHash(b"x" * 28), Network.TESTNET)
    with pytest.raises(KernelError, match="does not control"):
        close_order(b, PROFILE, row, wrong)
    b = CompositionBuilder(Context())
    output = close_order(b, PROFILE, row, OWNER)
    assert output.address == OWNER
    assert sum(q for assets in b.mint.values() for q in assets.values()) == -3
    assert OWNER.staking_part in b.required_signers


def test_reference_input_conversion_keeps_script_type_and_hash():
    rows = json.loads(Path("evidence/preprod-swaps-v1-scan.json").read_text())["data"][
        "rows"
    ]
    row = next(
        r
        for r in rows
        if r.get("reference_script")
        and r["reference_script"]["hash"] == V1["script_hash"]
    )
    utxo = to_utxo(row, PROFILE)
    assert utxo.output.script is not None
    bad = deepcopy(row)
    bad["reference_script"]["hash"] = "0" * 56
    with pytest.raises(KernelError, match="hash mismatch"):
        to_utxo(bad, PROFILE)
