"""Qualification harness, not a trading command. Requires the development extras.

Builds the controlled test candidate, then optionally asks hosted Ogmios to
evaluate it with synthetic funding/Swaps inputs in additionalUtxo. Never submits.
The candidate still uses recorded pool state; refresh evidence when it is spent.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from pycardano import ExecutionUnits

from defi_kernel.signing import value_units

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_composition import make_candidate, make_individual_candidate  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--evaluate", action="store_true")
parser.add_argument(
    "--action",
    choices=["composed", "create", "fill", "close", "dano"],
    default="composed",
)
parser.add_argument(
    "--budgets-from",
    type=Path,
    help="Rebuild using measured budgets from a successful probe",
)
parser.add_argument(
    "--output", type=Path, default=Path("evidence/preprod-composition-evaluation.json")
)
args = parser.parse_args()
budgets = None
if args.budgets_from:
    measured = json.loads(args.budgets_from.read_text())["response"]["result"]
    budgets = {
        f"{r['validator']['purpose'].replace('withdraw', 'withdrawal')}:{r['validator']['index']}": ExecutionUnits(
            r["budget"]["memory"], r["budget"]["cpu"]
        )
        for r in measured
    }
transaction, _, _, resolved, _ = (
    make_candidate(budgets=budgets)
    if args.action == "composed"
    else make_individual_candidate(args.action, budgets)
)
additional = []
for ref in ["0" * 64 + "#0", "f" * 64 + "#2", "01" * 32 + "#0"]:
    if ref not in resolved:
        continue
    utxo = resolved[ref]
    values = {"ada": {"lovelace": utxo.output.amount.coin}}
    for unit, quantity in value_units(utxo.output.amount).items():
        if unit != "lovelace":
            values.setdefault(unit[:56], {})[unit[56:]] = quantity
    output = {
        "transaction": {"id": str(utxo.input.transaction_id)},
        "index": utxo.input.index,
        "address": str(utxo.output.address),
        "value": values,
    }
    datum = utxo.output.datum
    if datum is not None:
        output["datum"] = (
            datum.to_cbor_hex() if hasattr(datum, "to_cbor_hex") else datum.cbor.hex()
        )
    additional.append(output)
request = {
    "jsonrpc": "2.0",
    "id": "kernel-qualification",
    "method": "evaluateTransaction",
    "params": {
        "transaction": {"cbor": transaction.to_cbor_hex()},
        "additionalUtxo": additional,
    },
}
payload = json.dumps(request, separators=(",", ":")).encode()
if len(payload) > 16384:
    raise RuntimeError("Qualification request exceeds the 16 KiB probe ceiling")
report = {
    "endpoint": "https://preprod.koios.rest/api/v1/ogmios",
    "observed_at": time.time(),
    "mode": "hosted evaluation with synthetic additional UTxOs; no submission",
    "action": args.action,
    "request_bytes": len(payload),
    "request": request,
    "transaction_id": str(transaction.transaction_body.id),
    "budgets_source": "structural placeholders; a successful probe must be followed by budgeted final evaluation",
}
if args.budgets_from:
    report["budgets_source"] = str(args.budgets_from)
if args.evaluate:
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        response = client.post(
            report["endpoint"],
            content=payload,
            headers={"Content-Type": "application/json"},
        )
        report["http_status"] = response.status_code
        report["response"] = response.json()
        if "result" in report["response"]:
            expected = {
                (
                    k.tag.name.lower().replace("withdrawal", "withdraw"),
                    k.index,
                ): v.ex_units
                for k, v in transaction.transaction_witness_set.redeemer.items()
            }
            actual = {
                (r["validator"]["purpose"], r["validator"]["index"]): r["budget"]
                for r in report["response"]["result"]
            }
            report["final_evaluation_within_assigned_budgets"] = set(actual) == set(
                expected
            ) and all(
                u["memory"] <= expected[k].mem and u["cpu"] <= expected[k].steps
                for k, u in actual.items()
            )
args.output.write_text(json.dumps(report, indent=2) + "\n")
print(
    json.dumps(
        {k: v for k, v in report.items() if k not in ("request", "response")}, indent=2
    )
)
if "response" in report:
    result = report["response"]
    print(
        json.dumps(
            result
            if "result" in result
            else {
                "error_code": result.get("error", {}).get("code"),
                "message": result.get("error", {}).get("message"),
            },
            indent=2,
        )
    )
