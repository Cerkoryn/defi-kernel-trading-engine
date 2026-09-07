import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from pycardano import (
    Address,
    ExecutionUnits,
    Network,
    PaymentSigningKey,
    StakeSigningKey,
    datum_hash,
)
from test_composition import (
    EVIDENCE,
    PROFILE,
    make_candidate,
    make_individual_candidate,
)

from defi_kernel.domain import KernelError
from defi_kernel.execution import evaluate_final
from defi_kernel.signing import LocalSigner, value_units
from defi_kernel.slots import SlotClock


def measured_budgets():
    report = json.loads(
        Path("evidence/preprod-composition-final-evaluation.json").read_text()
    )
    return report, {
        f"{r['validator']['purpose'].replace('withdraw', 'withdrawal')}:{r['validator']['index']}": ExecutionUnits(
            r["budget"]["memory"], r["budget"]["cpu"]
        )
        for r in report["response"]["result"]
    }


def test_final_candidate_reproduces_successful_hosted_evaluation():
    report, budgets = measured_budgets()
    tx, _, context, _, _ = make_candidate(budgets=budgets)
    assert report["http_status"] == 200
    assert report["final_evaluation_within_assigned_budgets"] is True
    assert tx.to_cbor_hex() == report["request"]["params"]["transaction"]["cbor"]
    assert str(tx.transaction_body.id) == report["transaction_id"]
    context.evaluate_tx_cbor = lambda cbor: budgets
    context.provider = SimpleNamespace(clock=lambda: EVIDENCE["observed_at"])
    receipt = evaluate_final(tx, context)
    assert receipt.transaction_digest == sha256(tx.to_cbor()).hexdigest()
    context.evaluate_tx_cbor = lambda cbor: dict(list(budgets.items())[:-1])
    with pytest.raises(KernelError, match="exactly"):
        evaluate_final(tx, context)
    context.evaluate_tx_cbor = lambda cbor: {
        k: ExecutionUnits(100_000_000, 10**12) for k in budgets
    }
    with pytest.raises(KernelError, match="exceeds assigned"):
        evaluate_final(tx, context)


def test_local_signer_checks_authority_and_signs_unchanged_body(tmp_path):
    _, budgets = measured_budgets()
    payment, stake = PaymentSigningKey.generate(), StakeSigningKey.generate()
    owner = Address(
        payment.to_verification_key().hash(),
        stake.to_verification_key().hash(),
        network=Network.TESTNET,
    )
    payment_path, stake_path = tmp_path / "payment.skey", tmp_path / "stake.skey"
    payment.save(str(payment_path))
    stake.save(str(stake_path))
    payment_path.chmod(0o600)
    stake_path.chmod(0o600)
    tx, auth, context, resolved, _ = make_candidate(budgets=budgets, owner=owner)
    rows = []
    for utxo in resolved.values():
        output = utxo.output
        row = {
            "tx_hash": str(utxo.input.transaction_id),
            "tx_index": utxo.input.index,
            "address": str(output.address),
            "value": str(output.amount.coin),
            "is_spent": False,
            "asset_list": [
                {"policy_id": u[:56], "asset_name": u[56:], "quantity": str(q)}
                for u, q in value_units(output.amount).items()
                if u != "lovelace"
            ],
        }
        if output.datum is not None:
            row["datum_hash"] = str(datum_hash(output.datum))
            row["inline_datum"] = {
                "bytes": output.datum.to_cbor_hex()
                if hasattr(output.datum, "to_cbor_hex")
                else output.datum.cbor.hex()
            }
        if output.script is not None:
            from pycardano import plutus_script_hash

            row["reference_script"] = {
                "type": "plutusV3",
                "hash": str(plutus_script_hash(output.script)),
                "bytes": bytes(output.script).hex(),
            }
        rows.append(row)
    context.provider = SimpleNamespace(
        clock=lambda: EVIDENCE["observed_at"],
        recheck_dependencies=lambda dependencies: dependencies,
        tip=lambda: {"abs_slot": context.last_block_slot},
    )
    context.evaluate_tx_cbor = lambda cbor: budgets
    receipt = evaluate_final(tx, context)
    signed = LocalSigner(payment_path, stake_path).sign(
        tx, auth, context, rows, receipt
    )
    assert signed.transaction_body.hash() == tx.transaction_body.hash()
    assert not tx.transaction_witness_set.vkey_witnesses
    assert (
        len(signed.transaction_witness_set.vkey_witnesses) == 1
    )  # fills need only payment authority
    from nacl.signing import VerifyKey

    witness = signed.transaction_witness_set.vkey_witnesses[0]
    VerifyKey(bytes(witness.vkey.payload)).verify(
        signed.transaction_body.hash(), witness.signature
    )
    with pytest.raises(KernelError, match="fresh final evaluation"):
        LocalSigner(payment_path).sign(
            tx,
            auth,
            context,
            rows,
            replace(receipt, observed_at=receipt.observed_at - 61),
        )
    wrong_path = tmp_path / "wrong.skey"
    PaymentSigningKey.generate().save(str(wrong_path))
    wrong_path.chmod(0o600)
    with pytest.raises(KernelError, match="required payment/stake authority"):
        LocalSigner(wrong_path).sign(tx, auth, context, rows, receipt)


def test_era_conversion_covers_byron_transition_and_rejects_horizon():
    eras = json.loads(Path("evidence/preprod-era-summaries.json").read_text())["data"]
    clock = SlotClock(PROFILE, eras)
    assert clock.slot_at_ms(PROFILE.system_start * 1000 + 20_000) == 1
    assert clock.slot_at_ms((PROFILE.system_start + 1_728_000) * 1000) == 86400
    with pytest.raises(KernelError, match="era horizon"):
        clock.slot_at_ms(
            (PROFILE.system_start + eras[-1]["end"]["time"]["seconds"]) * 1000
        )


@pytest.mark.parametrize("action", ["create", "fill", "close", "dano"])
def test_individual_final_candidates_reproduce_hosted_evidence(action):
    report = json.loads(
        Path(f"evidence/preprod-{action}-final-evaluation.json").read_text()
    )
    budgets = {
        f"{r['validator']['purpose'].replace('withdraw', 'withdrawal')}:{r['validator']['index']}": ExecutionUnits(
            r["budget"]["memory"], r["budget"]["cpu"]
        )
        for r in report["response"]["result"]
    }
    tx, _, _, _, _ = make_individual_candidate(action, budgets)
    assert report["http_status"] == 200
    assert report["final_evaluation_within_assigned_budgets"] is True
    assert tx.to_cbor_hex() == report["request"]["params"]["transaction"]["cbor"]


@pytest.mark.parametrize(
    "role,body_field,label",
    [
        ("inputs", "inputs", "spending"),
        ("reference_inputs", "reference_inputs", "reference"),
        ("collaterals", "collateral", "collateral"),
    ],
)
def test_finalizer_cannot_authorize_inputs_added_during_build(
    tmp_path, monkeypatch, role, body_field, label
):
    from copy import deepcopy

    from pycardano import (
        NonEmptyOrderedSet,
        TransactionInput,
        TransactionOutput,
        UTxO,
        Value,
    )
    from test_composition import OWNER

    from defi_kernel.execution import prepare_transaction
    from defi_kernel.signing import inspect_transaction, ref_text

    tx, auth, context, resolved, builder = make_candidate()
    extra = UTxO(
        TransactionInput.from_primitive([b"a" * 32, 0]),
        TransactionOutput(OWNER, Value(3_000_000)),
    )
    resolved[ref_text(extra.input)] = extra

    def changed_build(**kwargs):
        selected = getattr(builder, role)
        (selected.add if isinstance(selected, set) else selected.append)(extra)
        body = deepcopy(tx.transaction_body)
        setattr(
            body,
            body_field,
            NonEmptyOrderedSet([*getattr(body, body_field), extra.input]),
        )
        if role == "inputs":
            body.outputs.append(extra.output)
        elif role == "collaterals":
            body.collateral_return.amount.coin += extra.output.amount.coin
        return body

    def inspect(self, intent, candidate, authorization, *args):
        inspect_transaction(candidate, authorization, context, resolved)
        pytest.fail("Builder mutation reached signing preparation")

    monkeypatch.setattr(builder, "build", changed_build)
    monkeypatch.setattr("defi_kernel.coordinator.Coordinator.prepare", inspect)
    with pytest.raises(KernelError, match=f"Unauthorized {label} inputs"):
        prepare_transaction(
            SimpleNamespace(profile=PROFILE),
            None,
            builder,
            OWNER,
            {"payment_key": "never-read.skey", "stake_key": "never-read-stake.skey"},
            tmp_path,
            "mutated",
            auth.asset_delta_limits,
            {},
            [],
            max_fee=auth.max_fee,
            max_collateral=auth.max_collateral,
        )
