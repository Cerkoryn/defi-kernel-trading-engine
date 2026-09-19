"""Evaluate recorded/synthetic venue fills; never sign or submit transactions."""

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from pycardano import ExecutionUnits

from defi_kernel.config import load_profile
from defi_kernel.providers import Koios

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from benchmark_arbitrage import additional_utxos  # noqa: E402
from test_venues import make_direct_candidate, make_mixed_candidate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument(
        "--venue",
        choices=[
            "swaps-two-way",
            "splash-0",
            "splash-1",
            "genius",
            "saturn",
            "mixed",
            "mixed-dano",
        ],
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evidence/preprod-direct-venue-evaluation.json"),
    )
    args = parser.parse_args()
    profile = load_profile(Path("examples/preprod-test.toml"))
    # Additional synthetic UTxOs enlarge evaluation requests. This does not alter
    # production request or transaction limits; no wallet is opened here.
    provider = Koios(
        replace(
            profile,
            max_request_bytes=65536,
            token_env=profile.token_env if os.getenv(profile.token_env or "") else None,
        )
    )
    report = {
        "mode": "Hosted Plutus evaluation with synthetic additional UTxOs; no signing or submission",
        "observed_at": time.time(),
        "samples": [],
    }
    try:
        for key in (
            [args.venue]
            if args.venue
            else [
                "swaps-two-way",
                "splash-0",
                "splash-1",
                "genius",
                "saturn",
                "mixed",
                "mixed-dano",
            ]
        ):
            variants = [{"count": 1}, {"count": 2}]
            variants += (
                [{"full": True, "count": 2}]
                if key in ("genius", "saturn")
                else [{"direction": 1}]
            )
            if key in ("mixed", "mixed-dano"):
                variants = [{}]
            for variant in variants:
                sample = {"venue": key, **variant}
                report["samples"].append(sample)
                try:
                    budgets = None
                    for final in (False, True) if args.evaluate else (False,):
                        tx, auth, _, resolved, context, _ = (
                            make_mixed_candidate(
                                budgets=budgets, include_dano=key == "mixed-dano"
                            )
                            if key in ("mixed", "mixed-dano")
                            else make_direct_candidate(key, budgets=budgets, **variant)
                        )
                        extra = additional_utxos(tx, resolved)
                        for row in extra:
                            u = resolved[
                                row["transaction"]["id"] + "#" + str(row["index"])
                            ]
                            if u.output.datum_hash is not None:
                                row["datumHash"] = str(u.output.datum_hash)
                        request = {
                            "transaction": {"cbor": tx.to_cbor_hex()},
                            "additionalUtxo": extra,
                        }
                        sample.update(
                            unsigned_cbor=tx.to_cbor_hex(),
                            request=request,
                            fee_lovelace=tx.transaction_body.fee,
                        )
                        if args.evaluate:
                            measured = provider.rpc("evaluateTransaction", request)
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
                                assert set(budgets) == set(assigned) and all(
                                    u.mem <= assigned[k].mem
                                    and u.steps <= assigned[k].steps
                                    for k, u in budgets.items()
                                )
                                sample["stage"] = "final_evaluated"
                        else:
                            sample["stage"] = "structural_only"
                except Exception as error:
                    sample.update(stage="rejected", reason=str(error))
                    if getattr(error, "rpc_error", None):
                        sample["provider_error"] = error.rpc_error
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            k: v
                            for k, v in sample.items()
                            if k
                            not in (
                                "unsigned_cbor",
                                "request",
                                "provider_error",
                                "measured_budgets",
                            )
                        }
                    ),
                    flush=True,
                )
    finally:
        provider.close()
    if any(s["stage"] == "rejected" for s in report["samples"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
