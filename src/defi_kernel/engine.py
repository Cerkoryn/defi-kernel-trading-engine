"""Single-wallet scheduling with explicit atomic trades and staged repricing."""

import json
import time
from collections import Counter
from dataclasses import asdict
from uuid import uuid4

from .coordinator import Coordinator
from .domain import KernelError, ceil_fraction
from .protocols import DEPLOYMENTS, decode_swaps, row_assets
from .runtime import bind_wallet
from .settlement import OrderObserver
from .strategy import Inventory, MarketMaker
from .trading import observe_pool, prepare_action, wallet_inventory

TERMINAL = {"confirmed", "aborted", "expired", "conflicted", "failed"}


class TradingEngine:
    def __init__(
        self,
        provider,
        journal,
        market,
        wallet,
        key_dir,
        *,
        execute=False,
        strategy=None,
        clock=time.time,
        sleep=time.sleep,
    ):
        self.owner, self.order_address = bind_wallet(
            provider.profile, journal, market, wallet["address"]
        )
        if execute and provider.profile.name != "preprod":
            raise KernelError("Live MVP strategy is qualified only on preprod")
        self.provider, self.journal, self.market = provider, journal, market
        self.wallet, self.key_dir = wallet, key_dir
        self.execute, self.clock, self.sleep = execute, clock, sleep
        self.strategy = (
            strategy if strategy is not None else MarketMaker(market.settings)
        )
        self.preview_cancel = False
        self.observer = OrderObserver(provider, journal, self.order_address)
        self.coordinator = Coordinator(provider, journal)

    def request_cancel(self):
        self.journal.db.execute(
            "INSERT INTO engine_control VALUES(1,1) ON CONFLICT(singleton) DO UPDATE SET cancel_requested=1"
        )

    def cancelling(self):
        row = self.journal.db.execute(
            "SELECT cancel_requested FROM engine_control WHERE singleton=1"
        ).fetchone()
        return self.preview_cancel or bool(row and row[0])

    def costs(self):
        from .trading import execution_costs

        return execution_costs(self.provider, self.journal)

    def reconcile(self):
        projection = self.observer.sync()
        self.provider.verify_identity()
        tip = self.provider.tip()
        changed = False
        # Old confirmed entries are still checked for rollback. Provider histories
        # are bounded and cached, but no arbitrary finality assumption is made.
        for row in list(
            self.journal.db.execute("SELECT intent,status FROM transactions")
        ):
            if row["status"] == "aborted":
                continue
            status = self.coordinator.reconcile(row["intent"], tip=tip)
            changed |= status != row["status"]
        if changed:
            projection = self.observer.sync()
        return projection

    def tick(self, *, route_only=False):
        p, j, m = self.provider, self.journal, self.market
        projection = self.reconcile()
        live = [o for o in projection["orders"] if o["live"]]
        result = {
            "mode": "preprod execution" if self.execute else "live-data shadow",
            "observed_at": self.clock(),
            "network": p.profile.name,
            "orders": [{k: v for k, v in o.items() if k != "row"} for o in live],
            "fills": projection["fills"][-20:],
            "fill_count": len(projection["fills"]),
            "costs": self.costs(),
        }

        def decision(action, reason, **extra):
            result.update(action=action, reason=reason, **extra)
            return result

        if j.stop_requested():
            return decision("stopped", "Stop requested; open orders remain on-chain")
        pending = list(
            j.db.execute(
                "SELECT intent,status FROM transactions WHERE status NOT IN ('confirmed','aborted','expired','conflicted','failed')"
            )
        )
        if pending:
            for row in pending:
                entry = j.outbox_entry(row["intent"])
                if (
                    entry["status"] == "prepared"
                    and entry["signed"] is None
                    and self.execute
                ):
                    j.abandon_unsigned(
                        row["intent"], "Restart recovered a never-signed candidate"
                    )
                    return decision(
                        "recovered", "Released unsigned candidate", intent=row["intent"]
                    )
                metadata = json.loads(entry["metadata"])
                if (
                    entry["status"] == "prepared"
                    and self.execute
                    and (not self.cancelling() or metadata.get("action") == "cancel")
                    and metadata.get("mode") == "MVP preprod strategy execution"
                ):
                    from .trading import market_fingerprint

                    if metadata.get("market_fingerprint") != market_fingerprint(m):
                        return decision(
                            "paused",
                            "Prepared candidate belongs to different limits; await expiry/reconciliation",
                        )
                    self.coordinator.submit(row["intent"])
                    return decision(
                        "submitted",
                        "Submitted original prepared bytes",
                        intent=row["intent"],
                        txid=entry["txid"],
                    )
            return decision(
                "pending",
                "Original transactions require reconciliation",
                pending=[dict(r) for r in pending],
            )
        if projection["pending"]:
            return decision(
                "pending",
                "Order activity has not reached confirmation depth",
                pending=projection["pending"],
            )
        if self.cancelling() and not live:
            j.db.execute(
                "UPDATE engine_control SET cancel_requested=0 WHERE singleton=1"
            )
            return decision(
                "cancelled", "All selected-wallet orders are confirmed closed"
            )
        tip = p.tip()
        if not 0 <= p.clock() - tip["block_time"] <= m.settings.max_age_seconds:
            return decision("paused", "Provider tip is stale or future-dated")
        for order in live:
            decoded = decode_swaps(order["row"], p.profile)
            if {decoded.offer.unit, decoded.ask.unit} != {m.base.unit, "lovelace"}:
                return decision(
                    "paused",
                    "Another pair at this stake credential requires a separate operator configuration",
                )
        wallet_rows, collateral, protected_rows = wallet_inventory(
            p, j, self.owner, tip, m.execution
        )
        free, committed, protected = Counter(), Counter(), Counter()
        for row in protected_rows:
            protected.update(row_assets(row, p.profile.name))
        for row in wallet_rows:
            free.update(row_assets(row, p.profile.name))
        for order in live:
            committed.update(
                {
                    u: q
                    for u, q in row_assets(order["row"], p.profile.name).items()
                    if u in (m.base.unit, "lovelace")
                }
            )
        total_base = free[m.base.unit] + committed[m.base.unit] + protected[m.base.unit]
        deposits = sum(o["carrier_lovelace"] or 0 for o in live)
        result["inventory"] = {
            "free_base": free[m.base.unit],
            "free_lovelace": free["lovelace"],
            "committed_base": committed[m.base.unit],
            "committed_lovelace": committed["lovelace"],
            "known_minimum_ada_deposits": deposits,
            "unknown_deposit_orders": sum(o["carrier_lovelace"] is None for o in live),
            "collateral_lovelace": m.execution.collateral_lovelace,
            "operating_input_cap_lovelace": m.execution.max_funding_lovelace,
            "protected_assets": dict(protected),
        }
        fees = sum(
            result["costs"][k]
            for k in (
                "ledger_fees_paid_lovelace",
                "pending_fee_reserve_lovelace",
                "venue_fees_paid_lovelace",
            )
        )
        over_budget = (
            fees + m.execution.max_fee_lovelace > m.execution.max_total_fees_lovelace
        )
        action, reason, kwargs = None, None, {}
        observation = None
        if self.cancelling() or over_budget or total_base > m.settings.max_base:
            if live:
                action, reason, kwargs = (
                    "cancel",
                    "Close positions before pausing for cancellation or risk limits",
                    {"order_rows": [o["row"] for o in live]},
                )
            else:
                return decision(
                    "paused", "Fee budget or inventory exposure limit reached"
                )
        elif (
            live
            and not route_only
            and any(
                o["fills"]
                or p.clock() - o["created_at"] >= m.settings.max_order_age_seconds
                for o in live
            )
        ):
            action, reason, kwargs = (
                "cancel",
                "Staged reprice: close filled or aged orders before requesting fresh quotes",
                {"order_rows": [o["row"] for o in live]},
            )
        else:
            imbalance = total_base - m.settings.target_base
            rebalance = abs(imbalance) >= m.execution.rebalance_threshold_base > 0
            if rebalance and live and not route_only:
                action, reason, kwargs = (
                    "cancel",
                    "Release committed inventory before bounded rebalancing",
                    {"order_rows": [o["row"] for o in live]},
                )
            else:
                observation = observe_pool(p, m)
                if route_only:
                    candidates = p.credential_utxos(
                        DEPLOYMENTS["swaps-v1"]["script_hash"]
                    )
                    for row in candidates.rows:
                        try:
                            order = decode_swaps(row, p.profile)
                        except KernelError:
                            continue
                        if (
                            order.address == str(self.order_address)
                            or order.offer != m.base
                            or order.ask.unit != "lovelace"
                            or row.get("block_height", tip["block_no"])
                            > tip["block_no"] - p.profile.confirmations + 1
                        ):
                            continue
                        quantity = min(
                            order.held_offer,
                            m.settings.order_size,
                            m.execution.max_rebalance_base,
                        )
                        if quantity < observation.pool._datum.min_y_change:
                            continue
                        x, _ = observation.pool.compute_pool_change(
                            -quantity, observation.reward
                        )
                        minimum = (-x * (10000 - m.execution.slippage_bps)) // 10000
                        payment = ceil_fraction(quantity * order.price)
                        if (
                            payment <= m.execution.max_trade_lovelace
                            and minimum
                            - payment
                            - observation.fixed_fee
                            - m.execution.max_fee_lovelace
                            >= m.execution.min_route_gain_lovelace
                        ):
                            action, reason, kwargs = (
                                "route",
                                "One atomic Kernel-fill/Dano-swap meets configured net limits",
                                {"quantity": quantity, "route_row": row},
                            )
                            break
                    if action is None:
                        return decision(
                            "hold",
                            "No confirmed atomic route meets net gain and cost limits",
                        )
                elif rebalance:
                    quantity = min(abs(imbalance), m.execution.max_rebalance_base)
                    action = "rebalance-sell" if imbalance > 0 else "rebalance-buy"
                    if (
                        action == "rebalance-buy"
                        and total_base + quantity > m.settings.max_base
                    ):
                        return decision(
                            "paused", "Rebalance would exceed maximum inventory"
                        )
                    reason, kwargs = (
                        "Inventory crossed configured rebalance threshold",
                        {"quantity": quantity},
                    )
                else:
                    sell, buy = observation.quotes(m)
                    virtual = Inventory(
                        free[m.base.unit] + committed[m.base.unit],
                        free["lovelace"] + committed["lovelace"],
                        committed_base=protected[m.base.unit],
                    )
                    plan = self.strategy.decide(sell, buy, virtual, p.clock())
                    result.update(
                        sell_quote=asdict(sell),
                        buy_quote=asdict(buy),
                        proposals=[asdict(x) for x in plan.proposals],
                    )
                    if not plan.proposals:
                        return decision("hold", plan.reason)
                    if live:
                        by_offer = {x.offer.unit: x for x in plan.proposals}
                        reprice = len(live) != 2 or any(
                            o["fills"]
                            or self.strategy.should_reprice(
                                decode_swaps(o["row"], p.profile).price,
                                by_offer[
                                    decode_swaps(o["row"], p.profile).offer.unit
                                ].ask_per_offer,
                                o["created_at"],
                                p.clock(),
                            )
                            for o in live
                        )
                        if not reprice:
                            return decision(
                                "hold", "Both orders remain within age and price limits"
                            )
                        action, reason, kwargs = (
                            "cancel",
                            "Staged reprice: confirm cancellation before publishing fresh prices",
                            {"order_rows": [o["row"] for o in live]},
                        )
                    else:
                        action, reason, kwargs = (
                            "publish",
                            "Publish independently funded bid and ask",
                            {"proposals": plan.proposals},
                        )
        if not self.execute:
            return decision(
                action,
                reason,
                submission_enabled=False,
                quantity=kwargs.get("quantity"),
                order_inputs=[
                    f"{r['tx_hash']}#{r['tx_index']}"
                    for r in kwargs.get("order_rows", [])
                ],
            )
        if (
            action != "cancel"
            and fees
            + m.execution.max_fee_lovelace
            + (observation.fixed_fee if action != "publish" else 0)
            > m.execution.max_total_fees_lovelace
        ):
            return decision(
                "paused",
                "Transaction and venue fees would exceed the lifetime fee budget",
            )
        if j.stop_requested():
            return decision("stopped", "Stop requested before preparing new work")
        intent = f"mvp-{action}-{uuid4().hex}"
        prepared = prepare_action(
            p,
            j,
            m,
            self.wallet,
            self.key_dir,
            intent,
            action,
            wallet_rows,
            collateral,
            protected_base=protected[m.base.unit],
            observation=observation,
            **kwargs,
        )
        if j.stop_requested():
            return decision(
                "prepared",
                "Stopped before submission; original signed candidate retained",
                **{k: v for k, v in prepared.items() if k != "action"},
            )
        self.coordinator.submit(intent)
        return decision(
            action,
            reason,
            **{k: v for k, v in prepared.items() if k != "action"},
            status="submitted",
        )

    def run(
        self,
        *,
        interval=30,
        iterations=None,
        cancel_only=False,
        route_only=False,
        emit=lambda result: None,
    ):
        from .runtime import poll

        return poll(
            self,
            tick=lambda: self.tick(route_only=route_only),
            interval=interval,
            iterations=iterations,
            emit=emit,
            finished=lambda result: cancel_only and result.get("action") == "cancelled",
        )
