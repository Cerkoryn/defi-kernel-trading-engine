"""Regressions from the September 8 Preprod soak investigation."""

from types import SimpleNamespace

import pytest
from pycardano import Address
from test_arbitrage import OWNER, A, B, edge, make_dano_cycle, make_order_cycle
from test_composition import StructuralContext
from test_reporting import reporter

from defi_kernel.arbitrage import ArbitrageConfig, search_routes
from defi_kernel.arbitrage_runtime import (
    ArbitrageEngine,
    CandidateExpired,
    Liquidity,
    build_route,
    row_ref,
)
from defi_kernel.domain import KernelError
from defi_kernel.journal import Journal
from defi_kernel.protocols import DANO_PREPROD_HASH
from defi_kernel.providers import ProviderError, RateLimited


@pytest.mark.parametrize("failure_at", ["rewards", "references"])
@pytest.mark.parametrize(
    "error", [RateLimited(120), ProviderError("Transport unavailable")]
)
def test_discovery_provider_failures_invalidate_whole_observation(failure_at, error):
    _, _, meta, deps, context, _ = make_dano_cycle()
    now = meta["observed_at"] + 1801
    rows = {row_ref(r): r for r in deps}
    pools = [
        r
        for r in deps
        if str(Address.from_primitive(r["address"]).payment_part) == DANO_PREPROD_HASH
    ]
    accounts_seen, references_seen = [], []

    def rewards(accounts):
        accounts_seen.append(set(accounts))
        if failure_at == "rewards":
            raise error
        return dict.fromkeys(accounts, 0)

    def references(hashes):
        references_seen.append(set(hashes))
        raise error

    provider = SimpleNamespace(
        profile=context.profile,
        clock=lambda: now,
        utxos=lambda refs: [rows[r] for r in refs],
        credential_utxos=lambda credential: SimpleNamespace(
            complete=True,
            observed_at=now,
            rows=pools + [{"block_height": None}]
            if credential == DANO_PREPROD_HASH
            else [],
        ),
        stake_rewards_many=rewards,
        reference_scripts=references,
    )
    config = ArbitrageConfig(tuple(meta["path"][:-1]), venues=("swaps-v1", "dano"))
    quote_context = SimpleNamespace(
        _tip={"block_no": 10000000}, protocol_param=context.protocol_param
    )
    with pytest.raises(type(error)) as result:
        Liquidity().observe(provider, config, OWNER, quote_context)
    assert result.value is error
    assert len(accounts_seen) == 1 and accounts_seen[0]
    assert len(references_seen) == (failure_at == "references")
    with pytest.raises(KernelError, match="Stop requested"):
        Liquidity().observe(provider, config, OWNER, quote_context, lambda: True)
    assert len(accounts_seen) == 1


def test_epoch_deadline_rejected_before_construction(monkeypatch):
    import defi_kernel.arbitrage_runtime as runtime

    _, _, meta, _, context, _ = make_dano_cycle()
    anchor, length = 1647899091, 1800
    observed = (
        anchor + ((int(meta["observed_at"]) - anchor) // length + 1) * length - 20
    )
    context = StructuralContext([], observed, None)
    provider = SimpleNamespace(profile=context.profile, clock=lambda: observed)
    plan = SimpleNamespace(
        hops=[
            SimpleNamespace(
                amount_in=1,
                input_unit="lovelace",
                output_unit=A,
                venue="dano",
                ref="pool",
            )
        ]
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Expired route reached construction/evaluation")

    monkeypatch.setattr(runtime, "DanoSession", forbidden)
    with pytest.raises(CandidateExpired):
        build_route(
            provider,
            ArbitrageConfig(("lovelace", A)),
            OWNER,
            plan,
            {},
            {},
            observed,
            [],
            None,
            context,
        )


def test_distinct_directed_cycles_get_evaluation_slots_before_size_variants():
    config = ArbitrageConfig(("lovelace", A, B), max_trade_lovelace=10000000)
    edges = [
        edge(1, "lovelace", A, 1, 20000000),
        edge(2, A, "lovelace", "1/2", 20000000),
        edge(3, "lovelace", B, 1, 20000000),
        edge(4, B, "lovelace", "2/3", 20000000),
    ]
    plans, stats = search_routes(edges, config)
    assert stats["pre_fee_candidates"] > 3
    assert stats["distinct_candidate_cycles"] == 2
    assert plans[0].hops[0].output_unit != plans[1].hops[0].output_unit
    assert plans[2].hops[0].output_unit == plans[0].hops[0].output_unit


def test_expired_winner_does_not_displace_valid_runner_up(tmp_path, monkeypatch):
    import defi_kernel.arbitrage_runtime as runtime

    tx, auth, meta, deps, context, _ = make_order_cycle(3)
    now = [meta["observed_at"]]
    provider = SimpleNamespace(
        profile=context.profile,
        clock=lambda: now[0],
        verify_identity=lambda: None,
        tip=lambda: {
            "block_no": 100,
            "block_time": now[0],
            "abs_slot": context.last_block_slot,
        },
    )
    journal = Journal(context.profile.state_path(tmp_path), context.profile)
    engine = ArbitrageEngine(
        provider,
        journal,
        ArbitrageConfig(("lovelace", A, B)),
        {"address": str(OWNER)},
        None,
    )
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def identity():
        now[0] += 2

    provider.verify_identity = identity

    def observe_context(_):
        now[0] += 5
        return context

    monkeypatch.setattr(runtime, "ProviderChainContext", observe_context)
    monkeypatch.setattr(
        runtime,
        "wallet_inventory",
        lambda *a, **k: ([{"value": "10000000"}], {"value": "5000000"}, []),
    )

    def observe(*args):
        now[0] += 7
        return [], {}, {}, now[0], {}

    monkeypatch.setattr(engine.liquidity, "observe", observe)
    plans = [
        SimpleNamespace(
            route_id=name, hops=[SimpleNamespace(amount_in=1, amount_out=10**12)]
        )
        for name in ("winner", "runner")
    ]
    monkeypatch.setattr(runtime, "search_routes", lambda *a, **k: (plans, {}))

    def build(p, c, owner, plan, *args, **kwargs):
        return (
            tx,
            auth,
            meta
            | {
                "route_id": plan.route_id,
                "submit_before": meta["observed_at"]
                + (20 if plan is plans[0] else 100),
            },
            deps,
        )

    monkeypatch.setattr(runtime, "build_route", build)
    evaluations = []

    def evaluate(*args):
        evaluations.append(1)
        now[0] += 15
        return (
            SimpleReceipt(),
            {"lovelace": 1000000 // len(evaluations)},
            {"signed_bytes": 2000, "memory": 1000000, "steps": 1000000},
        )

    from dataclasses import dataclass

    @dataclass
    class SimpleReceipt:
        valid: bool = True

    monkeypatch.setattr(runtime, "validate_candidate", evaluate)
    result = engine.tick()
    assert result["stage"] == "evaluated" and result["selected"]["route_id"] == "runner"
    assert result["rejection_counts"] == {"deadline": 1}
    assert len(evaluations) == 2
    assert result["timings"] == {
        "reconciliation_seconds": 2,
        "observation_seconds": 12,
        "search_seconds": 0,
        "build_and_evaluation_seconds": 30,
    }
    # An already-expired built candidate never reaches final evaluation.
    now[0] = meta["observed_at"] + 100
    evaluations.clear()
    result = engine.tick()
    assert result["stage"] == "no_opportunity" and not evaluations
    assert result["rejection_counts"] == {"deadline": 2}

    def fresh_build(*args, **kwargs):
        tx, auth, metadata, deps = build(*args, **kwargs)
        metadata["submit_before"] = now[0] + 100
        return tx, auth, metadata, deps

    monkeypatch.setattr(runtime, "build_route", fresh_build)
    now[0] = meta["observed_at"]
    evaluations.clear()
    plans[1].hops[0].amount_out = 900_000
    result = engine.tick()
    assert len(evaluations) == 1 and result["selected"]["route_id"] == "winner"
    assert result["skipped_candidates"] == [
        {"route_id": "runner", "reason": "cannot_beat_evaluated_net"}
    ]
    # An equal upper bound could still tie and use fewer resources: evaluate it.
    now[0] = meta["observed_at"]
    evaluations.clear()
    plans[1].hops[0].amount_out = 1_000_001
    result = engine.tick()
    assert len(evaluations) == 2 and not result["skipped_candidates"]
    # A local request cap rejects only this route; a smaller runner can still win.
    from defi_kernel.domain import RequestTooLarge

    def bounded_build(*args, **kwargs):
        if args[3] is plans[0]:
            raise RequestTooLarge(20000, 16384)
        return fresh_build(*args, **kwargs)

    monkeypatch.setattr(runtime, "build_route", bounded_build)
    now[0] = meta["observed_at"]
    evaluations.clear()
    result = engine.tick()
    assert result["selected"]["route_id"] == "runner" and len(evaluations) == 1
    assert result["rejection_counts"] == {"request_size": 1}
    assert result["build_rejections"][0]["request_bytes"] == 20000
    journal.close()


def test_settlement_events_are_prompt_idempotent_and_rollback_aware(
    tmp_path, monkeypatch
):
    import defi_kernel.arbitrage_runtime as runtime

    tx, _, meta, deps, context, _ = make_order_cycle(3)
    meta["net_profit_lovelace"] = 2970000 - tx.transaction_body.fee
    now = [meta["observed_at"]]
    provider = SimpleNamespace(
        profile=context.profile,
        clock=lambda: now[0],
        verify_identity=lambda: None,
        tip=lambda: {
            "block_no": 110,
            "block_time": now[0],
            "abs_slot": context.last_block_slot,
        },
        blocks_at_heights=lambda heights: {h: {"hash": "a" * 64} for h in heights},
        transaction_cbor=lambda _: tx,
    )
    journal = Journal(context.profile.state_path(tmp_path), context.profile)
    r, output = reporter(journal, clock=lambda: now[0])
    meta["run_id"] = r.run_id
    journal.prepare_candidate("trade", tx, deps, meta)
    journal.record_inclusion("trade", "a" * 64, 100, 3, confirmed=True)
    engine = ArbitrageEngine(
        provider,
        journal,
        ArbitrageConfig(("lovelace", A)),
        {"address": str(OWNER)},
        None,
        reporter=r,
    )

    def unavailable(_):
        assert any("TRANSACTION" in line and "confirmed" in line for line in output)
        raise ProviderError("Market service unavailable")

    monkeypatch.setattr(runtime, "ProviderChainContext", unavailable)
    with pytest.raises(ProviderError):
        engine.tick()
    assert len(engine.current_report["settlement_changes"]) == 1
    assert "reconciliation_seconds" in engine.current_report["timings"]
    now[0] += 50
    assert engine.settlement() == []
    engine.confirmed.clear()  # Restart verifies bodies, but does not re-log old profit.
    assert engine.settlement() == []
    row = journal.db.execute("SELECT * FROM arbitrage_outcomes").fetchone()
    assert row["verified_at"] == meta["observed_at"]
    r.cycle(
        engine.current_report
        | {"stage": "paused", "reason": "Market service unavailable"}
    )
    assert sum("TRANSACTION" in line and "confirmed" in line for line in output) == 1
    journal.record_transaction_rollback("trade")
    assert engine.settlement() == []
    assert journal.arbitrage_history()["runs"][0]["realized_net_lovelace"] == 0
    journal.record_inclusion("trade", "b" * 64, 101, 3, confirmed=True)
    assert len(engine.settlement()) == 1
    r.transactions()
    assert sum("TRANSACTION" in line and "confirmed" in line for line in output) == 2
    assert (
        journal.arbitrage_history()["runs"][0]["realized_net_lovelace"]
        == meta["net_profit_lovelace"]
    )
    r.finish("stopped")
    journal.close()


def test_final_selection_uses_net_profit_not_hops_or_profit_per_byte():
    from defi_kernel.arbitrage_runtime import candidate_priority

    def candidate(name, hops, net, size, memory=1_000_000):
        return (
            None,
            None,
            {
                "route_id": name,
                "hops": [None] * hops,
                "net_profit_lovelace": net,
                "resources": {
                    "signed_bytes": size,
                    "memory": memory,
                    "steps": 500_000_000,
                },
            },
            None,
        )

    short = candidate("short", 2, 1_000_000, 1500)
    longer = candidate("longer", 5, 1_000_001, 4500)
    assert min([short, longer], key=candidate_priority) is longer
    expensive_longer = candidate("expensive", 5, 999_999, 4500)
    assert min([expensive_longer, short], key=candidate_priority) is short
    equal_longer = candidate("equal", 5, 1_000_000, 4500)
    assert min([equal_longer, short], key=candidate_priority) is short
    fewer_resources = candidate("leaner", 3, 1_000_000, 1500, 999_999)
    assert min([short, fewer_resources], key=candidate_priority) is fewer_resources


def test_fee_ranking_counts_shared_scripts_once_and_keeps_longer_winners():
    from dataclasses import replace
    from fractions import Fraction

    from defi_kernel.arbitrage import estimate_network_fee, fit_route
    from defi_kernel.domain import OutRef

    config = ArbitrageConfig(("lovelace", A, B), max_trade_lovelace=1_000_000)
    edges = [
        edge(1, "lovelace", A, 1, 10_000_000),
        edge(2, A, "lovelace", Fraction(1, 2), 10_000_000),
        edge(3, A, B, 1, 10_000_000),
        edge(4, B, "lovelace", Fraction(10, 21), 10_000_000),
    ]
    # The extra hop earns 100k; its marginal same-script cost is about 68k.
    # The old 140k-per-hop prior incorrectly put the two-hop cycle first.
    plans, stats = search_routes(edges, config)
    assert len(plans[0].hops) == 3
    assert (
        stats["shortlist"][0]["estimated_net_lovelace"]
        > stats["shortlist"][1]["estimated_net_lovelace"]
    )
    assert stats["candidate_cycles_by_hops"] == {2: 1, 3: 1}
    short, _ = fit_route((edges[0], edges[1]), 1_000_000, 64)
    same = estimate_network_fee(short, {})
    separate = estimate_network_fee(
        short, {OutRef(f"{1:064x}", 0): "first", OutRef(f"{2:064x}", 0): "second"}
    )
    assert separate - same == 72_000
    # Venue fees already reduce gross gain; ranking must not charge them twice.
    with_fee = replace(
        short, hops=(replace(short.hops[0], fixed_fee=100_000), short.hops[1])
    )
    assert estimate_network_fee(with_fee, {}) == same
    assert with_fee.gross_gain == short.gross_gain - 100_000
