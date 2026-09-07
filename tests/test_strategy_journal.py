from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from defi_kernel.config import load_profile
from defi_kernel.domain import Asset, KernelError, OutRef, Quote
from defi_kernel.journal import Journal
from defi_kernel.strategy import Inventory, MarketMaker, Settings

BASE = Asset("preprod", b"a" * 28, b"T")
ADA = Asset("preprod")
REF = OutRef("a" * 64, 0)
SELL = Quote(BASE, ADA, 1_000_000, 2_000_000, 200_000, (REF,), 100)
BUY = Quote(ADA, BASE, 2_100_000, 1_000_000, 200_000, (REF,), 100)
STRATEGY = MarketMaker(
    Settings(order_size=1_000_000, target_base=5_000_000, max_base=10_000_000)
)
INVENTORY = Inventory(5_000_000, 100_000_000)


def test_two_sides_have_independent_inventory_and_costs():
    d = STRATEGY.decide(SELL, BUY, INVENTORY, 100)
    bid, ask = d.proposals
    assert bid.offer == ADA and ask.offer == BASE
    assert bid.offer_quantity <= 1_800_000  # external fee is included
    assert ask.ask_per_offer >= Fraction(23, 10)
    assert (
        bid.offer_quantity
        + bid.carrier_lovelace
        + ask.carrier_lovelace
        + STRATEGY.settings.fee_reserve_lovelace
        <= INVENTORY.free_quote
    )


@pytest.mark.parametrize(
    "inventory",
    [
        Inventory(1, 100_000_000),
        Inventory(5_000_000, 1),
        Inventory(10_000_000, 100_000_000),
        Inventory(5_000_000, 100_000_000, unsettled_base=1),
    ],
)
def test_risk_limits_pause_both_sides(inventory):
    assert not STRATEGY.decide(SELL, BUY, inventory, 100).proposals


@pytest.mark.parametrize("now", [99, 161])
def test_freshness_pause(now):
    assert not STRATEGY.decide(SELL, BUY, INVENTORY, now).proposals


def test_reprice_hysteresis():
    assert not STRATEGY.should_reprice(Fraction(100), Fraction(1001, 10), 100, 200)
    assert STRATEGY.should_reprice(Fraction(100), Fraction(101), 100, 200)
    assert STRATEGY.should_reprice(Fraction(100), Fraction(100), 100, 4000)


def test_network_wallet_isolation_even_with_wrong_path(tmp_path):
    p = load_profile(Path("config.example.toml"), "preprod")
    preview = load_profile(Path("config.example.toml"), "preview")
    mainnet = load_profile(Path("config.example.toml"), "mainnet")
    assert (
        len(
            {
                x.state_path(tmp_path)
                for x in (p, preview, mainnet, replace(p, wallet_id="other"))
            }
        )
        == 4
    )
    j = Journal(p.state_path(tmp_path), p)
    j.close()
    with pytest.raises(KernelError, match="identity mismatch"):
        Journal(p.state_path(tmp_path), mainnet)
