"""Bounded ADA cycle search with exact integer quotes and no intermediate dust."""

import json
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

from .domain import Asset, KernelError, OutRef, ceil_fraction


@dataclass(frozen=True)
class ArbitrageConfig:
    assets: tuple[str, ...]
    max_hops: int = 8
    max_expansions: int = 10_000
    max_cycles: int = 256
    size_attempts: int = 64
    max_evaluations: int = 3
    max_search_seconds: int = 10
    max_age_seconds: int = 180
    max_trade_lovelace: int = 10_000_000
    max_funding_lovelace: int = 100_000_000
    max_fee_lovelace: int | None = None
    max_maintenance_fee_lovelace: int = 1_500_000
    collateral_lovelace: int = 5_000_000
    max_drawdown_lovelace: int = 20_000_000
    min_profit_lovelace: int = 100_000
    operating_target_lovelace: int = 10_000_000
    submission_margin_seconds: int = 30
    venues: tuple[str, ...] = (
        "swaps-v1",
        "dano",
        "swaps-v1-two-way",
        "splash",
        "genius-yield",
        "saturnswap",
    )

    def __post_init__(self):
        if any(
            type(v) is not int or v <= 0
            for k, v in vars(self).items()
            if k not in ("assets", "venues")
            and not (k == "max_fee_lovelace" and v is None)
        ):
            raise KernelError("Arbitrage limits must be positive integers")
        if not 2 <= self.max_hops <= 8 or self.max_evaluations > 3:
            raise KernelError(
                "Supported search bounds: 2–8 hops and at most 3 evaluations"
            )
        if not isinstance(self.assets, tuple) or len(set(self.assets)) != len(
            self.assets
        ):
            raise KernelError("Asset allowlist must be unique")
        if "lovelace" not in self.assets or len(self.assets) < 2:
            raise KernelError(
                "Allowlist requires ADA and at least one intermediate asset"
            )
        for unit in self.assets:
            Asset.from_unit("preprod", unit)
        from .venues import ALL_VENUES

        if (
            not isinstance(self.venues, tuple)
            or not self.venues
            or len(set(self.venues)) != len(self.venues)
            or not set(self.venues) <= set(ALL_VENUES)
        ):
            raise KernelError(
                "Arbitrage venues must be a unique list of supported deployments"
            )
        if self.operating_target_lovelace > self.max_funding_lovelace:
            raise KernelError("Operating target exceeds funding limit")
        if self.submission_margin_seconds >= self.max_age_seconds:
            raise KernelError(
                "Submission margin must be shorter than observation lifetime"
            )

    @classmethod
    def load(cls, path, profile):
        data = json.loads(Path(path).read_text())
        if (
            not isinstance(data, dict)
            or data.pop("chain_id", None) != profile.chain_id
            or data.pop("network", None) != profile.name
        ):
            raise KernelError("Arbitrage configuration chain identity mismatch")
        if not isinstance(data.get("assets"), list):
            raise KernelError("Arbitrage configuration requires an asset allowlist")
        if "max_total_fees_lovelace" in data:
            raise KernelError(
                "Replace max_total_fees_lovelace with max_drawdown_lovelace: "
                "the new limit measures losses from peak verified profit, not gross fees"
            )
        data["assets"] = tuple(data["assets"])
        if "venues" in data:
            if not isinstance(data["venues"], list):
                raise KernelError("Arbitrage venues must be a list")
            data["venues"] = tuple(data["venues"])
        try:
            return cls(**data)
        except TypeError as error:
            raise KernelError(
                "Unknown or missing arbitrage configuration field"
            ) from error

    @property
    def fingerprint(self):
        return sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class HopQuote:
    ref: OutRef
    venue: str
    input_unit: str
    output_unit: str
    amount_in: int
    amount_out: int
    fixed_fee: int = 0


@dataclass(frozen=True)
class Edge:
    ref: OutRef
    venue: str
    input_unit: str
    output_unit: str
    capacity: int  # Output capacity for orders; input bound for pools.
    price: Fraction | None = None
    pool: object = None
    reward: int = 0
    fixed_fee: int = 0
    fee_group: str = ""  # Shared spending script; used only for fee ranking.

    def quote(self, budget):
        if type(budget) is not int or budget <= 0:
            return None
        if self.venue == "swaps-v1":
            take = min(
                self.capacity, budget * self.price.denominator // self.price.numerator
            )
            if take <= 0:
                return None
            incoming, outgoing = ceil_fraction(take * self.price), take
        elif self.venue == "dano":
            incoming = min(budget, self.capacity)
            is_x = self.input_unit == self.pool.unit_a
            minimum = (
                self.pool._datum.min_x_change if is_x else self.pool._datum.min_y_change
            )
            if incoming < minimum:
                return None
            x, y = self.pool.compute_pool_change(
                incoming if is_x else -incoming, self.reward
            )
            outgoing = -y if is_x else -x
            available = self.pool.active_liquidity(self.reward)[1 if is_x else 0]
            if not 0 < outgoing < available:
                return None
        elif self.venue in ("swaps-v1-two-way", "splash", "genius-yield", "saturnswap"):
            quoted = self.pool.quote(self.input_unit, budget)
            if quoted is None:
                return None
            incoming, outgoing, fixed_fee = quoted
            return HopQuote(
                self.ref,
                self.venue,
                self.input_unit,
                self.output_unit,
                incoming,
                outgoing,
                fixed_fee,
            )
        else:
            raise KernelError("Unqualified arbitrage venue")
        return HopQuote(
            self.ref,
            self.venue,
            self.input_unit,
            self.output_unit,
            incoming,
            outgoing,
            self.fixed_fee,
        )


@dataclass(frozen=True)
class RoutePlan:
    hops: tuple[HopQuote, ...]
    requested_lovelace: int
    attempts: int

    def __post_init__(self):
        if (
            len(self.hops) < 2
            or self.hops[0].input_unit != "lovelace"
            or self.hops[-1].output_unit != "lovelace"
        ):
            raise KernelError("Arbitrage must be a closed ADA cycle")
        if len({h.ref for h in self.hops}) != len(self.hops):
            raise KernelError("A route cannot consume the same UTxO twice")
        units = [h.output_unit for h in self.hops[:-1]]
        if "lovelace" in units or len(set(units)) != len(units):
            raise KernelError("Route repeats an intermediate asset")
        for left, right in zip(self.hops, self.hops[1:]):
            if (
                left.output_unit != right.input_unit
                or left.amount_out != right.amount_in
            ):
                raise KernelError("Route has an intermediate remainder or deficit")
        if any(
            type(q) is not int or q <= 0
            for h in self.hops
            for q in (h.amount_in, h.amount_out)
        ):
            raise KernelError("Route amounts must be positive integers")

    @property
    def gross_gain(self):
        return (
            self.hops[-1].amount_out
            - self.hops[0].amount_in
            - sum(h.fixed_fee for h in self.hops)
        )

    @property
    def route_id(self):
        return sha256(
            json.dumps(asdict(self), default=str, sort_keys=True).encode()
        ).hexdigest()[:24]

    def describe(self):
        return {
            "route_id": self.route_id,
            "path": [self.hops[0].input_unit, *(h.output_unit for h in self.hops)],
            "hops": [asdict(h) | {"ref": str(h.ref)} for h in self.hops],
            "requested_lovelace": self.requested_lovelace,
            "notional_lovelace": self.hops[0].amount_in,
            "size_reduction_lovelace": self.requested_lovelace - self.hops[0].amount_in,
            "gross_gain_lovelace": self.gross_gain,
            "size_attempts": self.attempts,
        }


def cycles(edges, config, stats, *, rotation=0, stopped=lambda: False):
    graph = defaultdict(list)
    for edge in sorted(edges, key=lambda e: (e.input_unit, e.output_unit, str(e.ref))):
        if edge.input_unit in config.assets and edge.output_unit in config.assets:
            graph[edge.input_unit].append(edge)
    # Rotate each adjacency list; bounded sampling is not exhaustive coverage.
    for unit, choices in graph.items():
        offset = rotation % len(choices)
        graph[unit] = choices[offset:] + choices[:offset]
    result = []

    def visit(unit, path, assets, refs, depth, expansion_ceiling, cycle_ceiling):
        for edge in graph[unit]:
            if stopped():
                return
            if stats["expansions"] >= expansion_ceiling or len(result) >= cycle_ceiling:
                stats["search_truncated"] = True
                return
            stats["expansions"] += 1
            if edge.ref in refs:
                continue
            extended = (*path, edge)
            if edge.output_unit == "lovelace":
                if len(extended) == depth:
                    result.append(extended)
            elif len(extended) < depth and edge.output_unit not in assets:
                visit(
                    edge.output_unit,
                    extended,
                    assets | {edge.output_unit},
                    refs | {edge.ref},
                    depth,
                    expansion_ceiling,
                    cycle_ceiling,
                )

    # Share bounded work across lengths so deeper routes cannot crowd out short
    # ones. Unused allowances carry forward; rotate lengths for very small budgets.
    depths = list(range(2, min(config.max_hops, len(config.assets)) + 1))
    offset = rotation % len(depths)
    depths = depths[offset:] + depths[:offset]
    for index, depth in enumerate(depths):
        if stopped():
            break
        expansions = config.max_expansions - stats["expansions"]
        remaining_cycles = config.max_cycles - len(result)
        if expansions <= 0 or remaining_cycles <= 0:
            stats["search_truncated"] = True
            break
        remaining_depths = len(depths) - index
        visit(
            "lovelace",
            (),
            {"lovelace"},
            set(),
            depth,
            stats["expansions"] + max(1, expansions // remaining_depths),
            len(result) + max(1, remaining_cycles // remaining_depths),
        )
    stats["cycles"] = len(result)
    return result


def _prefix_output(edges, seed):
    """Relaxed monotone prefix used only to reduce a seed; never authorizes dust."""
    for edge in edges:
        quote = edge.quote(seed)
        if quote is None:
            return 0
        seed = quote.amount_out
    return seed


def fit_route(edges, seed, attempts, *, stopped=lambda: False):
    """Reduce ADA input until each actual payment equals the preceding output.

    Binary searches jump over integer rounding plateaus. Every forward attempt
    consumes the caller's shared budget; failure is an honest bounded-search miss.
    """
    requested, used = seed, 0
    while seed > 0 and used < attempts:
        if stopped():
            return None, used
        used += 1
        budget, hops = seed, []
        for index, edge in enumerate(edges):
            hop = edge.quote(budget)
            if hop is None:
                return None, used
            if index and hop.amount_in != budget:
                low, high = 0, seed - 1
                while low < high:
                    if stopped():
                        return None, used
                    mid = (low + high + 1) // 2
                    if _prefix_output(edges[:index], mid) <= hop.amount_in:
                        low = mid
                    else:
                        high = mid - 1
                seed = low
                break
            hops.append(hop)
            budget = hop.amount_out
        else:
            return RoutePlan(tuple(hops), requested, used), used
    return None, used


# Ranking priors fitted to recorded final evaluations, not admission fees.
# (shared overhead, marginal spend cost); see docs/arbitrage.md.
FEE_PRIORS = {
    "swaps-v1": (72_000, 68_000),
    "dano": (148_000, 115_000),
    "swaps-v1-two-way": (100_000, 66_000),
    "splash": (185_000, 75_000),
    "genius-yield": (92_000, 136_000),
    "saturnswap": (223_000, 92_000),
}


def estimate_network_fee(plan, fee_groups):
    fee, seen = 155_000, set()
    for hop in plan.hops:
        shared, marginal = FEE_PRIORS[hop.venue]
        group = (hop.venue, fee_groups.get(hop.ref, ""))
        fee += marginal + (0 if group in seen else shared)
        seen.add(group)
    return fee


def search_routes(
    edges, config, *, rotation=0, stop_requested=lambda: False, monotonic=time.monotonic
):
    stats = Counter(
        expansions=0, cycles=0, size_attempts=0, sizing_exhausted=0, rounding_rejected=0
    )
    stats["search_truncated"] = False
    deadline = monotonic() + config.max_search_seconds

    def stopped():
        reason = (
            "stop_requested"
            if stop_requested()
            else ("time_limit" if monotonic() >= deadline else None)
        )
        if reason:
            stats["search_truncated"] = True
            stats["stop_reason"] = reason
        return reason is not None

    plans = {}
    for cycle in cycles(edges, config, stats, rotation=rotation, stopped=stopped):
        if stopped():
            break
        stats["cycles_sized"] += 1
        remaining = config.size_attempts
        # ponytail: bounded integer grid plus rounding reconciliation; not a global optimum proof.
        seeds = sorted(
            {max(1, config.max_trade_lovelace * n // 16) for n in range(1, 17)}
            | {
                1,
                min(
                    config.max_trade_lovelace,
                    ceil_fraction(cycle[0].capacity * cycle[0].price),
                )
                if cycle[0].price
                else min(config.max_trade_lovelace, cycle[0].capacity),
            },
            reverse=True,
        )
        for seed in seeds:
            if stopped():
                break
            if not remaining:
                stats["sizing_exhausted"] += 1
                break
            plan, used = fit_route(cycle, seed, remaining, stopped=stopped)
            remaining -= used
            stats["size_attempts"] += used
            if stopped():
                break
            if plan is None:
                stats["rounding_rejected"] += 1
            elif plan.gross_gain >= config.min_profit_lovelace:
                identity = tuple((h.ref, h.amount_in, h.amount_out) for h in plan.hops)
                plans.setdefault(identity, plan)
    # Shared script overhead is charged once; profit already includes venue fees.
    # Exact built fees and inspected wallet deltas remain the admission authority.
    fee_groups = {e.ref: e.fee_group for e in edges}
    fees = {id(p): estimate_network_fee(p, fee_groups) for p in plans.values()}
    ranked = sorted(
        plans.values(),
        key=lambda p: (
            -(p.gross_gain - fees[id(p)]),
            len(p.hops),
            p.route_id,
        ),
    )
    stats["pre_fee_candidates"] = len(ranked)
    distinct, variants, seen = [], [], set()
    for plan in ranked:
        cycle = tuple((h.ref, h.venue, h.input_unit, h.output_unit) for h in plan.hops)
        (variants if cycle in seen else distinct).append(plan)
        seen.add(cycle)
    stats["distinct_candidate_cycles"] = len(distinct)
    ranked = distinct + variants
    stats["candidate_cycles_by_hops"] = dict(Counter(len(p.hops) for p in distinct))
    stats["shortlist"] = [
        {
            "route_id": p.route_id,
            "hops": len(p.hops),
            "gain_before_network_fee_lovelace": p.gross_gain,
            "estimated_network_fee_lovelace": fees[id(p)],
            "estimated_net_lovelace": p.gross_gain - fees[id(p)],
        }
        for p in ranked[: config.max_evaluations]
    ]
    return ranked, dict(stats)
