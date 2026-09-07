"""Narrow, explicit deployment adapters around pinned Dendrite decoding/math."""

import json
import re
from dataclasses import dataclass
from fractions import Fraction
from importlib.resources import files
from typing import Union

import cbor2
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dexs.amm.dano import DanoCLMMState
from charli3_dendrite.dexs.ob.cardanoswaps import (
    CardanoSwapsRational,
    CardanoSwapsSomeInt,
    CardanoSwapsSomeOutRef,
    CardanoSwapsSwapDatum,
    ask_beacon_name,
    offer_beacon_name,
    pair_beacon_name,
)
from pycardano import Address, Network, PlutusData, ScriptHash

from .domain import Asset, KernelError, Order, OutRef, Unsupported

DEPLOYMENTS = json.loads(
    files("defi_kernel").joinpath("data/deployments.json").read_text()
)
DANO_HASH = "d8b69fc53637bcfadbc4469083f706bc293f4d9d2296646c5ca167bb"
DANO_PREPROD_HASH = "04041c3c6ba87b33f2c9eb7f7dbeae3b26003c3e199d438bb99932a2"
DANO_CONFIG = {
    "mainnet": OutRef(
        "2cafd7c92f7093e5229af274be83dea660b0590b4174bbed79ba662b44fbd1ee", 0
    ),
    "preprod": OutRef(
        "3775af36f485f9c97101ee5b9b360c34f0f8e12186bc9060f358b7fc8ce468a4", 0
    ),
}


class DanoPreprodReadState(DanoCLMMState):
    """Same datum/math with the SDK's preprod NFT policy; never builds transactions."""

    @classmethod
    def dex_policy(cls):
        return [DANO_PREPROD_HASH]

    def swap_utxo(self, *args, **kwargs):
        raise Unsupported(
            "Preprod Dano transaction builder requires network-specific timing and references"
        )


def dano_config(provider):
    if provider.profile.name not in DANO_CONFIG:
        raise Unsupported("Dano deployment is not configured for this network")
    rows = provider.utxos([DANO_CONFIG[provider.profile.name]])
    if len(rows) != 1:
        raise KernelError(
            "Dano protocol config reference unavailable; requalify deployment"
        )
    raw = checked_datum(rows[0], 2)
    rate, fee = cbor2.loads(bytes.fromhex(raw)).value
    if (
        type(rate) is not int
        or type(fee) is not int
        or not 0 <= rate < 10000
        or fee < 0
    ):
        raise KernelError("Invalid protocol configuration")
    return rate, fee


@dataclass
class SwapsV1Datum(PlutusData):
    """Published v1 ABI: ten fields, no expiration. Reuse Dendrite field types."""

    CONSTR_ID = 0
    beacon_id: bytes
    pair_beacon: bytes
    offer_id: bytes
    offer_name: bytes
    offer_beacon: bytes
    ask_id: bytes
    ask_name: bytes
    ask_beacon: bytes
    swap_price: CardanoSwapsRational
    prev_input: Union[CardanoSwapsSomeOutRef, PlutusNone]


def row_assets(row: dict, network: str) -> dict[str, int]:
    def quantity(value):
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            return int(value)
        raise KernelError("Provider quantities must be nonnegative integer base units")

    assets = {"lovelace": quantity(row["value"])}
    for item in row.get("asset_list") or []:
        unit = item["policy_id"] + item["asset_name"]
        Asset.from_unit(network, unit)
        if unit in assets:
            raise KernelError("Duplicate asset in provider response")
        assets[unit] = quantity(item["quantity"])
    if any(q < 0 for q in assets.values()):
        raise KernelError("Negative on-chain quantity")
    return assets


def checked_datum(row, expected_fields):
    if row.get("is_spent") is not False:
        raise KernelError("Candidate is spent or spend status is unverified")
    raw = (row.get("inline_datum") or {}).get("bytes")
    if not raw:
        if row.get("reference_script"):
            raise KernelError(
                "Reference-script output without an inline order/pool datum"
            )
        raise KernelError("Candidate has no inline datum")
    data = cbor2.loads(bytes.fromhex(raw))
    if (
        not isinstance(data, cbor2.CBORTag)
        or data.tag != 121
        or len(data.value) != expected_fields
    ):
        raise KernelError("Datum does not match the selected deployment schema")
    # Hash original bytes: decoded/re-encoded CBOR need not be byte-identical.
    from hashlib import blake2b

    if row.get("datum_hash") != blake2b(bytes.fromhex(raw), digest_size=32).hexdigest():
        raise KernelError("Datum hash mismatch")
    return raw


def checked_address(row, profile, script_hash):
    address = Address.from_primitive(row["address"])
    category = (
        Network.MAINNET if profile.address_network == "mainnet" else Network.TESTNET
    )
    if (
        address.network != category
        or not isinstance(address.payment_part, ScriptHash)
        or str(address.payment_part) != script_hash
    ):
        raise KernelError("Address does not match selected deployment/network")
    return address


def decode_swaps(row, profile, deployment="swaps-v1") -> Order:
    manifest = DEPLOYMENTS[deployment]
    address = checked_address(row, profile, manifest["script_hash"])
    if deployment == "swaps-v2":
        raise Unsupported(
            "Published V3 swaps decoding is unqualified: OutputReference ABI differs from Dendrite's historical V2 datum"
        )
    is_v1 = deployment == "swaps-v1"
    raw = checked_datum(row, 10 if is_v1 else 11)
    # This is a beacon-policy address requirement, not a registration/delegation
    # requirement. An undelegated key credential is valid. See pinned v1
    # one_way_swap/utils.ak:343 (is_some(stake)).
    if address.staking_part is None:
        raise KernelError(
            "Selected Swaps beacon policy requires an address staking credential; delegation is not required"
        )
    datum = (SwapsV1Datum if is_v1 else CardanoSwapsSwapDatum).from_cbor(raw)
    offer = Asset(profile.name, datum.offer_id, datum.offer_name)
    ask = Asset(profile.name, datum.ask_id, datum.ask_name)
    if (
        offer == ask
        or datum.swap_price.numerator <= 0
        or datum.swap_price.denominator <= 0
    ):
        raise KernelError("Invalid order price or pair")
    policy = manifest["beacon_policy"]
    expected = (
        pair_beacon_name(offer.policy, offer.name, ask.policy, ask.name),
        offer_beacon_name(offer.policy, offer.name),
        ask_beacon_name(ask.policy, ask.name),
    )
    if datum.beacon_id.hex() != policy or expected != (
        datum.pair_beacon,
        datum.offer_beacon,
        datum.ask_beacon,
    ):
        raise KernelError("Datum beacon identity mismatch")
    assets = row_assets(row, profile.name)
    actual = {unit: q for unit, q in assets.items() if unit.startswith(policy)}
    if actual != {policy + name.hex(): 1 for name in expected}:
        raise KernelError("Missing, extra or invalid beacon tokens")
    allowed = {"lovelace", offer.unit, ask.unit, *actual}
    if any(unit not in allowed and q != 0 for unit, q in assets.items()):
        raise KernelError("Extraneous order assets")
    prev = None
    if isinstance(datum.prev_input, CardanoSwapsSomeOutRef):
        p = datum.prev_input.value
        prev = OutRef(p.transaction_id.tx_hash.hex(), p.output_index)
    return Order(
        OutRef(row["tx_hash"], row["tx_index"]),
        str(address),
        offer,
        ask,
        Fraction(datum.swap_price.numerator, datum.swap_price.denominator),
        assets.get(offer.unit, 0),
        assets["lovelace"],
        raw,
        prev,
        datum.expiration.value
        if not is_v1 and isinstance(datum.expiration, CardanoSwapsSomeInt)
        else None,
    )


def dendrite_row(row, profile):
    """The legacy Dendrite field block_index means height in its db-sync backend.

    Koios does not return a block hash here. Never invent one to fill a model.
    The runtime Observation retains the actual available provenance separately.
    """
    return {
        "address": row["address"],
        "tx_hash": row["tx_hash"],
        "tx_index": row["tx_index"],
        "datum_hash": row["datum_hash"],
        "datum_cbor": row["inline_datum"]["bytes"],
        "assets": row_assets(row, profile.name),
        "block_time": row["block_time"],
        "block_index": row["block_height"],
        "plutus_v2": True,
    }


def decode_dano(row, profile, *, platform_fee_rate: int):
    if profile.name not in DANO_CONFIG:
        raise Unsupported("Dano deployment is not qualified for this network")
    checked_address(
        row, profile, DANO_HASH if profile.name == "mainnet" else DANO_PREPROD_HASH
    )
    # Non-pool records can share the payment credential (e.g. LP positions).
    checked_datum(row, 12)
    cls = DanoCLMMState if profile.name == "mainnet" else DanoPreprodReadState
    values = dendrite_row(row, profile)
    datum = cls.pool_datum_class().from_cbor(values["datum_cbor"])
    for unit in (datum.unit_x, datum.unit_y):
        Asset.from_unit(profile.name, unit)
        values["assets"].setdefault(unit, 0)  # A depleted side is omitted by Koios.
    if datum.unit_x == datum.unit_y:
        raise KernelError("Pool assets must differ")
    policy = DANO_HASH if profile.name == "mainnet" else DANO_PREPROD_HASH
    nfts = {u: q for u, q in values["assets"].items() if u.startswith(policy)}
    if len(nfts) != 1 or next(iter(nfts.values())) != 1:
        raise KernelError("Pool validity NFT missing or ambiguous")
    state = cls.model_validate(values)
    if not 0 <= platform_fee_rate < 10000:
        raise KernelError("Invalid Dano protocol fee")
    state.platform_fee_rate = platform_fee_rate
    if min(state.reserve_a, state.reserve_b) < 0:
        raise KernelError("Negative active pool reserves")
    return state
