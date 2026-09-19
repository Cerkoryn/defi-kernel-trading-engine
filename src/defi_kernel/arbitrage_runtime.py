"""Observed liquidity → one inspected atomic transaction → durable settlement."""

import json
import time
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from uuid import uuid4

import cbor2
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dexs.amm.dano import DanoCLMMState
from charli3_dendrite.dexs.core.errors import InvalidPoolError
from charli3_dendrite.utility import asset_to_value
from pycardano import Address, ScriptHash, Transaction, TransactionOutput
from pycardano.exception import PyCardanoException
from pycardano.utils import min_lovelace

from .arbitrage import Edge, search_routes
from .arbitrage_risk import check_candidate, drawdown
from .chain_context import ProviderChainContext, to_utxo
from .coordinator import Coordinator
from .dendrite_bridge import (
    DANO_REFERENCE,
    DanoSession,
    clip_dano_validity,
    protocol_epoch,
)
from .domain import KernelError, OutRef, ceil_fraction
from .execution import build_transaction, validate_candidate
from .protocols import (
    DANO_CONFIG,
    DANO_PREPROD_HASH,
    DEPLOYMENTS,
    SwapsV1Datum,
    checked_datum,
    decode_dano,
    decode_swaps,
    row_assets,
)
from .providers import ProviderError
from .signing import LocalSigner, ref_text, value_units
from .trading import wallet_inventory
from .transactions import CompositionBuilder, _previous, fill_order
from .venues import (
    GENIUS_CONFIG_HASH,
    GENIUS_CONFIG_TOKEN,
    GENIUS_POLICY,
    NEW_VENUES,
    VENUE_HASHES,
    contribute_direct,
    decode_direct,
)


def row_ref(row):
    return OutRef(row["tx_hash"], row["tx_index"])


@dataclass(frozen=True)
class DanoQuoteSnapshot:
    """Quote-only values: decode once, reuse pinned SDK math, never build from here."""

    _datum: object
    reserve_a: int
    reserve_b: int
    unit_a: str
    unit_b: str

    @classmethod
    def from_pool(cls, pool):
        return cls(
            deepcopy(pool._datum),
            pool.reserve_a,
            pool.reserve_b,
            pool.unit_a,
            pool.unit_b,
        )

    active_liquidity = DanoCLMMState.active_liquidity
    compute_pool_change = DanoCLMMState.compute_pool_change


class Liquidity:
    """Cache only decoded immutable rows; rewards, freshness and references refresh."""

    def __init__(self):
        self.cache = {}

    def observe(self, provider, config, owner, context, stopped=lambda: False):
        profile, now = provider.profile, provider.clock()
        references = (
            provider.utxos([DANO_CONFIG[profile.name], DANO_REFERENCE[profile.name]])
            if "dano" in config.venues
            else []
        )
        by_ref = {row_ref(r): r for r in references}
        if "dano" in config.venues and set(by_ref) != {
            DANO_CONFIG[profile.name],
            DANO_REFERENCE[profile.name],
        }:
            raise KernelError("Dano configuration/reference is unavailable")
        rate, fixed = (
            cbor2.loads(
                bytes.fromhex(checked_datum(by_ref[DANO_CONFIG[profile.name]], 2))
            ).value
            if "dano" in config.venues
            else (0, 0)
        )
        if (
            type(rate) is not int
            or not 0 <= rate < 10000
            or type(fixed) is not int
            or fixed < 0
        ):
            raise KernelError("Invalid Dano protocol fee configuration")
        edges, rewards, reasons, cache = [], {}, Counter(), {}
        observed_at = now
        decoded_rows, accounts, script_hashes = [], set(), set()
        for venue, credential in (
            ("swaps-v1", DEPLOYMENTS["swaps-v1"]["script_hash"]),
            ("dano", DANO_PREPROD_HASH),
        ):
            if venue not in config.venues:
                continue
            observation = provider.credential_utxos(credential)
            if not observation.complete:
                raise KernelError("Incomplete arbitrage liquidity observation")
            observed_at = min(observed_at, observation.observed_at)
            for row in observation.rows:
                if stopped():
                    raise KernelError("Stop requested during liquidity discovery")
                try:
                    if (
                        type(row.get("block_height")) is not int
                        or context._tip["block_no"] - row["block_height"] + 1
                        < profile.confirmations
                    ):
                        raise KernelError("Liquidity has insufficient confirmations")
                    key = (venue, json.dumps(row, sort_keys=True), rate)
                    decoded = self.cache.get(key)
                    if decoded is None:
                        to_utxo(row, profile)
                        decoded = (
                            decode_swaps(row, profile)
                            if venue == "swaps-v1"
                            else decode_dano(row, profile, platform_fee_rate=rate)
                        )
                    cache[key] = decoded
                    account = None
                    if venue == "dano":
                        if not {decoded.unit_a, decoded.unit_b} <= set(config.assets):
                            continue
                        if (
                            decoded.unit_a == "lovelace"
                            and protocol_epoch(profile, int(now * 1000))
                            > decoded._datum.last_withdraw_epoch
                        ):
                            address = Address.from_primitive(row["address"])
                            if not isinstance(address.staking_part, ScriptHash):
                                raise KernelError(
                                    "Dano withdrawal requires a script stake credential"
                                )
                            account = str(
                                Address(
                                    staking_part=address.staking_part,
                                    network=address.network,
                                )
                            )
                            accounts.add(account)
                            script_hashes.add(str(address.staking_part))
                    decoded_rows.append((venue, row, decoded, account))
                except (
                    KernelError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ArithmeticError,
                ) as error:
                    reasons[
                        type(error).__name__ + ": " + str(error).splitlines()[0][:120]
                    ] += 1
        # Fetch shared mutable dependencies together. A provider failure invalidates
        # the entire observation, never just the affected liquidity rows.
        if stopped():
            raise KernelError("Stop requested during liquidity discovery")
        rewards = provider.stake_rewards_many(accounts)
        for venue, row, decoded, account in decoded_rows:
            if stopped():
                raise KernelError("Stop requested during liquidity discovery")
            try:
                if venue == "swaps-v1":
                    order = decoded
                    if (
                        Address.from_primitive(order.address).staking_part
                        == owner.staking_part
                    ):
                        raise KernelError("Own-wallet liquidity excluded")
                    if not {order.offer.unit, order.ask.unit} <= set(config.assets):
                        continue
                    capacity = order.held_offer
                    if order.offer.unit == "lovelace":
                        # Reserve a conservative carrier using the largest value encodings.
                        assets = row_assets(row, profile.name)
                        assets[order.ask.unit] = assets.get(
                            order.ask.unit, 0
                        ) + ceil_fraction(capacity * order.price)
                        datum = replace(
                            SwapsV1Datum.from_cbor(order.datum_cbor),
                            prev_input=_previous(order.ref.tx_hash, order.ref.index),
                        )
                        future = TransactionOutput(
                            Address.from_primitive(order.address),
                            asset_to_value(Assets(root=assets)),
                            datum=datum,
                        )
                        capacity -= min_lovelace(context, output=future)
                    if capacity > 0:
                        edges.append(
                            Edge(
                                order.ref,
                                venue,
                                order.ask.unit,
                                order.offer.unit,
                                capacity,
                                price=order.price,
                            )
                        )
                else:
                    pool = DanoQuoteSnapshot.from_pool(decoded)
                    if not {pool.unit_a, pool.unit_b} <= set(config.assets):
                        continue
                    reward = rewards[account] if account is not None else 0
                    for is_x in (True, False):
                        available = pool.active_liquidity(reward)[1 if is_x else 0]
                        low, high = 0, 2**63 - 1
                        while low < high:
                            mid = (low + high + 1) // 2
                            x, y = pool.compute_pool_change(
                                mid if is_x else -mid, reward
                            )
                            if (-y if is_x else -x) < available:
                                low = mid
                            else:
                                high = mid - 1
                        if low:
                            edges.append(
                                Edge(
                                    row_ref(row),
                                    venue,
                                    pool.unit_a if is_x else pool.unit_b,
                                    pool.unit_b if is_x else pool.unit_a,
                                    low,
                                    pool=pool,
                                    reward=reward,
                                    fixed_fee=fixed,
                                )
                            )
                by_ref[row_ref(row)] = row
            except (
                KernelError,
                ValueError,
                TypeError,
                KeyError,
                ArithmeticError,
            ) as error:
                reasons[
                    type(error).__name__ + ": " + str(error).splitlines()[0][:120]
                ] += 1
        self.cache = cache
        config_row = None
        if "genius-yield" in config.venues:
            observation = provider.credential_utxos(GENIUS_CONFIG_HASH)
            if not observation.complete:
                raise KernelError("Incomplete Genius configuration discovery")
            candidates = [
                r
                for r in observation.rows
                if row_assets(r, profile.name).get(GENIUS_CONFIG_TOKEN) == 1
            ]
            if len(candidates) != 1:
                raise KernelError("Genius configuration is missing or ambiguous")
            config_row = candidates[0]
            by_ref[row_ref(config_row)] = config_row
            observed_at = min(observed_at, observation.observed_at)
        for venue in NEW_VENUES:
            if venue not in config.venues:
                continue
            for credential in VENUE_HASHES[venue]:
                observation = provider.credential_utxos(credential)
                if not observation.complete:
                    raise KernelError("Incomplete direct-venue liquidity observation")
                observed_at = min(observed_at, observation.observed_at)
                direct_rows = (
                    provider.resolve_datums(observation.rows)
                    if venue == "genius-yield"
                    else observation.rows
                )
                for row in direct_rows:
                    if stopped():
                        raise KernelError("Stop requested during liquidity discovery")
                    try:
                        if (
                            type(row.get("block_height")) is not int
                            or context._tip["block_no"] - row["block_height"] + 1
                            < profile.confirmations
                        ):
                            raise KernelError(
                                "Liquidity has insufficient confirmations"
                            )
                        state = decode_direct(
                            venue,
                            row,
                            profile,
                            context,
                            owner=owner,
                            config_row=config_row,
                        )
                        if not {state.unit_a, state.unit_b} <= set(config.assets):
                            reasons[f"{venue}: assets outside allowlist"] += 1
                            continue
                        edges.extend(state.edges())
                        by_ref[row_ref(row)] = row
                        if venue != "saturnswap":
                            script_hashes.add(credential)
                        if venue == "genius-yield":
                            script_hashes.add(GENIUS_POLICY)
                    except (
                        KernelError,
                        ValueError,
                        TypeError,
                        KeyError,
                        ArithmeticError,
                    ) as error:
                        reasons[
                            type(error).__name__
                            + ": "
                            + str(error).splitlines()[0][:120]
                        ] += 1
        if any(e.venue == "swaps-v1" for e in edges):
            script_hashes.add(DEPLOYMENTS["swaps-v1"]["script_hash"])
        for reference in provider.reference_scripts(script_hashes).values():
            by_ref[row_ref(reference)] = reference
        if not 0 <= provider.clock() - observed_at <= config.max_age_seconds:
            raise KernelError("Liquidity observation expired during discovery")
        return edges, by_ref, rewards, observed_at, dict(reasons)


class CandidateExpired(KernelError):
    reason_code = "deadline"


def candidate_priority(candidate):
    """Profit first; prefer fewer bytes/resources only when net profit is equal."""
    metadata = candidate[2]
    resources = metadata["resources"]
    return (
        -metadata["net_profit_lovelace"],
        resources["signed_bytes"],
        resources["memory"],
        resources["steps"],
        len(metadata["hops"]),
        metadata["route_id"],
    )


def require_submission_window(provider, submit_before):
    if provider.clock() >= submit_before:
        raise CandidateExpired(
            "Insufficient time left for submission; refresh liquidity"
        )


def build_route(
    provider,
    config,
    owner,
    plan,
    rows,
    rewards,
    observed_at,
    wallet_rows,
    collateral,
    context,
    *,
    loss_headroom=None,
):
    """Net all contributions before selecting ADA-only operating inputs."""
    if provider.profile.name != "preprod":
        raise KernelError("Arbitrage construction is qualified only on preprod")
    if len(plan.hops) > config.max_hops:
        raise KernelError("Route exceeds operator hop limit")
    if plan.hops[0].amount_in > config.max_trade_lovelace or any(
        h.input_unit not in config.assets
        or h.output_unit not in config.assets
        or h.venue not in config.venues
        for h in plan.hops
    ):
        raise KernelError("Route exceeds operator notional/asset limits")
    if not 0 <= provider.clock() - observed_at <= config.max_age_seconds:
        raise KernelError("Route observation expired before construction")
    builder = CompositionBuilder(context)
    builder.validity_start = max(0, context.last_block_slot - 60)
    builder.ttl = min(
        context.last_block_slot + 300,
        context.slot_at_ms(int((observed_at + config.max_age_seconds) * 1000)),
    )
    dano_refs = {h.ref for h in plan.hops if h.venue == "dano"}
    if dano_refs:
        clip_dano_validity(builder, provider.profile, observed_at, context.slot_at_ms)
    submit_before = (
        context.slot_clock.time_at_slot(builder.ttl) / 1000
        - config.submission_margin_seconds
    )
    require_submission_window(provider, submit_before)
    session = (
        DanoSession(
            provider.profile,
            [
                r
                for ref, r in rows.items()
                if ref in dano_refs
                or ref == DANO_CONFIG[provider.profile.name]
                or r.get("reference_script")
            ],
            now=observed_at,
            rewards=rewards,
        )
        if dano_refs
        else None
    )
    swap_reference = next(
        (
            to_utxo(r, provider.profile)
            for r in rows.values()
            if (r.get("reference_script") or {}).get("hash")
            == DEPLOYMENTS["swaps-v1"]["script_hash"]
        ),
        None,
    )
    for hop in plan.hops:
        if hop.venue == "swaps-v1":
            if swap_reference is None:
                raise KernelError("Missing Swaps reference script")
            order = decode_swaps(rows[hop.ref], provider.profile)
            if (order.ask.unit, order.offer.unit) != (hop.input_unit, hop.output_unit):
                raise KernelError("Quote/order identity mismatch")
            _, paid = fill_order(
                builder,
                provider.profile,
                rows[hop.ref],
                hop.amount_out,
                hop.amount_in,
                swap_reference=swap_reference,
            )
            if paid != hop.amount_in:
                raise KernelError("Swaps quote payment changed")
        elif hop.venue == "dano":
            session.contribute(
                builder,
                hop.ref,
                hop.input_unit,
                hop.amount_in,
                hop.output_unit,
                hop.amount_out,
            )
        elif hop.venue in NEW_VENUES:
            config_rows = [
                r
                for r in rows.values()
                if row_assets(r, provider.profile.name).get(GENIUS_CONFIG_TOKEN) == 1
            ]
            if hop.venue == "genius-yield" and len(config_rows) != 1:
                raise KernelError("Missing or ambiguous Genius configuration")
            state = decode_direct(
                hop.venue,
                rows[hop.ref],
                provider.profile,
                context,
                owner=owner,
                config_row=config_rows[0] if len(config_rows) == 1 else None,
            )
            contribute_direct(builder, state, hop, rows)
        else:
            raise KernelError("Unsupported arbitrage venue")
    # Only net ADA can come from the wallet: intermediate inventory is unavailable.
    net = Counter()
    for utxo in builder.inputs:
        net.update(value_units(utxo.output.amount))
    for output in builder.outputs:
        net.subtract(value_units(output.amount))
    net["lovelace"] += sum((builder.withdrawals or {}).values())
    for policy, names in (builder.mint or {}).items():
        for name, quantity in names.items():
            net[str(policy) + bytes(name).hex()] += quantity
    if any(q for unit, q in net.items() if unit != "lovelace"):
        raise KernelError(
            "Protocol contributions leave an intermediate remainder/deficit"
        )
    builder.collaterals.append(to_utxo(collateral, provider.profile))
    collateral_value = int(collateral["value"])
    return_minimum = min_lovelace(
        context, output=TransactionOutput(owner, collateral_value)
    )
    exposure = min(config.collateral_lovelace, collateral_value - return_minimum)
    percent = context.protocol_param.collateral_percent
    if type(percent) is not int or percent <= 0:
        raise KernelError("Invalid collateral percentage")
    allowances = {
        "profit floor": net["lovelace"] - config.min_profit_lovelace,
        "collateral reserve": exposure * 100 // percent,
    }
    if loss_headroom is not None:
        exposure = min(exposure, loss_headroom)
        allowances["drawdown headroom"] = loss_headroom * 100 // percent
    if config.max_fee_lovelace is not None:
        allowances["operator fee cap"] = config.max_fee_lovelace
    fee_reason = min(allowances, key=allowances.get)
    fee_limit = allowances[fee_reason]
    if fee_limit <= 0:
        raise KernelError(f"No fee allowance within {fee_reason}")
    selected, funding = [], 0
    # Reserve change minimum and the fee cap, without prefunding intermediate hops.
    change_minimum = min_lovelace(context, output=TransactionOutput(owner, 10_000_000))
    required = max(1, fee_limit + change_minimum - net["lovelace"])
    for row in sorted(wallet_rows, key=lambda r: (int(r["value"]), str(row_ref(r)))):
        if row.get("asset_list"):
            continue
        if funding >= required:
            break
        selected.append(row)
        funding += int(row["value"])
    if not required <= funding <= config.max_funding_lovelace:
        raise KernelError("Insufficient bounded ADA-only operating inputs")
    for row in selected:
        builder.add_input(to_utxo(row, provider.profile))
    # Large profits must not turn the only operating input into protected change.
    # Keep input limits unchanged and authorize both outputs before balancing.
    operating_return = max(funding, config.operating_target_lovelace)
    if funding + net["lovelace"] - fee_limit >= operating_return + change_minimum:
        builder.add_output(TransactionOutput(owner, operating_return))
    elif funding + net["lovelace"] > config.max_funding_lovelace:
        if funding < change_minimum or net["lovelace"] - fee_limit < change_minimum:
            raise KernelError("Cannot preserve bounded operating change within fee cap")
        builder.add_output(TransactionOutput(owner, funding))
    dependencies = {**rows, **{row_ref(r): r for r in [*selected, collateral]}}
    used = {
        ref_text(u.input)
        for u in [*builder.inputs, *builder.reference_inputs, *builder.collaterals]
    }
    dependencies = [row for ref, row in dependencies.items() if str(ref) in used]
    deltas = [("lovelace", config.min_profit_lovelace, net["lovelace"])]
    metadata = {
        "mode": "atomic arbitrage",
        "action": "arbitrage",
        "max_drawdown_lovelace": config.max_drawdown_lovelace,
        "fee_allowances_lovelace": allowances,
        "fee_limit_reason": fee_reason,
        "strategy_fingerprint": config.fingerprint,
        **plan.describe(),
        "wallet": str(owner),
        "venue_fee_lovelace": sum(h.fixed_fee for h in plan.hops),
        "observed_at": observed_at,
        "asset_delta_limits": deltas,
    }
    require_submission_window(provider, submit_before)
    tx, auth, metadata = build_transaction(
        builder,
        owner,
        deltas,
        metadata,
        max_fee=fee_limit,
        max_collateral=exposure,
    )
    metadata["submit_before"] = (
        context.slot_clock.time_at_slot(tx.transaction_body.ttl) / 1000
        - config.submission_margin_seconds
    )
    return tx, auth, metadata, dependencies


def build_allocation(
    provider, journal, config, owner, inventory, collateral, protected, context
):
    """Restore ADA UTxO roles using operating funds or verified bot proceeds only."""
    operating = [r for r in inventory if not r.get("asset_list")]
    if (
        sum(int(r["value"]) for r in operating) >= config.operating_target_lovelace
        and collateral
    ):
        return None
    proceeds = {}
    for record in journal.db.execute(
        "SELECT t.txid,o.unsigned FROM transactions t JOIN outbox o USING(intent) "
        "JOIN arbitrage_outcomes a USING(intent) WHERE t.status='confirmed' "
        "AND a.block_hash=o.block_hash AND json_extract(o.metadata,'$.mode')='atomic arbitrage' "
        "AND json_extract(o.metadata,'$.wallet')=?",
        (str(owner),),
    ):
        body = Transaction.from_cbor(record["unsigned"]).transaction_body
        if str(body.id) != record["txid"]:
            raise KernelError("Allocation source transaction identity mismatch")
        for index, output in enumerate(body.outputs):
            if (
                output.address == owner
                and not output.amount.multi_asset
                and output.datum is None
                and output.datum_hash is None
                and output.script is None
            ):
                proceeds[f"{record['txid']}#{index}"] = output.to_cbor()
    sources = list(operating)
    for row in protected:
        expected = proceeds.get(str(row_ref(row)))
        if expected is not None:
            if to_utxo(row, provider.profile).output.to_cbor() != expected:
                raise KernelError("Verified allocation proceeds changed; resynchronize")
            sources.append(row)
    # One source limits consolidation and cannot expose the unrelated faucet reserve.
    reserve = 0 if collateral else config.collateral_lovelace
    required = (
        config.operating_target_lovelace + reserve + config.max_maintenance_fee_lovelace
    )
    source = next(
        (
            r
            for r in sorted(sources, key=lambda r: int(r["value"]))
            if int(r["value"]) >= required
        ),
        None,
    )
    if source is None:
        return None
    builder = CompositionBuilder(context)
    builder.validity_start = max(0, context.last_block_slot - 60)
    builder.ttl = context.slot_at_ms(
        int((provider.clock() + config.max_age_seconds) * 1000)
    )
    builder.add_input(to_utxo(source, provider.profile))
    builder.add_output(TransactionOutput(owner, config.operating_target_lovelace))
    if reserve:
        builder.add_output(TransactionOutput(owner, reserve))
    metadata = {
        "mode": "atomic arbitrage",
        "action": "allocate",
        "max_drawdown_lovelace": config.max_drawdown_lovelace,
        "wallet": str(owner),
        "strategy_fingerprint": config.fingerprint,
        "path": [],
        "venue_fee_lovelace": 0,
        "operating_target_lovelace": config.operating_target_lovelace,
        "collateral_created_lovelace": reserve,
    }
    tx, auth, metadata = build_transaction(
        builder,
        owner,
        [("lovelace", -config.max_maintenance_fee_lovelace, 0)],
        metadata,
        max_fee=config.max_maintenance_fee_lovelace,
        max_collateral=0,
    )
    metadata.update(
        net_profit_lovelace=-tx.transaction_body.fee,
        submit_before=context.slot_clock.time_at_slot(tx.transaction_body.ttl) / 1000
        - config.submission_margin_seconds,
    )
    return tx, auth, metadata, [source]


class ArbitrageEngine:
    def __init__(
        self,
        provider,
        journal,
        config,
        wallet,
        key_dir,
        *,
        execute=False,
        reporter=None,
    ):
        from .runtime import bind_wallet

        if provider.profile.name != "preprod":
            raise KernelError("Arbitrage is currently qualified only on preprod")
        self.owner, _ = bind_wallet(
            provider.profile,
            journal,
            None,
            wallet["address"],
        )
        self.provider, self.journal, self.config = provider, journal, config
        self.wallet, self.key_dir, self.execute = wallet, key_dir, execute
        self.reporter = reporter
        self.coordinator = Coordinator(
            provider, journal, before_submit=reporter.guard if reporter else None
        )
        self.liquidity = Liquidity()
        self.confirmed = {}
        self.rotation = 0
        self.current_report = {}
        self.clock, self.sleep = provider.clock, time.sleep

    def stop_requested(self):
        return (
            bool(self.reporter and self.reporter.stop_requested)
            or self.journal.stop_requested()
        )

    def phase(self, name):
        self.current_report["phase"] = name
        if self.reporter:
            self.reporter.set_phase(name)

    def settlement(self):
        """Report confirmed body-derived PnL, invalidating cached results on rollback."""
        settled = []
        for row in self.journal.db.execute(
            "SELECT t.intent,t.txid,o.unsigned,o.dependencies,o.metadata,o.block_hash,t.status, "
            "a.block_hash AS verified_block,a.net_lovelace AS verified_net "
            "FROM outbox o JOIN transactions t USING(intent) "
            "LEFT JOIN arbitrage_outcomes a USING(intent) "
            "WHERE json_extract(o.metadata,'$.mode')='atomic arbitrage'"
        ):
            if row["status"] == "failed":
                from .trading import collateral_loss

                loss = collateral_loss(
                    Transaction.from_cbor(row["unsigned"]).transaction_body,
                    json.loads(row["dependencies"]),
                )
                self.journal.record_arbitrage_outcome(
                    row["intent"], row["block_hash"], -loss, self.clock()
                )
                if (row["verified_block"], row["verified_net"]) != (
                    row["block_hash"],
                    -loss,
                ):
                    settled.append(
                        {
                            "txid": row["txid"],
                            "stage": "failed",
                            "net_profit_lovelace": -loss,
                        }
                    )
            if row["status"] != "confirmed":
                self.confirmed.pop(row["txid"], None)
                continue
            key = (row["txid"], row["block_hash"])
            if self.confirmed.get(row["txid"], (None,))[0] != key:
                tx = self.provider.transaction_cbor(row["txid"])
                expected = Transaction.from_cbor(row["unsigned"])
                if (
                    not tx.valid
                    or tx.transaction_body.to_cbor()
                    != expected.transaction_body.to_cbor()
                ):
                    raise KernelError(
                        "Confirmed transaction does not match arbitrage candidate"
                    )
                delta = Counter()
                inputs = {ref_text(i) for i in tx.transaction_body.inputs}
                for dependency in json.loads(row["dependencies"]):
                    if str(row_ref(dependency)) in inputs and dependency[
                        "address"
                    ] == str(self.owner):
                        delta.subtract(
                            row_assets(dependency, self.provider.profile.name)
                        )
                for output in tx.transaction_body.outputs:
                    if output.address == self.owner:
                        delta.update(value_units(output.amount))
                expected_profit = json.loads(row["metadata"])["net_profit_lovelace"]
                if (
                    any(q for unit, q in delta.items() if unit != "lovelace")
                    or delta["lovelace"] != expected_profit
                ):
                    raise KernelError("Confirmed arbitrage wallet delta mismatch")
                self.journal.record_arbitrage_outcome(
                    row["intent"], row["block_hash"], delta["lovelace"], self.clock()
                )
                self.confirmed[row["txid"]] = (key, delta["lovelace"])
            if (row["verified_block"], row["verified_net"]) == (
                row["block_hash"],
                self.confirmed[row["txid"]][1],
            ):
                continue
            settled.append(
                {
                    "txid": row["txid"],
                    "stage": "confirmed",
                    "net_profit_lovelace": self.confirmed[row["txid"]][1],
                }
            )
        return settled

    def tick(self):
        p, j, c = self.provider, self.journal, self.config
        started = time.monotonic()
        report = self.current_report = {
            "mode": "preprod execution" if self.execute else "evaluated shadow",
            "observed_at": p.clock(),
            "network": p.profile.name,
        }
        self.phase("reconciliation")
        p.verify_identity()
        tip = p.tip()
        try:
            self.coordinator.reconcile_all(tip=tip)
            report["settlement_changes"] = self.settlement()
        finally:
            report["timings"] = {"reconciliation_seconds": time.monotonic() - started}
            if self.reporter:
                self.reporter.transactions()
        risk = drawdown(j, c.max_drawdown_lovelace)
        report["risk"] = risk
        report["loss_headroom_lovelace"] = risk["loss_headroom_lovelace"]
        j.require_no_execution_incident()
        pending = list(
            j.db.execute(
                "SELECT intent,status FROM transactions WHERE status NOT IN ('confirmed','aborted','expired','conflicted','failed')"
            )
        )
        if self.stop_requested():
            return report | {"stage": "stopped"}
        if self.reporter and not self.reporter.event(
            "logging_check", namespace="SYSTEM", visible=False
        ):
            return report | {
                "stage": "paused",
                "reason": "Diagnostic logging unavailable; reconciliation remains enabled",
            }
        if pending:
            if self.execute and len(pending) == 1:
                entry = j.outbox_entry(pending[0]["intent"])
                metadata = json.loads(entry["metadata"])
                if entry["status"] == "prepared" and entry["signed"] is None:
                    j.abandon_unsigned(
                        entry["intent"], "Recovered never-signed candidate"
                    )
                    return report | {
                        "stage": "recovered",
                        "intent": entry["intent"],
                        "reason": "Released never-signed candidate",
                    }
                elif (
                    entry["status"] == "prepared"
                    and metadata.get("mode") == "atomic arbitrage"
                    and metadata.get("strategy_fingerprint") == c.fingerprint
                ):
                    self.coordinator.submit(entry["intent"])
                    return report | {
                        "stage": "submitted",
                        "intent": entry["intent"],
                        "txid": entry["txid"],
                        "reason": "Submitted original prepared bytes",
                    }
            return report | {"stage": "pending", "pending": [dict(r) for r in pending]}
        if not 0 <= p.clock() - tip["block_time"] <= c.max_age_seconds:
            raise KernelError("Provider tip is stale or future-dated")
        if risk["loss_headroom_lovelace"] <= 0:
            return report | {
                "stage": "paused",
                "reason": "Drawdown limit reached; no loss headroom for new execution",
            }
        self.phase("observation")
        observation_started = time.monotonic()
        context = ProviderChainContext(p)
        inventory, collateral, protected = wallet_inventory(
            p, j, self.owner, tip, c, require_collateral=False
        )
        report["funds"] = {
            "operating": sum(
                int(r["value"]) for r in inventory if not r.get("asset_list")
            ),
            "collateral": int(collateral["value"]) if collateral else 0,
            "protected": sum(int(r["value"]) for r in protected)
            + sum(int(r["value"]) for r in inventory if r.get("asset_list")),
        }
        if self.reporter:
            self.reporter.funds(report["funds"])
        allocation = build_allocation(
            p, j, c, self.owner, inventory, collateral, protected, context
        )
        if allocation is not None:
            tx, auth, metadata, dependencies = allocation
            self.phase("allocation")
            require_submission_window(p, metadata["submit_before"])
            validate_candidate(p, tx, auth, context, dependencies)
            report["allocation"] = {
                k: metadata[k]
                for k in (
                    "operating_target_lovelace",
                    "collateral_created_lovelace",
                    "fee_lovelace",
                )
            }
            return self.finish_candidate(
                tx, auth, metadata, dependencies, context, report
            )
        if collateral is None:
            return report | {
                "stage": "paused",
                "reason": "Insufficient verified ADA to restore dedicated collateral and operating reserve",
            }
        if not report["funds"]["operating"]:
            return report | {
                "stage": "paused",
                "reason": "No eligible ADA operating inputs or sufficient verified bot proceeds for allocation",
            }
        edges, rows, rewards, observed_at, rejected = self.liquidity.observe(
            p, c, self.owner, context, self.stop_requested
        )
        discovered = time.monotonic()
        report["liquidity_observed_at"] = observed_at
        self.phase("search")
        plans, stats = search_routes(
            edges, c, rotation=self.rotation, stop_requested=self.stop_requested
        )
        self.rotation += 1
        searched = time.monotonic()
        report.update(
            search=stats,
            rejected_liquidity=rejected,
            edge_count=len(edges),
            venue_edges={v: sum(e.venue == v for e in edges) for v in c.venues},
            atomic_snapshot=False,
        )
        candidates, failures, skipped = [], [], []
        self.phase("build_and_evaluation")
        for plan in plans[: c.max_evaluations]:
            if self.stop_requested():
                return report | {"stage": "stopped"}
            best_net = max(
                (
                    x[2]["net_profit_lovelace"]
                    for x in candidates
                    if p.clock() < x[2]["submit_before"]
                ),
                default=None,
            )
            # Even without network or venue fees, this route cannot beat the winner.
            # Never prune using the heuristic fee estimate or an expired winner.
            if best_net is not None and (
                plan.hops[-1].amount_out - plan.hops[0].amount_in < best_net
            ):
                skipped.append(
                    {"route_id": plan.route_id, "reason": "cannot_beat_evaluated_net"}
                )
                continue
            phase = "build"
            try:
                tx, auth, metadata, dependencies = build_route(
                    p,
                    c,
                    self.owner,
                    plan,
                    rows,
                    rewards,
                    observed_at,
                    inventory,
                    collateral,
                    context,
                    loss_headroom=risk["loss_headroom_lovelace"],
                )
                phase = "profit_floor"
                built_gain = auth.asset_delta_limits[0][2] - tx.transaction_body.fee
                if built_gain < c.min_profit_lovelace:
                    raise KernelError(
                        f"Net ADA gain {built_gain / 1_000_000:.6f} tADA is below "
                        f"minimum {c.min_profit_lovelace / 1_000_000:.6f} tADA "
                        f"(fee {tx.transaction_body.fee / 1_000_000:.6f} tADA)"
                    )
                phase = "deadline"
                require_submission_window(p, metadata["submit_before"])
                phase = "evaluation"
                receipt, delta, resources = validate_candidate(
                    p, tx, auth, context, dependencies
                )
                phase = "drawdown"
                check_candidate(j, tx, dependencies, metadata)
                metadata.update(
                    net_profit_lovelace=delta["lovelace"],
                    resources=resources,
                    final_evaluation=asdict(receipt),
                )
                candidates.append((tx, auth, metadata, dependencies))
            except ProviderError:
                raise
            except (
                KernelError,
                ValueError,
                PyCardanoException,
                InvalidPoolError,
            ) as error:
                from .reporting import error_details

                failure = {
                    "route_id": plan.route_id,
                    "phase": getattr(error, "reason_code", phase),
                    **error_details(error),
                }
                if failure["phase"] == "request_size":
                    failure.update(
                        request_bytes=error.actual, request_limit=error.limit
                    )
                failures.append(failure)
                if self.reporter and self.reporter.debug:
                    self.reporter.event("candidate_rejected", failure, level="DEBUG")
        # Another candidate's evaluation can consume this one's remaining window.
        current = p.clock()
        valid = []
        for candidate in candidates:
            if current >= candidate[2]["submit_before"]:
                failures.append(
                    {
                        "route_id": candidate[2]["route_id"],
                        "phase": "deadline",
                        "reason": "Candidate expired while evaluating other routes",
                    }
                )
            else:
                valid.append(candidate)
        candidates = valid
        report.update(
            build_rejections=failures,
            skipped_candidates=skipped,
            selection_policy="highest_evaluated_net_then_smallest_transaction",
            rejection_counts=dict(Counter(f["phase"] for f in failures)),
            evaluated_candidates=[
                {
                    k: x[2][k]
                    for k in (
                        "route_id",
                        "path",
                        "net_profit_lovelace",
                        "fee_lovelace",
                        "resources",
                    )
                }
                for x in candidates
            ],
            timings={
                **report["timings"],
                "observation_seconds": discovered - observation_started,
                "search_seconds": searched - discovered,
                "build_and_evaluation_seconds": time.monotonic() - searched,
            },
            elapsed_seconds=time.monotonic() - started,
        )
        if self.stop_requested():
            return report | {"stage": "stopped"}
        if not candidates:
            reason = (
                "Candidates rejected: "
                + "; ".join(
                    f"{count} below minimum net profit"
                    if phase == "profit_floor"
                    else f"{count} expired before submission"
                    if phase == "deadline"
                    else f"{count} {phase}: "
                    + next(f["reason"] for f in failures if f["phase"] == phase)
                    for phase, count in sorted(report["rejection_counts"].items())
                )
                if failures
                else "No eligible liquidity"
                if not edges
                else "No profitable candidate found within search bounds"
            )
            return report | {"stage": "no_opportunity", "reason": reason}
        tx, auth, metadata, dependencies = min(candidates, key=candidate_priority)
        report["selected"] = metadata
        return self.finish_candidate(tx, auth, metadata, dependencies, context, report)

    def finish_candidate(self, tx, auth, metadata, dependencies, context, report):
        p = self.provider
        check_candidate(self.journal, tx, dependencies, metadata)
        require_submission_window(p, metadata["submit_before"])
        if not self.execute:
            return report | {"stage": "evaluated", "submission_enabled": False}
        if self.stop_requested():
            return report | {"stage": "stopped"}
        intent = "arbitrage-" + uuid4().hex
        if self.reporter:
            metadata["run_id"] = self.reporter.run_id
        self.phase("prepare")
        signer = LocalSigner(
            self.key_dir / self.wallet["payment_key"],
            self.key_dir / self.wallet["stake_key"],
        )
        txid = self.coordinator.prepare(
            intent, tx, auth, context, dependencies, signer, metadata
        )
        report["risk"] = drawdown(self.journal, self.config.max_drawdown_lovelace)
        report["loss_headroom_lovelace"] = report["risk"]["loss_headroom_lovelace"]
        if self.stop_requested():
            return report | {"stage": "prepared", "intent": intent, "txid": txid}
        self.phase("submission")
        self.coordinator.submit(intent)
        report["elapsed_seconds"] = p.clock() - report["observed_at"]
        return report | {"stage": "submitted", "intent": intent, "txid": txid}

    def run(self, *, interval=30, iterations=None, emit=lambda result: None):
        from .runtime import poll

        return poll(
            self, tick=self.tick, interval=interval, iterations=iterations, emit=emit
        )
