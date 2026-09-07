"""Independent authorization of a finalized transaction, outside strategy code."""

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from pycardano import (
    Address,
    PaymentSigningKey,
    StakeSigningKey,
    Transaction,
    VerificationKeyHash,
    VerificationKeyWitness,
)

from .domain import KernelError, Unsupported
from .wallet import load_private_key


def ref_text(tx_input):
    return f"{tx_input.transaction_id}#{tx_input.index}"


def value_units(value):
    return {
        "lovelace": value.coin,
        **{
            str(p) + bytes(n).hex(): q
            for p, names in value.multi_asset.items()
            for n, q in names.items()
        },
    }


@dataclass(frozen=True)
class Authorization:
    """Built by the trusted coordinator from operator limits and protocol actions.

    Protocol outputs are committed byte-for-byte, including datum/stake address.
    The only flexible outputs are plain change to explicitly owned addresses.
    Asset delta limits include fees and minimum-ADA deposits, in base units.
    """

    chain_id: str
    wallet_id: str
    inputs: frozenset[str]
    references: frozenset[str]
    collateral: frozenset[str]
    owned_addresses: frozenset[str]
    protocol_outputs: tuple[bytes, ...]
    asset_delta_limits: tuple[tuple[str, int, int], ...]
    mint: tuple[tuple[str, int], ...]
    withdrawals: tuple[tuple[bytes, int], ...]
    required_signers: frozenset[str]
    max_fee: int
    max_collateral: int
    validity_start: int
    ttl: int


def inspect_transaction(transaction, authorization, context, resolved):
    """Return balance deltas only after every authorized body constraint passes.

    `resolved` contains verified input/reference/collateral UTxOs. Inspection is
    not script evaluation or ledger validation; both remain execution gates.
    """
    a, b, profile = authorization, transaction.transaction_body, context.profile
    if (a.chain_id, a.wallet_id) != (profile.chain_id, profile.wallet_id):
        raise KernelError("Signing policy chain/wallet mismatch")
    if not transaction.valid or transaction.auxiliary_data is not None:
        raise KernelError("Unexpected invalid flag or auxiliary data")
    if b.network_id is not None and b.network_id != context.network:
        raise KernelError("Final transaction network mismatch")
    for field in (
        "certificates",
        "update",
        "auxiliary_data_hash",
        "voting_procedures",
        "proposal_procedures",
        "current_treasury_value",
        "donation",
    ):
        if getattr(b, field) is not None:
            raise KernelError(f"Unauthorized transaction field: {field}")
    if type(b.fee) is not int or not 0 <= b.fee <= a.max_fee:
        raise KernelError("Transaction fee exceeds authorization")
    if (
        b.validity_start is None
        or b.ttl is None
        or not a.validity_start
        <= b.validity_start
        <= context.last_block_slot
        < b.ttl
        <= a.ttl
    ):
        raise KernelError("Transaction validity interval is unauthorized or expired")

    def check_refs(items, expected, label):
        actual = [ref_text(i) for i in items or []]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise KernelError(f"Unauthorized {label}")
        if any(r not in resolved or ref_text(resolved[r].input) != r for r in actual):
            raise KernelError(f"Unresolved {label}")

    check_refs(b.inputs, a.inputs, "spending inputs")
    check_refs(b.reference_inputs, a.references, "reference inputs")
    check_refs(b.collateral, a.collateral, "collateral inputs")
    if (
        (a.inputs & a.references)
        or (a.inputs & a.collateral)
        or (a.references & a.collateral)
    ):
        raise KernelError("Input roles must be disjoint")
    if set(map(str, b.required_signers or [])) != a.required_signers:
        raise KernelError("Unauthorized required signers")
    mint = {
        str(p) + bytes(n).hex(): q
        for p, names in (b.mint or {}).items()
        for n, q in names.items()
        if q
    }
    if mint != dict(a.mint):
        raise KernelError("Unauthorized mint or burn")
    if dict(b.withdraws or {}) != dict(a.withdrawals):
        raise KernelError("Unauthorized withdrawals")
    for reward in b.withdraws or {}:
        if Address.from_primitive(reward).network != context.network:
            raise KernelError("Withdrawal network mismatch")

    owned = [Address.from_primitive(address) for address in a.owned_addresses]
    if not owned or any(
        address.network != context.network
        or not isinstance(address.payment_part, VerificationKeyHash)
        for address in owned
    ):
        raise KernelError("Invalid owned change address")
    delta, balance = Counter(), Counter()
    for ref in a.inputs:
        output = resolved[ref].output
        if output.address.network != context.network:
            raise KernelError("Input network mismatch")
        if (
            isinstance(output.address.payment_part, VerificationKeyHash)
            and output.address not in owned
        ):
            raise KernelError("Spending key input is outside the selected wallet")
        units = value_units(output.amount)
        balance.update(units)
        if output.address in owned:
            delta.subtract(units)
    balance.update(mint)
    balance["lovelace"] += sum((b.withdraws or {}).values())
    expected_outputs = Counter(a.protocol_outputs)
    from pycardano.utils import min_lovelace

    for output in b.outputs:
        if output.address.network != context.network:
            raise KernelError("Output network mismatch")
        units = value_units(output.amount)
        if any(
            type(q) is not int or q < 0 for q in units.values()
        ) or output.amount.coin < min_lovelace(context, output=output):
            raise KernelError("Output has invalid value or insufficient minimum ADA")
        encoded = output.to_cbor()
        if expected_outputs[encoded]:
            expected_outputs[encoded] -= 1
        elif (
            output.address not in owned
            or output.datum is not None
            or output.datum_hash is not None
            or output.script is not None
        ):
            raise KernelError("Unauthorized destination or protocol output mutation")
        balance.subtract(units)
        if output.address in owned:
            delta.update(units)
    if any(expected_outputs.values()):
        raise KernelError("Authorized protocol continuation is missing")
    balance["lovelace"] -= b.fee
    if any(balance.values()):
        raise KernelError("Final transaction does not conserve assets")
    limits = {unit: (low, high) for unit, low, high in a.asset_delta_limits}
    if len(limits) != len(a.asset_delta_limits):
        raise KernelError("Duplicate asset delta limit")
    for unit in delta.keys() | limits.keys():
        low, high = limits.get(unit, (0, 0))
        if (
            type(low) is not int
            or type(high) is not int
            or not low <= delta[unit] <= high
        ):
            raise KernelError(f"Asset delta exceeds authorization: {unit}")

    collateral = Counter()
    for ref in a.collateral:
        output = resolved[ref].output
        if output.address not in owned:
            raise KernelError("Collateral does not belong to selected wallet")
        collateral.update(value_units(output.amount))
    if a.collateral:
        # Before CIP-40 (and still when its optional fields are omitted), all
        # collateral is at risk. Never interpret an omitted amount as zero.
        loss = b.total_collateral
        if loss is None and b.collateral_return is None:
            loss = collateral["lovelace"]
        if type(loss) is not int or not 0 <= loss <= a.max_collateral:
            raise KernelError("Collateral loss exceeds authorization")
        if loss * 100 < b.fee * context.protocol_param.collateral_percent:
            raise KernelError("Insufficient total collateral")
        if b.collateral_return is not None:
            output = b.collateral_return
            if (
                output.address not in owned
                or output.datum is not None
                or output.datum_hash is not None
                or output.script is not None
            ):
                raise KernelError("Unauthorized collateral return")
            if output.amount.coin < min_lovelace(context, output=output):
                raise KernelError("Collateral return is below minimum ADA")
            if any(
                type(q) is not int or q < 0 for q in value_units(output.amount).values()
            ):
                raise KernelError("Invalid collateral return value")
            collateral.subtract(value_units(output.amount))
        collateral["lovelace"] -= loss
        if any(collateral.values()):
            raise KernelError("Collateral return does not conserve assets")
    elif b.total_collateral is not None or b.collateral_return is not None:
        raise KernelError("Unexpected collateral fields")
    return {unit: q for unit, q in delta.items() if q}


@dataclass(frozen=True)
class LocalSigner:
    payment_key: Path
    stake_key: Path | None = None
    allow_mainnet: bool = False

    def sign(self, transaction, authorization, context, dependency_rows, evaluation):
        from .chain_context import to_utxo

        if context.profile.address_network == "mainnet" and not self.allow_mainnet:
            raise Unsupported(
                "Mainnet signing requires separate explicit authorization"
            )
        # Freeze before inspection: later changes to a builder cannot change what
        # is authorized or signed. Credentials are only opened after inspection.
        encoded = transaction.to_cbor()
        frozen = Transaction.from_cbor(encoded)
        if frozen.to_cbor() != encoded:
            raise KernelError(
                "CBOR round trip changed the candidate; use the pinned pure Python cbor2 build"
            )
        if (
            evaluation.chain_id != context.profile.chain_id
            or evaluation.transaction_digest != sha256(encoded).hexdigest()
            or not 0 <= context.provider.clock() - evaluation.observed_at <= 60
        ):
            raise KernelError(
                "Signing requires a fresh final evaluation of these exact bytes"
            )
        fresh = context.provider.recheck_dependencies(dependency_rows)
        resolved = {
            f"{r['tx_hash']}#{r['tx_index']}": to_utxo(r, context.profile)
            for r in fresh
        }
        current = SimpleNamespace(
            profile=context.profile,
            network=context.network,
            protocol_param=context.protocol_param,
            last_block_slot=int(context.provider.tip()["abs_slot"]),
        )
        inspect_transaction(frozen, authorization, current, resolved)
        witness = frozen.transaction_witness_set
        if witness.vkey_witnesses or witness.bootstrap_witness:
            raise KernelError("Expected an unsigned candidate")
        keys = [load_private_key(self.payment_key, PaymentSigningKey)]
        if self.stake_key is not None:
            keys.append(load_private_key(self.stake_key, StakeSigningKey))
        available = {str(key.to_verification_key().hash()): key for key in keys}
        needed = set(authorization.required_signers)
        for ref in authorization.inputs | authorization.collateral:
            credential = resolved[ref].output.address.payment_part
            if isinstance(credential, VerificationKeyHash):
                needed.add(str(credential))
        if not needed <= available.keys():
            raise KernelError(
                "Local keys do not provide the required payment/stake authority"
            )
        witness.vkey_witnesses = [
            VerificationKeyWitness(
                available[h].to_verification_key(),
                available[h].sign(frozen.transaction_body.hash()),
            )
            for h in sorted(needed)
        ]
        return frozen
