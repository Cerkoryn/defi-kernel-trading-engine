"""Public confirmed evidence: signature/body replay, not a chain emulator."""

import json
from collections import Counter
from pathlib import Path

from nacl.signing import VerifyKey
from pycardano import Transaction

from defi_kernel.protocols import row_assets
from defi_kernel.signing import ref_text, value_units


def test_confirmed_atomic_routes_and_maker_cleanup():
    strategy = json.loads(Path("evidence/preprod-arbitrage-execution.json").read_text())
    maker = json.loads(Path("evidence/preprod-arbitrage-maker.json").read_text())
    routes = []
    for evidence in (strategy, maker):
        assert evidence["final_order_observation"]["complete"]
        assert not evidence["final_order_observation"]["rows"]
        for entry in evidence["transactions"]:
            tx = Transaction.from_cbor(entry["on_chain_cbor"])
            body = tx.transaction_body
            assert str(body.id) == entry["txid"] == entry["inclusion"]["tx_hash"]
            assert tx.valid and entry["inclusion"]["valid_contract"]
            assert entry["confirmations"] >= 3 and entry["attempts"] == 1
            assert entry["canonical_block"]["hash"] == entry["inclusion"]["block_hash"]
            for witness in tx.transaction_witness_set.vkey_witnesses:
                VerifyKey(witness.vkey.payload).verify(body.hash(), witness.signature)
            delta = Counter()
            inputs = set(map(ref_text, body.inputs))
            for row in entry["dependencies"]:
                if (
                    f"{row['tx_hash']}#{row['tx_index']}" in inputs
                    and row["address"] == evidence["wallet_address"]
                ):
                    delta.subtract(row_assets(row, "preprod"))
            for output in body.outputs:
                if str(output.address) == evidence["wallet_address"]:
                    delta.update(value_units(output.amount))
            assert {u: q for u, q in delta.items() if q} == entry[
                "wallet_delta_base_units"
            ]
            if entry["metadata"]["action"] == "arbitrage":
                metadata = entry["metadata"]
                routes.append(metadata)
                assert delta["lovelace"] == metadata["net_profit_lovelace"] >= 100_000
                assert all(q == 0 for u, q in delta.items() if u != "lovelace")
                assert (
                    len(tx.to_cbor()) == metadata["resources"]["signed_bytes"] <= 16384
                )
                assert body.fee == metadata["fee_lovelace"] <= 1_500_000
                assert metadata["notional_lovelace"] <= 10_000_000
                assert not body.mint
    assert {len(r["hops"]) for r in routes} == {3, 4}
    assert sum(r["net_profit_lovelace"] for r in routes) == 418291
    final_refs = {
        f"{r['tx_hash']}#{r['tx_index']}"
        for r in strategy["final_wallet_observation"]["rows"]
    }
    original_split = "9b72c2e64da4a9e03fd7aaba630ce6c9103d417f388f0b886470db8391562e15"
    assert {original_split + "#0", original_split + "#2"} <= final_refs
    assert len(strategy["runtime_decisions"][-1]["settled"]) == 2
