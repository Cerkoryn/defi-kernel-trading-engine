"""Independent authorization of a finalized transaction, outside strategy code."""

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import cbor2
from pycardano import (
    Address,
    NativeScript,
    PaymentSigningKey,
    RawPlutusData,
    RedeemerMap,
    StakeSigningKey,
    Transaction,
    VerificationKeyHash,
    VerificationKeyWitness,
)
from pycardano.exception import PyCardanoException

from .domain import KernelError, RequestTooLarge, Unsupported
from .wallet import load_private_key


def decode_transaction(encoded):
    """Turn malformed transaction evidence into a controlled, fail-closed error."""
    if not isinstance(encoded, (str, bytes)):
        raise KernelError("Invalid transaction CBOR: expected bytes or hex text")
    try:
        return Transaction.from_cbor(encoded)
    except (
        cbor2.CBORDecodeError,
        PyCardanoException,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
    ) as error:
        # SDK exception strings may include entire payloads. Keep only the type.
        raise KernelError(
            f"Invalid transaction CBOR ({type(error).__name__})"
        ) from None


def decode_candidate(encoded):
    """Decode an owned candidate without changing any evaluated/signed bytes."""
    frozen = decode_transaction(encoded)
    if frozen.to_cbor() != encoded:
        # RawPlutusData loses ByteString's >64-byte chunks (Dano multi-pool data).
        # Restore via the SDK, then demand exact parity; never normalize wire bytes.
        # Removal criteria: docs/upstream-contributions.md#long-redeemer-bytes
        redeemers = frozen.transaction_witness_set.redeemer
        for value in (
            redeemers.values()
            if isinstance(redeemers, RedeemerMap)
            else redeemers or []
        ):
            data = value.data
            if not isinstance(data, RawPlutusData):
                data = RawPlutusData(data)
            # Keep primitives: RawPlutusData's copy hook re-decodes the payload.
            value.data = RawPlutusData.from_dict(data.to_dict()).data
    # Keep this even after the upstream startup self-test:
    # https://github.com/Python-Cardano/pycardano/pull/496
    if frozen.to_cbor() != encoded:
        raise KernelError("CBOR round trip changed the candidate; signing is blocked")
    return frozen


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


def transaction_resources(transaction, context, resolved):
    """Count final witness bytes before opening keys; signed calls use exact bytes."""
    import json
    from copy import deepcopy

    from pycardano import VerificationKey

    tx = deepcopy(transaction)
    body, witness, params = (
        tx.transaction_body,
        tx.transaction_witness_set,
        context.protocol_param,
    )
    if not witness.vkey_witnesses:
        needed = set(map(str, body.required_signers or []))
        for tx_input in [*body.inputs, *(body.collateral or [])]:
            credential = resolved[ref_text(tx_input)].output.address.payment_part
            if isinstance(credential, VerificationKeyHash):
                needed.add(str(credential))
        witness.vkey_witnesses = [
            VerificationKeyWitness(VerificationKey(i.to_bytes(32, "big")), bytes(64))
            for i, _ in enumerate(sorted(needed), 1)
        ] or None
    encoded = tx.to_cbor()
    if len(encoded) > params.max_tx_size:
        raise KernelError("Signed transaction exceeds maximum transaction size")
    if len(body.collateral or []) > params.max_collateral_inputs:
        raise KernelError("Too many collateral inputs")
    for output in [
        *body.outputs,
        *([body.collateral_return] if body.collateral_return else []),
    ]:
        if len(output.amount.to_cbor()) > params.max_val_size:
            raise KernelError("Output value exceeds maximum value size")
    references = [
        resolved[ref_text(i)].output.script
        for i in set(body.inputs) | set(body.reference_inputs or [])
    ]
    reference_bytes = sum(
        len(script.to_cbor()) if isinstance(script, NativeScript) else len(script)
        for script in references
        if script is not None
    )
    if reference_bytes > params.maximum_reference_scripts_size["bytes"]:
        raise KernelError("Reference scripts exceed transaction limit")
    redeemers = witness.redeemer or {}
    units = (
        [value.ex_units for value in redeemers.values()]
        if hasattr(redeemers, "values")
        else []
    )
    memory, steps = sum(u.mem for u in units), sum(u.steps for u in units)
    if memory > params.max_tx_ex_mem or steps > params.max_tx_ex_steps:
        raise KernelError("Assigned execution budgets exceed transaction limits")
    payload_bytes = max(
        len(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "kernel",
                    "method": method,
                    "params": {"transaction": {"cbor": encoded.hex()}},
                },
                separators=(",", ":"),
            ).encode()
        )
        for method in ("evaluateTransaction", "submitTransaction")
    )
    if payload_bytes > context.profile.max_request_bytes:
        raise RequestTooLarge(payload_bytes, context.profile.max_request_bytes)
    return {
        "signed_bytes": len(encoded),
        "reference_script_bytes": reference_bytes,
        "memory": memory,
        "steps": steps,
        "provider_request_bytes": payload_bytes,
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
        or not a.validity_start <= b.validity_start < b.ttl <= a.ttl
    ):
        raise KernelError("Transaction validity interval is unauthorized")
    if context.last_block_slot >= b.ttl:
        raise KernelError(
            "Candidate validity interval expired; a fresh route is required"
        )
    if context.last_block_slot < b.validity_start:
        raise KernelError("Candidate validity interval has not started yet")

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
        frozen = decode_candidate(encoded)
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
        transaction_resources(frozen, context, resolved)
        # Keep key objects out of diagnostics; the SDK's repr/str exposes private bytes.
        # https://github.com/Python-Cardano/pycardano/pull/494
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
        transaction_resources(frozen, context, resolved)
        return frozen
