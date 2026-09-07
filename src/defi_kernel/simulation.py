"""Recorded-data strategy exercise. Synthetic inventory; no chain execution."""

import json
from dataclasses import asdict
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets

from .config import Profile
from .domain import Asset, KernelError, OutRef, Quote
from .protocols import decode_dano
from .strategy import Inventory, MarketMaker, Settings


def fixture_decision(path: Path, profile: Profile):
    fixture = json.loads(path.read_text())
    if fixture["network"] != profile.name or fixture["chain_id"] != profile.chain_id:
        raise KernelError("Fixture chain identity mismatch")
    state = decode_dano(
        fixture["pool"], profile, platform_fee_rate=fixture["platform_fee_rate"]
    )
    base = Asset.from_unit(profile.name, fixture["base_unit"])
    ada = Asset(profile.name)
    if state.unit_a != "lovelace" or state.unit_b != base.unit:
        raise KernelError("Fixture does not describe selected token/ADA market")
    size = fixture["order_size"]
    sell_x, sell_input = state.compute_pool_change(-size)
    buy_input, _ = state.get_amount_in(Assets(root={base.unit: size}))
    buy_x, buy_y = state.compute_pool_change(buy_input.quantity())
    if sell_input != size or -sell_x <= 0 or -buy_y < size:
        raise KernelError("Fixture liquidity cannot support the configured size")
    if -sell_x >= state.reserve_a or -buy_y >= state.reserve_b:
        raise KernelError("Quote reaches a concentrated-liquidity band boundary")
    ref = (OutRef(state.tx_hash, state.tx_index),)
    clock = fixture["observed_at"]  # injected recorded clock, not wall-clock freshness
    fee = fixture["fixed_fee_lovelace"] + fixture["assumed_tx_fee_lovelace"]
    sell = Quote(base, ada, size, -sell_x, fee, ref, clock)
    buy = Quote(ada, base, buy_x, -buy_y, fee, ref, clock)
    settings = Settings(order_size=size, target_base=5 * size, max_base=10 * size)
    inventory = Inventory(**fixture["synthetic_inventory"])
    decision = MarketMaker(settings).decide(sell, buy, inventory, clock)
    return {
        "mode": "recorded-fixture simulation",
        "network": profile.name,
        "observed_at": clock,
        "inventory_source": "synthetic",
        "inventory": asdict(inventory),
        "price_source": "recorded Dano pool decoded and quoted with pinned Dendrite",
        "execution_qualified": False,
        "assumptions": "zero staking rewards; explicit assumed transaction fee; carrier reserve requires final min-ADA calculation",
        "sell_quote": asdict(sell),
        "buy_quote": asdict(buy),
        "decision": asdict(decision),
    }
