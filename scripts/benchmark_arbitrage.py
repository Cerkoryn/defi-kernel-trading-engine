"""Measure 2/3/4/6/8-hop Swaps cycles; synthetic inputs never reach submission.

--evaluate uses hosted Plutus evaluation with explicit additionalUtxo. Those
requests have more overhead than real-input execution. A stopped probe reports
its binding limit, not an inferred chain-wide hop limit.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from pycardano import ExecutionUnits

from defi_kernel.chain_context import protocol_parameters
from defi_kernel.config import load_profile
from defi_kernel.domain import KernelError
from defi_kernel.providers import Koios
from defi_kernel.signing import transaction_resources, value_units

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_arbitrage import make_dano_cycle, make_order_cycle  # noqa: E402


def additional_utxos(tx, resolved):
    extra = []
    for tx_input in [
        *tx.transaction_body.inputs,
        *(tx.transaction_body.collateral or []),
    ]:
        output = resolved[f"{tx_input.transaction_id}#{tx_input.index}"].output
        value = {"ada": {"lovelace": output.amount.coin}}
        for unit, quantity in value_units(output.amount).items():
            if unit != "lovelace":
                value.setdefault(unit[:56], {})[unit[56:]] = quantity
        row = {
            "transaction": {"id": str(tx_input.transaction_id)},
            "index": tx_input.index,
            "address": str(output.address),
            "value": value,
        }
        if output.datum is not None:
            row["datum"] = (
                output.datum.cbor.hex()
                if hasattr(output.datum, "cbor")
                else output.datum.to_cbor_hex()
            )
        extra.append(row)
    return extra


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("evidence/preprod-arbitrage-benchmark.json")
    )
    args = parser.parse_args()
    logging.getLogger("PyCardano").setLevel(logging.ERROR)
    profile = load_profile(Path("examples/preprod-test.toml"))
    provider = Koios(profile)
    report = {
        "mode": "synthetic inputs; no signing or submission",
        "observed_at": time.time(),
        "samples": [],
    }
    try:
        parameters = None
        if args.evaluate:
            provider.verify_identity()
            report["protocol_parameters"] = provider.rpc(
                "queryLedgerState/protocolParameters"
            )
            parameters = protocol_parameters(report["protocol_parameters"])
        for hops in (2, 3, 4, 6, 8, "dano-dano", "swaps-native-dano-dano"):
            started = time.monotonic()
            sample = {"hops": hops}
            report["samples"].append(sample)
            try:
                budgets = None
                now = time.time()
                for final in (False, True) if args.evaluate else (False,):
                    tx, _, _, _, context, resolved = (
                        make_order_cycle(
                            hops, budgets=budgets, now=now, parameters=parameters
                        )
                        if type(hops) is int
                        else make_dano_cycle(
                            budgets=budgets,
                            now=now,
                            parameters=parameters,
                            token_pair=hops != "dano-dano",
                        )
                    )
                    resources = transaction_resources(tx, context, resolved)
                    sample.update(
                        resources=resources,
                        fee_lovelace=tx.transaction_body.fee,
                        unsigned_cbor=tx.to_cbor_hex(),
                    )
                    if args.evaluate:
                        params = {
                            "transaction": {"cbor": tx.to_cbor_hex()},
                            "additionalUtxo": additional_utxos(tx, resolved),
                        }
                        sample["probe_request_bytes"] = len(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": "kernel",
                                    "method": "evaluateTransaction",
                                    "params": params,
                                },
                                separators=(",", ":"),
                            ).encode()
                        )
                        measured = provider.rpc("evaluateTransaction", params)
                        budgets = {
                            f"{r['validator']['purpose'].replace('withdraw', 'withdrawal')}:{r['validator']['index']}": ExecutionUnits(
                                r["budget"]["memory"], r["budget"]["cpu"]
                            )
                            for r in measured
                        }
                        sample["measured_budgets"] = measured
                        if final:
                            assigned = {
                                f"{k.tag.name.lower()}:{k.index}": v.ex_units
                                for k, v in tx.transaction_witness_set.redeemer.items()
                            }
                            if set(budgets) != set(assigned) or any(
                                u.mem > assigned[k].mem or u.steps > assigned[k].steps
                                for k, u in budgets.items()
                            ):
                                raise KernelError(
                                    "Final evaluation exceeds assigned budgets"
                                )
                            sample["stage"] = "final_evaluated_with_synthetic_inputs"
                sample.setdefault("stage", "structural_only")
            except KernelError as error:
                sample.update(stage="rejected", reason=str(error))
                if getattr(error, "rpc_error", None):
                    sample["provider_error"] = error.rpc_error
            sample["elapsed_seconds"] = time.monotonic() - started
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in sample.items()
                        if k
                        not in ("unsigned_cbor", "provider_error", "measured_budgets")
                    }
                ),
                flush=True,
            )
    finally:
        provider.close()


if __name__ == "__main__":
    main()
