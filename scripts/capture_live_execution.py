"""Export public evidence of designated preprod execution, never wallet keys.

Reads the local outbox and independently queries inclusion/CBOR and final UTxOs.
Does not sign, submit, alter journal state or infer fills from absent outputs.
"""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from pycardano import Address, ScriptHash, Transaction

from defi_kernel.config import load_profile
from defi_kernel.domain import KernelError
from defi_kernel.journal import Journal
from defi_kernel.protocols import DEPLOYMENTS, row_assets
from defi_kernel.providers import Koios
from defi_kernel.signing import ref_text, value_units
from defi_kernel.wallet import load_wallet


def capture(profile, journal, wallet, provider):
    identity = provider.verify_identity()
    owner = Address.from_primitive(wallet["address"])
    order_address = Address(
        ScriptHash(bytes.fromhex(DEPLOYMENTS["swaps-v1"]["script_hash"])),
        owner.staking_part,
        owner.network,
    )
    transactions, aborted = [], []
    for row in journal.db.execute("SELECT intent FROM outbox ORDER BY rowid"):
        entry = journal.outbox_entry(row["intent"])
        if entry["status"] == "aborted":
            aborted.append(
                {
                    "intent": entry["intent"],
                    "attempts": entry["attempts"],
                    "signed": entry["signed"] is not None,
                }
            )
            continue
        info = provider.transaction_info(entry["txid"])
        if not info or info.get("valid_contract") is not True:
            raise KernelError("Public evidence requires successful chain inclusion")
        canonical = provider.block_at_height(info["block_height"])
        tip = provider.tip()
        confirmations = int(tip["block_no"]) - info["block_height"] + 1
        if (
            not canonical
            or canonical["hash"] != info["block_hash"]
            or confirmations < profile.confirmations
        ):
            raise KernelError("Public evidence requires canonical confirmed inclusion")
        serialized = provider.request("tx_cbor", body={"_tx_hashes": [entry["txid"]]})
        if len(serialized) != 1 or serialized[0]["tx_hash"] != entry["txid"]:
            raise KernelError("Missing on-chain CBOR")
        tx = Transaction.from_cbor(serialized[0]["cbor"])
        if str(tx.transaction_body.id) != entry["txid"]:
            raise KernelError("On-chain transaction hash mismatch")
        tx.transaction_witness_set.vkey_witnesses = None
        if tx.to_cbor() != entry["unsigned"]:
            raise KernelError(
                "Confirmed transaction differs from the prepared candidate"
            )
        body = tx.transaction_body
        dependencies = json.loads(entry["dependencies"])
        inputs = set(map(ref_text, body.inputs))
        delta = Counter()
        for dependency in dependencies:
            if (
                f"{dependency['tx_hash']}#{dependency['tx_index']}" in inputs
                and dependency["address"] == wallet["address"]
            ):
                delta.subtract(row_assets(dependency, profile.name))
        for output in body.outputs:
            if str(output.address) == wallet["address"]:
                delta.update(value_units(output.amount))
        metadata = json.loads(entry["metadata"])
        for unit, low, high in metadata["asset_delta_limits"]:
            if not low <= delta[unit] <= high:
                raise KernelError("Confirmed wallet delta exceeded action limits")
        transactions.append(
            {
                "intent": entry["intent"],
                "txid": entry["txid"],
                "metadata": metadata,
                "attempts": entry["attempts"],
                "inclusion": info,
                "canonical_block": canonical,
                "observed_tip": tip,
                "confirmations": confirmations,
                "on_chain_cbor": serialized[0]["cbor"],
                "dependencies": dependencies,
                "wallet_delta_base_units": {u: q for u, q in delta.items() if q},
            }
        )
    wallet_rows = provider.scan(
        "address_utxos", {"_addresses": [wallet["address"]], "_extended": True}
    )
    order_rows = provider.scan(
        "address_utxos", {"_addresses": [str(order_address)], "_extended": True}
    )
    balance = Counter()
    for row in wallet_rows.rows:
        balance.update(row_assets(row, profile.name))
    return {
        "mode": "confirmed designated preprod execution",
        "network": profile.name,
        "chain_id": profile.chain_id,
        "provider": profile.koios_url,
        "observed_at": provider.clock(),
        "identity": identity,
        "wallet_address": wallet["address"],
        "order_address": str(order_address),
        "stake_account": provider.request(
            "account_info",
            body={
                "_stake_addresses": [
                    str(Address(staking_part=owner.staking_part, network=owner.network))
                ]
            },
        ),
        "transactions": transactions,
        "aborted_candidates": aborted,
        "final_wallet_observation": asdict(wallet_rows),
        "final_order_observation": asdict(order_rows),
        "final_balance_base_units": dict(balance),
        "total_transaction_fees_lovelace": sum(
            t["metadata"]["fee_lovelace"] for t in transactions
        ),
        "runtime_order_ledger": journal.status()["order_ledger"],
        "runtime_decisions": [
            json.loads(row["decision"])
            for row in journal.db.execute("SELECT decision FROM shadow ORDER BY id")
        ],
        "limitations": [
            "Controlled own-order fills; not evidence of organic demand or profitable trading.",
            "Canonical inclusion relies on the identified hosted provider; REST observations are not atomic snapshots.",
            "Recovery under rollback, expiry and cancellation races is tested with deterministic fixtures, not deliberately induced chain failures.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = load_profile(args.config, "preprod")
    wallet, _ = load_wallet(profile, args.manifest)
    provider = Koios(profile)
    journal = Journal(profile.state_path(args.state_dir), profile)
    try:
        evidence = capture(profile, journal, wallet, provider)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")
        print(
            f"Saved {len(evidence['transactions'])} confirmed transactions to {args.output}"
        )
    finally:
        provider.close()
        journal.close()


if __name__ == "__main__":
    main()
