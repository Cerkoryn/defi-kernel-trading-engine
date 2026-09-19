"""Published v1 transaction contributions and pinned builder composition hooks.

Builders produce unsigned candidates. Only full evaluation and independent final
inspection can promote a candidate; these functions never sign or submit.
"""

from copy import deepcopy
from dataclasses import dataclass, replace
from fractions import Fraction
from importlib.resources import files

from charli3_dendrite.dataclasses.datums import PlutusNone
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.ob.cardanoswaps import (
    CardanoSwapsOutputReference,
    CardanoSwapsRational,
    CardanoSwapsSomeOutRef,
    CardanoSwapsTxId,
    SpendWithMint,
    Swap,
    ask_beacon_name,
    offer_beacon_name,
    pair_beacon_name,
)
from charli3_dendrite.utility import asset_to_value
from pycardano import (
    Address,
    NativeScript,
    NonEmptyOrderedSet,
    PlutusData,
    PlutusV2Script,
    Redeemer,
    ScriptHash,
    Transaction,
    TransactionBuilder,
    TransactionOutput,
    Value,
    VerificationKeyHash,
    plutus_script_hash,
)
from pycardano.utils import min_lovelace

from .chain_context import to_utxo
from .domain import Asset, KernelError, Unsupported, ceil_fraction
from .protocols import DEPLOYMENTS, SwapsV1Datum, decode_swaps, row_assets

V1 = DEPLOYMENTS["swaps-v1"]


def _context(builder, profile):
    if getattr(builder.context, "profile", None) != profile:
        raise KernelError(
            "Transaction context does not match the selected chain/wallet profile"
        )


# Published v1 uses different beacon dispatch; replace with its qualified adapter.
# https://github.com/Charli3-Official/charli3-dendrite/issues/225
@dataclass
class V1CreateOrClose(PlutusData):
    CONSTR_ID = 0


class CompositionBuilder(TransactionBuilder):
    """PyCardano 0.18 integration point: resolve protocol indices before budgets."""

    collateral_fee_limit = None

    def _ref_script_size(self):
        # The SDK counts repeated uses of one reference input multiple times.
        # Count input occurrences, not hashes; removal criteria:
        # docs/upstream-contributions.md#reference-script-fee-accounting
        inputs = {u.input: u for u in [*self.inputs, *self.reference_inputs]}
        return sum(
            len(u.output.script.to_cbor())
            if isinstance(u.output.script, NativeScript)
            else len(u.output.script)
            for u in inputs.values()
            if u.output.script is not None
        )

    def _set_collateral_return(self, collateral_return_address):
        if self.collateral_fee_limit is None or not self._redeemer_list:
            return super()._set_collateral_return(collateral_return_address)
        # Size against our authorized fee ceiling, not maximum ledger resources.
        # See docs/upstream-contributions.md#bounded-collateral.
        if not self.collaterals or collateral_return_address is None:
            raise KernelError(
                "Script transaction requires explicit collateral and return address"
            )
        required = (
            self.collateral_fee_limit * self.context.protocol_param.collateral_percent
            + 99
        ) // 100
        total = sum((u.output.amount for u in self.collaterals), Value())
        if total.coin < required:
            raise KernelError(
                f"Collateral insufficient: have {total.coin} lovelace; "
                f"fee ceiling requires {required} lovelace"
            )
        returned = total - required
        output = TransactionOutput(collateral_return_address, returned)
        if returned.coin or returned.multi_asset:
            minimum = min_lovelace(self.context, output=deepcopy(output))
            if returned.coin < minimum:
                raise KernelError(
                    f"Collateral return insufficient: have {returned.coin} lovelace; "
                    f"minimum {minimum} lovelace"
                )
            self._collateral_return = output
        else:
            self._collateral_return = None
        self._total_collateral = required

    def _estimate_execution_units(
        self, change_address=None, merge_change=False, collateral_change_address=None
    ):
        if self.collateral_fee_limit is None:
            return super()._estimate_execution_units(
                change_address, merge_change, collateral_change_address
            )
        # The SDK creates a base-class copy, losing the bounded collateral hook.
        # Preserve our hooks and linked Dano outputs; share only the chain context.
        candidate = deepcopy(self, {id(self.context): self.context})
        candidate._should_estimate_execution_units = False
        self._should_estimate_execution_units = False
        body = candidate.build(change_address, merge_change, collateral_change_address)
        return self.context.evaluate_tx(
            Transaction(
                body,
                candidate._build_fake_witness_set(),
                auxiliary_data=candidate.auxiliary_data,
            )
        )

    def _set_redeemer_index(self):
        # Private hook until a supported helper resolves payload indices and pool layout.
        # https://github.com/Charli3-Official/charli3-dendrite/issues/12
        dano_outputs = getattr(self, "_kernel_dano_outputs", [])
        if any(
            actual is not expected
            for actual, expected in zip(self.outputs, dano_outputs, strict=False)
        ) or len(self.outputs) < len(dano_outputs):
            raise KernelError(
                "Dano pool continuations must occupy the leading outputs in batch order"
            )
        super()._set_redeemer_index()
        for redeemer in self._redeemer_list:
            resolve = getattr(redeemer.data, "set_idx", None)
            if resolve:
                resolve(self)
        # Bind by the spent out-ref and output identity, never equal redeemer data.
        # Required when several Splash/Saturn inputs share one validator.
        for data, utxo, output in getattr(self, "_kernel_indexed_spends", []):
            indices = [i for i, u in enumerate(self.inputs) if u.input == utxo.input]
            if len(indices) != 1:
                raise KernelError("Indexed protocol input is missing or duplicated")
            if output is None:
                data.self_index = indices[0]
            else:
                outputs = [i for i, o in enumerate(self.outputs) if o is output]
                if len(outputs) != 1:
                    raise KernelError(
                        "Indexed protocol payment is missing or duplicated"
                    )
                data.input_index, data.output_index = indices[0], outputs[0]

    def _build_tx_body(self):
        body = super()._build_tx_body()
        # Replace with qualified opt-in ordering; never sort imported transaction bytes.
        # https://github.com/Python-Cardano/pycardano/issues/504
        if body.reference_inputs:
            body.reference_inputs = NonEmptyOrderedSet(
                sorted(
                    body.reference_inputs,
                    key=lambda i: (bytes(i.transaction_id), i.index),
                )
            )
        return body


def _script(name, expected):
    script = PlutusV2Script(
        bytes.fromhex(
            files("defi_kernel").joinpath(f"data/{name}.hex").read_text().strip()
        )
    )
    if str(plutus_script_hash(script)) != expected:
        raise KernelError("Packaged script hash mismatch")
    return script


def _script_arg(name, expected, reference):
    if reference is None:
        return _script(name, expected)
    if (
        reference.output.script is None
        or str(plutus_script_hash(reference.output.script)) != expected
        or not isinstance(reference.output.script, PlutusV2Script)
    ):
        raise KernelError("Reference script does not match published v1")
    return reference


def beacons(datum, quantity):
    return {
        V1["beacon_policy"] + name.hex(): quantity
        for name in (datum.pair_beacon, datum.offer_beacon, datum.ask_beacon)
    }


def _mint(builder, datum, quantity, reference):
    script = _script_arg("swaps-v1-beacon", V1["beacon_policy"], reference)
    # Exactly one policy redeemer even when creating both sides in one tx.
    existing = [
        r
        for s, r in builder._minting_script_to_redeemers
        if str(plutus_script_hash(s)) == V1["beacon_policy"]
    ]
    if existing and any(not isinstance(r.data, V1CreateOrClose) for r in existing):
        raise KernelError("Conflicting Swaps beacon redeemer")
    if not existing:
        builder.add_minting_script(script, Redeemer(V1CreateOrClose()))
    mint = asset_to_value(Assets(root=beacons(datum, quantity))).multi_asset
    builder.mint = mint if builder.mint is None else builder.mint + mint


def _owner(builder, owner_address, expected_stake=None):
    if owner_address.network != builder.context.network:
        raise KernelError("Owner address network mismatch")
    stake = owner_address.staking_part
    if not isinstance(stake, VerificationKeyHash):
        raise Unsupported(
            "Local owner operations require a stake key credential; script/pointer authorization is not implemented"
        )
    if expected_stake is not None and stake != expected_stake:
        raise KernelError("Owner stake key does not control this order")
    if builder.required_signers is None:
        builder.required_signers = []
    if stake not in builder.required_signers:
        builder.required_signers.append(stake)
    return Address(
        ScriptHash(bytes.fromhex(V1["script_hash"])), stake, owner_address.network
    )


def _previous(tx_hash: str, index: int):
    return CardanoSwapsSomeOutRef(
        CardanoSwapsOutputReference(CardanoSwapsTxId(bytes.fromhex(tx_hash)), index)
    )


def create_order(
    builder,
    profile,
    owner_address,
    offer: Asset,
    ask: Asset,
    quantity: int,
    price: Fraction,
    *,
    beacon_reference=None,
):
    _context(builder, profile)
    if offer.network != profile.name or ask.network != profile.name or offer == ask:
        raise KernelError("Order pair/network mismatch")
    if (
        type(quantity) is not int
        or quantity <= 0
        or not isinstance(price, Fraction)
        or price <= 0
    ):
        raise KernelError(
            "Order requires a positive integer quantity and exact rational price"
        )
    address = _owner(builder, owner_address)
    datum = SwapsV1Datum(
        bytes.fromhex(V1["beacon_policy"]),
        pair_beacon_name(offer.policy, offer.name, ask.policy, ask.name),
        offer.policy,
        offer.name,
        offer_beacon_name(offer.policy, offer.name),
        ask.policy,
        ask.name,
        ask_beacon_name(ask.policy, ask.name),
        CardanoSwapsRational(price.numerator, price.denominator),
        PlutusNone(),
    )
    beacon_assets = beacons(datum, 1)
    amount = asset_to_value(Assets(root={**beacon_assets, offer.unit: quantity}))
    output = TransactionOutput(address, amount, datum=datum)
    # Reserve for the larger partial-fill datum AND both traded assets. Some(prev)
    # is larger than None; simply sizing the initial datum strands an ADA tail.
    future = replace(datum, prev_input=_previous("f" * 64, 65535))
    carrier = 0
    for values, d in [
        ({**beacon_assets, offer.unit: quantity}, datum),
        (
            {
                **beacon_assets,
                offer.unit: quantity,
                ask.unit: ceil_fraction(quantity * price),
            },
            future,
        ),
    ]:
        hypothetical = TransactionOutput(
            address, asset_to_value(Assets(root=values)), datum=d
        )
        hypothetical.amount.coin = max(hypothetical.amount.coin, 3_000_000)
        carrier = max(carrier, min_lovelace(builder.context, output=hypothetical))
    output.amount.coin += carrier
    _mint(builder, datum, 1, beacon_reference)
    builder.add_output(output)
    return output


def fill_order(
    builder, profile, row, take: int, max_payment: int, *, swap_reference=None
):
    _context(builder, profile)
    order = decode_swaps(row, profile, "swaps-v1")
    if type(take) is not int or take <= 0 or take > order.held_offer:
        raise KernelError("Fill exceeds held offer or has invalid size")
    pay = ceil_fraction(take * order.price)
    if type(max_payment) is not int or pay > max_payment:
        raise KernelError("Fill exceeds maximum payment")
    assets = row_assets(row, profile.name)
    assets[order.offer.unit] -= take
    assets[order.ask.unit] = assets.get(order.ask.unit, 0) + pay
    utxo = to_utxo(row, profile)
    output = deepcopy(utxo.output)
    output.amount = asset_to_value(Assets(root=assets))
    output.datum_hash = None
    output.datum = replace(
        SwapsV1Datum.from_cbor(order.datum_cbor),
        prev_input=_previous(order.ref.tx_hash, order.ref.index),
    )
    if output.amount.coin < min_lovelace(builder.context, output=output):
        raise KernelError("Fill would consume the order's minimum-ADA carrier")
    script = _script_arg("swaps-v1", V1["script_hash"], swap_reference)
    builder.add_script_input(utxo, script=script, redeemer=Redeemer(Swap()))
    builder.add_output(output)
    return output, pay


def close_order(
    builder, profile, row, owner_address, *, swap_reference=None, beacon_reference=None
):
    _context(builder, profile)
    order = decode_swaps(row, profile, "swaps-v1")
    _owner(builder, owner_address, Address.from_primitive(order.address).staking_part)
    datum = SwapsV1Datum.from_cbor(order.datum_cbor)
    assets = row_assets(row, profile.name)
    for unit in beacons(datum, 1):
        del assets[unit]
    output = TransactionOutput(owner_address, asset_to_value(Assets(root=assets)))
    if output.amount.coin < min_lovelace(builder.context, output=output):
        raise KernelError("Owner output needs additional minimum ADA")
    script = _script_arg("swaps-v1", V1["script_hash"], swap_reference)
    builder.add_script_input(
        to_utxo(row, profile), script=script, redeemer=Redeemer(SpendWithMint())
    )
    _mint(builder, datum, -1, beacon_reference)
    builder.add_output(output)
    return output
