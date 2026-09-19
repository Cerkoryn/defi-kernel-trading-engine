import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_composition import OWNER, PROFILE, StructuralContext
from test_engine import MARKET, Provider
from test_transactions import as_row

from defi_kernel.chain_context import to_utxo
from defi_kernel.domain import Asset, KernelError
from defi_kernel.signing import inspect_transaction
from defi_kernel.strategy import Inventory, MarketMaker
from defi_kernel.trading import observe_pool, prepare_action
from defi_kernel.transactions import CompositionBuilder, create_order


@pytest.mark.parametrize(
    "action", ["publish", "cancel", "rebalance-sell", "rebalance-buy", "route"]
)
def test_final_plans_balance_and_pass_independent_policy(monkeypatch, action, tmp_path):
    from fractions import Fraction

    import defi_kernel.trading as module

    p = Provider()
    market = MARKET
    if action == "route":
        market = replace(
            MARKET, execution=replace(MARKET.execution, max_fee_lovelace=1000000)
        )
    historical = json.loads(Path("evidence/preprod-live-execution.json").read_text())
    seen = {(r["tx_hash"], r["tx_index"]) for r in p.rows}
    for entry in historical["transactions"]:
        for row in entry["dependencies"]:
            ref = row["tx_hash"], row["tx_index"]
            if ref not in seen:
                p.rows.append(row)
                seen.add(ref)
    context = StructuralContext(p.rows, p.clock())
    monkeypatch.setattr(module, "ProviderChainContext", lambda provider: context)
    observation = observe_pool(p, MARKET)
    wallet_rows = list(p.scan("address_utxos", {}).rows)
    collateral = wallet_rows.pop()
    arguments = {"observation": observation}
    if action == "publish":
        sell, buy = observation.quotes(MARKET)
        proposals = (
            MarketMaker(MARKET.settings)
            .decide(sell, buy, Inventory(600000, 30000000), p.clock())
            .proposals
        )
        arguments["proposals"] = proposals
    elif action == "cancel":
        initial = create_order(
            CompositionBuilder(context),
            PROFILE,
            OWNER,
            MARKET.base,
            Asset("preprod"),
            200000,
            Fraction(4),
        )
        arguments["order_rows"] = [as_row(initial)]
    elif action == "route":
        initial = create_order(
            CompositionBuilder(context),
            PROFILE,
            OWNER,
            MARKET.base,
            Asset("preprod"),
            300000,
            Fraction(1, 1000),
        )
        arguments.update(quantity=300000, route_row=as_row(initial))
    else:
        arguments["quantity"] = 200000
    checked = []

    def prepare(
        self, intent, tx, authorization, chain_context, dependencies, signer, metadata
    ):
        resolved = {
            f"{r['tx_hash']}#{r['tx_index']}": to_utxo(r, PROFILE) for r in dependencies
        }
        delta = inspect_transaction(tx, authorization, chain_context, resolved)
        checked.append((tx, delta, metadata))
        return str(tx.transaction_body.id)

    from defi_kernel.coordinator import Coordinator

    monkeypatch.setattr(Coordinator, "prepare", prepare)
    result = prepare_action(
        p,
        None,
        market,
        {
            "address": str(OWNER),
            "payment_key": "unused.skey",
            "stake_key": "unused-stake.skey",
        },
        tmp_path,
        "test",
        action,
        wallet_rows,
        collateral,
        **arguments,
    )
    assert result["txid"] == str(checked[0][0].transaction_body.id)
    assert checked[0][2]["market_fingerprint"]
    assert checked[0][0].transaction_witness_set.vkey_witnesses is None
    if action == "publish":
        assert len(checked[0][2]["order_outputs"]) == 2
    if action == "rebalance-sell":
        assert checked[0][1][MARKET.base.unit] == -200000
    if action == "rebalance-buy":
        assert checked[0][1][MARKET.base.unit] >= 200000
        bounded = replace(market, settings=replace(market.settings, max_base=600000))
        with pytest.raises(KernelError, match="Rounded rebalance"):
            prepare_action(
                p,
                None,
                bounded,
                {"address": str(OWNER)},
                tmp_path,
                "over-limit",
                action,
                wallet_rows,
                collateral,
                **arguments,
            )
    if action == "route":
        assert checked[0][1].get(MARKET.base.unit, 0) == 0
        assert checked[0][1]["lovelace"] >= 0


def test_bid_exposure_includes_spendable_carrier_not_only_quote_size():
    from fractions import Fraction

    from defi_kernel.trading import bid_token_exposure

    context = StructuralContext([], 1788650000)
    output = create_order(
        CompositionBuilder(context),
        PROFILE,
        OWNER,
        Asset("preprod"),
        MARKET.base,
        200000,
        Fraction(1),
    )
    exposure = bid_token_exposure(output, PROFILE, context)
    assert exposure > 200000
    assert exposure < output.amount.coin  # Mandatory continuation ADA is protected.
