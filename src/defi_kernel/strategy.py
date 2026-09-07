"""Conservative two-sided strategy; no provider or signing access."""

from dataclasses import dataclass
from fractions import Fraction

from .domain import Asset, Quote


@dataclass(frozen=True)
class Settings:
    order_size: int
    target_base: int
    max_base: int
    spread_bps: int = 100
    skew_bps: int = 50
    max_age_seconds: int = 60
    reprice_bps: int = 50
    max_order_age_seconds: int = 3600
    fee_reserve_lovelace: int = 5_000_000
    carrier_per_order: int = 3_000_000

    def __post_init__(self):
        if any(type(value) is not int for value in vars(self).values()):
            raise ValueError("Strategy settings require integer base units and limits")
        if self.order_size <= 0 or not 0 <= self.target_base <= self.max_base:
            raise ValueError("Invalid size or inventory limits")
        if not 0 < self.spread_bps < 10000 or not 0 <= self.skew_bps < 10000:
            raise ValueError("Invalid spread/skew")
        if (
            min(
                self.max_age_seconds,
                self.max_order_age_seconds,
                self.reprice_bps,
                self.fee_reserve_lovelace,
                self.carrier_per_order,
            )
            <= 0
        ):
            raise ValueError("Invalid freshness, repricing or reserve limits")


@dataclass(frozen=True)
class Inventory:
    free_base: int
    free_quote: int
    committed_base: int = 0
    committed_quote: int = 0
    unsettled_base: int = 0

    def __post_init__(self):
        if any(type(value) is not int for value in vars(self).values()):
            raise ValueError("Inventory requires integer base units")
        if (
            min(
                self.free_base,
                self.free_quote,
                self.committed_base,
                self.committed_quote,
            )
            < 0
        ):
            raise ValueError("Negative inventory")


@dataclass(frozen=True)
class Proposal:
    side: str
    offer: Asset
    ask: Asset
    offer_quantity: int
    ask_per_offer: Fraction
    carrier_lovelace: int


@dataclass(frozen=True)
class Decision:
    proposals: tuple[Proposal, ...]
    reason: str


class MarketMaker:
    def __init__(self, settings: Settings):
        self.settings = settings

    def decide(
        self, sell: Quote, buy: Quote, inventory: Inventory, now: float
    ) -> Decision:
        s = self.settings
        if (
            max(now - sell.observed_at, now - buy.observed_at) > s.max_age_seconds
            or min(now - sell.observed_at, now - buy.observed_at) < 0
        ):
            return Decision((), "stale or future-dated market observation")
        base, quote = sell.input_asset, sell.output_asset
        if (
            quote.unit != "lovelace"
            or buy.input_asset != quote
            or buy.output_asset != base
        ):
            return Decision(
                (), "initial strategy requires a token/ADA pair with opposite quotes"
            )
        if sell.amount_in != s.order_size or buy.amount_out < s.order_size:
            return Decision((), "external quotes do not cover configured size")
        if sell.settlement != "atomic" or buy.settlement != "atomic":
            return Decision((), "asynchronous pricing path is not yet qualified")
        total = (
            inventory.free_base + inventory.committed_base + inventory.unsettled_base
        )
        if total < 0 or total > s.max_base or inventory.unsettled_base:
            return Decision((), "inventory exposure limit or unsettled exposure")
        bid_reference = Fraction(sell.amount_out - sell.fee_lovelace, sell.amount_in)
        # All costs charged against the requested size, even when integer math overdelivers.
        ask_reference = Fraction(buy.amount_in + buy.fee_lovelace, s.order_size)
        if bid_reference <= 0 or bid_reference > ask_reference:
            return Decision((), "invalid/crossed external reference")
        mid = (bid_reference + ask_reference) / 2
        imbalance = Fraction(total - s.target_base, max(s.max_base, 1))
        center = mid * (1 - imbalance * Fraction(s.skew_bps, 10000))
        bid = min(bid_reference, center * (1 - Fraction(s.spread_bps, 10000)))
        ask = max(ask_reference, center * (1 + Fraction(s.spread_bps, 10000)))
        # Both one-way orders need distinct inventory and separate ADA carriers.
        free_ada = (
            inventory.free_quote - s.fee_reserve_lovelace - 2 * s.carrier_per_order
        )
        bid_funding = (bid * s.order_size).__floor__()
        if (
            inventory.free_base < s.order_size
            or total + s.order_size > s.max_base
            or bid_funding <= 0
            or free_ada < bid_funding
        ):
            return Decision(
                (), "insufficient independently allocated inventory for both sides"
            )
        return Decision(
            (
                Proposal(
                    "bid",
                    quote,
                    base,
                    bid_funding,
                    Fraction(s.order_size, bid_funding),
                    s.carrier_per_order,
                ),
                Proposal("ask", base, quote, s.order_size, ask, s.carrier_per_order),
            ),
            "two one-way proposals; execution and minimum ADA require final transaction validation",
        )

    def should_reprice(
        self, old: Fraction, new: Fraction, created_at: float, now: float
    ):
        if old <= 0 or new <= 0 or now < created_at:
            raise ValueError("Invalid price or clock")
        return (
            now - created_at >= self.settings.max_order_age_seconds
            or abs(new - old) * 10000 >= old * self.settings.reprice_bps
        )
