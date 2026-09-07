import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from defi_kernel.config import load_profile
from defi_kernel.domain import OutRef
from defi_kernel.providers import Koios, ProviderError, UnknownSubmission

PROFILE = load_profile(Path("config.example.toml"), "preprod")
GENESIS = {"networkmagic": "1", "systemstart": 1654041600, "networkid": "Testnet"}
TIP = {"hash": "a" * 64, "block_no": 100}


@pytest.mark.parametrize(
    "status,reward,valid",
    [
        ("registered", "0", True),
        ("registered", "12345", True),
        ("not registered", "0", False),
        ("registered", 1.5, False),
    ],
)
def test_stake_rewards_never_invent_unverified_zero(status, reward, valid):
    c = client(
        lambda r: httpx.Response(
            200,
            json=[
                {
                    "stake_address": "test-reward",
                    "status": status,
                    "rewards_available": reward,
                }
            ],
        )
    )
    if valid:
        assert c.stake_rewards("test-reward") == int(reward)
    else:
        with pytest.raises(ProviderError, match="verified rewards"):
            c.stake_rewards("test-reward")


def client(handler, **kwargs):
    return Koios(
        replace(PROFILE, request_interval=0, **kwargs),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )


def test_identity_distinguishes_public_testnets():
    c = client(lambda r: httpx.Response(200, json=[dict(GENESIS, networkmagic="2")]))
    with pytest.raises(ProviderError, match="mismatch"):
        c.verify_identity()


@pytest.mark.parametrize("body", [[], {}, [{"networkid": "Testnet"}]])
def test_identity_missing_fails_closed(body):
    with pytest.raises(ProviderError, match="Unverified"):
        client(lambda r: httpx.Response(200, json=body)).verify_identity()


def test_server_cap_does_not_truncate_scan():
    offsets = []

    def handler(r):
        if r.url.path.endswith("genesis"):
            return httpx.Response(200, json=[GENESIS])
        if r.url.path.endswith("tip"):
            return httpx.Response(200, json=[TIP])
        offset = int(r.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200, json=[{"tx_hash": "a" * 64, "tx_index": offset}] if offset < 3 else []
        )

    result = client(handler, page_size=100).credential_utxos("b" * 56)
    assert len(result.rows) == 3 and result.complete
    assert offsets == [0, 1, 2, 3]
    assert result.tip_before == TIP


def test_repeated_page_is_not_complete():
    def handler(r):
        data = (
            [GENESIS]
            if r.url.path.endswith("genesis")
            else [TIP]
            if r.url.path.endswith("tip")
            else [{"tx_hash": "a" * 64, "tx_index": 0}]
        )
        return httpx.Response(200, json=data)

    with pytest.raises(ProviderError, match="no progress"):
        client(handler).credential_utxos("b" * 56)


def test_page_budget_does_not_publish_partial_market():
    def handler(r):
        data = (
            [GENESIS]
            if r.url.path.endswith("genesis")
            else [TIP]
            if r.url.path.endswith("tip")
            else [{"tx_hash": "a" * 64, "tx_index": 0}]
        )
        return httpx.Response(200, json=data)

    with pytest.raises(ProviderError, match="page limit"):
        client(handler, max_pages=1).credential_utxos("b" * 56)


def test_bounded_retry_and_no_submission_retry():
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(503)

    with pytest.raises(ProviderError):
        client(handler).verify_identity()
    assert len(calls) == 3
    calls.clear()
    with pytest.raises(UnknownSubmission):
        client(handler).request("ogmios", body={}, retry=False)
    assert len(calls) == 1


def test_oversized_request_never_sent():
    def handler(r):
        pytest.fail("Oversized request sent")

    with pytest.raises(ProviderError, match="exceeds"):
        client(handler).request("ogmios", body={"data": "f" * 2000})


def test_indexer_absence_does_not_prove_spent_or_filled():
    c = client(lambda r: httpx.Response(200, json=[]))
    assert c.transaction_status("a" * 64) is None
    assert c.utxos([OutRef("a" * 64, 0)]) == []


@pytest.mark.parametrize("valid", [True, False])
def test_transaction_info_verifies_cbor_when_validity_flag_missing(valid):
    from pycardano import Transaction, TransactionBody, TransactionWitnessSet

    tx = Transaction(
        TransactionBody(inputs=[], outputs=[], fee=0), TransactionWitnessSet(), valid
    )
    txid = str(tx.transaction_body.id)

    def handler(r):
        if r.url.path.endswith("tx_info"):
            return httpx.Response(200, json=[{"tx_hash": txid, "block_height": 100}])
        assert r.url.path.endswith("tx_cbor")
        return httpx.Response(200, json=[{"tx_hash": txid, "cbor": tx.to_cbor_hex()}])

    assert client(handler).transaction_info(txid)["valid_contract"] is valid
    tx.transaction_body.fee = 1
    with pytest.raises(ProviderError, match="body hash mismatch"):
        client(handler).transaction_info(txid)


def test_evaluation_requires_budgets_and_identity():
    def handler(r):
        if r.url.path.endswith("genesis"):
            return httpx.Response(200, json=[GENESIS])
        assert json.loads(r.content)["method"] == "evaluateTransaction"
        return httpx.Response(200, json={"id": "kernel", "result": []})

    with pytest.raises(ProviderError, match="no script budgets"):
        client(handler).evaluate("00")


def test_consumed_plan_dependency_invalidates_plan():
    def handler(r):
        return httpx.Response(
            200, json=[GENESIS] if r.url.path.endswith("genesis") else []
        )

    with pytest.raises(ProviderError, match="consumed"):
        client(handler).recheck_dependencies([{"tx_hash": "a" * 64, "tx_index": 0}])


def test_datum_change_invalidates_plan():
    row = {"tx_hash": "a" * 64, "tx_index": 0, "is_spent": False, "datum_hash": "old"}

    def handler(r):
        return httpx.Response(
            200,
            json=[GENESIS]
            if r.url.path.endswith("genesis")
            else [dict(row, datum_hash="new")],
        )

    with pytest.raises(ProviderError, match="changed"):
        client(handler).recheck_dependencies([row])


def test_scan_rollback_rejected():
    tips = iter([TIP, dict(TIP, block_no=99)])

    def handler(r):
        data = (
            [GENESIS]
            if r.url.path.endswith("genesis")
            else [next(tips)]
            if r.url.path.endswith("tip")
            else []
        )
        return httpx.Response(200, json=data)

    with pytest.raises(ProviderError, match="Chain inconsistency"):
        client(handler).credential_utxos("b" * 56)


def test_address_history_paginates_without_output_indices():
    def handler(r):
        if r.url.path.endswith("genesis"):
            data = [GENESIS]
        elif r.url.path.endswith("tip"):
            data = [TIP]
        else:
            assert r.url.params["order"] == "tx_hash.asc"
            offset = int(r.url.params["offset"])
            data = (
                [{"tx_hash": str(offset) * 64, "block_height": offset + 1}]
                if offset < 2
                else []
            )
        return httpx.Response(200, json=data)

    observation = client(handler, page_size=1).scan(
        "address_txs", {"_addresses": ["public"]}
    )
    assert observation.complete and len(observation.rows) == 2


def test_oversized_response_is_bounded_and_never_retried_for_submission():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1))

    with pytest.raises(UnknownSubmission, match="8 MiB"):
        client(handler).request("ogmios", body={}, retry=False)
    assert len(calls) == 1


def test_duplicate_utxo_lookup_cannot_inflate_inventory():
    row = {"tx_hash": "a" * 64, "tx_index": 0, "is_spent": False}
    with pytest.raises(ProviderError, match="lookup"):
        client(lambda r: httpx.Response(200, json=[row, row])).utxos(
            [OutRef("a" * 64, 0)]
        )
