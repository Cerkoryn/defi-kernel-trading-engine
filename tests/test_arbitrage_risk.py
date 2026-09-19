"""Profit authorization, canonical loss accounting, and durable failure stops."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
from pycardano import (
    PlutusV2Script,
    TransactionBuilder,
    TransactionInput,
    TransactionOutput,
    UTxO,
)
from test_arbitrage import A, make_dano_cycle, make_order_cycle
from test_coordinator import Provider, prepared
from test_transactions import OWNER, PROFILE, Context

from defi_kernel.arbitrage import ArbitrageConfig
from defi_kernel.arbitrage_risk import candidate_loss, check_candidate, drawdown
from defi_kernel.coordinator import Coordinator
from defi_kernel.domain import KernelError
from defi_kernel.journal import Journal
from defi_kernel.signing import inspect_transaction, transaction_resources
from defi_kernel.transactions import CompositionBuilder


def record(journal, source, metadata, dependencies, *, net=None, status="confirmed"):
    """Projection fixture: distinct transaction bodies in settlement order."""
    n = journal.db.execute("SELECT count(*) FROM transactions").fetchone()[0] + 1
    tx = deepcopy(source)
    tx.transaction_body.ttl += n
    intent, block = str(n), f"{n:064x}"
    journal.db.execute(
        "INSERT INTO transactions VALUES(?,?,?)",
        (intent, str(tx.transaction_body.id), status),
    )
    journal.db.execute(
        "INSERT INTO outbox(intent,unsigned,dependencies,metadata,expires_slot,block_height,block_hash) VALUES(?,?,?,?,?,?,?)",
        (
            intent,
            tx.to_cbor(),
            json.dumps(dependencies),
            json.dumps(metadata),
            tx.transaction_body.ttl,
            n,
            block,
        ),
    )
    if net is not None:
        journal.record_arbitrage_outcome(intent, block, net, n)
    return intent, tx


def test_dynamic_fee_above_old_cap_profit_boundary_and_collateral():
    params = replace(Context.protocol_param, min_fee_constant=1_500_000)
    tx, auth, metadata, _, context, resolved = make_order_cycle(3, parameters=params)
    assert tx.transaction_body.fee > 1_500_000
    assert inspect_transaction(tx, auth, context, resolved)["lovelace"] >= 100_000
    assert tx.transaction_body.total_collateral <= 5_000_000
    assert tx.transaction_body.collateral_return is not None
    with pytest.raises(KernelError, match="operator fee cap"):
        make_order_cycle(3, parameters=params, max_fee=1_500_000)
    with pytest.raises(KernelError, match="drawdown headroom"):
        make_order_cycle(3, parameters=params, loss_headroom=1_000_000)

    # Fix the serialized fee field width to isolate the exact one-lovelace floor.
    tx, auth, _, _, context, resolved = make_order_cycle(3, proceeds=130_000_000)
    gain = inspect_transaction(tx, auth, context, resolved)["lovelace"]
    inspect_transaction(
        tx,
        replace(auth, asset_delta_limits=(("lovelace", gain, 129_970_000),)),
        context,
        resolved,
    )
    with pytest.raises(KernelError, match="Asset delta exceeds authorization"):
        inspect_transaction(
            tx,
            replace(auth, asset_delta_limits=(("lovelace", gain + 1, 129_970_000),)),
            context,
            resolved,
        )
    with pytest.raises(KernelError, match="below minimum") as rejected:
        make_order_cycle(3, proceeds=150_000)
    assert rejected.value.reason_code == "profit_floor"
    assert metadata["max_drawdown_lovelace"] == 20_000_000


def test_reference_fees_count_utxos_not_uses_or_script_hashes(monkeypatch):
    from pycardano.utils import tiered_reference_script_fee

    corrected, _, _, _, context, resolved = make_dano_cycle(token_pair=True)
    expected_bytes = sum(
        len(resolved[str(i.transaction_id) + "#" + str(i.index)].output.script or b"")
        for i in corrected.transaction_body.reference_inputs
    )
    assert (
        transaction_resources(corrected, context, resolved)["reference_script_bytes"]
        == expected_bytes
    )
    with monkeypatch.context() as legacy:
        legacy.setattr(
            CompositionBuilder, "_ref_script_size", TransactionBuilder._ref_script_size
        )
        old, _, _, _, _, _ = make_dano_cycle(token_pair=True)
    assert old.transaction_body.fee - corrected.transaction_body.fee >= 300_000
    assert tiered_reference_script_fee(context, expected_bytes) > 0

    builder = CompositionBuilder(context)
    script = PlutusV2Script(b"example")
    first = UTxO(
        TransactionInput.from_primitive([bytes(32), 0]),
        TransactionOutput(OWNER, 5_000_000, script=script),
    )
    second = UTxO(
        TransactionInput.from_primitive([bytes(32), 1]),
        TransactionOutput(OWNER, 5_000_000, script=script),
    )
    builder.add_input(first)
    builder.reference_inputs.update([first, second])
    builder._reference_scripts = [script] * 9
    assert builder._ref_script_size() == 2 * len(script)


def test_drawdown_counts_net_once_reserves_failure_and_survives_restart(tmp_path):
    tx, _, metadata, dependencies, _, _ = make_dano_cycle()
    limit = 5_000_000
    metadata = metadata | {"max_drawdown_lovelace": limit}
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    try:
        # Winning fees can exceed the old spending limit without reducing headroom.
        for _ in range(12):
            record(journal, tx, metadata, dependencies, net=1_000_000)
        assert 12 * tx.transaction_body.fee > limit
        risk = drawdown(journal, limit)
        assert risk["loss_headroom_lovelace"] == limit
        assert risk["peak_net_lovelace"] == 12_000_000
        check_candidate(journal, tx, dependencies, metadata)
        maintenance, _ = record(
            journal, tx, metadata | {"action": "allocate"}, dependencies, net=-200_000
        )
        pending, _ = record(journal, tx, metadata, dependencies, status="unknown")
        loss = candidate_loss(tx, dependencies)
        assert (
            drawdown(journal, limit)["loss_headroom_lovelace"] == limit - 200_000 - loss
        )
        assert (
            check_candidate(journal, tx, dependencies, metadata, intent=pending)[
                "loss_headroom_lovelace"
            ]
            == limit - 200_000
        )
        journal.close()
        journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
        assert drawdown(journal, limit)["reserved_loss_lovelace"] == loss
        journal.db.execute(
            "UPDATE transactions SET status='expired' WHERE intent=?", (pending,)
        )
        journal.record_transaction_rollback(maintenance)
        # Rolled-back maintenance is pending risk, not a realized expense.
        assert drawdown(journal, limit)["drawdown_lovelace"] == 0
        journal.db.execute(
            "UPDATE transactions SET status='aborted' WHERE intent=?", (maintenance,)
        )
        assert drawdown(journal, limit)["loss_headroom_lovelace"] == limit
        record(journal, tx, metadata, dependencies, status="confirmed")
        with pytest.raises(KernelError, match="unverified"):
            check_candidate(journal, tx, dependencies, metadata)
    finally:
        journal.close()


def test_confirmed_failure_blocks_prepare_and_recovery_until_acknowledged(tmp_path):
    journal, tx, context = prepared(tmp_path)
    provider = Provider(context)
    journal.claim_submission("test")
    provider.info = {
        "valid_contract": False,
        "block_height": 101,
        "block_hash": "a" * 64,
    }
    provider.canonical = {"hash": "a" * 64}
    failed = deepcopy(tx)
    failed.valid = False
    provider.transaction_cbor = lambda _: failed
    coordinator = Coordinator(provider, journal)
    assert coordinator.reconcile("test") == "failed"
    original = journal.outbox_entry("test")["signed"]
    assert len(journal.execution_incidents()) == 1
    with pytest.raises(KernelError, match="investigate"):
        coordinator.prepare("new", None, None, None, [], None, {})
    journal.close()
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    try:
        coordinator = Coordinator(provider, journal)
        with pytest.raises(KernelError, match="investigate"):
            coordinator.submit("test")
        journal.record_transaction_rollback("test")
        with pytest.raises(KernelError, match="investigate"):
            journal.require_no_execution_incident()
        txid = str(tx.transaction_body.id)
        with pytest.raises(KernelError, match="full transaction ID"):
            journal.acknowledge_execution_incident(txid[:12], "investigated", 1000)
        with pytest.raises(KernelError, match="reason"):
            journal.acknowledge_execution_incident(txid, " ", 1000)
        journal.acknowledge_execution_incident(
            txid, "Fixed and independently qualified the cause", 1000
        )
        journal.require_no_execution_incident()
        assert journal.outbox_entry("test")["signed"] == original
        assert journal.outbox_entry("test")["attempts"] == 1
        assert provider.attempts == 0
    finally:
        journal.close()


def test_config_requires_explicit_budget_migration(tmp_path):
    config = {
        "network": PROFILE.name,
        "chain_id": PROFILE.chain_id,
        "assets": ["lovelace", A],
        "max_total_fees_lovelace": 50_000_000,
    }
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(config))
    with pytest.raises(KernelError, match="Replace max_total_fees_lovelace"):
        ArbitrageConfig.load(path, PROFILE)
    config["max_drawdown_lovelace"] = config.pop("max_total_fees_lovelace")
    path.write_text(json.dumps(config))
    selected = ArbitrageConfig.load(path, PROFILE)
    assert selected.max_fee_lovelace is None
    assert selected.max_drawdown_lovelace == 50_000_000
    assert selected.max_maintenance_fee_lovelace == 1_500_000
    for invalid in (False, 0, -1, 1.5):
        with pytest.raises(KernelError, match="positive integers"):
            replace(selected, max_fee_lovelace=invalid)


def test_recovery_rechecks_loss_and_anchor_without_claiming_attempt(tmp_path):
    journal, tx, context = prepared(tmp_path)
    provider = Provider(context)
    loss = candidate_loss(tx, [])
    metadata = {"mode": "atomic arbitrage", "max_drawdown_lovelace": loss + 500_000}
    journal.db.execute(
        "UPDATE outbox SET metadata=? WHERE intent='test'", (json.dumps(metadata),)
    )
    historical, _ = record(journal, tx, metadata, [], net=-600_000)
    coordinator = Coordinator(provider, journal)
    try:
        with pytest.raises(KernelError, match="Drawdown limit"):
            coordinator.submit("test")
        journal.record_arbitrage_outcome(
            historical, f"{int(historical):064x}", -100_000, 1000
        )
        provider.canonical = {"hash": "f" * 64}
        with pytest.raises(KernelError, match="accounting anchor changed"):
            coordinator.submit("test")
        provider.canonical = {"hash": f"{int(historical):064x}"}

        def new_loss(_):
            journal.record_arbitrage_outcome(
                historical, provider.canonical["hash"], -600_000, 1001
            )

        coordinator.before_submit = new_loss
        with pytest.raises(KernelError, match="Drawdown limit"):
            coordinator.submit("test")
        assert journal.outbox_entry("test")["attempts"] == provider.attempts == 0
        assert journal.outbox_entry("test")["status"] == "prepared"
    finally:
        journal.close()


def test_acknowledgment_cli_is_local_and_does_not_reset_loss_or_submit(
    tmp_path, monkeypatch
):
    from typer.testing import CliRunner

    import defi_kernel.cli as cli
    from defi_kernel.wallet import create_test_wallet

    manifest, _ = create_test_wallet(PROFILE, tmp_path)
    journal, tx, _ = prepared(tmp_path)
    journal.db.execute(
        "INSERT INTO execution_incidents(intent,evidence) VALUES('test','{}')"
    )
    original = journal.outbox_entry("test")
    journal.close()

    def forbidden(*args, **kwargs):
        pytest.fail("Acknowledgment attempted provider access")

    monkeypatch.setattr(cli, "create_provider", forbidden)
    result = CliRunner().invoke(
        cli.app,
        [
            "--config",
            "config.example.toml",
            "--network",
            "preprod",
            "--state-dir",
            str(tmp_path),
            "arbitrage-acknowledge",
            "--manifest",
            str(manifest),
            "--txid",
            str(tx.transaction_body.id),
            "--reason",
            "Investigated and corrected the cause",
        ],
    )
    assert result.exit_code == 0, result.output
    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    try:
        assert not journal.execution_incidents()
        assert journal.outbox_entry("test") == original
        row = journal.db.execute(
            "SELECT acknowledgment,acknowledged_at FROM execution_incidents"
        ).fetchone()
        assert row[0] == "Investigated and corrected the cause" and row[1] > 0
    finally:
        journal.close()
