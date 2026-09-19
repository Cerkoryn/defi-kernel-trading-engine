"""Operator accounting and logging failures at the actual submission boundary."""

import json
import logging
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_arbitrage import A, B, edge, make_dano_cycle
from test_coordinator import Provider, prepared
from test_provider import PROFILE, client

from defi_kernel.arbitrage import ArbitrageConfig, Edge, search_routes
from defi_kernel.arbitrage_runtime import DanoQuoteSnapshot
from defi_kernel.coordinator import Coordinator
from defi_kernel.domain import KernelError
from defi_kernel.journal import Journal
from defi_kernel.protocols import decode_dano
from defi_kernel.reporting import (
    PrivateLog,
    Reporter,
    asset_names,
    error_details,
    status_text,
)


def reporter(journal, **kwargs):
    output = []
    r = Reporter(
        journal, ArbitrageConfig(("lovelace", A)), write=output.append, **kwargs
    )
    r.start("evaluated shadow")
    return r, output


@pytest.mark.parametrize("columns", [96, 240])
def test_event_output_deduplicates_and_heartbeats_without_terminal_injection(
    tmp_path, monkeypatch, columns
):
    monkeypatch.setattr(
        "defi_kernel.reporting.shutil.get_terminal_size",
        lambda **_: os.terminal_size((columns, 40)),
    )
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    now = [1000.0]
    r, output = reporter(journal, clock=lambda: now[0], monotonic=lambda: now[0])
    selected = {
        "route_id": "route",
        "path": ["lovelace", A, B, "a" * 56 + b"x\n\x1b[31m".hex(), "lovelace"],
        "fee_lovelace": 123456,
        "net_profit_lovelace": 100001,
    }
    selected["hops"] = [
        {"input_unit": left, "output_unit": right, "venue": venue}
        for left, right, venue in zip(
            selected["path"],
            selected["path"][1:],
            ("swaps-v1", "dano", "dano", "swaps-v1"),
        )
    ]
    assert (
        r.route(selected)
        == r"4-way hop · ADA->A(Swaps), A->B(Dano), B->x\n\u001b[31m(Dano), x\n\u001b[31m->ADA(Swaps)"
    )
    cycle = {
        "stage": "evaluated",
        "observed_at": now[0],
        "selected": selected,
        "search": {"cycles": 2, "cycles_sized": 2},
        "timings": {"search_seconds": 0.5},
    }
    for n in range(1, 4):
        r.cycle_id = n
        r.cycle(dict(cycle))
    assert sum("OPPORTUNITY" in line for line in output) == 1
    assert sum("HEALTH" in line for line in output) == 1
    assert all("\n" not in line and "\x1b" not in line for line in output)
    assert any(r"x\n\u001b" in line for line in output)
    assert any("0.100001 tADA" in line for line in output)
    assert all(len(line) <= columns for line in output)
    if columns == 240:
        assert any(len(line) > 96 for line in output)
    now[0] += 300
    r.health()
    assert sum("HEALTH" in line for line in output) == 2
    assert r.path.exists()
    assert journal.arbitrage_history()["runs"][0]["summary"]["counts"]["polls"] == 3
    r.finish("stopped")
    assert all(line[9:18].strip() in ("SYSTEM", "ARBITRAGE") for line in output)
    assert not any("INFO" in line for line in output)
    assert any(line.split()[1:4] == ["SYSTEM", "START", "starting"] for line in output)
    assert any(
        line.split()[1:4] == ["ARBITRAGE", "OPPORTUNITY", "expected"] for line in output
    )
    records = [json.loads(line) for line in r.path.read_text().splitlines()]
    assert all(
        e["namespace"]
        == (
            "SYSTEM"
            if e["event"] in ("start", "logs", "access", "venues", "stop")
            else "ARBITRAGE"
        )
        for e in records
    )
    journal.close()


def test_private_rotation_is_bounded_and_rejects_symlinks_and_hardlinks(tmp_path):
    path = tmp_path / "logs/events.jsonl"
    handler = PrivateLog(path, max_bytes=256, backups=2)
    for n in range(40):
        message = json.dumps({"n": n, "message": "x" * 70})
        handler.emit(logging.LogRecord("test", logging.INFO, "", 0, message, (), None))
    handler.close()
    files = list(path.parent.iterdir())
    assert len(files) == 3 and sum(f.stat().st_size for f in files) <= 768
    assert all(f.stat().st_mode & 0o777 == 0o600 for f in files)
    assert path.parent.stat().st_mode & 0o777 == 0o700
    for f in files:
        for line in f.read_text().splitlines():
            json.loads(line)
    target = tmp_path / "target"
    target.write_text("preserve")
    target.chmod(0o600)
    alias = path.parent / "alias"
    alias.symlink_to(target)
    with pytest.raises(KernelError):
        PrivateLog(alias)
    alias.unlink()
    os.link(target, alias)
    with pytest.raises(KernelError):
        PrivateLog(alias)
    assert target.read_text() == "preserve"


def test_common_fields_keep_columns_when_values_and_states_change(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "defi_kernel.reporting.shutil.get_terminal_size",
        lambda **_: os.terminal_size((240, 40)),
    )
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    now = [1000.0]
    r, output = reporter(journal, clock=lambda: now[0], monotonic=lambda: now[0])
    r.cycle_id = 1
    r.cycle({"stage": "paused", "observed_at": now[0]})
    now[0] += 660
    r.cycle_id = 2
    r.cycle(
        {
            "stage": "no_opportunity",
            "observed_at": now[0],
            "liquidity_observed_at": now[0] - 10,
            "loss_headroom_lovelace": 41129715,
        }
    )
    r.finish("stopped")
    health = [line for line in output if "HEALTH" in line]
    stop = next(line for line in output if "STOP" in line)
    assert len(health) == 2
    assert "loss headroom 41.129715 tADA" in health[-1]
    assert "cost left" not in "\n".join(output)
    for field in (
        " | run net ",
        " | pending ",
        " | uptime ",
    ):
        assert health[0].index(field) == health[1].index(field)
    for field in (" | run net ", " | pending "):
        assert stop.index(field) == health[0].index(field)
    assert all(len(line) <= 240 for line in output)
    assert all(len(line) <= 180 for line in health)
    assert all("polls" not in line and "reject" not in line for line in health)
    assert "market not observed" in health[0]
    assert "market 10s old" in health[1]
    r.funds(
        {"operating": 16_212_960, "collateral": 5_000_000, "protected": 10_086_077_297}
    )
    funds = next(line for line in output if "FUNDS" in line)
    assert funds.index(" | ") == health[0].index(" | ") == stop.index(" | ")
    r.handler.close()
    events = [json.loads(line) for line in r.path.read_text().splitlines()]
    assert (
        next(e for e in events if e["event"] == "health")["data"]["counts"]["polls"]
        == 1
    )
    journal.close()


@pytest.mark.parametrize("output_mode", ["text", "jsonl"])
def test_blocked_candidate_cleanup_is_quiet_but_diagnostics_remain_complete(
    tmp_path, output_mode, monkeypatch
):
    journal, _, _ = prepared(tmp_path)
    monkeypatch.setattr(
        "defi_kernel.reporting.shutil.get_terminal_size",
        lambda **_: os.terminal_size((96, 40)),
    )
    r, output = reporter(journal, output=output_mode)
    trade = {
        "intent": "test",
        "run_id": r.run_id,
        "txid": "a" * 64,
        "status": "prepared",
        "net_lovelace": None,
        "block_hash": None,
    }
    monkeypatch.setattr(r, "_trades", lambda: {"test": dict(trade)})
    output.clear()
    selected = {
        "route_id": "route",
        "path": ["lovelace", A, "lovelace"],
        "fee_lovelace": 1_200_000,
        "net_profit_lovelace": 2_000_000,
    }
    for n, stage in enumerate(("paused", "recovered", "paused", "submitted"), 1):
        r.cycle_id = n + 1  # Health is independently tested above.
        trade["status"] = "aborted" if stage == "recovered" else "prepared"
        r.cycle(
            {
                "stage": stage,
                "observed_at": r.clock(),
                "selected": selected,
                "reason": "Serialization blocked" if stage == "paused" else None,
            }
        )
    events = [json.loads(line) for line in r.path.read_text().splitlines()]
    assert sum(e["event"] == "cycle" for e in events) == 4
    assert any(
        e["event"] == "transaction" and e["data"]["status"] == "aborted" for e in events
    )
    if output_mode == "text":
        assert sum("[WARN]" in line for line in output) == 1
        assert not any("TRANSACTION" in line or "recovered" in line for line in output)
        assert not any("OPPORTUNITY" in line for line in output)
        assert all(len(line) <= 96 for line in output)
    else:
        records = [json.loads(line) for line in output]
        assert sum(e["event"] == "cycle" for e in records) == 4
        assert any(e["event"] == "transaction" for e in records)
        assert all(e["namespace"] == "ARBITRAGE" for e in records)
    r.finish("stopped")
    journal.close()


def test_log_failure_gates_recovered_bytes_but_cannot_obscure_wire_success(
    tmp_path, monkeypatch
):
    journal, tx, context = prepared(tmp_path)
    r, output = reporter(journal)
    provider = Provider(context)
    coordinator = Coordinator(provider, journal, before_submit=r.guard)

    def full(*args, **kwargs):
        raise OSError("disk full")

    emit = PrivateLog.emit
    monkeypatch.setattr(PrivateLog, "emit", full)
    with pytest.raises(KernelError, match="logging unavailable"):
        coordinator.submit("test")
    assert journal.outbox_entry("test")["attempts"] == 0
    assert journal.outbox_entry("test")["status"] == "prepared"
    assert provider.attempts == 0
    assert any(
        line.split()[1:4] == ["SYSTEM", "LOGGING_FAILED", "blocked"]
        and "[ERROR]" in line
        for line in output
    )
    monkeypatch.setattr(PrivateLog, "emit", emit)

    def submit(encoded):
        provider.attempts += 1
        monkeypatch.setattr(PrivateLog, "emit", full)
        r.event("request", {"status": 200}, visible=False)
        return str(tx.transaction_body.id)

    provider.submit = submit
    coordinator.submit("test")
    assert not r.healthy
    assert journal.outbox_entry("test")["status"] == "submitted"
    assert provider.attempts == 1
    with pytest.raises(KernelError):
        coordinator.submit("test")
    assert provider.attempts == 1
    monkeypatch.setattr(PrivateLog, "emit", emit)
    r.finish("stopped")
    journal.close()


def test_run_history_survives_pruning_and_reverses_confirmations_and_losses(tmp_path):
    journal, tx, _ = prepared(tmp_path)
    first, _ = reporter(journal)
    metadata = {
        "mode": "atomic arbitrage",
        "run_id": first.run_id,
        "fee_lovelace": tx.transaction_body.fee,
        "net_profit_lovelace": 123456,
    }
    journal.db.execute("UPDATE outbox SET metadata=?", (json.dumps(metadata),))
    first.cycle_id = 1
    first.cycle({"stage": "submitted", "observed_at": 1000})
    journal.claim_submission("test")
    first.finish("stopped")
    second, output = reporter(journal)
    journal.record_inclusion("test", "a" * 64, 100, 3, confirmed=True)
    journal.record_arbitrage_outcome("test", "a" * 64, 123456, 1001)
    second.cycle_id = 1
    second.cycle({"stage": "no_opportunity", "observed_at": 1001})
    second.cycle_id += 1
    second.cycle({"stage": "no_opportunity", "observed_at": 1002})
    assert sum("TRANSACTION" in line for line in output) == 1
    assert (
        journal.arbitrage_history(first.run_id)["runs"][0]["realized_net_lovelace"]
        == 123456
    )
    assert (
        journal.arbitrage_history(second.run_id)["runs"][0]["realized_net_lovelace"]
        == 0
    )
    journal.record_transaction_rollback("test")
    assert (
        journal.arbitrage_history(first.run_id)["runs"][0]["realized_net_lovelace"] == 0
    )
    journal.retire_candidate(
        "test",
        "failed",
        [],
        {"block_hash": "b" * 64, "block_height": 101, "confirmations": 3},
    )
    journal.record_arbitrage_outcome("test", "b" * 64, -5000000, 1003)
    assert (
        journal.arbitrage_history(first.run_id)["runs"][0]["realized_net_lovelace"]
        == -5000000
    )
    journal.record_transaction_rollback("test")
    assert (
        journal.arbitrage_history(first.run_id)["runs"][0]["realized_net_lovelace"] == 0
    )
    for n in range(1005):
        journal.record_shadow(n, "{}")
    second.finish("stopped")
    third, _ = reporter(journal)  # Simulate a crash without finish.
    fourth, _ = reporter(journal)
    assert journal.arbitrage_history(third.run_id)["runs"][0]["state"] == "interrupted"
    assert "polls: 1" in status_text(journal.arbitrage_history(first.run_id))
    assert journal.db.execute("SELECT count(*) FROM shadow").fetchone()[0] == 1000
    fourth.finish("stopped")
    third.handler.close()
    journal.close()


def test_trade_milestones_use_saved_routes_and_keep_unverified_profit_out_of_health(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "defi_kernel.reporting.shutil.get_terminal_size",
        lambda **_: os.terminal_size((240, 40)),
    )
    journal, _, _ = prepared(tmp_path)
    r, output = reporter(journal)
    metadata = {
        "mode": "atomic arbitrage",
        "action": "arbitrage",
        "run_id": r.run_id,
        "route_id": "route",
        "path": ["lovelace", A, "lovelace"],
        "hops": [
            {"input_unit": "lovelace", "output_unit": A, "venue": "swaps-v1"},
            {"input_unit": A, "output_unit": "lovelace", "venue": "dano"},
        ],
        "fee_lovelace": 450500,
        "net_profit_lovelace": 6047841,
    }
    journal.db.execute("UPDATE outbox SET metadata=?", (json.dumps(metadata),))
    original_bytes = journal.outbox_entry("test")["signed"]
    journal.claim_submission("test")  # Durable fixture only; no provider submission.
    output.clear()
    r.cycle_id = 2  # Force health explicitly at the reconciliation boundaries below.
    for state in ("submitted", "unknown"):
        journal.mark_transaction("test", state)
        r.cycle(
            {
                "stage": "submitted" if state == "submitted" else "pending",
                "selected": metadata,
                "txid": journal.outbox_entry("test")["txid"],
            }
        )
    submission = next(
        line for line in output if "TRANSACTION" in line and "submitted" in line
    )
    assert (
        "expected net 6.047841 tADA" in submission and "fee 0.450500 tADA" in submission
    )
    assert "2-way hop · ADA->A(Swaps), A->ADA(Dano)" in submission
    assert not any(
        "OPPORTUNITY" in line or "unsettled" in line or "unknown" in line
        for line in output
    )
    assert any(
        "checking" in line and "Awaiting chain evidence" in line for line in output
    )
    r.health(force=True)
    assert "checking" in output[-1] and "pending 1" in output[-1]
    r.finish("stopped")

    # A new reporter recovers hop metadata from the journal, not the last selected plan.
    resumed, resumed_output = reporter(journal)
    saved = resumed._trades()["test"]
    assert saved["hops"] == metadata["hops"]
    assert resumed.route(saved) == "2-way hop · ADA->A(Swaps), A->ADA(Dano)"
    resumed.cycle_id = 2
    journal.record_inclusion("test", "a" * 64, 100, 1, confirmed=False)
    resumed.cycle({"stage": "pending"})
    assert any(
        "confirming" in line and "confirmations 1/3" in line for line in resumed_output
    )
    journal.record_inclusion("test", "a" * 64, 100, 3, confirmed=True)
    resumed.cycle({"stage": "pending"})
    assert any(
        "verifying" in line and "checking wallet result" in line
        for line in resumed_output
    )
    r.health(force=True)
    assert (
        "verifying" in output[-1]
        and "run net 0.000000 tADA" in output[-1]
        and "pending 0" in output[-1]
    )
    journal.record_arbitrage_outcome("test", "a" * 64, 6047841, 1001)
    r.health(
        force=True
    )  # Latest completed poll still says pending, but the journal does not.
    assert "scanning" in output[-1] and "run net 6.047841 tADA" in output[-1]
    resumed.cycle({"stage": "pending"})
    assert any(
        "confirmed" in line and "net 6.047841 tADA" in line for line in resumed_output
    )
    assert journal.outbox_entry("test")["signed"] == original_bytes
    assert journal.outbox_entry("test")["attempts"] == 1
    records = [json.loads(line) for line in r.path.read_text().splitlines()]
    assert any(
        e["event"] == "opportunity" and e["data"]["hops"] == metadata["hops"]
        for e in records
    )
    assert any(
        e["event"] == "transaction"
        and e["data"]["status"] == "unknown"
        and e["display_status"] == "checking"
        for e in records
    )
    resumed.debug = True
    resumed.health(force=True)
    assert any("[INFO]" in line for line in resumed_output)
    journal.record_transaction_rollback("test")
    resumed.cycle({"stage": "pending"})
    assert any("rolled back" in line and "[WARN]" in line for line in resumed_output)
    assert journal.arbitrage_history(r.run_id)["runs"][0]["realized_net_lovelace"] == 0
    resumed.finish("stopped")
    r.handler.close()
    journal.close()


def test_route_labels_disambiguate_names_and_never_guess_historical_venues(tmp_path):
    units = [
        "lovelace",
        "a" * 56 + b"same".hex(),
        "b" * 56 + b"same".hex(),
        "c" * 56,
        "d" * 56 + "ff",
        "e" * 56 + b"ADA".hex(),
    ]
    labels = asset_names(units)
    assert labels == asset_names(reversed(units))
    assert labels["lovelace"] == "ADA"
    assert len(set(labels.values())) == len(units)
    assert all(
        label.startswith("token #")
        for unit, label in labels.items()
        if unit != "lovelace"
    )
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    r, _ = reporter(journal)
    assert r.route({"path": ["lovelace", A]}) == "1-way hop · ADA->A(venue unavailable)"
    # Even an existing venue must not be attached to the wrong leg.
    assert (
        r.route(
            {
                "path": ["lovelace", A],
                "hops": [{"venue": "dano", "input_unit": A, "output_unit": "lovelace"}],
            }
        )
        == "1-way hop · ADA->A(venue unavailable)"
    )
    route = r.route({"path": units})
    assert not any(unit in route for unit in units if unit != "lovelace")
    records = [json.loads(line) for line in r.path.read_text().splitlines()]
    assert any(
        e["event"] == "asset_labels" and units[1] in e["data"]["labels"]
        for e in records
    )
    r.finish("stopped")
    journal.close()


def test_reported_run_replay_excludes_last_pending_gain(tmp_path, monkeypatch):
    """Replay quoted outcomes into a synthetic journal, not a ledger-validation test."""
    from copy import deepcopy

    from pycardano import TransactionInput
    from test_composition import make_candidate

    monkeypatch.setattr(
        "defi_kernel.reporting.shutil.get_terminal_size",
        lambda **_: os.terminal_size((240, 40)),
    )
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    r, output = reporter(journal)
    template = make_candidate()[0]
    gains = [
        371486,
        158579,
        6047841,
        42207147,
        854451,
        52476060,
        43907160,
        40595996,
        34870253,
    ]
    for n, gain in enumerate(gains, 1):
        tx = deepcopy(template)
        # Independent synthetic spending refs keep this bookkeeping fixture isolated.
        tx.transaction_body.inputs = [
            TransactionInput(i.transaction_id, i.index + 1000 * n)
            for i in tx.transaction_body.inputs
        ]
        intent = f"replay-{n}"
        metadata = {
            "mode": "atomic arbitrage",
            "run_id": r.run_id,
            "net_profit_lovelace": gain,
        }
        journal.prepare_candidate(intent, tx, [], metadata)
        journal.mark_transaction(intent, "submitting")
        journal.mark_transaction(intent, "submitted")
        r.cycle_id = n + 1
        r.cycle({"stage": "submitted"})
        if n < len(gains):
            block = f"{n:064x}"
            journal.record_inclusion(intent, block, 100 + n, 3, confirmed=True)
            journal.record_arbitrage_outcome(intent, block, gain, 1000 + n)
            r.cycle({"stage": "pending"})
    r.finish("stopped")
    stop = next(line for line in output if "STOP" in line)
    assert "run net 186.618720 tADA" in stop and "pending 1" in stop
    assert "34.870253" not in stop and "restart reconciles" in stop
    assert sum("TRANSACTION" in line and "confirmed" in line for line in output) == 8
    history = journal.arbitrage_history(r.run_id)
    assert history["runs"][0]["realized_net_lovelace"] == 186618720
    assert len(history["pending_transactions"]) == 1
    assert journal.outbox_entry("replay-9")["status"] == "submitted"
    journal.close()


def test_provider_debug_is_structured_and_omits_secrets_even_when_observer_breaks(
    tmp_path,
):
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    r, output = reporter(journal, output="jsonl", debug=True)
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(
            503 if len(calls) == 1 else 200, json={"secret": "response-secret"}
        )

    p = client(reply)
    p.observer = r
    assert p.request("tip", body={"secret": "request-secret"}) == {
        "secret": "response-secret"
    }
    events = [json.loads(line) for line in output]
    assert any(
        e["event"] == "request" and e["data"].get("status") == 503 for e in events
    )
    assert (
        "request-secret" not in "".join(output)
        and "response-secret" not in r.path.read_text()
    )
    detail = error_details(ValueError("private-key-object-secret"))
    assert "private-key-object-secret" not in json.dumps(detail)
    p.observer = SimpleNamespace(
        stop_requested=False, healthy=True, provider_event=lambda data: 1 / 0
    )
    assert p.request("tip", retry=False) == {"secret": "response-secret"}
    assert not p.observer.healthy
    bad_rpc = client(
        lambda request: httpx.Response(
            200, json={"id": "kernel", "error": {"code": "response-secret"}}
        )
    )
    with pytest.raises(KernelError) as failure:
        bad_rpc.rpc("queryLedgerState/tip")
    assert "response-secret" not in json.dumps(error_details(failure.value))
    bad_rpc.close()
    r.finish("stopped")
    p.close()
    journal.close()


def test_collateral_build_failure_has_safe_actionable_diagnostics():
    from test_transactions import Context

    with pytest.raises(KernelError) as failure:
        make_dano_cycle(
            parameters=replace(Context.protocol_param, collateral_percent=10000)
        )
    detail = error_details(failure.value)
    assert detail["error_type"] == "KernelError"
    assert "allowance 0.040216 tADA (collateral reserve)" in detail["reason"]
    assert "fee " in detail["reason"]
    assert any(f["function"] == "build_transaction" for f in detail["frames"])
    assert "Value(" not in json.dumps(detail)


def test_search_deadline_stop_and_rotation_preserve_route_constraints():
    edges = [
        edge(1, "lovelace", A, 1),
        edge(2, A, "lovelace", 0.5),
        edge(3, "lovelace", B, 1),
        edge(4, B, "lovelace", 0.5),
    ]
    config = ArbitrageConfig(
        ("lovelace", A, B), max_cycles=1, max_trade_lovelace=100, min_profit_lovelace=1
    )
    first, _ = search_routes(edges, config)
    second, _ = search_routes(edges, config, rotation=1)
    assert first[0].hops[0].output_unit != second[0].hops[0].output_unit
    plans, stats = search_routes(edges, config, stop_requested=lambda: True)
    assert plans == [] and stats["stop_reason"] == "stop_requested"
    ticks = iter(range(0, 1000, 11))
    plans, stats = search_routes(edges, config, monotonic=lambda: next(ticks))
    assert plans == [] and stats["stop_reason"] == "time_limit"


def test_final_provider_gate_catches_failure_during_its_identity_read(
    tmp_path, monkeypatch
):
    from test_provider import GENESIS

    journal, tx, _ = prepared(tmp_path)
    r, _ = reporter(journal, debug=True)
    requests = []
    emit = PrivateLog.emit

    def reply(request):
        requests.append(request.url.path)
        monkeypatch.setattr(
            PrivateLog, "emit", lambda *args: (_ for _ in ()).throw(OSError("full"))
        )
        return httpx.Response(200, json=[GENESIS])

    p = client(reply, max_request_bytes=16384)
    p.enable_testnet_submission = True
    p.observer = r
    with pytest.raises(KernelError, match="logging unavailable"):
        p.submit(tx.to_cbor_hex())
    assert len(requests) == 1 and requests[0].endswith("genesis")
    monkeypatch.setattr(PrivateLog, "emit", emit)
    r.finish("stopped")
    p.close()
    journal.close()


def test_terminal_close_at_gate_requests_stop_before_claim(tmp_path):
    journal, _, context = prepared(tmp_path)
    r, _ = reporter(journal, output="jsonl")

    def closed(line):
        raise BrokenPipeError

    r.write = closed
    with pytest.raises(KernelError, match="Stop requested"):
        Coordinator(Provider(context), journal, before_submit=r.guard).submit("test")
    assert journal.outbox_entry("test")["attempts"] == 0
    r.finish("stopped")
    journal.close()


def test_shared_poll_preserves_failure_phase_and_logs_graceful_stop(tmp_path):
    from defi_kernel.runtime import poll

    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    r = Reporter(journal, ArbitrageConfig(("lovelace", A)), write=lambda _: None)
    engine = SimpleNamespace(
        journal=journal,
        reporter=r,
        execute=False,
        clock=lambda: 1000,
        sleep=lambda _: None,
        current_report={"phase": "observation"},
        stop_requested=lambda: r.stop_requested,
    )
    states = []

    def tick():
        r.set_phase("observation")
        raise KernelError("Incomplete liquidity observation")

    def emit(report):
        states.append(journal.status()["run"]["state"])
        r.stop_requested = True

    result = poll(engine, tick=tick, interval=1, iterations=None, emit=emit)
    assert states == ["paused"] and result["phase"] == "observation"
    run = journal.arbitrage_history()["runs"][0]
    assert run["state"] == "stopped" and run["summary"]["counts"]["paused"] == 1
    assert journal.status()["run"]["state"] == "stopped"
    journal.close()


@pytest.mark.parametrize("interrupt", [False, True])
def test_provider_cooldown_waits_between_polls_and_remains_interruptible(
    tmp_path, interrupt
):
    from defi_kernel.providers import RateLimited
    from defi_kernel.runtime import poll

    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    now, calls, sleeps = [1000.0], [], []

    def tick():
        calls.append(now[0])
        if len(calls) == 1:
            raise RateLimited(120)
        return {"stage": "no_opportunity"}

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if interrupt:
            journal.request_stop()

    engine = SimpleNamespace(
        journal=journal, clock=lambda: now[0], sleep=sleep, execute=False
    )
    poll(engine, tick=tick, interval=30, iterations=2, emit=lambda _: None)
    assert calls == ([1000.0] if interrupt else [1000.0, 1120.0])
    assert max(sleeps) == 1
    assert journal.status()["run"]["state"] == "stopped"
    journal.close()


def test_cli_interrupt_is_graceful_and_machine_output_stays_parseable(
    tmp_path, monkeypatch
):
    import signal
    import time

    from test_transactions import OWNER
    from typer.testing import CliRunner

    import defi_kernel.cli as cli

    prior = signal.getsignal(signal.SIGINT)

    def provider(profile, **kwargs):
        return SimpleNamespace(
            profile=profile,
            clock=time.time,
            close=lambda: None,
            verify_identity=lambda: signal.raise_signal(signal.SIGINT),
            tip=lambda: {"block_time": time.time(), "block_no": 100},
        )

    monkeypatch.setattr(cli, "create_provider", provider)
    monkeypatch.setattr(
        "defi_kernel.wallet.load_wallet", lambda *args: ({"address": str(OWNER)}, None)
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "--state-dir",
            str(tmp_path),
            "arbitrage",
            "--manifest",
            "not-opened",
            "--strategy",
            "examples/preprod-arbitrage.json",
            "--output",
            "jsonl",
        ],
    )
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["event"] == "stop"
    assert not any(e["event"] == "submission_check" for e in events)
    assert signal.getsignal(signal.SIGINT) == prior


@pytest.mark.parametrize("native", [False, True])
def test_quote_snapshot_parity_and_isolation_for_rewards_and_boundaries(native):
    _, _, _, dependencies, context, _ = make_dano_cycle(token_pair=native)
    pools = []
    for row in dependencies:
        try:
            pools.append(decode_dano(row, context.profile, platform_fee_rate=1000))
        except KernelError:
            continue
    assert len(pools) == 2
    for pool in pools:
        snapshot = DanoQuoteSnapshot.from_pool(pool)
        for reward in (0, 123456):
            for direction in (True, False):
                minimum = (
                    pool._datum.min_x_change if direction else pool._datum.min_y_change
                )
                original = Edge(
                    SimpleNamespace(),
                    "dano",
                    pool.unit_a if direction else pool.unit_b,
                    pool.unit_b if direction else pool.unit_a,
                    2**63 - 1,
                    pool=pool,
                    reward=reward,
                )
                cached = replace(original, pool=snapshot)
                for q in {
                    1,
                    max(1, minimum - 1),
                    minimum,
                    minimum + 1,
                    1000000,
                    pool.reserve_a,
                    pool.reserve_b,
                    2**63 - 1,
                }:
                    assert original.quote(q) == cached.quote(q)
        before = snapshot.compute_pool_change(1000000, 0)
        pool.assets.root[pool.unit_a] += 10000000
        assert snapshot.compute_pool_change(1000000, 0) == before
        assert DanoQuoteSnapshot.from_pool(pool).reserve_a != snapshot.reserve_a
        datum = pool._datum
        pool.datum_cbor = replace(
            datum, lp_fee_rate=datum.lp_fee_rate + 1
        ).to_cbor_hex()
        assert (
            DanoQuoteSnapshot.from_pool(pool)._datum.lp_fee_rate
            == datum.lp_fee_rate + 1
        )
        assert snapshot._datum.lp_fee_rate == datum.lp_fee_rate
