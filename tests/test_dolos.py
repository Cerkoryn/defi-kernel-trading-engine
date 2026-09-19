"""Dolos wire contract and recovery tests; no network, keys, or live submission."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pycardano import Address, Network, VerificationKeyHash

from defi_kernel.backends import ProviderBundle, create_provider
from defi_kernel.chain_context import protocol_parameters, to_utxo
from defi_kernel.config import load_profile, provider_url
from defi_kernel.coordinator import Coordinator
from defi_kernel.dolos import Dolos
from defi_kernel.domain import KernelError, OutRef, RequestTooLarge
from defi_kernel.providers import (
    EvidenceUnavailable,
    Koios,
    ProviderError,
    ProviderLag,
    RateLimited,
    UnknownSubmission,
)
from defi_kernel.slots import SlotClock

DEPLOYED_PROFILE = load_profile(Path("examples/preprod-dolos.toml"))
# Exercise the original five-role contract as well as the explicit ledger binding.
PROFILE = replace(
    DEPLOYED_PROFILE,
    capabilities={
        k: v for k, v in DEPLOYED_PROFILE.capabilities.items() if k != "ledger"
    },
)
SETTINGS = PROFILE.providers["nas"]
PARAMS = json.loads(Path("evidence/preprod-ogmios-parameters.json").read_text())[
    "result"
]
GENESIS = {
    "network_magic": 1,
    "system_start": 1654041600,
    "active_slots_coefficient": 0.05,
    "max_lovelace_supply": "45000000000000000",
    "epoch_length": 432000,
    "slot_length": 1,
    "slots_per_kes_period": 129600,
    "update_quorum": 5,
    "max_kes_evolutions": 62,
    "security_param": 2160,
}
TIP = {
    "hash": "a" * 64,
    "height": 100,
    "slot": 90000000,
    "time": 1788800000,
    "epoch": 200,
}
BF_PARAMS = {
    "epoch": 200,
    "min_fee_a": 44,
    "min_fee_b": 155381,
    "max_block_size": 90112,
    "max_tx_size": 16384,
    "max_block_header_size": 1100,
    "key_deposit": "2000000",
    "pool_deposit": "500000000",
    "a0": 0.3,
    "rho": 0.003,
    "tau": 0.2,
    "min_pool_cost": "75000000",
    "protocol_major_ver": 11,
    "protocol_minor_ver": 0,
    "coins_per_utxo_size": "4310",
    "min_utxo": "4310",
    "price_mem": 0.0577,
    "price_step": 0.0000721,
    "max_tx_ex_mem": "17500000",
    "max_tx_ex_steps": "10000000000",
    "max_block_ex_mem": "77500000",
    "max_block_ex_steps": "20000000000",
    "max_val_size": "5000",
    "collateral_percent": 150,
    "max_collateral_inputs": 3,
    "min_fee_ref_script_cost_per_byte": 15,
    "cost_models_raw": {
        "PlutusV" + k[-1]: v for k, v in PARAMS["plutusCostModels"].items()
    },
}
OWNER = str(Address(VerificationKeyHash(b"p" * 28), network=Network.TESTNET))


def match(row):
    """MiniKupo's documented/source-reviewed wire shape, populated from public evidence."""
    return {
        "transaction_id": row["tx_hash"],
        "output_index": row["tx_index"],
        "transaction_index": 0,
        "address": row["address"],
        "value": {
            "coins": int(row["value"]),
            "assets": {
                a["policy_id"] + "." + a["asset_name"]: int(a["quantity"])
                for a in row["asset_list"]
            },
        },
        "datum_hash": row.get("datum_hash"),
        "datum_type": "inline"
        if row.get("inline_datum")
        else "hash"
        if row.get("datum_hash")
        else None,
        "datum": (row.get("inline_datum") or {}).get("bytes", row.get("datum_cbor")),
        "script_hash": (row.get("reference_script") or {}).get("hash"),
        "script": {
            "language": "plutus:v" + row["reference_script"]["type"][-1],
            "script": row["reference_script"]["bytes"],
        }
        if row.get("reference_script")
        else None,
        "created_at": {"slot_no": TIP["slot"], "header_hash": TIP["hash"]},
        "spent_at": None,
    }


def client(handler=None, *, settings=SETTINGS, profile=PROFILE, **kwargs):
    calls = []

    def reply(r):
        calls.append(r)
        if handler:
            value = handler(r)
            if value is not None:
                return value
        data = {
            "/genesis": GENESIS,
            "/blocks/latest": TIP,
            "/blocks/100": TIP,
            "/blocks/" + TIP["hash"]: TIP,
            "/epochs/latest/parameters": BF_PARAMS,
            "/health": {
                "connection_status": "connected",
                "most_recent_checkpoint": TIP["slot"],
            },
        }
        if r.url.path in data:
            return httpx.Response(200, json=data[r.url.path])
        raise AssertionError(str(r.url))

    transport = httpx.MockTransport(reply)
    p = Dolos(
        profile,
        settings,
        client=httpx.Client(transport=transport),
        kupo_client=httpx.Client(transport=transport),
        sleep=lambda _: None,
        clock=lambda: TIP["time"] + 10,
        **kwargs,
    )
    return p, calls


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://8.8.8.8",
        "http://169.254.169.254",
        "https://user:secret@example.com",
        "http://192.168.0.3:bad",
        "https://example.com:99999",
        "http://localhost",
        "https://example.com/?key=secret",
    ],
)
def test_private_http_does_not_weaken_public_url_validation(url):
    with pytest.raises(KernelError):
        provider_url(url, allow_private_http=True)


def test_explicit_binding_preserves_journal_and_rejects_unsigned_dolos_evaluation():
    legacy = load_profile(Path("examples/preprod-test.toml"))
    assert legacy.chain_id == PROFILE.chain_id
    assert legacy.state_path(Path("state")) == PROFILE.state_path(Path("state"))
    assert provider_url(SETTINGS.url, allow_private_http=True) == SETTINGS.url
    with pytest.raises(KernelError):
        provider_url(SETTINGS.url)
    with pytest.raises(KernelError, match="unsigned evaluation"):
        replace(PROFILE, capabilities={**PROFILE.capabilities, "evaluation": "nas"})
    with pytest.raises(KernelError, match="bindings"):
        replace(PROFILE, capabilities={"chain": "nas"})


def test_real_venue_outputs_and_reference_scripts_preserve_bytes():
    evidence = json.loads(Path("evidence/preprod-direct-venues.json").read_text())
    rows = [*evidence["references"], evidence["config"]]
    for value in evidence["rows"].values():
        rows.extend(value if isinstance(value, list) else [value])
    for row in rows:
        p, _ = client(
            lambda r: (
                httpx.Response(200, json=[match(row)])
                if r.url.path.startswith("/matches/")
                else None
            )
        )
        got = p.credential_utxos(row["payment_cred"]).rows
        assert len(got) == 1
        assert (
            to_utxo(got[0], PROFILE).output.to_cbor()
            == to_utxo(row, PROFILE).output.to_cbor()
        )
        p.close()


def test_unstaked_output_is_indexed_and_not_dropped():
    row = {
        "tx_hash": "b" * 64,
        "tx_index": 0,
        "address": OWNER,
        "value": "3000000",
        "asset_list": [],
    }
    p, _ = client(
        lambda r: (
            httpx.Response(200, json=[match(row)])
            if r.url.path.startswith("/matches/")
            else None
        )
    )
    assert (
        len(p.credential_utxos(str(Address.from_primitive(OWNER).payment_part)).rows)
        == 1
    )
    assert p.address_utxos(OWNER).rows[0]["address"] == OWNER
    p.close()


@pytest.mark.parametrize(
    "failure",
    ["duplicate", "datum", "foreign", "spent", "limit", "rollback", "index_lag"],
)
def test_incomplete_or_inconsistent_index_evidence_fails_closed(failure):
    row = {
        "tx_hash": "b" * 64,
        "tx_index": 0,
        "address": OWNER,
        "value": "3000000",
        "asset_list": [],
    }
    data = [match(row)]
    if failure == "duplicate":
        data *= 2
    if failure == "limit":
        data *= 10001
    if failure == "datum":
        data[0].update(datum_type="inline", datum="00", datum_hash="c" * 64)
    if failure == "spent":
        data[0]["spent_at"] = {"slot_no": 1}
    if failure == "foreign":
        data[0]["address"] = str(
            Address(VerificationKeyHash(b"q" * 28), network=Network.TESTNET)
        )

    def reply(r):
        if r.url.path.startswith("/matches/"):
            return httpx.Response(200, json=data)
        if failure == "rollback" and r.url.path == "/blocks/100":
            return httpx.Response(200, json={**TIP, "hash": "c" * 64})
        if failure == "index_lag" and r.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "connection_status": "connected",
                    "most_recent_checkpoint": TIP["slot"] - 10,
                },
            )

    p, _ = client(reply)
    with pytest.raises(KernelError):
        p.address_utxos(OWNER)
    p.close()


def test_registered_undelegated_rewards_are_valid_and_absence_is_not_zero():
    value = {
        "stake_address": "stake_test",
        "registered": True,
        "active": False,
        "withdrawable_amount": "123",
    }
    p, _ = client(
        lambda r: (
            httpx.Response(200, json=value)
            if r.url.path.startswith("/accounts/")
            else None
        )
    )
    assert p.stake_rewards_many(["stake_test"]) == {"stake_test": 123}
    value["registered"] = False
    with pytest.raises(ProviderError):
        p.stake_rewards_many(["stake_test"])
    value["registered"] = True
    del value["withdrawable_amount"]
    with pytest.raises(ProviderError):
        p.stake_rewards_many(["stake_test"])
    p.close()


def test_protocol_parameters_match_exact_cost_arrays_and_fee_rules():
    p, _ = client()
    assert protocol_parameters(p.protocol_parameters()) == protocol_parameters(PARAMS)
    p.close()


def test_era_tip_is_extended_only_within_bounded_forecast():
    data = [
        {
            "start": {"time": 0, "slot": 0, "epoch": 0},
            "end": {"time": 1728000, "slot": 86400, "epoch": 4},
            "parameters": {"slot_length": 20, "epoch_length": 21600, "safe_zone": 4320},
        },
        {
            "start": {"time": 1728000, "slot": 86400, "epoch": 4},
            "end": {"time": 1728000, "slot": 86400, "epoch": 4},
            "parameters": {
                "slot_length": 1,
                "epoch_length": 432000,
                "safe_zone": 129600,
            },
        },
        {
            "start": {"time": 1728000, "slot": 86400, "epoch": 4},
            "end": {"time": 1729000, "slot": 87400, "epoch": 4},
            "parameters": {
                "slot_length": 1,
                "epoch_length": 432000,
                "safe_zone": 129600,
            },
        },
    ]
    p, _ = client(
        lambda r: (
            httpx.Response(200, json=data) if r.url.path == "/network/eras" else None
        )
    )
    clock = SlotClock(PROFILE, p.era_summaries())
    assert clock.slot_at_ms((PROFILE.system_start + 1729200) * 1000) == 87600
    with pytest.raises(KernelError, match="horizon"):
        clock.slot_at_ms((PROFILE.system_start + 1733000) * 1000)
    p.close()


def bundle(p, reply):
    evaluator = Koios(
        PROFILE,
        base_url="https://evaluator.test",
        client=httpx.Client(transport=httpx.MockTransport(reply)),
        sleep=lambda _: None,
    )
    evaluator.clock = p.clock
    return ProviderBundle(PROFILE, {"nas": p, "koios-evaluator": evaluator})


@pytest.mark.parametrize(
    "missing", ["verify_identity", "block_at_height", "protocol_parameters"]
)
def test_evaluator_requires_identity_anchor_and_parameter_evidence(missing):
    p, calls = client()
    evaluator = Koios(PROFILE, base_url="https://evaluator.test")
    setattr(evaluator, missing, None)
    try:
        with pytest.raises(ProviderError, match="implement evaluation"):
            ProviderBundle(PROFILE, {"nas": p, "koios-evaluator": evaluator})
        assert calls == []
    finally:
        evaluator.close()
        p.close()


def test_evaluator_cooldown_does_not_block_local_confirmation_queries():
    p, local_calls = client()
    calls = []

    def reply(r):
        calls.append(r)
        return httpx.Response(429, headers={"Retry-After": "600"})

    b = bundle(p, reply)
    with pytest.raises(RateLimited):
        b.evaluate("84a0a0f5f6")
    b.verify_identity()
    assert b.tip()["hash"] == TIP["hash"]
    with pytest.raises(RateLimited):
        b.evaluate("84a0a0f5f6")
    assert len(calls) == 1
    assert all("Authorization" not in r.headers for r in local_calls)
    b.close()


@pytest.mark.parametrize("failure", [None, "network", "anchor", "cooldown"])
def test_explicit_ledger_authority_preserves_local_recovery(failure):
    p, local_calls = client()
    requests = []

    def reply(r):
        requests.append(r)
        if failure == "cooldown":
            return httpx.Response(429, headers={"Retry-After": "600"})
        if r.url.path == "/genesis":
            return httpx.Response(
                200,
                json=[
                    {
                        "networkmagic": 2 if failure == "network" else 1,
                        "systemstart": PROFILE.system_start,
                        "networkid": "testnet",
                    }
                ],
            )
        if r.url.path == "/tip":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": TIP["hash"],
                        "block_no": 100,
                        "block_time": TIP["time"],
                    }
                ],
            )
        if r.url.path == "/blocks":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "b" * 64 if failure == "anchor" else TIP["hash"],
                        "block_height": 100,
                    }
                ],
            )
        if r.url.path == "/ogmios":
            method = json.loads(r.content)["method"]
            result = {
                "queryLedgerState/protocolParameters": PARAMS,
                "queryLedgerState/eraSummaries": [{"source": "ledger"}],
            }[method]
            return httpx.Response(200, json={"id": "kernel", "result": result})
        if r.url.path == "/account_info":
            return httpx.Response(
                200,
                json=[
                    {
                        "stake_address": "test-stake",
                        "status": "registered",
                        "rewards_available": "123",
                    }
                ],
            )
        raise AssertionError(str(r.url))

    original = bundle(p, reply)
    assert original.bindings["ledger"] is p  # Omitted binding preserves old behavior.
    b = ProviderBundle(DEPLOYED_PROFILE, original.providers)
    submissions = []
    b.bindings["submission"].submit = lambda cbor, *, deadline=None: submissions.append(
        (cbor, deadline)
    )
    try:
        assert b.bindings["ledger"] is b.bindings["evaluation"]
        if failure:
            with pytest.raises(KernelError):
                b.protocol_parameters()
            assert not any(r.url.path == "/ogmios" for r in requests)
        else:
            assert protocol_parameters(b.protocol_parameters()) == protocol_parameters(
                PARAMS
            )
            assert b.era_summaries() == [{"source": "ledger"}]
            assert b.stake_rewards_many(["test-stake"]) == {"test-stake": 123}
        if failure:
            with pytest.raises(KernelError):
                b.submit("original-signed-bytes", deadline=123)
            assert submissions == []
        else:
            b.submit("original-signed-bytes", deadline=123)
            assert submissions == [("original-signed-bytes", 123)]
        # Monitoring never depends on ledger/evaluator availability, including a 429.
        remote_count = len(requests)
        b.verify_identity()
        assert b.tip()["hash"] == TIP["hash"]
        assert b.block_at_height(100)["hash"] == TIP["hash"]
        assert len(requests) == remote_count
        assert not any(r.url.path == "/epochs/latest/parameters" for r in local_calls)
    finally:
        b.close()


@pytest.mark.parametrize("failure", ["network", "anchor", "parameters"])
def test_evaluator_disagreement_prevents_building(failure):
    p, _ = client()
    calls = []

    def reply(r):
        calls.append(r)
        if r.url.path == "/genesis":
            return httpx.Response(
                200,
                json=[
                    {
                        "networkmagic": 2 if failure == "network" else 1,
                        "systemstart": PROFILE.system_start,
                        "networkid": "testnet",
                    }
                ],
            )
        if r.url.path == "/tip":
            return httpx.Response(
                200,
                json=[
                    {"hash": TIP["hash"], "block_no": 100, "block_time": TIP["time"]}
                ],
            )
        if r.url.path == "/blocks":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "b" * 64 if failure == "anchor" else TIP["hash"],
                        "block_height": 100,
                    }
                ],
            )
        if r.url.path == "/ogmios":
            params = deepcopy(PARAMS)
            params["minFeeCoefficient"] += 1
            return httpx.Response(200, json={"id": "kernel", "result": params})
        raise AssertionError(str(r.url))

    b = bundle(p, reply)
    with pytest.raises(KernelError) as error:
        b.protocol_parameters()
    if failure == "parameters":
        assert "min_fee_coefficient" in str(error.value)
    assert not any(
        r.method == "POST"
        and json.loads(r.content).get("method") == "evaluateTransaction"
        for r in calls
    )
    b.close()


def test_missing_reference_hint_and_spent_hint_never_fall_back():
    p, calls = client(
        lambda r: (
            httpx.Response(200, json=[]) if r.url.path.startswith("/matches/") else None
        )
    )
    with pytest.raises(EvidenceUnavailable, match="hint missing"):
        p.reference_scripts(["a" * 56])
    with pytest.raises(EvidenceUnavailable, match="spent or mismatched"):
        p.reference_scripts([next(iter(SETTINGS.reference_scripts))])
    assert all(r.url.host == "192.168.0.3" for r in calls)
    p.close()


def transaction_reply(tx, r):
    txid = str(tx.id)
    if r.url.path == f"/txs/{txid}":
        return httpx.Response(
            200,
            json={
                "hash": txid,
                "block": TIP["hash"],
                "block_height": TIP["height"],
                "slot": TIP["slot"],
                "index": 2,
            },
        )
    if r.url.path == f"/txs/{txid}/cbor":
        return httpx.Response(200, json={"cbor": tx.to_cbor_hex()})


def test_truncated_cbor_retains_journal_then_recovers_without_resubmission(tmp_path):
    from test_coordinator import prepared

    journal, tx, _ = prepared(tmp_path)
    journal.claim_submission("test")
    before = dict(journal.outbox_entry("test"))
    bad = [True]

    def reply(r):
        if bad[0] and r.url.path.endswith("/cbor"):
            return httpx.Response(200, json={"cbor": "84a0a0d9010281"})
        if r.url.path == "/blocks/latest":
            return httpx.Response(200, json={**TIP, "height": 103})
        return transaction_reply(tx, r)

    p, calls = client(reply)
    with pytest.raises(KernelError, match="CBOR"):
        Coordinator(p, journal).reconcile("test")
    assert dict(journal.outbox_entry("test")) == before
    bad[0] = False
    assert Coordinator(p, journal).reconcile("test") == "confirmed"
    assert journal.outbox_entry("test")["attempts"] == 1
    assert not any(r.method == "POST" for r in calls)
    journal.close()
    p.close()


@pytest.mark.parametrize("failure", ["timeout", "429", "hash", "size", "deadline"])
def test_submission_is_one_attempt_with_exact_bytes_or_no_transmission(
    tmp_path, failure
):
    from test_coordinator import prepared

    journal, tx, _ = prepared(tmp_path)

    def reply(r):
        if r.url.path == "/tx/submit":
            assert r.content == tx.to_cbor()
            assert r.headers["Content-Type"] == "application/cbor"
            if failure == "timeout":
                raise httpx.ReadTimeout("lost response")
            return httpx.Response(429 if failure == "429" else 200, json="f" * 64)

    profile = replace(PROFILE, max_request_bytes=1) if failure == "size" else PROFILE
    p, calls = client(reply, profile=profile, enable_testnet_submission=True)
    error = (
        RequestTooLarge
        if failure == "size"
        else KernelError
        if failure == "deadline"
        else UnknownSubmission
    )
    with pytest.raises(error):
        p.submit(
            tx.to_cbor_hex(), deadline=TIP["time"] if failure == "deadline" else None
        )
    assert sum(r.url.path == "/tx/submit" for r in calls) == (
        0 if failure in ("size", "deadline") else 1
    )
    journal.close()
    p.close()


def test_absent_live_output_never_proves_spending(tmp_path):
    from test_coordinator import prepared

    journal, tx, _ = prepared(tmp_path)
    ref = OutRef(str(tx.id), 0)

    def reply(r):
        if r.url.path.endswith("/utxos"):
            return httpx.Response(
                200,
                json={
                    "hash": str(tx.id),
                    "outputs": [
                        {"output_index": 0, "address": OWNER, "consumed_by_tx": None}
                    ],
                },
            )
        if r.url.path.startswith("/matches/"):
            return httpx.Response(200, json=[])
        return transaction_reply(tx, r)

    p, _ = client(reply)
    with pytest.raises(EvidenceUnavailable, match="does not prove spending"):
        p.input_states([ref])
    journal.close()
    p.close()


def test_factory_keeps_hosted_credentials_off_local_apis(monkeypatch):
    from defi_kernel import providers

    monkeypatch.setenv("KOIOS_API_KEY", "test-token-never-log")
    original_client = httpx.Client
    requests = []

    def reply(r):
        requests.append(r)
        if r.url.host == "preprod.koios.rest":
            assert r.headers.get("Authorization") == "Bearer test-token-never-log"
            return httpx.Response(
                200,
                json=[
                    {
                        "networkmagic": 1,
                        "systemstart": PROFILE.system_start,
                        "networkid": "testnet",
                    }
                ],
            )
        assert "Authorization" not in r.headers
        assert r.url.host == "192.168.0.3"
        return httpx.Response(200, json=GENESIS)

    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(reply), **kwargs
        ),
    )
    p = create_provider(PROFILE)
    p.verify_identity()
    p.bindings["evaluation"].verify_identity()
    assert len(requests) == 2
    assert p.bindings["chain"] is p.bindings["index"] is p.bindings["observation"]
    p.close()
    assert all(
        x.http.client.is_closed if isinstance(x, Dolos) else x.client.is_closed
        for x in p.providers.values()
    )


def test_hybrid_chain_context_builds_from_local_data_and_checks_evaluator():
    # A consistent synthetic ledger history isolates the adapter contract from wall time.
    era = [
        {
            "start": {"time": 0, "slot": 0, "epoch": 0},
            "end": {"time": TIP["slot"], "slot": TIP["slot"], "epoch": 208},
            "parameters": {
                "slot_length": 1,
                "epoch_length": 432000,
                "safe_zone": 129600,
            },
        }
    ]
    p, _ = client(
        lambda r: (
            httpx.Response(200, json=era) if r.url.path == "/network/eras" else None
        )
    )

    def reply(r):
        if r.url.path == "/genesis":
            return httpx.Response(
                200,
                json=[
                    {
                        "networkmagic": 1,
                        "systemstart": PROFILE.system_start,
                        "networkid": "testnet",
                    }
                ],
            )
        if r.url.path == "/tip":
            return httpx.Response(
                200,
                json=[
                    {"hash": TIP["hash"], "block_no": 100, "block_time": TIP["time"]}
                ],
            )
        if r.url.path == "/blocks":
            return httpx.Response(
                200, json=[{"hash": TIP["hash"], "block_height": 100}]
            )
        if r.url.path == "/ogmios":
            return httpx.Response(200, json={"id": "kernel", "result": PARAMS})
        raise AssertionError(str(r.url))

    b = bundle(p, reply)
    from defi_kernel.chain_context import ProviderChainContext

    context = ProviderChainContext(b)
    assert context.protocol_param == protocol_parameters(PARAMS)
    assert context.genesis_param.network_magic == 1
    assert context.last_block_slot == TIP["slot"]
    b.close()


def test_history_pages_are_bounded_and_never_silently_deduplicated():
    page = [{"tx_hash": "b" * 64, "block_height": 90}]
    repeated = [False]

    def reply(r):
        if r.url.path.endswith("/transactions"):
            assert "from" not in r.url.params
            return httpx.Response(
                200, json=page if r.url.params["page"] == "1" or repeated[0] else []
            )

    p, _ = client(reply)
    assert p.address_transactions(OWNER).rows == tuple(page)
    repeated[0] = True
    with pytest.raises(ProviderError, match="repeated"):
        p.address_transactions(OWNER)
    p.close()


def test_spender_identity_alone_cannot_release_reservations(tmp_path):
    from test_coordinator import prepared

    from defi_kernel.signing import ref_text

    journal, tx, _ = prepared(tmp_path)

    def reply(r):
        if r.url.path == "/blocks/latest":
            return httpx.Response(200, json={**TIP, "height": 103})
        return transaction_reply(tx, r)

    p, _ = client(reply)
    consumed = ref_text(next(iter(tx.transaction_body.inputs)))
    proof = p.confirmed_spender(
        consumed, {"consumed_by_tx": str(tx.id)}, exclude="f" * 64
    )
    assert proof["consumed"] == consumed
    with pytest.raises(ProviderError, match="does not consume"):
        p.confirmed_spender(
            "e" * 64 + "#0", {"consumed_by_tx": str(tx.id)}, exclude="f" * 64
        )
    p.block_at_height = lambda _: {"hash": "c" * 64}
    assert (
        p.confirmed_spender(consumed, {"consumed_by_tx": str(tx.id)}, exclude="f" * 64)
        is None
    )
    p.close()
    journal.close()


def test_stale_tip_and_missing_archive_pause_instead_of_claiming_ready():
    p, _ = client(
        lambda r: (
            httpx.Response(200, json={**TIP, "time": TIP["time"] - 301})
            if r.url.path == "/blocks/latest"
            else None
        )
    )
    with pytest.raises(ProviderLag, match="not fresh"):
        p.tip()
    p.close()
    p, _ = client(
        lambda r: httpx.Response(404) if r.url.path.startswith("/txs/") else None
    )
    with pytest.raises(EvidenceUnavailable):
        p.transaction_cbor("b" * 64)
    with pytest.raises(EvidenceUnavailable):
        p.input_states([OutRef("b" * 64, 0)])
    p.close()


def test_separately_bound_observation_cannot_confirm_on_another_fork():
    p, _ = client()
    profile = replace(
        PROFILE, capabilities={**PROFILE.capabilities, "observation": "koios-evaluator"}
    )

    def reply(r):
        if r.url.path == "/genesis":
            return httpx.Response(
                200,
                json=[
                    {
                        "networkmagic": 1,
                        "systemstart": PROFILE.system_start,
                        "networkid": "testnet",
                    }
                ],
            )
        if r.url.path == "/tip":
            return httpx.Response(
                200,
                json=[{"hash": "b" * 64, "block_no": 100, "block_time": TIP["time"]}],
            )
        if r.url.path == "/blocks":
            return httpx.Response(200, json=[{"hash": "b" * 64, "block_height": 100}])
        raise AssertionError(str(r.url))

    evaluator = Koios(
        profile,
        base_url="https://evaluator.test",
        client=httpx.Client(transport=httpx.MockTransport(reply)),
    )
    b = ProviderBundle(profile, {"nas": p, "koios-evaluator": evaluator})
    with pytest.raises(ProviderLag, match="canonical anchor"):
        b.verify_identity()
    b.close()


def test_point_lookup_skips_unrequested_native_script_outputs():
    ref = OutRef("b" * 64, 0)
    data = [
        match(
            {
                "tx_hash": ref.tx_hash,
                "tx_index": 0,
                "address": OWNER,
                "value": "3000000",
                "asset_list": [],
            }
        )
    ]
    unrelated = deepcopy(data[0])
    unrelated.update(
        output_index=1,
        script_hash="a" * 56,
        script={"language": "native", "script": "00"},
    )
    data.append(unrelated)
    p, _ = client(
        lambda r: (
            httpx.Response(200, json=data)
            if r.url.path.startswith("/matches/")
            else None
        )
    )
    assert len(p.utxos([ref])) == 1
    p.close()


def test_read_only_qualification_does_not_create_a_journal_or_read_keys(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from defi_kernel import cli, wallet

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only qualification must not open keys or a journal")

    closed = []
    provider = SimpleNamespace(
        profile=PROFILE,
        clock=lambda: TIP["time"] + 10,
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(cli, "create_provider", lambda profile: provider)
    monkeypatch.setattr(cli, "Journal", forbidden)
    monkeypatch.setattr(wallet, "load_private_key", forbidden)
    from defi_kernel import chain_context

    monkeypatch.setattr(
        chain_context,
        "ProviderChainContext",
        lambda _: SimpleNamespace(
            _tip={"block_time": TIP["time"]}, protocol_param=protocol_parameters(PARAMS)
        ),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "--config",
            "examples/preprod-dolos.toml",
            "--state-dir",
            str(tmp_path),
            "provider-check",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["mode"].startswith("read-only")
    assert closed == [True] and not list(tmp_path.iterdir())
