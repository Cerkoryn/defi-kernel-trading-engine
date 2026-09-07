import json
from copy import deepcopy
from pathlib import Path

import pytest

from defi_kernel.config import load_profile
from defi_kernel.domain import KernelError
from defi_kernel.journal import Journal
from defi_kernel.settlement import project_history

EVIDENCE = json.loads(Path("evidence/preprod-live-execution.json").read_text())
PROFILE = load_profile(Path("examples/preprod-test.toml"))
EVENTS = [
    {"txid": t["txid"], "info": t["inclusion"], "cbor": t["on_chain_cbor"]}
    for t in EVIDENCE["transactions"]
]
ADDRESS = EVIDENCE["order_address"]


def test_replay_partial_fills_cancel_restart_and_rollback_without_duplicate_accounting(
    tmp_path,
):
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    complete = project_history(EVENTS, PROFILE, ADDRESS)
    for _ in range(2):
        journal.replace_order_projection(ADDRESS, EVENTS, complete)
    assert len(complete["fills"]) == 2
    assert all(not o["live"] and o["status"] == "cancelled" for o in complete["orders"])
    assert journal.balances_delta()["lovelace"] == 700_000
    journal.close()
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    # Remove close and composed fill as if their canonical branch disappeared.
    restored = project_history(EVENTS[:4], PROFILE, ADDRESS)
    journal.replace_order_projection(ADDRESS, EVENTS[:4], restored)
    assert sum(o["live"] for o in restored["orders"]) == 2
    assert len(restored["fills"]) == 1
    assert journal.balances_delta()["lovelace"] == 200_000
    assert next(o for o in restored["orders"] if o["fills"])["row"]["asset_list"]
    # Re-inclusion restores one event, never two copies.
    journal.replace_order_projection(ADDRESS, EVENTS, complete)
    assert journal.balances_delta()["lovelace"] == 700_000
    assert (
        journal.db.execute("SELECT count(*) FROM fills WHERE reversed=0").fetchone()[0]
        == 2
    )
    journal.close()


def test_missing_predecessor_and_unverified_bytes_fail_closed():
    with pytest.raises(KernelError, match="predecessor"):
        project_history(EVENTS[3:], PROFILE, ADDRESS)
    changed = deepcopy(EVENTS)
    changed[2]["txid"] = "a" * 64
    with pytest.raises(KernelError, match="bytes"):
        project_history(changed, PROFILE, ADDRESS)


def test_observer_does_not_erase_canonical_events_on_indexer_absence(tmp_path):
    from types import SimpleNamespace

    from defi_kernel.domain import Observation
    from defi_kernel.settlement import OrderObserver

    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    complete = project_history(EVENTS, PROFILE, ADDRESS)
    journal.replace_order_projection(ADDRESS, EVENTS, complete)
    provider = SimpleNamespace(
        profile=PROFILE,
        scan=lambda *a: Observation(
            "preprod",
            PROFILE.koios_url,
            0,
            (),
            True,
            {"block_no": 9999999},
            {"block_no": 9999999},
        ),
        block_at_height=lambda height: next(
            {"hash": e["info"]["block_hash"]}
            for e in EVENTS
            if e["info"]["block_height"] == height
        ),
    )
    with pytest.raises(KernelError, match="temporarily omits"):
        OrderObserver(provider, journal, ADDRESS).sync()
    assert journal.balances_delta()["lovelace"] == 700000
    journal.close()


def test_observer_waits_for_confirmations_and_replays_positive_rollback(
    tmp_path, monkeypatch
):
    from pycardano import Transaction

    from defi_kernel.domain import Observation
    from defi_kernel.settlement import OrderObserver

    class Provider:
        profile = PROFILE
        events = EVENTS[:4]
        depth = 1

        def clock(self):
            return 1788778000

        def tip(self):
            return {
                "block_no": self.events[-1]["info"]["block_height"] + self.depth - 1
            }

        def block_at_height(self, height):
            return next(
                (
                    {"hash": e["info"]["block_hash"]}
                    for e in self.events
                    if e["info"]["block_height"] == height
                ),
                {"hash": "f" * 64},
            )

        def transaction_info(self, txid):
            return next(e["info"] for e in self.events if e["txid"] == txid)

        def transaction_cbor(self, txid):
            return Transaction.from_cbor(
                next(e["cbor"] for e in self.events if e["txid"] == txid)
            )

        def scan(self, endpoint, body):
            if endpoint == "address_txs":
                rows = [
                    dict(tx_hash=e["txid"], block_height=e["info"]["block_height"])
                    for e in self.events
                ]
            else:
                rows = [
                    o["row"]
                    for o in project_history(self.events, PROFILE, ADDRESS)["orders"]
                    if o["live"]
                ]
            return Observation(
                "preprod",
                PROFILE.koios_url,
                self.clock(),
                tuple(rows),
                True,
                self.tip(),
                self.tip(),
            )

    p = Provider()
    j = Journal(PROFILE.state_path(tmp_path), PROFILE)
    observer = OrderObserver(p, j, ADDRESS)
    first = observer.sync()
    assert first["pending"] == [EVENTS[3]["txid"]]
    assert not first["fills"]
    p.depth = PROFILE.confirmations
    assert len(observer.sync()["fills"]) == 1
    assert len(observer.sync()["fills"]) == 1
    canonical_scan = p.scan
    saved = j.status()["order_ledger"]
    for mutation in ("ada", "tokens", "terms", "duplicate", "foreign"):

        def inconsistent_scan(endpoint, body):
            from dataclasses import replace

            from pycardano import datum_hash

            from defi_kernel.protocols import DEPLOYMENTS, SwapsV1Datum

            observation = canonical_scan(endpoint, body)
            if endpoint != "address_utxos":
                return observation
            rows = deepcopy(list(observation.rows))
            row = rows[0]
            if mutation == "ada":
                row["value"] = str(int(row["value"]) + 1)
            elif mutation == "tokens":
                token = next(
                    a
                    for r in rows
                    for a in r["asset_list"]
                    if a["policy_id"] != DEPLOYMENTS["swaps-v1"]["beacon_policy"]
                )
                token["quantity"] = str(int(token["quantity"]) + 1)
            elif mutation == "terms":
                datum = SwapsV1Datum.from_cbor(row["inline_datum"]["bytes"])
                datum.swap_price.numerator += 1
                row["inline_datum"]["bytes"] = datum.to_cbor_hex()
                row["datum_hash"] = str(datum_hash(datum))
            elif mutation == "duplicate":
                rows.append(deepcopy(row))
            else:
                row["address"] = EVIDENCE["wallet_address"]
            return replace(observation, rows=tuple(rows))

        monkeypatch.setattr(p, "scan", inconsistent_scan)
        with pytest.raises(KernelError, match="disagree|Duplicate|foreign"):
            observer.sync()
        assert j.status()["order_ledger"] == saved
        assert j.balances_delta()["lovelace"] == 200000
    monkeypatch.setattr(p, "scan", canonical_scan)
    p.events = EVENTS[:3]  # Positive replacement block for the removed fill.
    assert not observer.sync()["fills"]
    assert not j.balances_delta().get("lovelace", 0)
    p.events = EVENTS[:4]
    assert len(observer.sync()["fills"]) == 1
    assert j.balances_delta()["lovelace"] == 200000
    j.close()
