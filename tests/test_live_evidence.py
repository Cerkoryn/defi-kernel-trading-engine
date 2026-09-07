"""Replay recorded real-chain evidence; these tests themselves do not use a node."""

import json
from collections import Counter
from pathlib import Path

from nacl.signing import VerifyKey
from pycardano import Address, Transaction
from test_transactions import PROFILE, as_row

from defi_kernel.protocols import DEPLOYMENTS, decode_swaps
from defi_kernel.signing import ref_text

EVIDENCE = json.loads(Path("evidence/preprod-live-execution.json").read_text())


def test_confirmed_bytes_signatures_inclusion_and_wallet_deltas_agree():
    delta, fees = Counter(), 0
    assert len(EVIDENCE["transactions"]) == 6
    for entry in EVIDENCE["transactions"]:
        tx = Transaction.from_cbor(entry["on_chain_cbor"])
        body = tx.transaction_body
        assert tx.valid and entry["inclusion"]["valid_contract"]
        assert str(body.id) == entry["txid"] == entry["inclusion"]["tx_hash"]
        assert entry["canonical_block"]["hash"] == entry["inclusion"]["block_hash"]
        assert entry["confirmations"] >= 3
        assert entry["attempts"] == 1
        for witness in tx.transaction_witness_set.vkey_witnesses:
            VerifyKey(witness.vkey.payload).verify(body.hash(), witness.signature)
        assert body.fee == entry["metadata"]["fee_lovelace"] <= 2_000_000
        fees += body.fee
        change = entry["wallet_delta_base_units"]
        limits = {u: (lo, hi) for u, lo, hi in entry["metadata"]["asset_delta_limits"]}
        for unit in change.keys() | limits.keys():
            low, high = limits.get(unit, (0, 0))
            assert low <= change.get(unit, 0) <= high
        delta.update(change)
    delta["lovelace"] += 10_000_000_000  # Recorded faucet grant.
    assert dict(delta) == EVIDENCE["final_balance_base_units"]
    assert fees == EVIDENCE["total_transaction_fees_lovelace"] == 2_611_452
    assert EVIDENCE["final_order_observation"]["complete"]
    assert not EVIDENCE["final_order_observation"]["rows"]
    assert EVIDENCE["stake_account"][0]["status"] == "not registered"
    assert EVIDENCE["stake_account"][0]["delegated_pool"] is None


def test_actual_order_lineage_atomic_inputs_and_cleanup():
    owner = Address.from_primitive(EVIDENCE["wallet_address"])
    bodies, orders, entries = {}, {}, {}
    for entry in EVIDENCE["transactions"]:
        action = entry["metadata"]["action"]
        body = Transaction.from_cbor(entry["on_chain_cbor"]).transaction_body
        bodies[action], entries[action] = body, entry
        orders[action] = []
        for index, output in enumerate(body.outputs):
            if str(output.address) == EVIDENCE["order_address"]:
                row = as_row(output)
                row.update(tx_hash=str(body.id), tx_index=index)
                orders[action].append(decode_swaps(row, PROFILE))
                assert output.address.staking_part == owner.staking_part
    assert len(orders["create"]) == 2
    assert len(orders["fill"]) == len(orders["compose"]) == 1
    filled, composed = orders["fill"][0], orders["compose"][0]
    assert filled.previous in {o.ref for o in orders["create"]}
    assert filled.held_offer == 200_000
    assert composed.previous == filled.ref and composed.held_offer == 75_000
    composed_inputs = set(map(ref_text, bodies["compose"].inputs))
    assert str(filled.ref) in composed_inputs
    assert f"{bodies['buy-base'].id}#0" in composed_inputs
    assert (
        str(bodies["compose"].outputs[0].address.payment_part)
        == "04041c3c6ba87b33f2c9eb7f7dbeae3b26003c3e199d438bb99932a2"
    )
    assert entries["compose"]["wallet_delta_base_units"] == {"lovelace": -804_698}
    assert not orders["close"]
    assert str(composed.ref) in set(map(ref_text, bodies["close"].inputs))
    assert all(q < 0 for names in bodies["close"].mint.values() for q in names.values())
    assert {str(p) for p in bodies["close"].mint} == {
        DEPLOYMENTS["swaps-v1"]["beacon_policy"]
    }
    final_refs = {
        f"{r['tx_hash']}#{r['tx_index']}"
        for r in EVIDENCE["final_wallet_observation"]["rows"]
    }
    assert f"{bodies['split'].id}#0" in final_refs  # Dedicated collateral survived.
