"""Qualified market observations and bounded transaction planning for the MVP."""

from dataclasses import dataclass
from fractions import Fraction

from charli3_dendrite.dataclasses.models import Assets
from pycardano import Address

from .chain_context import KoiosChainContext, to_utxo
from .dendrite_bridge import DANO_REFERENCE, DanoSession, protocol_epoch
from .domain import Asset, KernelError, OutRef, Quote, ceil_fraction
from .protocols import (
    DANO_CONFIG,
    DEPLOYMENTS,
    dano_config,
    decode_dano,
    decode_swaps,
    row_assets,
)
from .transactions import CompositionBuilder, close_order, create_order, fill_order


def market_fingerprint(market):
    import json
    from dataclasses import asdict
    from hashlib import sha256

    from .domain import json_value

    return sha256(
        json.dumps(asdict(market), default=json_value, sort_keys=True).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ExecutionLimits:
    max_fee_lovelace: int = 2_000_000
    collateral_lovelace: int = 5_000_000
    max_funding_lovelace: int = 100_000_000
    max_total_fees_lovelace: int = 20_000_000
    max_trade_lovelace: int = 10_000_000
    rebalance_threshold_base: int = 250_000
    max_rebalance_base: int = 500_000
    slippage_bps: int = 100
    min_route_gain_lovelace: int = 0

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in vars(self).values()):
            raise ValueError("Execution limits require nonnegative integers")
        if (
            min(
                self.max_fee_lovelace,
                self.collateral_lovelace,
                self.max_funding_lovelace,
                self.max_total_fees_lovelace,
                self.max_trade_lovelace,
                self.max_rebalance_base,
            )
            <= 0
            or self.slippage_bps >= 10000
        ):
            raise ValueError("Invalid execution limits")


@dataclass
class PoolObservation:
    row: dict
    pool: object
    rows: list
    rewards: dict
    reward: int
    fixed_fee: int
    observed_at: float

    def quotes(self, market):
        size, pool = market.settings.order_size, self.pool
        active_x, active_y = pool.active_liquidity(self.reward)
        if size >= active_y or size < pool._datum.min_y_change:
            raise KernelError(
                "Configured size is outside the Dano liquidity/minimum bounds"
            )
        sell_x, _ = pool.compute_pool_change(-size, self.reward)
        adjusted = pool.model_copy(deep=True)
        adjusted.assets.root["lovelace"] += self.reward
        buy_input, _ = adjusted.get_amount_in(Assets(root={market.base.unit: size}))
        x, y = pool.compute_pool_change(buy_input.quantity(), self.reward)
        if (
            -sell_x <= 0
            or -sell_x >= active_x
            or -y < size
            or -y >= active_y
            or x < pool._datum.min_x_change
        ):
            raise KernelError("Dano quote exceeds executable liquidity bounds")
        ref = OutRef(self.row["tx_hash"], self.row["tx_index"])
        fee = self.fixed_fee + market.assumed_tx_fee_lovelace
        ada = Asset(market.base.network)
        return (
            Quote(market.base, ada, size, -sell_x, fee, (ref,), self.observed_at),
            Quote(ada, market.base, x, -y, fee, (ref,), self.observed_at),
        )


def observe_pool(provider, market):
    now = provider.clock()
    rate, fixed = dano_config(provider)
    rows = provider.utxos([market.pool])
    if not rows and market.pool_nft:
        nft = Asset.from_unit(provider.profile.name, market.pool_nft)
        rows = list(
            provider.scan(
                "asset_utxos",
                {
                    "_asset_list": [[nft.policy.hex(), nft.name.hex()]],
                    "_extended": True,
                },
            ).rows
        )
    if len(rows) != 1:
        raise KernelError("Dano pool continuation is missing or ambiguous")
    row = rows[0]
    pool = decode_dano(row, provider.profile, platform_fee_rate=rate)
    if (
        market.pool_nft
        and pool.pool_id != market.pool_nft
        or (pool.unit_a, pool.unit_b) != ("lovelace", market.base.unit)
    ):
        raise KernelError("Dano pool identity/pair mismatch")
    rows += provider.utxos(
        [DANO_CONFIG[provider.profile.name], DANO_REFERENCE[provider.profile.name]]
    )
    rewards, reward = {}, 0
    if (
        protocol_epoch(provider.profile, int(now * 1000))
        > pool._datum.last_withdraw_epoch
    ):
        address = Address.from_primitive(row["address"])
        reward_address = str(
            Address(staking_part=address.staking_part, network=address.network)
        )
        reward = provider.stake_rewards(reward_address)
        rewards[reward_address] = reward
        rows.append(provider.reference_script(str(address.staking_part)))
    return PoolObservation(row, pool, rows, rewards, reward, fixed, now)


def wallet_inventory(provider, journal, owner, tip, limits):
    observed = provider.scan(
        "address_utxos", {"_addresses": [str(owner)], "_extended": True}
    )
    if not observed.complete:
        raise KernelError("Incomplete wallet observation")
    reserved = {r[0] for r in journal.db.execute("SELECT ref FROM reservations")}
    rows, protected = [], []
    for row in observed.rows:
        if row.get("address") != str(owner) or row.get("is_spent") is not False:
            raise KernelError("Wallet observation has a foreign or spent output")
        row_assets(row, provider.profile.name)
        if f"{row['tx_hash']}#{row['tx_index']}" in reserved:
            continue
        if (
            type(row.get("block_height")) is not int
            or tip["block_no"] - row["block_height"] + 1
            < provider.profile.confirmations
        ):
            raise KernelError("Wallet activity has not reached confirmation depth")
        if (
            row.get("datum_hash")
            or row.get("inline_datum")
            or row.get("reference_script")
            or int(row["value"]) > limits.max_funding_lovelace
        ):
            protected.append(row)
        else:
            rows.append(row)
    collateral = [
        r
        for r in rows
        if not r.get("asset_list") and int(r["value"]) == limits.collateral_lovelace
    ]
    if not collateral:
        raise KernelError("Dedicated confirmed collateral is unavailable")
    rows.remove(collateral[0])
    # A designated operating budget also keeps the untouched faucet remainder
    # outside coin selection. Protected tokens still count toward exposure.
    return rows, collateral[0], protected


def bid_token_exposure(output, profile, context):
    """Conservative fixed-price exposure, including any spendable carrier excess.

    A minimal encoding gives a lower bound on the continuation's ADA deposit.
    Larger amounts/indices only raise that requirement. Unsolicited donations
    cannot be capped on-chain and are handled by the observed inventory limit.
    """
    from copy import deepcopy
    from dataclasses import replace

    from charli3_dendrite.utility import asset_to_value
    from pycardano.utils import min_lovelace

    from .protocols import SwapsV1Datum
    from .transactions import _previous, beacons

    datum = SwapsV1Datum.from_cbor(output.datum.to_cbor())
    if datum.offer_id or datum.offer_name:
        return 0
    future = deepcopy(output)
    future.datum = replace(datum, prev_input=_previous("0" * 64, 0))
    ask = datum.ask_id.hex() + datum.ask_name.hex()
    future.amount = asset_to_value(
        Assets(root={**beacons(datum, 1), ask: 1, "lovelace": 1})
    )
    minimum = min_lovelace(context, output=future)
    price = Fraction(datum.swap_price.numerator, datum.swap_price.denominator)
    return ceil_fraction(max(0, output.amount.coin - minimum) * price)


def prepare_action(
    provider,
    journal,
    market,
    wallet,
    key_dir,
    intent,
    action,
    wallet_rows,
    collateral,
    *,
    proposals=(),
    order_rows=(),
    observation=None,
    quantity=0,
    route_row=None,
    protected_base=0,
):
    """Build one explicit atomic transaction; repricing is cancel then publish."""
    profile, limits = provider.profile, market.execution
    if profile.name != "preprod":
        raise KernelError("MVP execution is qualified only for preprod")
    owner = Address.from_primitive(wallet["address"])
    context, dependencies = KoiosChainContext(provider), {}
    builder = CompositionBuilder(context)
    builder.validity_start, builder.ttl = (
        context.last_block_slot - 60,
        context.last_block_slot + 300,
    )
    if observation:
        if (
            not 0
            <= provider.clock() - observation.observed_at
            <= market.settings.max_age_seconds
        ):
            raise KernelError("Market observation expired before construction")
        builder.ttl = min(
            builder.ttl,
            context.slot_at_ms(
                int((observation.observed_at + market.settings.max_age_seconds) * 1000)
            ),
        )

    def remember(row):
        dependencies[f"{row['tx_hash']}#{row['tx_index']}"] = row
        return to_utxo(row, profile)

    builder.collaterals.append(remember(collateral))

    def reference(role):
        return remember(provider.reference_script(DEPLOYMENTS["swaps-v1"][role]))

    base, ada = market.base, Asset(profile.name)
    required_base, required_ada = 0, limits.max_fee_lovelace + 2_000_000
    deltas, order_outputs, venue_fee = [], [], 0
    if action == "publish":
        if len(proposals) != 2 or {p.side for p in proposals} != {"bid", "ask"}:
            raise KernelError("Publishing requires two independently allocated sides")
        beacon = reference("beacon_policy")
        for proposal in proposals:
            if {proposal.offer, proposal.ask} != {base, ada}:
                raise KernelError("Proposal pair mismatch")
            if (
                (proposal.side == "bid") != (proposal.offer == ada)
                or proposal.offer == base
                and proposal.offer_quantity > market.settings.order_size
            ):
                raise KernelError("Proposal direction or size exceeds market limits")
            output = create_order(
                builder,
                profile,
                owner,
                proposal.offer,
                proposal.ask,
                proposal.offer_quantity,
                proposal.ask_per_offer,
                beacon_reference=beacon,
            )
            carrier = output.amount.coin - (
                proposal.offer_quantity if proposal.offer == ada else 0
            )
            if carrier > proposal.carrier_lovelace:
                raise KernelError(
                    "Actual minimum-ADA deposit exceeds strategy allocation"
                )
            order_outputs.append(
                {
                    "index": len(builder.outputs) - 1,
                    "quantity": proposal.offer_quantity,
                    "carrier_lovelace": carrier,
                    "side": proposal.side,
                }
            )
            required_base += proposal.offer_quantity if proposal.offer == base else 0
        committed = sum(o.amount.coin for o in builder.outputs)
        worst_bid = sum(
            bid_token_exposure(o, profile, context) for o in builder.outputs
        )
        current_base = (
            sum(row_assets(r, profile.name).get(base.unit, 0) for r in wallet_rows)
            + protected_base
        )
        if current_base + worst_bid > market.settings.max_base:
            raise KernelError(
                "Executable bid exposure, including carrier excess, exceeds maximum inventory"
            )
        if (
            sum(p.offer_quantity for p in proposals if p.offer == ada)
            > limits.max_trade_lovelace
        ):
            raise KernelError("Bid exceeds maximum ADA exposure")
        required_ada += committed
        deltas = [
            (base.unit, -required_base, -required_base),
            ("lovelace", -committed - limits.max_fee_lovelace, -committed),
        ]
    elif action == "cancel":
        if not order_rows:
            raise KernelError("No orders to cancel")
        spend, beacon = reference("script_hash"), reference("beacon_policy")
        returned = {}
        for row in order_rows:
            remember(row)
            close_order(
                builder,
                profile,
                row,
                owner,
                swap_reference=spend,
                beacon_reference=beacon,
            )
            for unit, q in row_assets(row, profile.name).items():
                if unit == "lovelace" or unit == base.unit:
                    returned[unit] = returned.get(unit, 0) + q
        deltas = [
            (base.unit, returned.get(base.unit, 0), returned.get(base.unit, 0)),
            (
                "lovelace",
                returned["lovelace"] - limits.max_fee_lovelace,
                returned["lovelace"],
            ),
        ]
    elif action in ("rebalance-sell", "rebalance-buy", "route"):
        if observation is None or quantity <= 0 or quantity > limits.max_rebalance_base:
            raise KernelError("Swap exceeds configured token amount bounds")
        pool = observation.pool
        venue_fee = observation.fixed_fee
        if action in ("rebalance-sell", "route"):
            x, _ = pool.compute_pool_change(-quantity, observation.reward)
            min_out = (-x * (10000 - limits.slippage_bps)) // 10000
            input_unit, output_unit, amount = base.unit, "lovelace", quantity
            required_base = quantity
            deltas = [
                (base.unit, -quantity, -quantity),
                (
                    "lovelace",
                    min_out - venue_fee - limits.max_fee_lovelace,
                    -x - venue_fee,
                ),
            ]
            if action == "route":
                if route_row is None:
                    raise KernelError("Route has no Kernel input")
                order = decode_swaps(route_row, profile)
                if order.offer != base or order.ask != ada:
                    raise KernelError("Route direction mismatch")
                payment = ceil_fraction(quantity * order.price)
                if (
                    payment > limits.max_trade_lovelace
                    or min_out - payment - venue_fee - limits.max_fee_lovelace
                    < limits.min_route_gain_lovelace
                ):
                    raise KernelError("Atomic route does not meet net gain/cost limits")
                remember(route_row)
                fill_order(
                    builder,
                    profile,
                    route_row,
                    quantity,
                    payment,
                    swap_reference=reference("script_hash"),
                )
                required_base, required_ada = 0, required_ada + payment
                deltas = [
                    (base.unit, 0, 0),
                    (
                        "lovelace",
                        limits.min_route_gain_lovelace,
                        -x - payment - venue_fee,
                    ),
                ]
        else:
            adjusted = pool.model_copy(deep=True)
            adjusted.assets.root["lovelace"] += observation.reward
            if quantity >= pool.active_liquidity(observation.reward)[1]:
                raise KernelError("Rebalance buy exceeds available pool liquidity")
            incoming, _ = adjusted.get_amount_in(Assets(root={base.unit: quantity}))
            amount = incoming.quantity()
            _, y = pool.compute_pool_change(amount, observation.reward)
            current_base = protected_base + sum(
                row_assets(r, profile.name).get(base.unit, 0) for r in wallet_rows
            )
            if current_base - y > market.settings.max_base:
                raise KernelError("Rounded rebalance output exceeds maximum inventory")
            min_out = quantity
            input_unit, output_unit = "lovelace", base.unit
            if amount + venue_fee > limits.max_trade_lovelace:
                raise KernelError("Rebalance exceeds ADA spending cap")
            required_ada += amount + venue_fee
            deltas = [
                (base.unit, quantity, -y),
                (
                    "lovelace",
                    -amount - venue_fee - limits.max_fee_lovelace,
                    -amount - venue_fee,
                ),
            ]
        for row in observation.rows:
            remember(row)
        DanoSession(
            profile,
            observation.rows,
            now=observation.observed_at,
            rewards=observation.rewards,
        ).contribute(
            builder,
            OutRef(observation.row["tx_hash"], observation.row["tx_index"]),
            input_unit,
            amount,
            output_unit,
            min_out,
        )
    else:
        raise KernelError("Unsupported trading action")
    # Smallest useful UTxOs first, with explicit coin-selection bounds.
    selected, ada_total, base_total = [], 0, 0
    for row in sorted(
        wallet_rows,
        key=lambda r: (
            not (required_base and row_assets(r, profile.name).get(base.unit, 0)),
            int(r["value"]),
        ),
    ):
        if ada_total >= required_ada and base_total >= required_base:
            break
        ada_total += int(row["value"])
        base_total += row_assets(row, profile.name).get(base.unit, 0)
        selected.append(row)
    if (
        ada_total < required_ada
        or base_total < required_base
        or ada_total > limits.max_funding_lovelace
    ):
        raise KernelError("Insufficient independently allocated operating inventory")
    for row in selected:
        builder.add_input(remember(row))
    metadata = {
        "mode": "MVP preprod strategy execution",
        "market_fingerprint": market_fingerprint(market),
        "action": action,
        "wallet": str(owner),
        "venue_fee_lovelace": venue_fee,
        "asset_delta_limits": deltas,
        "order_outputs": order_outputs,
        "order_inputs": [f"{r['tx_hash']}#{r['tx_index']}" for r in order_rows],
        "quantity": quantity,
    }
    from .execution import prepare_transaction

    return {
        "action": action,
        **prepare_transaction(
            provider,
            journal,
            builder,
            owner,
            wallet,
            key_dir,
            intent,
            deltas,
            metadata,
            list(dependencies.values()),
            max_fee=limits.max_fee_lovelace,
            max_collateral=limits.collateral_lovelace,
        ),
    }
