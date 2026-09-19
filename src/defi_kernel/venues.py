"""Direct protocol fills contributing to the same caller-owned atomic transaction.

Datums come from pinned Dendrite codecs; arithmetic and output construction follow
the deployment-specific sources in docs/venues.md. No global SDK backend or clock.
Conversion/context gaps: https://github.com/Charli3-Official/charli3-dendrite/issues/222
and https://github.com/Charli3-Official/charli3-dendrite/issues/224 (originally Dano).
"""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from fractions import Fraction
from hashlib import sha256
from importlib.resources import files

import cbor2
from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.splash import SplashCPPPoolDatum
from charli3_dendrite.dexs.ob.geniusyield import (
    GeniusCompleteRedeemer,
    GeniusSubmitRedeemer,
    GeniusTimestamp,
    GeniusTxRef,
    GeniusUTxORef,
    GeniusYieldFeeDatum,
    GeniusYieldOrder,
    GeniusYieldSettings,
)
from charli3_dendrite.dexs.ob.saturnswap import (
    SaturnSwapOutputReferenceV3,
    SaturnSwapPaymentDatumV3,
    SaturnSwapSwapDatumV3,
)
from charli3_dendrite.utility import asset_to_value
from pycardano import (
    Address,
    PlutusData,
    PlutusV3Script,
    RawPlutusData,
    Redeemer,
    TransactionOutput,
    plutus_script_hash,
)
from pycardano.serialization import RawCBOR
from pycardano.utils import min_lovelace

from .chain_context import to_utxo
from .domain import Asset, KernelError, OutRef, ceil_fraction
from .protocols import SwapsTwoWayDatum, checked_address, checked_datum, row_assets
from .signing import value_units
from .transactions import _previous

NEW_VENUES = ("swaps-v1-two-way", "splash", "genius-yield", "saturnswap")
ALL_VENUES = ("swaps-v1", "dano", *NEW_VENUES)
TWO_WAY_HASH = "87381f0bf416e2dae7497d3fcd8087cf677b3cb4b2aeba36ed8f8f79"
TWO_WAY_POLICY = "84662c22dc5c0cadad7b2ebf9757ce9ea61dbd8fe64bc8c43c112a40"
SPLASH_HASHES = (
    "f002facfd69d51b63e7046c6d40349b0b17c8dd775ee415c66af3ccc",
    "9dee0659686c3ab807895c929e3284c11222affd710b09be690f924d",
)
# Exact Preprod deployments; never derive a deployment by changing address headers.
GENIUS_HASH = "44376a5f63342097a4f20401088c62da272639e60644a9ec1d70f444"
GENIUS_POLICY = "53827a77e4ed3d5c211706708c0aa9b9a3be19db901b1cbf7fa515b8"
GENIUS_CONFIG_HASH = "044563a452ddbc0347206d57488108b59ab252af1b76583e6a6595a8"
GENIUS_CONFIG_TOKEN = "fae686ea8f21d567841d703dea4d4221c2af071a6f2b433ff07c0af2b5121487c7661f202bc1f95cbc16f0fce720a0a9d3dba63a9f128e617f2ddedc"
SATURN_HASH = "ec457591a4f5ab0d070146558e5f1729fcc5c0b230472437be337625"
SATURN_FEE_ADDRESS = "addr_test1vrjau4npl8vg8fvp38ahj3lxu3wtlp3qyh2agu4u6vqxlds065ldr"
VENUE_HASHES = {
    "swaps-v1-two-way": (TWO_WAY_HASH,),
    "splash": SPLASH_HASHES,
    "genius-yield": (GENIUS_HASH,),
    "saturnswap": (SATURN_HASH,),
}


@dataclass
class PoolAction(PlutusData):
    CONSTR_ID = 0
    action: int
    self_index: int


@dataclass
class SaturnAction(PlutusData):
    CONSTR_ID = 0
    user_sell_amount: int
    input_index: int
    output_index: int


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise KernelError("Invalid protocol integer")
    return value


def _address(datum_address, profile):
    # Dendrite's address codec defaults to Mainnet. Credentials are network-neutral;
    # apply the explicitly selected network only when decoding a datum address.
    address = datum_address.to_address()
    return Address(address.payment_part, address.staking_part, to_network(profile))


def to_network(profile):
    from pycardano import Network

    return Network.MAINNET if profile.address_network == "mainnet" else Network.TESTNET


def _output(address, assets, datum, context, *, top_up=False):
    output = TransactionOutput(
        address,
        asset_to_value(Assets(root={u: q for u, q in assets.items() if q})),
        datum=datum,
    )
    if top_up:
        # Use the encoded coin width of a realistic min-ADA output, then converge.
        original = output.amount.coin
        output.amount.coin = max(original, 2_000_000)
        output.amount.coin = max(original, min_lovelace(context, output=output))
        output.amount.coin = max(
            output.amount.coin, min_lovelace(context, output=output)
        )
    elif output.amount.coin < min_lovelace(context, output=output):
        raise KernelError("Protocol continuation would consume minimum ADA")
    if any(type(q) is not int or q < 0 for q in value_units(output.amount).values()):
        raise KernelError("Negative protocol output")
    return output


def _ratio(old, new, amount):
    # Saturn uses two ceiling operations with a 10^12 intermediate scale.
    scale = 10**12
    ratio = (new * scale + old - 1) // old
    return (amount * ratio + scale - 1) // scale


@dataclass(frozen=True)
class Fill:
    incoming: int
    outgoing: int
    fixed_fee: int
    outputs: tuple
    redeemer: object
    mint: tuple = ()


@dataclass(frozen=True)
class DirectState:
    venue: str
    row: dict
    profile: object
    context: object
    datum: object
    unit_a: str  # Maker offer / pool X.
    unit_b: str  # Maker ask / pool Y.
    config_row: dict | None = None
    settings: object = None
    assets: dict | None = None
    carrier: int = 0
    partial_fee: int = 0

    @property
    def ref(self):
        return OutRef(self.row["tx_hash"], self.row["tx_index"])

    def edges(self):
        from .arbitrage import Edge

        directions = [(self.unit_b, self.unit_a)]
        if self.venue in ("splash", "swaps-v1-two-way"):
            directions.append((self.unit_a, self.unit_b))
        return [
            Edge(
                self.ref,
                self.venue,
                a,
                b,
                2**63 - 1,
                pool=self,
                fee_group=str(Address.from_primitive(self.row["address"]).payment_part),
            )
            for a, b in directions
        ]

    def quote(self, input_unit, budget):
        if (
            type(budget) is not int
            or budget <= 0
            or input_unit not in (self.unit_a, self.unit_b)
        ):
            return None
        # Search uses only integer arithmetic for the common pool/order paths.
        # Serialize outputs only when constructing a candidate or pricing a full fill.
        if self.venue in ("splash", "swaps-v1-two-way", "genius-yield"):
            if self.venue == "genius-yield" and input_unit != self.unit_b:
                return None
            incoming, outgoing = self.amounts(input_unit, budget)
            if min(incoming, outgoing) <= 0:
                return None
            if self.venue != "genius-yield" or outgoing < self.datum.offered_amount:
                return incoming, outgoing, self.partial_fee
        try:
            fill = self.fill(input_unit, budget)
        except KernelError:
            return None
        return fill.incoming, fill.outgoing, fill.fixed_fee

    def amounts(self, input_unit, budget):
        d = self.datum
        output_unit = self.unit_b if input_unit == self.unit_a else self.unit_a
        if self.venue == "splash":
            rx = self.assets[self.unit_a] - d.treasury_x
            ry = self.assets[self.unit_b] - d.treasury_y
            if input_unit == self.unit_b:
                rx, ry = ry, rx
            adjusted = budget * (d.pool_fee - d.treasury_fee)
            return budget, ry * adjusted // (rx * 100000 + adjusted)
        p = (
            d.price
            if self.venue == "genius-yield"
            else d.asset1_price
            if output_unit == self.unit_a
            else d.asset2_price
        )
        capacity = (
            d.offered_amount
            if self.venue == "genius-yield"
            else self.assets.get(output_unit, 0)
            - (self.carrier if output_unit == "lovelace" else 0)
        )
        take = min(capacity, budget * p.denominator // p.numerator)
        return (take * p.numerator + p.denominator - 1) // p.denominator, take

    def fill(self, input_unit, budget):
        _integer(budget, 1)
        if input_unit not in (self.unit_a, self.unit_b):
            raise KernelError("Quote asset does not match protocol pair")
        if (
            self.venue not in ("splash", "swaps-v1-two-way")
            and input_unit != self.unit_b
        ):
            raise KernelError("Order direction mismatch")
        outgoing_unit = self.unit_b if input_unit == self.unit_a else self.unit_a
        d = deepcopy(self.datum)
        assets = dict(self.assets)
        address = Address.from_primitive(self.row["address"])
        mint = {}
        if self.venue == "swaps-v1-two-way":
            first = outgoing_unit == self.unit_a
            incoming, outgoing = self.amounts(input_unit, budget)
            if min(incoming, outgoing) <= 0:
                raise KernelError("Order direction is depleted")
            assets[input_unit] = assets.get(input_unit, 0) + incoming
            assets[outgoing_unit] -= outgoing
            d.prev_input = _previous(self.ref.tx_hash, self.ref.index)
            outputs = [_output(address, assets, d, self.context)]
            redeemer = RawPlutusData(cbor2.CBORTag(123 if first else 124, []))
        elif self.venue == "splash":
            incoming, outgoing = self.amounts(input_unit, budget)
            assets[input_unit] += incoming
            assets[outgoing_unit] -= outgoing
            treasury = incoming * d.treasury_fee // 100000
            if input_unit == self.unit_a:
                d.treasury_x += treasury
            else:
                d.treasury_y += treasury
            outputs = [_output(address, assets, d, self.context)]
            redeemer = PoolAction(2, 0)
        elif self.venue == "genius-yield":
            incoming, outgoing = self.amounts(input_unit, budget)
            nft = GENIUS_POLICY + d.nft.hex()
            if outgoing < d.offered_amount:
                d.offered_amount -= outgoing
                d.partial_fills += 1
                d.contained_payment += incoming
                assets[input_unit] = assets.get(input_unit, 0) + incoming
                assets[outgoing_unit] -= outgoing
                # A dedicated fee output supports several Genius orders in one body.
                outputs = [_output(address, assets, d, self.context)]
                redeemer = GeniusSubmitRedeemer(outgoing)
                contained = {}
            else:
                contained = Counter({"lovelace": d.contained_fee.lovelaces})
                contained[outgoing_unit] += d.contained_fee.offered_tokens
                contained[input_unit] += d.contained_fee.asked_tokens
                payment = Counter(assets)
                payment.subtract(contained)
                payment[nft] -= 1
                payment[outgoing_unit] -= outgoing
                payment[input_unit] += incoming
                mint[nft] = -1
                pay_datum = GeniusUTxORef(
                    GeniusTxRef(bytes.fromhex(self.ref.tx_hash)), self.ref.index
                )
                outputs = [
                    _output(
                        _address(d.owner_address, self.profile),
                        payment,
                        pay_datum,
                        self.context,
                        top_up=True,
                    )
                ]
                redeemer = GeniusCompleteRedeemer()
            ref = GeniusUTxORef(
                GeniusTxRef(bytes.fromhex(self.ref.tx_hash)), self.ref.index
            )
            fee_map = {}
            for u, q in contained.items():
                if q:
                    a = Asset.from_unit(self.profile.name, u)
                    fee_map.setdefault(a.policy, {})[a.name] = q
            fee_datum = GeniusYieldFeeDatum({ref: fee_map}, {}, PlutusNone())
            fee_assets = Counter(contained)
            fee_assets["lovelace"] += d.taker_lovelace_fee
            outputs.append(
                _output(
                    _address(self.settings.fee_address, self.profile),
                    fee_assets,
                    fee_datum,
                    self.context,
                    top_up=True,
                )
            )
        elif self.venue == "saturnswap":
            incoming = min(budget, d.amount_buy)
            full = incoming == d.amount_buy
            if not full and incoming < d.min_partial_fill:
                raise KernelError("Saturn partial fill is below the minimum")
            if full and "lovelace" not in (input_unit, outgoing_unit):
                incoming -= 1
                full = False
                if incoming < d.min_partial_fill:
                    raise KernelError(
                        "Saturn token/token partial fill is below minimum"
                    )
            released = _ratio(d.amount_buy, incoming, d.amount_sell)
            fee = released // 100
            if fee <= 0:
                raise KernelError("Saturn fill is too small to pay the taker fee")
            ref = SaturnSwapOutputReferenceV3(
                bytes.fromhex(self.ref.tx_hash), self.ref.index
            )
            pay_datum = SaturnSwapPaymentDatumV3(ref)
            buffer = 0
            continuation = None
            if not full:
                remain = _ratio(d.amount_buy, d.amount_buy - incoming, d.amount_sell)
                buffer = (
                    2_000_000
                    if outgoing_unit == "lovelace" and remain > 2_000_000
                    else 0
                )
                remaining_buy = (
                    _ratio(d.amount_sell, remain - buffer, d.amount_buy)
                    if buffer
                    else d.amount_buy - incoming
                )
                d.amount_sell, d.amount_buy = remain - buffer, remaining_buy
                if min(d.amount_sell, d.amount_buy) <= 0:
                    raise KernelError("Saturn continuation is empty")
                d.output_reference = ref
                residual = {outgoing_unit: d.amount_sell}
                if outgoing_unit != "lovelace":
                    residual["lovelace"] = assets["lovelace"]
                continuation = _output(address, residual, d, self.context, top_up=True)
            owner_assets = Counter({input_unit: incoming})
            owner_assets["lovelace"] += (
                assets["lovelace"] if full and outgoing_unit != "lovelace" else buffer
            )
            outputs = [
                _output(
                    _address(d.owner, self.profile),
                    owner_assets,
                    pay_datum,
                    self.context,
                    top_up=True,
                ),
                _output(
                    Address.from_primitive(SATURN_FEE_ADDRESS),
                    {outgoing_unit: fee},
                    pay_datum,
                    self.context,
                    top_up=True,
                ),
            ]
            if continuation is not None:
                outputs.append(continuation)
            # Complement rounding determines what actually leaves the order.
            outgoing = assets[outgoing_unit] - sum(
                value_units(o.amount).get(outgoing_unit, 0) for o in outputs
            )
            redeemer = SaturnAction(incoming, 0, 0)
        else:
            raise KernelError("Unsupported direct venue")
        if min(incoming, outgoing) <= 0:
            raise KernelError("Empty protocol fill")
        delta = Counter(row_assets(self.row, self.profile.name))
        delta.update(mint)
        for output in outputs:
            delta.subtract(value_units(output.amount))
        expected = Counter({outgoing_unit: outgoing})
        expected[input_unit] -= incoming
        if any(
            delta[u] != expected[u]
            for u in delta.keys() | expected.keys()
            if u != "lovelace"
        ):
            raise KernelError("Protocol fill leaves unexpected native assets")
        fixed_fee = expected["lovelace"] - delta["lovelace"]
        if fixed_fee < 0:
            raise KernelError("Protocol fill releases unquoted ADA")
        return Fill(
            incoming, outgoing, fixed_fee, tuple(outputs), redeemer, tuple(mint.items())
        )


def decode_direct(venue, row, profile, context, *, owner=None, config_row=None):
    if profile.name != "preprod":
        raise KernelError(
            "Direct venues require an explicitly qualified Preprod deployment"
        )
    h = str(Address.from_primitive(row["address"]).payment_part)
    if h not in VENUE_HASHES[venue]:
        raise KernelError("Unrecognized venue deployment")
    address = checked_address(row, profile, h)
    to_utxo(row, profile)
    assets = row_assets(row, profile.name)
    settings = None
    carrier = partial_fee = 0
    if venue == "swaps-v1-two-way":
        d = SwapsTwoWayDatum.from_cbor(checked_datum(row, 11))
        a, b = (
            Asset(profile.name, d.asset1_id, d.asset1_name),
            Asset(profile.name, d.asset2_id, d.asset2_name),
        )
        if (a.policy, a.name) >= (b.policy, b.name):
            raise KernelError("Invalid two-way order pair")
        if address.staking_part is None:
            raise KernelError(
                "Swaps beacon policy requires a staking credential, not delegation"
            )
        for p in (d.asset1_price, d.asset2_price):
            _integer(p.numerator, 1)
            _integer(p.denominator, 1)
        pair = sha256(
            (a.policy or b"\0") + a.name + (b.policy or b"\0") + b.name
        ).digest()
        names = (
            pair,
            sha256(a.policy + a.name).digest(),
            sha256(b.policy + b.name).digest(),
        )
        if d.beacon_id.hex() != TWO_WAY_POLICY or names != (
            d.pair_beacon,
            d.asset1_beacon,
            d.asset2_beacon,
        ):
            raise KernelError("Two-way beacon identity mismatch")
        beacon_assets = {TWO_WAY_POLICY + n.hex(): 1 for n in names}
        if {
            u: q for u, q in assets.items() if u.startswith(TWO_WAY_POLICY)
        } != beacon_assets:
            raise KernelError("Invalid two-way beacon tokens")
        allowed = {"lovelace", a.unit, b.unit, *beacon_assets}
        if owner is not None and address.staking_part == owner.staking_part:
            raise KernelError("Own-wallet liquidity excluded")
        if a.unit == "lovelace":
            future = dict(assets)
            p = d.asset1_price
            future[b.unit] = future.get(b.unit, 0) + ceil_fraction(
                assets["lovelace"] * Fraction(p.numerator, p.denominator)
            )
            future_datum = replace(
                d, prev_input=_previous(row["tx_hash"], row["tx_index"])
            )
            carrier = min_lovelace(
                context, output=_output(address, future, future_datum, context)
            )
    elif venue == "splash":
        d = SplashCPPPoolDatum.from_cbor(checked_datum(row, 11))
        a, b = (
            Asset.from_unit(profile.name, v.assets.unit())
            for v in (d.asset_x, d.asset_y)
        )
        nft, lp = (v.assets.unit() for v in (d.pool_nft, d.lp_token))
        for v in (d.treasury_x, d.treasury_y, d.lq_bound, d.treasury_fee):
            _integer(v)
        _integer(d.pool_fee, 1)
        if not d.treasury_fee < d.pool_fee <= 100000:
            raise KernelError("Invalid Splash fee")
        if (
            len({a.unit, b.unit, nft, lp}) != 4
            or "lovelace" in (nft, lp)
            or assets.get(nft) != 1
            or assets.get(lp, 0) <= 0
        ):
            raise KernelError("Invalid Splash pool or liquidity token identity")
        if (
            assets.get(a.unit, 0) <= d.treasury_x
            or assets.get(b.unit, 0) <= d.treasury_y
            or d.lq_bound > 2 * (assets.get(a.unit, 0) - d.treasury_x)
        ):
            raise KernelError("Splash pool has insufficient active reserves")
        allowed = {"lovelace", a.unit, b.unit, nft, lp}
    elif venue == "genius-yield":
        if config_row is None:
            raise KernelError("Missing Genius Yield configuration")
        checked_address(config_row, profile, GENIUS_CONFIG_HASH)
        if row_assets(config_row, profile.name).get(GENIUS_CONFIG_TOKEN) != 1:
            raise KernelError("Genius configuration token mismatch")
        settings = GeniusYieldSettings.from_cbor(checked_datum(config_row, 8))
        if settings.nft_symbol.hex() != GENIUS_POLICY:
            raise KernelError("Genius configuration NFT policy mismatch")
        d = GeniusYieldOrder.from_cbor(checked_datum(row, 15))
        a, b = (
            Asset.from_unit(profile.name, v.assets.unit())
            for v in (d.offered_asset, d.asked_asset)
        )
        for v in (
            d.offered_amount,
            d.offered_original_amount,
            d.price.numerator,
            d.price.denominator,
        ):
            _integer(v, 1)
        for v in (
            d.partial_fills,
            d.maker_lovelace_fee,
            d.taker_lovelace_fee,
            d.contained_payment,
            d.contained_fee.lovelaces,
            d.contained_fee.offered_tokens,
            d.contained_fee.asked_tokens,
        ):
            _integer(v)
        for timestamp in (d.start_time, d.end_time):
            if isinstance(timestamp, GeniusTimestamp):
                _integer(timestamp.timestamp)
        nft = GENIUS_POLICY + d.nft.hex()
        if (
            len(d.nft) != 32
            or len(d.owner_key) != 28
            or assets.get(nft) != 1
            or d.offered_amount > d.offered_original_amount
        ):
            raise KernelError("Invalid Genius order identity or amount")
        required = Counter({"lovelace": d.contained_fee.lovelaces, nft: 1})
        required[a.unit] += d.offered_amount + d.contained_fee.offered_tokens
        required[b.unit] += d.contained_payment + d.contained_fee.asked_tokens
        if any(assets.get(u, 0) < q for u, q in required.items()):
            raise KernelError("Genius order does not hold its promised value")
        if owner is not None and (
            _address(d.owner_address, profile) == owner
            or d.owner_key == bytes(owner.payment_part)
        ):
            raise KernelError("Own-wallet liquidity excluded")
        allowed = {"lovelace", a.unit, b.unit, nft}
        ref = GeniusUTxORef(GeniusTxRef(bytes.fromhex(row["tx_hash"])), row["tx_index"])
        partial_fee = _output(
            _address(settings.fee_address, profile),
            {"lovelace": d.taker_lovelace_fee},
            GeniusYieldFeeDatum({ref: {}}, {}, PlutusNone()),
            context,
            top_up=True,
        ).amount.coin
    else:
        d = SaturnSwapSwapDatumV3.from_cbor(checked_datum(row, 11))
        a, b = (
            Asset(profile.name, d.policy_id_sell, d.asset_name_sell),
            Asset(profile.name, d.policy_id_buy, d.asset_name_buy),
        )
        _integer(d.amount_sell, 1)
        _integer(d.amount_buy, 1)
        _integer(d.min_partial_fill)
        if not isinstance(d.valid_before_time, PlutusNone):
            _integer(d.valid_before_time.value)
        if not isinstance(d.coverage, PlutusNone):
            raise KernelError("Covered Saturn orders are not enabled")
        if _address(d.owner, profile) == Address.from_primitive(SATURN_FEE_ADDRESS):
            raise KernelError("Saturn owner and fee recipient collide")
        if assets.get(a.unit, 0) < d.amount_sell:
            raise KernelError("Saturn order does not hold its offered amount")
        if owner is not None and _address(d.owner, profile) == owner:
            raise KernelError("Own-wallet liquidity excluded")
        allowed = {"lovelace", a.unit}
    if a == b or any(u not in allowed and q for u, q in assets.items()):
        raise KernelError("Invalid protocol pair or extraneous assets")
    return DirectState(
        venue,
        deepcopy(row),
        profile,
        context,
        d,
        a.unit,
        b.unit,
        config_row,
        settings,
        assets,
        carrier,
        partial_fee,
    )


def contribute_direct(builder, state, hop, rows):
    if builder.context.profile != state.profile:
        raise KernelError("Direct venue context mismatch")
    if (
        hop.ref != state.ref
        or hop.venue != state.venue
        or (hop.input_unit, hop.output_unit)
        not in {(e.input_unit, e.output_unit) for e in state.edges()}
    ):
        raise KernelError("Direct venue quote identity mismatch")
    fill = state.fill(hop.input_unit, hop.amount_in)
    if (fill.incoming, fill.outgoing, fill.fixed_fee) != (
        hop.amount_in,
        hop.amount_out,
        hop.fixed_fee,
    ):
        raise KernelError("Direct venue quote changed before construction")
    utxo = to_utxo(state.row, state.profile)
    if any(u.input == utxo.input for u in builder.inputs):
        raise KernelError("Protocol input already consumed")

    def script(h):
        matches = [
            to_utxo(r, state.profile)
            for r in rows.values()
            if (r.get("reference_script") or {}).get("hash") == h
        ]
        if not matches and h == SATURN_HASH:
            # Published Preprod reference was spent. These exact deployed bytes
            # keep fills available; normal transaction size/fee limits still apply.
            fallback = PlutusV3Script(
                bytes.fromhex(
                    files("defi_kernel")
                    .joinpath("data/saturn-preprod-v3.hex")
                    .read_text()
                    .strip()
                )
            )
            if str(plutus_script_hash(fallback)) != h:
                raise KernelError("Packaged Saturn script hash mismatch")
            return fallback
        if not matches or str(plutus_script_hash(matches[0].output.script)) != h:
            raise KernelError("Missing or mismatched venue reference script")
        expected = 3 if state.venue == "saturnswap" else 2
        if matches[0].output.script.version != expected:
            raise KernelError("Wrong venue Plutus language")
        return matches[0]

    if state.venue == "genius-yield":
        builder.reference_inputs.add(to_utxo(state.config_row, state.profile))
        for time_value, lower in (
            (state.datum.start_time, True),
            (state.datum.end_time, False),
        ):
            if isinstance(time_value, GeniusTimestamp):
                slot = builder.context.slot_at_ms(time_value.timestamp)
                if lower:
                    builder.validity_start = max(builder.validity_start or 0, slot + 1)
                else:
                    builder.ttl = min(builder.ttl, slot)
    elif state.venue == "saturnswap" and not isinstance(
        state.datum.valid_before_time, PlutusNone
    ):
        builder.ttl = min(
            builder.ttl, builder.context.slot_at_ms(state.datum.valid_before_time.value)
        )
    if (
        builder.ttl <= builder.context.last_block_slot
        or builder.validity_start > builder.context.last_block_slot
    ):
        raise KernelError("Order validity interval is not currently usable")
    builder.add_script_input(
        utxo,
        script=script(str(utxo.output.address.payment_part)),
        datum=RawCBOR(bytes.fromhex(state.row["datum_cbor"]))
        if utxo.output.datum_hash
        else None,
        redeemer=Redeemer(fill.redeemer),
    )
    if fill.mint:
        # One burn redeemer per policy, even with several complete fills.
        if not any(
            str(plutus_script_hash(s)) == GENIUS_POLICY
            for s, _ in builder._minting_script_to_redeemers
        ):
            builder.add_minting_script(
                script(GENIUS_POLICY), Redeemer(RawPlutusData(cbor2.CBORTag(122, [])))
            )
        value = asset_to_value(Assets(root=dict(fill.mint))).multi_asset
        builder.mint = value if builder.mint is None else builder.mint + value
    for output in fill.outputs:
        if isinstance(output.datum, GeniusYieldFeeDatum):
            existing = next(
                (
                    o
                    for o in builder.outputs
                    if o.address == output.address
                    and isinstance(o.datum, GeniusYieldFeeDatum)
                ),
                None,
            )
            if existing is not None:
                # The validator examines the FIRST fee output. Merge mentioned fees
                # for all fills; retaining each quoted ADA allowance is conservative.
                if existing.datum.fees.keys() & output.datum.fees.keys():
                    raise KernelError("Duplicate Genius fee attribution")
                existing.amount += output.amount
                existing.datum.fees.update(output.datum.fees)
                continue
        builder.add_output(output)
    if state.venue in ("splash", "saturnswap"):
        bound = getattr(builder, "_kernel_indexed_spends", [])
        bound.append(
            (
                fill.redeemer,
                utxo,
                fill.outputs[0] if state.venue == "saturnswap" else None,
            )
        )
        builder._kernel_indexed_spends = bound
