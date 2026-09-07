"""Replays real strategy evidence offline; does not emulate chain validation."""

import json
from collections import Counter
from pathlib import Path

from nacl.signing import VerifyKey
from pycardano import Transaction

from defi_kernel.config import load_profile
from defi_kernel.settlement import project_history


def test_live_strategy_cycle_signatures_limits_accounting_and_cleanup():
    evidence = json.loads(Path("evidence/preprod-mvp-execution.json").read_text())
    profile = load_profile(Path("examples/preprod-test.toml"))
    transactions = evidence["transactions"]
    assert len(transactions) == 12
    balances = Counter(lovelace=10_000_000_000)
    events, metadata = [], {}
    for entry in transactions:
        tx = Transaction.from_cbor(entry["on_chain_cbor"])
        body = tx.transaction_body
        assert str(body.id) == entry["txid"] == entry["inclusion"]["tx_hash"]
        assert tx.valid and entry["inclusion"]["valid_contract"]
        assert entry["canonical_block"]["hash"] == entry["inclusion"]["block_hash"]
        assert entry["confirmations"] >= profile.confirmations
        assert entry["attempts"] == 1
        for witness in tx.transaction_witness_set.vkey_witnesses:
            VerifyKey(witness.vkey.payload).verify(body.hash(), witness.signature)
        delta = entry["wallet_delta_base_units"]
        for unit, low, high in entry["metadata"]["asset_delta_limits"]:
            assert low <= delta.get(unit, 0) <= high
        assert body.fee == entry["metadata"]["fee_lovelace"] <= 2_000_000
        balances.update(delta)
        metadata[entry["txid"]] = entry["metadata"]
        events.append(
            dict(
                txid=entry["txid"], info=entry["inclusion"], cbor=entry["on_chain_cbor"]
            )
        )
    assert dict(balances) == evidence["final_balance_base_units"]
    projection = project_history(events, profile, evidence["order_address"], metadata)
    assert len(projection["orders"]) == 6
    assert len(projection["fills"]) == 3
    assert not any(o["live"] for o in projection["orders"])
    assert projection["fills"] == evidence["runtime_order_ledger"]["fills"]
    assert not evidence["final_order_observation"]["rows"]
    assert evidence["final_order_observation"]["complete"]
    decisions = evidence["runtime_decisions"]
    actions = [d.get("action") for d in decisions]
    assert {"rebalance-sell", "publish", "cancel", "hold", "cancelled"} <= set(actions)
    repriced = next(
        d for d in decisions if d.get("action") == "cancel" and "reprice" in d["reason"]
    )
    assert any(o["fills"] == 1 for o in repriced["orders"])
    replacement = next(
        d
        for d in decisions
        if d.get("action") == "publish" and d["observed_at"] > repriced["observed_at"]
    )
    hold = next(
        d
        for d in decisions
        if d.get("action") == "hold"
        and d.get("mode") == "preprod execution"
        and d["observed_at"] > replacement["observed_at"]
    )
    assert len(hold["orders"]) == 2
    assert all(o["ref"].startswith(replacement["txid"]) for o in hold["orders"])
    assert hold["costs"]["pending_fee_reserve_lovelace"] == 0
    assert evidence["stake_account"][0]["status"] == "not registered"
    split = transactions[0]["txid"]
    final_refs = {
        f"{r['tx_hash']}#{r['tx_index']}"
        for r in evidence["final_wallet_observation"]["rows"]
    }
    assert {
        split + "#0",
        split + "#2",
    } <= final_refs  # Collateral and large remainder survived.
