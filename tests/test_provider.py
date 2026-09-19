import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from defi_kernel.config import load_profile
from defi_kernel.domain import OutRef, RequestTooLarge
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


@pytest.mark.parametrize(
    "token", [None, "", "Bearer secret-token", "secret\nheader", "sëcret"]
)
def test_invalid_environment_token_fails_without_requests_or_secret_output(
    monkeypatch, token
):
    name = "KOIOS_API_KEY"
    monkeypatch.delenv(name, raising=False)
    if token is not None:
        monkeypatch.setenv(name, token)

    def forbidden(request):
        pytest.fail("Invalid credentials must not send a request")

    with pytest.raises(ProviderError) as error:
        client(forbidden, token_env=name)
    assert name in str(error.value)
    if token:
        assert token not in str(error.value)


def test_environment_token_authenticates_requests_without_leaking_to_diagnostics(
    monkeypatch,
):
    token = "test-secret-api-token"
    monkeypatch.setenv("KOIOS_API_KEY", token)
    calls, diagnostics = [], []

    def reply(request):
        calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(
            200 if len(calls) == 1 else 401, json={"echoed-secret": token}
        )

    from types import SimpleNamespace

    p = client(reply, token_env="KOIOS_API_KEY")
    p.observer = SimpleNamespace(
        provider_event=diagnostics.append, stop_requested=False
    )
    p.request("tip")
    with pytest.raises(ProviderError, match="check KOIOS_API_KEY") as error:
        p.request("tip")
    assert len(calls) == 2
    assert token not in str(error.value) + json.dumps(diagnostics)
    p.close()


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

    with pytest.raises(RequestTooLarge) as error:
        client(handler).request("ogmios", body={"data": "f" * 2000})
    assert error.value.actual > error.value.limit == 1000
    assert not isinstance(error.value, ProviderError)


def test_submission_deadline_is_checked_after_identity_and_request_pacing(tmp_path):
    from test_coordinator import prepared

    from defi_kernel.domain import KernelError

    journal, tx, _ = prepared(tmp_path)
    now, calls = [1000.0], []

    def reply(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[GENESIS])

    p = client(reply, max_request_bytes=16384)
    p.enable_testnet_submission = True
    p.profile = replace(p.profile, request_interval=5)
    p.clock = p.monotonic = lambda: now[0]
    p.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    with pytest.raises(KernelError, match="deadline elapsed"):
        p.submit(tx.to_cbor_hex(), deadline=1004)
    assert len(calls) == 1 and calls[0].endswith("genesis")
    assert now[0] == 1005
    journal.close()


def test_wire_diagnostics_exclude_pacing_and_payloads():
    import json
    from types import SimpleNamespace

    now, events = [1000.0], []

    def reply(request):
        now[0] += 2
        return httpx.Response(200, json={"result": "accepted"})

    p = client(reply)
    p.profile = replace(p.profile, request_interval=5)
    p.clock = p.monotonic = lambda: now[0]
    p.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    p._requests.last_request = 1000
    p.observer = SimpleNamespace(
        healthy=True,
        stop_requested=False,
        guard=lambda phase: None,
        provider_event=events.append,
    )
    p.request(
        "ogmios",
        body={"method": "submitTransaction", "params": {"cbor": "private-payload"}},
        retry=False,
    )
    assert events[-1]["started_at"] == 1005
    assert events[-1]["finished_at"] == 1007
    assert events[-1]["seconds"] == 2
    assert events[-1]["method"] == "submitTransaction"
    assert "private-payload" not in json.dumps(events)


@pytest.mark.parametrize(
    "header,delay",
    [
        ("1200", 1200),
        ("Thu, 01 Jan 1970 00:20:00 GMT", 1200),
        ("invalid", 60),
        ("NaN", 60),
    ],
)
def test_rate_limit_cooldown_covers_all_endpoints_and_never_retries_submission(
    header, delay
):
    from defi_kernel.providers import RateLimited

    now, calls = [0.0], []
    status = [429]

    def reply(request):
        calls.append(request)
        return httpx.Response(status[0], headers={"Retry-After": header}, json={})

    p = client(reply)
    p.clock = p.monotonic = lambda: now[0]
    with pytest.raises(RateLimited) as failure:
        p.request("blocks")
    assert failure.value.retry_after_seconds == delay and len(calls) == 1
    with pytest.raises(RateLimited):
        p.request("genesis")
    assert len(calls) == 1
    now[0] += delay
    status[0] = 200
    p.request("genesis")  # One successful endpoint must not reset backoff.
    status[0] = 429
    with pytest.raises(UnknownSubmission):
        p.request("ogmios", body={}, retry=False)
    assert len(calls) == 3
    with pytest.raises(RateLimited) as failure:
        p.request("tip")
    assert failure.value.retry_after_seconds == (1200 if header == "1200" else 120)
    assert len(calls) == 3


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


def test_batched_lookups_obey_payload_limit_and_preserve_requested_order():
    requests = []
    refs = [OutRef(f"{n:064x}", 0) for n in range(5)]

    def handler(request):
        requests.append(request.content)
        selected = json.loads(request.content)["_utxo_refs"]
        return httpx.Response(
            200,
            json=[
                {"tx_hash": r.split("#")[0], "tx_index": 0, "is_spent": False}
                for r in reversed(selected)
            ],
        )

    provider = client(handler, max_request_bytes=180)
    result = provider.utxos(refs)
    assert [r["tx_hash"] for r in result] == [r.tx_hash for r in refs]
    assert len(requests) == 3 and all(len(r) <= 180 for r in requests)
    with pytest.raises(ProviderError, match="Duplicate requested"):
        provider.utxos([refs[0], refs[0]])


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "foreign", "unregistered"]
)
def test_batched_rewards_require_every_unique_registered_account(mutation):
    rows = [
        {"stake_address": a, "status": "registered", "rewards_available": "42"}
        for a in ("a", "b")
    ]
    calls = []

    def reply(request):
        calls.append(json.loads(request.content)["_stake_addresses"])
        return httpx.Response(200, json=rows)

    p = client(reply)
    assert p.stake_rewards_many(["b", "a", "a"]) == {"a": 42, "b": 42}
    assert calls == [["a", "b"]]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[1] = rows[0]
    elif mutation == "foreign":
        rows[1]["stake_address"] = "c"
    else:
        rows[1]["status"] = "not registered"
    with pytest.raises(ProviderError):
        p.stake_rewards_many(["a", "b"])


def test_block_batches_bound_requests_and_reject_ambiguous_evidence():
    calls, response = [], [None]

    def reply(request):
        heights = [int(h) for h in request.url.params["block_height"][4:-1].split(",")]
        calls.append(heights)
        return httpx.Response(
            200,
            json=response[0]
            if response[0] is not None
            else [{"block_height": h, "hash": f"{h:064x}"} for h in reversed(heights)],
        )

    p = client(reply)
    assert len(p.blocks_at_heights(range(1, 53))) == 52
    assert list(map(len, calls)) == [50, 2]
    response[0] = []
    assert p.blocks_at_heights([1]) == {1: None}  # Missing is never canonical evidence.
    for invalid in (
        [{"block_height": 2, "hash": "a" * 64}],
        [{"block_height": 1, "hash": "a" * 64}] * 2,
        [{"block_height": 1}],
        ["invalid"],
    ):
        response[0] = invalid
        with pytest.raises(ProviderError):
            p.blocks_at_heights([1])


def test_reference_identity_cache_rechecks_unspent_bytes_and_rediscovers_spent_refs():
    from defi_kernel.protocols import DEPLOYMENTS

    script = DEPLOYMENTS["swaps-v1"]["script_hash"]
    source = next(
        r
        for r in json.loads(Path("evidence/preprod-swaps-v1-scan.json").read_text())[
            "data"
        ]["rows"]
        if (r.get("reference_script") or {}).get("hash") == script
    )
    replacement = dict(source, tx_hash="e" * 64)
    available, calls = [source], []

    def reply(request):
        endpoint = request.url.path.rsplit("/", 1)[-1]
        calls.append(endpoint)
        if endpoint == "genesis":
            return httpx.Response(200, json=[GENESIS])
        if endpoint == "reference_script_utxos":
            return httpx.Response(
                200,
                json=[
                    {
                        "script_hash": script,
                        "tx_hash": available[0]["tx_hash"],
                        "tx_index": available[0]["tx_index"],
                    }
                ],
            )
        assert endpoint == "utxo_info"
        refs = json.loads(request.content)["_utxo_refs"]
        return httpx.Response(
            200,
            json=[r for r in available if f"{r['tx_hash']}#{r['tx_index']}" in refs],
        )

    p = client(reply)
    assert p.reference_scripts([script])[script] == source
    calls.clear()
    assert p.reference_scripts([script])[script] == source
    assert calls == ["genesis", "utxo_info"]
    available[:] = [replacement]
    assert p.reference_script(script) == replacement
    # A cached identity does not authorize changed bytes at that identity.
    available[0] = replacement | {
        "reference_script": replacement["reference_script"] | {"bytes": "00"}
    }
    from defi_kernel.domain import KernelError

    with pytest.raises(KernelError):
        p.reference_script(script)


def test_datum_witness_lookup_batches_caches_and_preserves_hash_outputs():
    from hashlib import blake2b

    witnesses = {
        blake2b(bytes([i]), digest_size=32).hexdigest(): bytes([i]).hex()
        for i in range(20)
    }
    requests = []

    def reply(request):
        requests.append(request)
        assert len(request.content) <= 512
        hashes = json.loads(request.content)["_datum_hashes"]
        return httpx.Response(
            200, json=[{"datum_hash": h, "bytes": witnesses[h]} for h in hashes]
        )

    p = client(reply, max_request_bytes=512)
    rows = [{"datum_hash": h, "inline_datum": None} for h in witnesses]
    resolved = p.resolve_datums(rows)
    assert len(requests) == 3
    assert all(
        r["inline_datum"] is None and r["datum_cbor"] == witnesses[r["datum_hash"]]
        for r in resolved
    )
    assert all("datum_cbor" not in r for r in rows)
    assert p.resolve_datums(rows) == resolved and len(requests) == 3


@pytest.mark.parametrize(
    "response",
    [
        {},
        [None],
        [{"datum_hash": [], "bytes": "00"}],
        [{"datum_hash": "00" * 32, "bytes": "00"}],
        [{"datum_hash": "wanted", "bytes": "zz"}],
        [{"datum_hash": "wanted", "bytes": "01"}],
        [{"datum_hash": "wanted", "bytes": "00"}] * 2,
    ],
)
def test_datum_witness_lookup_rejects_unverified_responses(response):
    from hashlib import blake2b

    h = blake2b(b"\0", digest_size=32).hexdigest()
    response = (
        [
            dict(r, datum_hash=h)
            if isinstance(r, dict) and r.get("datum_hash") == "wanted"
            else r
            for r in response
        ]
        if isinstance(response, list)
        else response
    )
    p = client(lambda _: httpx.Response(200, json=response))
    with pytest.raises(ProviderError):
        p.resolve_datums([{"datum_hash": h}])
    assert not getattr(p, "_datum_witnesses", {})


def test_missing_datum_is_ineligible_and_rate_limit_is_not_swallowed():
    from defi_kernel.providers import RateLimited

    rows = [{"datum_hash": "00" * 32}]
    assert client(lambda _: httpx.Response(200, json=[])).resolve_datums(rows) == rows
    p = client(lambda _: httpx.Response(429, headers={"Retry-After": "60"}))
    with pytest.raises(RateLimited):
        p.resolve_datums(rows)
