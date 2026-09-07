"""Offline request-count benchmark using the twelve confirmed MVP transactions."""

import argparse
import importlib.util
import json
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx
from pycardano import Transaction

from defi_kernel.config import load_profile
from defi_kernel.coordinator import Coordinator
from defi_kernel.journal import Journal
from defi_kernel.providers import Koios


def measure(coordinator_type=Coordinator, *, shared_tip=True):
    evidence = json.loads(Path("evidence/preprod-mvp-execution.json").read_text())
    entries = {t["txid"]: t for t in evidence["transactions"]}
    blocks = {
        t["inclusion"]["block_height"]: t["canonical_block"] for t in entries.values()
    }
    calls = Counter()

    def respond(request):
        endpoint = request.url.path.rsplit("/", 1)[1]
        calls[endpoint] += 1
        if endpoint == "genesis":
            data = [evidence["identity"]]
        elif endpoint == "tip":
            data = [evidence["final_order_observation"]["tip_after"]]
        elif endpoint == "blocks":
            data = [blocks[int(request.url.params["block_height"].split(".")[1])]]
        else:
            txid = json.loads(request.content)["_tx_hashes"][0]
            entry = entries[txid]
            if endpoint == "tx_info":
                data = [
                    {
                        k: v
                        for k, v in entry["inclusion"].items()
                        if k not in ("valid_contract", "valid_contract_source")
                    }
                ]
            elif endpoint == "tx_cbor":
                data = [{"tx_hash": txid, "cbor": entry["on_chain_cbor"]}]
            else:
                raise AssertionError(endpoint)
        return httpx.Response(200, json=data)

    profile = load_profile(Path("examples/preprod-test.toml"))
    with tempfile.TemporaryDirectory() as root:
        journal = Journal(profile.state_path(Path(root)), profile)
        provider = Koios(
            profile,
            client=httpx.Client(transport=httpx.MockTransport(respond)),
            sleep=lambda _: None,
        )
        try:
            for entry in entries.values():
                tx = Transaction.from_cbor(entry["on_chain_cbor"])
                tx.transaction_witness_set.vkey_witnesses = None
                journal.prepare_candidate(
                    entry["intent"], tx, entry["dependencies"], entry["metadata"]
                )
                journal.record_inclusion(
                    entry["intent"],
                    entry["inclusion"]["block_hash"],
                    entry["inclusion"]["block_height"],
                    entry["confirmations"],
                    confirmed=True,
                )
            started = time.perf_counter()
            coordinator = coordinator_type(provider, journal)
            options = {}
            if shared_tip:
                provider.verify_identity()
                options["tip"] = provider.tip()
            for entry in entries.values():
                assert coordinator.reconcile(entry["intent"], **options) == "confirmed"
            return {
                "requests": dict(calls),
                "total_requests": sum(calls.values()),
                "offline_seconds": time.perf_counter() - started,
            }
        finally:
            provider.close()
            journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, help="Optional pre-audit coordinator.py"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {
        "scope": "Transaction reconciliation only, twelve already-confirmed intents; real HTTP latency and order-history polling excluded.",
        "after": measure(),
    }
    if args.baseline:
        spec = importlib.util.spec_from_file_location(
            "defi_kernel.audit_baseline", args.baseline
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report["before"] = measure(module.Coordinator, shared_tip=False)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded)


if __name__ == "__main__":
    main()
