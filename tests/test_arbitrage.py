"""Exact cycle economics, bounded search and full multi-hop CBOR inspection."""

from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace

import pytest
from pycardano import Address, Network, TransactionOutput, VerificationKeyHash
from test_composition import EVIDENCE, StructuralContext
from test_transactions import OWNER, PROFILE, as_row

from defi_kernel.arbitrage import (
    ArbitrageConfig,
    Edge,
    HopQuote,
    RoutePlan,
    fit_route,
    search_routes,
)
from defi_kernel.arbitrage_runtime import build_route
from defi_kernel.chain_context import to_utxo
from defi_kernel.domain import Asset, KernelError, OutRef
from defi_kernel.signing import inspect_transaction, transaction_resources
from defi_kernel.transactions import CompositionBuilder, create_order

A, B = "a" * 56 + "41", "b" * 56 + "42"


def edge(n, incoming, outgoing, price, capacity=10000):
    return Edge(
        OutRef(f"{n:064x}", 0),
        "swaps-v1",
        incoming,
        outgoing,
        capacity,
        price=Fraction(price),
    )


def test_rounding_reduces_input_instead_of_donating_or_using_wallet_tokens():
    edges = (
        edge(1, "lovelace", A, 1),
        edge(2, A, B, 3),
        edge(3, B, "lovelace", Fraction(1, 4)),
    )
    plan, used = fit_route(edges, 10, 64)
    assert used == 2
    assert [(h.amount_in, h.amount_out) for h in plan.hops] == [(9, 9), (9, 3), (3, 12)]
    assert plan.gross_gain == 3
    assert plan.describe()["size_reduction_lovelace"] == 1
    assert fit_route(edges, 10, 1)[0] is None
    with pytest.raises(KernelError, match="remainder"):
        RoutePlan((replace(plan.hops[0], amount_out=10), *plan.hops[1:]), 10, 1)


def test_search_caps_and_parallel_venues_do_not_reuse_assets_or_utxos():
    edges = [
        edge(1, "lovelace", A, 1),
        edge(2, A, "lovelace", Fraction(1, 2)),
        edge(3, "lovelace", A, Fraction(1, 2)),
        edge(1, A, "lovelace", Fraction(1, 4)),
    ]
    config = ArbitrageConfig(
        ("lovelace", A), max_trade_lovelace=100, min_profit_lovelace=1
    )
    plans, stats = search_routes(edges, config)
    assert plans and not stats["search_truncated"]
    assert all(len({h.ref for h in p.hops}) == len(p.hops) for p in plans)
    _, stats = search_routes(edges, replace(config, max_expansions=1))
    assert stats["search_truncated"] and stats["expansions"] == 1
    _, stats = search_routes(edges, replace(config, size_attempts=1))
    assert stats["sizing_exhausted"]


def test_small_exact_fit_matches_brute_force_for_integer_rounding_and_capacity():
    # Independent enumeration: the fitted route is the largest exact feasible seed.
    for numerator in range(1, 6):
        edges = (
            edge(1, "lovelace", A, 1),
            edge(2, A, B, Fraction(numerator, 2), 13),
            edge(3, B, "lovelace", Fraction(1, 4)),
        )
        for seed in range(1, 41):
            feasible = []
            for q in range(1, seed + 1):
                first = edges[0].quote(q)
                second = edges[1].quote(first.amount_out)
                if second and second.amount_in == first.amount_out:
                    final = edges[2].quote(second.amount_out)
                    if final and final.amount_in == second.amount_out:
                        feasible.append(q)
            plan, _ = fit_route(edges, seed, 64)
            assert (plan.hops[0].amount_in if plan else None) == (
                max(feasible) if feasible else None
            )


def make_order_cycle(
    hops,
    *,
    budgets=None,
    now=None,
    parameters=None,
    proceeds=3_000_000,
    owner=OWNER,
    max_fee=None,
    min_profit=100_000,
    loss_headroom=None,
):
    import json
    from pathlib import Path

    from defi_kernel.transactions import V1

    now = EVIDENCE["observed_at"] if now is None else now
    profile = replace(PROFILE, max_request_bytes=16384)
    units = ["lovelace", *(f"{n:056x}41" for n in range(1, hops)), "lovelace"]
    context = StructuralContext([], now, budgets)
    if parameters is not None:
        context.protocol_param = parameters
    context.profile = profile
    rows, quotes = {}, []
    maker = Address(
        VerificationKeyHash(b"m" * 28), VerificationKeyHash(b"n" * 28), Network.TESTNET
    )
    for index, (incoming, outgoing) in enumerate(zip(units, units[1:]), 1):
        output_qty = proceeds if outgoing == "lovelace" else 300_000
        input_qty = 30_000 if incoming == "lovelace" else 300_000
        output = create_order(
            CompositionBuilder(context),
            profile,
            maker,
            Asset.from_unit(profile.name, outgoing),
            Asset.from_unit(profile.name, incoming),
            output_qty,
            Fraction(input_qty, output_qty),
        )
        row = as_row(output) | {"tx_hash": f"{index:064x}", "tx_index": 0}
        ref = OutRef(row["tx_hash"], 0)
        rows[ref] = row
        quotes.append(
            HopQuote(ref, "swaps-v1", incoming, outgoing, input_qty, output_qty)
        )
    reference = next(
        r
        for r in json.loads(Path("evidence/preprod-swaps-v1-scan.json").read_text())[
            "data"
        ]["rows"]
        if (r.get("reference_script") or {}).get("hash") == V1["script_hash"]
    )
    rows[OutRef(reference["tx_hash"], reference["tx_index"])] = reference
    funding = as_row(TransactionOutput(owner, 10_000_000)) | {"tx_hash": "d" * 64}
    collateral = as_row(TransactionOutput(owner, 5_000_000)) | {"tx_hash": "e" * 64}
    provider = SimpleNamespace(profile=profile, clock=lambda: now)
    config = ArbitrageConfig(
        tuple(units[:-1]),
        max_hops=max(2, hops),
        max_fee_lovelace=max_fee,
        min_profit_lovelace=min_profit,
    )
    plan = RoutePlan(tuple(quotes), 30_000, 1)
    tx, auth, metadata, dependencies = build_route(
        provider,
        config,
        owner,
        plan,
        rows,
        {},
        now,
        [funding],
        collateral,
        context,
        loss_headroom=loss_headroom,
    )
    resolved = {
        str(OutRef(r["tx_hash"], r["tx_index"])): to_utxo(r, profile)
        for r in dependencies
    }
    return tx, auth, metadata, dependencies, context, resolved


@pytest.mark.parametrize("hops", [2, 3, 4, 6, 8])
def test_full_atomic_order_cycle_needs_no_intermediate_wallet_inventory(hops):
    tx, auth, metadata, _, context, resolved = make_order_cycle(hops)
    profile = context.profile
    delta = inspect_transaction(tx, auth, context, resolved)
    assert delta == {"lovelace": 2_970_000 - tx.transaction_body.fee}
    assert len(tx.transaction_body.inputs) == hops + 1
    assert len(tx.transaction_body.reference_inputs) == 1
    resources = transaction_resources(tx, context, resolved)
    assert resources["signed_bytes"] > len(tx.to_cbor())
    with pytest.raises(KernelError, match="request limit"):
        transaction_resources(
            tx,
            SimpleNamespace(
                profile=replace(profile, max_request_bytes=1000),
                protocol_param=context.protocol_param,
            ),
            resolved,
        )
    assert metadata["notional_lovelace"] == 30_000


def make_dano_cycle(
    *,
    budgets=None,
    now=None,
    parameters=None,
    token_pair=False,
    owner=OWNER,
    max_funding=100_000_000,
    max_fee=1_500_000,
):
    """Two synthetic pools with distinct NFTs; structural coverage, not liquidity evidence."""
    from copy import deepcopy

    from pycardano import IndefiniteList, datum_hash
    from test_composition import MARKET

    from defi_kernel.dendrite_bridge import protocol_epoch
    from defi_kernel.protocols import DANO_CONFIG, decode_dano

    now = EVIDENCE["observed_at"] if now is None else now
    profile = replace(PROFILE, max_request_bytes=16384)
    rows = {OutRef(r["tx_hash"], r["tx_index"]): deepcopy(r) for r in EVIDENCE["rows"]}
    initial = rows.pop(OutRef(MARKET["pool"]["tx_hash"], MARKET["pool"]["tx_index"]))
    pools = []
    for n in (1, 2):
        row = deepcopy(initial)
        row.update(tx_hash=f"{n:064x}", tx_index=0)
        pool = decode_dano(row, profile, platform_fee_rate=1000)
        datum = replace(
            pool._datum, last_withdraw_epoch=protocol_epoch(profile, int(now * 1000))
        )
        if n == 1 and token_pair:
            token = Asset.from_unit(profile.name, A)
            datum = replace(datum, token_x=IndefiniteList([token.policy, token.name]))
            row["asset_list"].append(
                {
                    "policy_id": token.policy.hex(),
                    "asset_name": token.name.hex(),
                    "quantity": str(pool.reserve_a + datum.platform_fee_x),
                }
            )
            row["value"] = str(3_000_000 + datum.total_swap_fee)
        if n == 2:
            datum = replace(
                datum,
                sqrt_lower_price=replace(
                    datum.sqrt_lower_price,
                    denominator=datum.sqrt_lower_price.denominator * 2,
                ),
                sqrt_upper_price=replace(
                    datum.sqrt_upper_price,
                    denominator=datum.sqrt_upper_price.denominator * 2,
                ),
            )
            row["value"] = str(int(row["value"]) * 4)
            for asset in row["asset_list"]:
                if asset["policy_id"] == pool.pool_id[:56]:
                    asset["asset_name"] = "fe" * 32
        row.update(
            datum_hash=str(datum_hash(datum)),
            inline_datum={"bytes": datum.to_cbor_hex()},
        )
        ref = OutRef(row["tx_hash"], 0)
        rows[ref] = row
        pools.append((ref, decode_dano(row, profile, platform_fee_rate=1000)))
    context = StructuralContext(list(rows.values()), now, budgets)
    if parameters is not None:
        context.protocol_param = parameters
    context.profile = profile
    config_row = rows[DANO_CONFIG[profile.name]]
    import cbor2

    _, fixed = cbor2.loads(bytes.fromhex(config_row["inline_datum"]["bytes"])).value
    first = Edge(
        pools[0][0],
        "dano",
        A if token_pair else "lovelace",
        MARKET["base_unit"],
        10_000_000,
        pool=pools[0][1],
        fixed_fee=fixed,
    ).quote(1_000_000)
    second = Edge(
        pools[1][0],
        "dano",
        MARKET["base_unit"],
        "lovelace",
        10_000_000,
        pool=pools[1][1],
        fixed_fee=fixed,
    ).quote(first.amount_out)
    hops = (first, second)
    if token_pair:
        import json
        from pathlib import Path

        from defi_kernel.transactions import V1

        output = create_order(
            CompositionBuilder(context),
            profile,
            owner,
            Asset.from_unit(profile.name, A),
            Asset(profile.name),
            first.amount_in,
            Fraction(3, 100),
        )
        row = as_row(output) | {"tx_hash": "f" * 64}
        ref = OutRef(row["tx_hash"], row["tx_index"])
        rows[ref] = row
        reference = next(
            r
            for r in json.loads(
                Path("evidence/preprod-swaps-v1-scan.json").read_text()
            )["data"]["rows"]
            if (r.get("reference_script") or {}).get("hash") == V1["script_hash"]
        )
        rows[OutRef(reference["tx_hash"], reference["tx_index"])] = reference
        hops = (
            HopQuote(ref, "swaps-v1", "lovelace", A, 30_000, first.amount_in),
            *hops,
        )
    plan = RoutePlan(hops, hops[0].amount_in, 1)
    funding = as_row(TransactionOutput(owner, 10_000_000)) | {"tx_hash": "d" * 64}
    collateral = as_row(TransactionOutput(owner, 5_000_000)) | {"tx_hash": "e" * 64}
    tx, auth, metadata, dependencies = build_route(
        SimpleNamespace(profile=profile, clock=lambda: now),
        ArbitrageConfig(
            ("lovelace", MARKET["base_unit"], A),
            max_funding_lovelace=max_funding,
            max_fee_lovelace=max_fee,
        ),
        owner,
        plan,
        rows,
        {},
        now,
        [funding],
        collateral,
        context,
    )
    resolved = {
        str(OutRef(r["tx_hash"], r["tx_index"])): to_utxo(r, profile)
        for r in dependencies
    }
    return tx, auth, metadata, dependencies, context, resolved


@pytest.mark.parametrize("max_funding", [10_000_000, 100_000_000])
def test_multiple_dano_pools_share_one_withdrawal_and_resolve_all_batch_indices(
    max_funding,
):
    tx, auth, metadata, _, context, resolved = make_dano_cycle(max_funding=max_funding)
    delta = inspect_transaction(tx, auth, context, resolved)
    assert delta["lovelace"] >= 100_000 and len(delta) == 1
    assert len(tx.transaction_body.withdraws) == 1
    assert len(tx.transaction_body.reference_inputs) == 2
    assert len(tx.transaction_witness_set.redeemer) == 3
    # The packed withdrawal payload must contain BOTH pool entries (2 + 34*N bytes).
    # Decode raw serialized Plutus bytes, including CBOR's chunked byte-string form.
    import cbor2

    withdrawal = next(
        v
        for k, v in tx.transaction_witness_set.redeemer.items()
        if k.tag.name == "WITHDRAWAL"
    )
    payload = cbor2.loads(withdrawal.data.to_cbor())
    assert len(payload) == 70
    assert [payload[2], payload[36]] == [0, 1]
    assert metadata["venue_fee_lovelace"] == 200_000
    if max_funding == 10_000_000:
        own_outputs = [o for o in tx.transaction_body.outputs if o.address == OWNER]
        assert len(own_outputs) == 2
        assert any(o.amount.coin == max_funding for o in own_outputs)


def test_collateral_uses_authorized_fee_when_sdk_resource_maximum_exceeds_reserve(
    monkeypatch,
):
    from copy import deepcopy

    from pycardano import TransactionBuilder
    from test_transactions import Context

    parameters = replace(Context.protocol_param, max_tx_ex_mem=40_000_000)
    with monkeypatch.context() as old:
        old.setattr(
            CompositionBuilder,
            "_set_collateral_return",
            TransactionBuilder._set_collateral_return,
        )
        with pytest.raises(ValueError, match="Minimum collateral amount"):
            make_dano_cycle(token_pair=True, parameters=parameters)
    tx, auth, _, _, context, resolved = make_dano_cycle(
        token_pair=True, parameters=parameters
    )
    body = tx.transaction_body
    assert body.fee < auth.max_fee == 1_500_000
    assert body.total_collateral == 2_250_000
    assert body.collateral_return.amount.coin == 2_750_000
    assert body.collateral_return.address == OWNER
    assert inspect_transaction(tx, auth, context, resolved)["lovelace"] > 0
    transaction_resources(tx, context, resolved)
    # Budget estimation must retain the same collateral policy and Dano batch layout.
    provisional = context.evaluated_candidate.transaction_body
    assert provisional.total_collateral == body.total_collateral
    assert provisional.collateral_return.to_cbor() == body.collateral_return.to_cbor()
    assert provisional.outputs[:2] == body.outputs[:2]
    bad = deepcopy(tx)
    bad.transaction_body.total_collateral = 1
    with pytest.raises(KernelError, match="Insufficient total collateral"):
        inspect_transaction(bad, auth, context, resolved)
    with pytest.raises(KernelError, match="fee exceeds authorization"):
        inspect_transaction(tx, replace(auth, max_fee=body.fee - 1), context, resolved)
    with pytest.raises(KernelError, match="Collateral loss exceeds authorization"):
        inspect_transaction(
            tx,
            replace(auth, max_collateral=body.total_collateral - 1),
            context,
            resolved,
        )


@pytest.mark.parametrize("percent", [151, 333, 334])
def test_collateral_rounds_up_and_fee_allowance_preserves_valid_return(percent):
    from pycardano.utils import min_lovelace
    from test_transactions import Context

    tx, auth, metadata, _, context, resolved = make_dano_cycle(
        parameters=replace(Context.protocol_param, collateral_percent=percent),
        max_fee=1_500_001,
    )
    body = tx.transaction_body
    assert body.total_collateral == (auth.max_fee * percent + 99) // 100
    assert body.total_collateral + body.collateral_return.amount.coin == 5_000_000
    assert body.collateral_return.amount.coin >= min_lovelace(
        context, output=body.collateral_return
    )
    if percent > 151:
        assert auth.max_fee < 1_500_001
        assert metadata["fee_limit_reason"] == "collateral reserve"
    inspect_transaction(tx, auth, context, resolved)


def test_fee_above_collateral_fee_ceiling_is_rejected_during_build():
    with pytest.raises(KernelError, match="fee exceeds the authorized fee ceiling"):
        make_dano_cycle(max_fee=200_000)


def test_large_profit_preserves_bounded_operating_output(tmp_path):
    from defi_kernel.domain import Observation
    from defi_kernel.journal import Journal
    from defi_kernel.trading import wallet_inventory

    tx, auth, _, _, context, resolved = make_order_cycle(3, proceeds=130_000_000)
    delta = inspect_transaction(tx, auth, context, resolved)
    assert delta == {"lovelace": 129_970_000 - tx.transaction_body.fee}
    outputs = [o for o in tx.transaction_body.outputs if o.address == OWNER]
    assert len(outputs) == 2
    assert any(o.amount.coin > 100_000_000 for o in outputs)
    rows = [
        as_row(o)
        | {"tx_hash": str(tx.transaction_body.id), "tx_index": n, "block_height": 100}
        for n, o in enumerate(outputs)
    ]
    rows.append(
        as_row(TransactionOutput(OWNER, 5_000_000))
        | {"tx_hash": "e" * 64, "block_height": 100}
    )
    provider = SimpleNamespace(
        profile=context.profile,
        address_utxos=lambda *args: Observation(
            context.profile.name, "test", 1000, tuple(rows), True
        ),
    )
    journal = Journal(context.profile.state_path(tmp_path), context.profile)
    operating, collateral, protected = wallet_inventory(
        provider, journal, OWNER, {"block_no": 110}, ArbitrageConfig(("lovelace", A))
    )
    assert sum(int(r["value"]) for r in operating) == 10_000_000
    assert int(collateral["value"]) == 5_000_000
    assert len(protected) == 1 and int(protected[0]["value"]) > 100_000_000
    journal.close()


@pytest.mark.parametrize("has_collateral", [False, True])
def test_allocation_signs_only_verified_bot_proceeds_and_preserves_ada(
    tmp_path, has_collateral, monkeypatch
):
    from pycardano import PaymentSigningKey

    from defi_kernel.arbitrage_runtime import build_allocation
    from defi_kernel.coordinator import Coordinator
    from defi_kernel.journal import Journal
    from defi_kernel.signing import LocalSigner, decode_candidate, ref_text

    key = PaymentSigningKey.generate()
    owner = Address(
        key.to_verification_key().hash(), OWNER.staking_part, Network.TESTNET
    )
    path = tmp_path / "payment.skey"
    key.save(str(path))
    path.chmod(0o600)
    prior, _, meta, dependencies, context, _ = make_order_cycle(
        3, proceeds=130_000_000, owner=owner
    )
    meta["net_profit_lovelace"] = 129_970_000 - prior.transaction_body.fee
    config = ArbitrageConfig(("lovelace", A))
    journal = Journal(context.profile.state_path(tmp_path), context.profile)
    journal.prepare_candidate("prior", prior, dependencies, meta)
    journal.record_inclusion("prior", "b" * 64, 100, 3, confirmed=True)
    index, output = next(
        (n, o)
        for n, o in enumerate(prior.transaction_body.outputs)
        if o.address == owner and o.amount.coin > config.max_funding_lovelace
    )
    source = as_row(output) | {
        "tx_hash": str(prior.transaction_body.id),
        "tx_index": index,
    }
    faucet = as_row(TransactionOutput(owner, 9_000_000_000)) | {"tx_hash": "f" * 64}
    collateral = {"value": "5000000"} if has_collateral else None
    provider = SimpleNamespace(
        profile=context.profile,
        clock=lambda: EVIDENCE["observed_at"],
        verify_identity=lambda: None,
        recheck_dependencies=lambda rows: rows,
        tip=lambda: {
            "abs_slot": context.last_block_slot,
            "block_no": 110,
            "block_time": EVIDENCE["observed_at"],
        },
        block_at_height=lambda _: {"hash": "b" * 64},
        blocks_at_heights=lambda heights: {h: {"hash": "b" * 64} for h in heights},
        transaction_cbor=lambda _: prior,
    )
    context.provider = provider
    context.rows[row_ref_for_test := OutRef(source["tx_hash"], source["tx_index"])] = (
        source
    )

    def build(rows):
        return build_allocation(
            provider, journal, config, owner, [], collateral, rows, context
        )

    assert build([source, faucet]) is None  # Confirmed status alone is insufficient.
    journal.record_arbitrage_outcome(
        "prior", "b" * 64, 129_970_000 - prior.transaction_body.fee, provider.clock()
    )
    assert build([faucet]) is None
    with pytest.raises(KernelError, match="proceeds changed"):
        build([source | {"value": str(int(source["value"]) + 1)}])
    tx, auth, metadata, rows = build([faucet, source])
    resolved = {str(row_ref_for_test): to_utxo(source, context.profile)}
    assert inspect_transaction(tx, auth, context, resolved) == {
        "lovelace": -tx.transaction_body.fee
    }
    assert metadata["collateral_created_lovelace"] == (
        0 if has_collateral else 5_000_000
    )
    assert set(map(ref_text, tx.transaction_body.inputs)) == {str(row_ref_for_test)}
    assert (
        not tx.transaction_body.collateral and not tx.transaction_witness_set.redeemer
    )
    assert all(
        o.address == owner and not o.amount.multi_asset
        for o in tx.transaction_body.outputs
    )
    import defi_kernel.arbitrage_runtime as runtime

    engine = runtime.ArbitrageEngine(
        provider, journal, config, {"address": str(owner)}, None
    )
    monkeypatch.setattr(runtime, "ProviderChainContext", lambda _: context)
    monkeypatch.setattr(
        runtime,
        "wallet_inventory",
        lambda *args, **kwargs: ([], collateral, [faucet, source]),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Allocation shadow must neither scan markets nor sign/submit")

    monkeypatch.setattr(engine.liquidity, "observe", forbidden)
    monkeypatch.setattr(engine.coordinator, "prepare", forbidden)
    monkeypatch.setattr(engine.coordinator, "submit", forbidden)
    report = engine.tick()
    assert (
        report["stage"] == "evaluated"
        and "allocation" in report
        and "selected" not in report
    )
    assert journal.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    Coordinator(provider, journal).prepare(
        "allocate", tx, auth, context, rows, LocalSigner(path), metadata
    )
    signed = decode_candidate(journal.claim_submission("allocate"))
    signed.transaction_witness_set.vkey_witnesses = None
    assert signed.to_cbor() == tx.to_cbor()
    journal.record_transaction_rollback("prior")
    assert build([source, faucet]) is None
    journal.close()


def test_shadow_evaluates_without_keys_reservations_or_submission(
    tmp_path, monkeypatch
):
    import defi_kernel.arbitrage_runtime as runtime
    import defi_kernel.wallet as wallet_module
    from defi_kernel.journal import Journal

    tx, auth, metadata, dependencies, context, _ = make_order_cycle(3)
    now = EVIDENCE["observed_at"]
    provider = SimpleNamespace(
        profile=context.profile,
        clock=lambda: now,
        verify_identity=lambda: None,
        tip=lambda: {
            "abs_slot": context.last_block_slot,
            "block_time": now,
            "block_no": 100,
        },
        recheck_dependencies=lambda rows: rows,
    )
    context.provider = provider
    context.evaluate_tx_cbor = lambda cbor: {
        f"{k.tag.name.lower()}:{k.index}": v.ex_units
        for k, v in tx.transaction_witness_set.redeemer.items()
    }
    journal = Journal(context.profile.state_path(tmp_path), context.profile)
    try:
        engine = runtime.ArbitrageEngine(
            provider,
            journal,
            ArbitrageConfig(("lovelace", A, B)),
            {"address": str(OWNER)},
            None,
        )
        monkeypatch.setattr(runtime, "ProviderChainContext", lambda p: context)
        monkeypatch.setattr(
            runtime,
            "wallet_inventory",
            lambda *args, **kwargs: ([{"value": "3000000"}], {"value": "5000000"}, []),
        )
        monkeypatch.setattr(
            engine.liquidity, "observe", lambda *args: ([], {}, {}, now, {})
        )
        monkeypatch.setattr(
            runtime,
            "search_routes",
            lambda *args, **kwargs: ([SimpleNamespace(route_id="test")], {}),
        )
        monkeypatch.setattr(
            runtime,
            "build_route",
            lambda *args, **kwargs: (tx, auth, metadata, dependencies),
        )

        def forbidden(*args, **kwargs):
            pytest.fail("Shadow attempted signing, reservation, or submission")

        monkeypatch.setattr(wallet_module, "load_private_key", forbidden)
        monkeypatch.setattr(engine.coordinator, "prepare", forbidden)
        monkeypatch.setattr(engine.coordinator, "submit", forbidden)
        monkeypatch.setattr(journal, "prepare_candidate", forbidden)
        result = engine.tick()
        assert (
            result["stage"] == "evaluated"
            and result["selected"]["net_profit_lovelace"] > 0
        )
        assert journal.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        from defi_kernel.providers import RateLimited

        calls = []

        def rate_limited(rows):
            calls.append(rows)
            raise RateLimited(120)

        provider.recheck_dependencies = rate_limited
        with pytest.raises(RateLimited):
            engine.tick()
        assert len(calls) == 1 and "selected" not in engine.current_report
        provider.recheck_dependencies = lambda rows: rows
        monkeypatch.setattr(
            runtime,
            "wallet_inventory",
            lambda *args, **kwargs: ([], {"value": "5000000"}, []),
        )
        monkeypatch.setattr(engine.liquidity, "observe", forbidden)
        result = engine.tick()
        assert result["stage"] == "paused" and "operating inputs" in result["reason"]
        assert (
            journal.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        )
        journal.start_run(now)
        journal.request_stop()
        assert engine.tick()["stage"] == "stopped"
    finally:
        journal.close()


def test_native_pair_dano_preserves_ada_carrier_and_nets_both_intermediates():
    tx, auth, _, _, context, resolved = make_dano_cycle(token_pair=True)
    delta = inspect_transaction(tx, auth, context, resolved)
    assert set(delta) == {"lovelace"} and delta["lovelace"] > 100_000
    original = resolved[str(OutRef(f"{1:064x}", 0))].output
    assert tx.transaction_body.outputs[0].amount.coin == original.amount.coin + 100_000


@pytest.mark.parametrize(
    "execute,signed,status,matching,expected",
    [
        (True, False, "prepared", True, "recovered"),
        (True, True, "prepared", True, "submitted"),
        (True, True, "prepared", False, "pending"),
        (True, True, "unknown", True, "pending"),
        (False, True, "prepared", True, "pending"),
    ],
)
def test_arbitrage_restart_resumes_only_original_matching_unattempted_work(
    tmp_path, execute, signed, status, matching, expected
):
    from defi_kernel.arbitrage_runtime import ArbitrageEngine
    from defi_kernel.journal import Journal

    tx, _, metadata, dependencies, context, _ = make_order_cycle(2)
    now = EVIDENCE["observed_at"]
    provider = SimpleNamespace(
        profile=context.profile,
        clock=lambda: now,
        verify_identity=lambda: None,
        tip=lambda: {
            "abs_slot": context.last_block_slot,
            "block_time": now,
            "block_no": 100,
        },
    )
    journal = Journal(context.profile.state_path(tmp_path), context.profile)
    try:
        config = ArbitrageConfig(("lovelace", A))
        engine = ArbitrageEngine(
            provider, journal, config, {"address": str(OWNER)}, None, execute=execute
        )
        metadata["strategy_fingerprint"] = (
            config.fingerprint if matching else "different"
        )
        journal.prepare_candidate("original", tx, dependencies, metadata)
        if signed:
            # Orchestration fixture only: submit is intercepted below; no fake bytes go on wire.
            journal.attach_signature("original", tx)
        if status == "unknown":
            journal.mark_transaction("original", "submitting")
        journal.mark_transaction("original", status)
        engine.coordinator.reconcile = lambda *args, **kwargs: status
        submissions = []
        engine.coordinator.submit = submissions.append
        result = engine.tick()
        assert result["stage"] == expected
        assert submissions == (["original"] if expected == "submitted" else [])
        if expected == "recovered":
            assert journal.outbox_entry("original")["status"] == "aborted"
            assert (
                journal.db.execute("SELECT count(*) FROM reservations").fetchone()[0]
                == 0
            )
        else:
            assert (
                journal.db.execute("SELECT count(*) FROM transactions").fetchone()[0]
                == 1
            )
            assert journal.outbox_entry("original")["unsigned"] == tx.to_cbor()
    finally:
        journal.close()


@pytest.mark.parametrize("maximum", [2, 4, 8])
def test_hop_limit_includes_every_shorter_cycle_and_is_enforced_at_build(maximum):
    units = ("lovelace", *(f"{n:056x}41" for n in range(1, 8)))
    edges = []
    for i, unit in enumerate(units[1:], 1):
        edges.append(edge(2 * i, units[i - 1], unit, 1))
        edges.append(edge(2 * i + 1, unit, "lovelace", Fraction(1, 2)))
    config = ArbitrageConfig(
        units, max_hops=maximum, max_trade_lovelace=100, min_profit_lovelace=1
    )
    plans, stats = search_routes(edges, config)
    assert not stats["search_truncated"]
    assert {len(p.hops) for p in plans} == set(range(2, maximum + 1))
    if maximum > 2:
        with pytest.raises(KernelError, match="hop limit"):
            build_route(
                SimpleNamespace(profile=PROFILE),
                replace(config, max_hops=2),
                OWNER,
                max(plans, key=lambda p: len(p.hops)),
                {},
                {},
                0,
                [],
                None,
                None,
            )


def test_bounded_dense_search_gives_short_and_long_cycles_a_budget():
    from collections import Counter

    from defi_kernel.arbitrage import cycles

    units = ("lovelace", *(f"{n:056x}41" for n in range(1, 8)))
    edges = [
        edge(i * 8 + j, left, right, 1)
        for i, left in enumerate(units)
        for j, right in enumerate(units)
        if left != right
    ]
    config = ArbitrageConfig(units, max_cycles=14)
    for rotation in range(8):
        stats = Counter()
        found = cycles(edges, config, stats, rotation=rotation)
        assert Counter(map(len, found)) == {hops: 2 for hops in range(2, 9)}
        assert (
            stats["search_truncated"] and stats["expansions"] <= config.max_expansions
        )
