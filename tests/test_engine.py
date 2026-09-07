from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from pycardano import TransactionOutput, Value
from test_composition import EVIDENCE, OWNER, PROFILE, StructuralContext
from test_transactions import as_row

from defi_kernel.domain import Observation
from defi_kernel.engine import TradingEngine
from defi_kernel.journal import Journal
from defi_kernel.runtime import Market
from defi_kernel.settlement import output_row
from defi_kernel.strategy import Inventory, MarketMaker
from defi_kernel.trading import observe_pool
from defi_kernel.transactions import CompositionBuilder, create_order

MARKET = Market.load(Path("examples/preprod-mvp.json"), PROFILE)


class Provider:
    profile = PROFILE

    def __init__(self, base=600_000):
        self.now = EVIDENCE["observed_at"]
        self.rows = deepcopy(EVIDENCE["rows"])
        self.base = base

    def clock(self):
        return self.now

    def tip(self):
        return {**EVIDENCE["tip_after"], "block_time": self.now - 10}

    def verify_identity(self):
        return EVIDENCE["identity"]

    def utxos(self, refs):
        return [
            r
            for r in self.rows
            if any(r["tx_hash"] == x.tx_hash and r["tx_index"] == x.index for x in refs)
        ]

    def stake_rewards(self, address):
        return EVIDENCE["rewards"][address]

    def reference_script(self, script_hash):
        return next(
            r
            for r in self.rows
            if (r.get("reference_script") or {}).get("hash") == script_hash
        )

    def scan(self, endpoint, body):
        if endpoint == "asset_utxos":
            rows = [self.rows[0]]
        elif endpoint == "address_utxos":
            row = output_row(
                "a" * 64,
                0,
                TransactionOutput(OWNER, Value(30_000_000)),
                {"block_height": 1},
            )
            row["asset_list"] = [
                {
                    "policy_id": MARKET.base.policy.hex(),
                    "asset_name": MARKET.base.name.hex(),
                    "quantity": str(self.base),
                }
            ]
            collateral = output_row(
                "b" * 64,
                0,
                TransactionOutput(OWNER, Value(5_000_000)),
                {"block_height": 1},
            )
            rows = [row, collateral]
        else:
            raise AssertionError(endpoint)
        return Observation(
            "preprod",
            PROFILE.koios_url,
            self.now,
            tuple(rows),
            True,
            self.tip(),
            self.tip(),
        )


def engine(tmp_path, base=600_000):
    p = Provider(base)
    j = Journal(PROFILE.state_path(tmp_path), PROFILE)
    e = TradingEngine(p, j, MARKET, {"address": str(OWNER)}, tmp_path, clock=p.clock)
    projection = {"orders": [], "fills": [], "pending": []}
    e.reconcile = lambda: projection
    return e, p, j, projection


def test_shadow_proposes_both_sides_then_holds_and_stages_repricing(
    tmp_path, monkeypatch
):
    e, p, j, projection = engine(tmp_path)
    result = e.tick()
    assert result["action"] == "publish" and not result["submission_enabled"]
    assert len(result["proposals"]) == 2
    observation = observe_pool(p, MARKET)
    sell, buy = observation.quotes(MARKET)
    plan = MarketMaker(MARKET.settings).decide(
        sell, buy, Inventory(600_000, 30_000_000), p.clock()
    )
    builder = CompositionBuilder(StructuralContext(p.rows, p.clock()))
    for index, proposal in enumerate(plan.proposals):
        output = create_order(
            builder,
            PROFILE,
            OWNER,
            proposal.offer,
            proposal.ask,
            proposal.offer_quantity,
            proposal.ask_per_offer,
        )
        row = as_row(output)
        row["tx_index"] = index
        projection["orders"].append(
            {
                "id": f"root{index}",
                "ref": f"{row['tx_hash']}#{index}",
                "row": row,
                "live": True,
                "fills": 0,
                "created_at": p.clock(),
                "carrier_lovelace": 3_000_000,
            }
        )
    p.base = 400_000
    assert e.tick()["action"] == "hold"

    def unavailable(*args):
        raise AssertionError(
            "Aged/filled order cleanup must not depend on venue quotes"
        )

    monkeypatch.setattr("defi_kernel.engine.observe_pool", unavailable)
    p.now += MARKET.settings.max_order_age_seconds + 1
    assert e.tick()["action"] == "cancel"
    # A observed partial fill also forces reconciliation/repricing, even young.
    projection["orders"][0]["created_at"] = p.clock()
    projection["orders"][1]["created_at"] = p.clock()
    projection["orders"][1]["fills"] = 1
    assert e.tick()["action"] == "cancel"
    assert not j.status()["transactions"]  # Shadow never creates signed work.
    j.close()


def test_inventory_rebalance_stop_and_cancel_are_separate(tmp_path):
    e, p, j, projection = engine(tmp_path, 858_553)
    assert e.tick()["action"] == "rebalance-sell"
    p.base = 350_000
    assert e.tick()["action"] == "rebalance-buy"
    j.start_run(p.clock())
    j.request_stop()
    assert e.tick()["action"] == "stopped"
    assert not e.cancelling()
    j.start_run(p.clock())
    e.request_cancel()
    assert e.tick()["action"] == "cancelled"
    assert not e.cancelling()
    j.close()


def test_risk_limit_halts_publishing_and_wallet_reservations_exclude_funds(tmp_path):
    e, p, j, _ = engine(tmp_path)
    e.market = replace(
        MARKET, execution=replace(MARKET.execution, max_total_fees_lovelace=1)
    )
    assert e.tick()["action"] == "paused"
    e.market = MARKET
    j.db.execute(
        "INSERT INTO reservations VALUES(?,?)",
        ("a" * 64 + "#0", "external-reservation"),
    )
    result = e.tick()
    assert result["inventory"]["free_base"] == 0
    # The strategy can propose replenishment, but the bounded builder must still
    # obtain independently free ADA funding; it cannot use the reserved input.
    assert result["action"] == "rebalance-buy"
    j.close()


def test_stop_during_preparation_does_not_submit_new_work(tmp_path, monkeypatch):
    import defi_kernel.engine as module

    e, p, j, _ = engine(tmp_path)
    e.execute = True
    j.start_run(p.clock())

    def prepare(*args, **kwargs):
        j.request_stop()
        return {
            "intent": "prepared",
            "txid": "a" * 64,
            "action": "publish",
            "fee_lovelace": 300000,
        }

    monkeypatch.setattr(module, "prepare_action", prepare)
    e.coordinator.submit = lambda intent: (_ for _ in ()).throw(
        AssertionError("submitted after stop")
    )
    assert e.tick()["action"] == "prepared"
    j.close()


def test_changed_configuration_does_not_submit_previously_signed_plan(tmp_path):
    from pycardano import PaymentSigningKey, VerificationKeyWitness
    from test_composition import make_candidate

    from defi_kernel.trading import market_fingerprint

    e, p, j, _ = engine(tmp_path)
    tx, *_ = make_candidate()
    j.prepare_candidate(
        "old-plan",
        tx,
        [],
        {
            "mode": "MVP preprod strategy execution",
            "action": "publish",
            "market_fingerprint": market_fingerprint(MARKET),
        },
    )
    key = PaymentSigningKey.generate()
    tx.transaction_witness_set.vkey_witnesses = [
        VerificationKeyWitness(
            key.to_verification_key(), key.sign(tx.transaction_body.hash())
        )
    ]
    j.attach_signature("old-plan", tx)
    e.execute = True
    e.market = replace(
        MARKET, execution=replace(MARKET.execution, max_fee_lovelace=1000000)
    )
    e.coordinator.submit = lambda intent: (_ for _ in ()).throw(
        AssertionError("submitted with changed limits")
    )
    assert e.tick()["action"] == "paused"
    assert j.outbox_entry("old-plan")["attempts"] == 0
    assert j.status()["reservations"]
    j.close()


def test_atomic_route_discovery_excludes_own_orders_and_unprofitable_prices(tmp_path):
    from fractions import Fraction

    from pycardano import Address, VerificationKeyHash

    from defi_kernel.domain import Asset

    e, p, j, _ = engine(tmp_path)
    e.market = replace(
        MARKET,
        settings=replace(MARKET.settings, order_size=300000),
        execution=replace(MARKET.execution, max_fee_lovelace=1000000),
    )
    builder = CompositionBuilder(StructuralContext(p.rows, p.clock()))
    other = Address(
        OWNER.payment_part, VerificationKeyHash(b"\x08" * 28), OWNER.network
    )

    def candidate(owner, price):
        return {
            **as_row(
                create_order(
                    builder,
                    PROFILE,
                    owner,
                    MARKET.base,
                    Asset("preprod"),
                    300000,
                    price,
                )
            ),
            "block_height": 1,
        }

    rows = [candidate(OWNER, Fraction(1, 1000)), candidate(other, Fraction(100))]
    p.credential_utxos = lambda _: Observation(
        "preprod", PROFILE.koios_url, p.clock(), tuple(rows), True
    )
    assert e.tick(route_only=True)["action"] == "hold"
    rows.append(candidate(other, Fraction(1, 1000)))
    result = e.tick(route_only=True)
    assert result["action"] == "route" and result["quantity"] == 300000
    assert not j.status()["transactions"]
    j.close()


def test_custom_strategy_uses_same_observations_without_signing_access(tmp_path):
    from defi_kernel.strategy import Decision

    e, p, j, _ = engine(tmp_path)

    class Custom:
        def decide(self, sell, buy, inventory, now):
            assert sell.amount_in == MARKET.settings.order_size
            assert inventory.free_base == 600000 and now == p.clock()
            return Decision((), "custom policy holds")

    e.strategy = Custom()
    assert e.tick()["reason"] == "custom policy holds"
    j.close()


def test_cancel_race_waits_then_targets_confirmed_continuation(tmp_path):
    from pycardano import Transaction
    from test_coordinator import Provider as RecoveryProvider
    from test_coordinator import prepared
    from test_settlement import ADDRESS, EVENTS

    from defi_kernel.coordinator import Coordinator
    from defi_kernel.settlement import project_history
    from defi_kernel.signing import ref_text

    # A signed cancellation-like candidate becomes unknown while a competing
    # fill supplies a new head. It must not be replaced until evidence retires it.
    journal, tx, context = prepared(tmp_path)
    recovery = RecoveryProvider(context)
    journal.claim_submission("test")
    journal.mark_transaction("test", "unknown")
    recovery.slot = tx.transaction_body.ttl + 10
    recovery.canonical = {"hash": "a" * 64, "abs_slot": recovery.slot}
    spent = ref_text(tx.transaction_body.inputs[0])
    recovery.input_states = lambda refs: {
        str(r): {"is_spent": str(r) == spent} for r in refs
    }
    recovery.confirmed_spender = lambda *a, **kw: None
    p = Provider()
    e = TradingEngine(
        p, journal, MARKET, {"address": str(OWNER)}, tmp_path, clock=p.clock
    )
    projected = project_history(EVENTS[:4], PROFILE, ADDRESS)
    projected["pending"] = []
    coordinator = Coordinator(recovery, journal)

    def reconcile():
        coordinator.reconcile("test")
        return projected

    e.reconcile = reconcile
    e.request_cancel()
    assert e.tick()["action"] == "pending"
    recovery.confirmed_spender = lambda *a, **kw: {
        "txid": EVENTS[3]["txid"],
        "consumed": spent,
    }
    result = e.tick()
    assert result["action"] == "cancel"
    assert EVENTS[3]["txid"] + "#0" in result["order_inputs"]
    old_ref = ref_text(
        Transaction.from_cbor(EVENTS[3]["cbor"]).transaction_body.inputs[0]
    )
    assert old_ref not in result["order_inputs"]
    assert journal.outbox_entry("test")["attempts"] == 1
    assert len(projected["fills"]) == 1
    journal.close()


def test_protected_wallet_tokens_count_toward_exposure(tmp_path):
    e, p, j, _ = engine(tmp_path)
    original = p.scan

    def scan(endpoint, body):
        result = original(endpoint, body)
        if endpoint == "address_utxos":
            rich = deepcopy(result.rows[0])
            rich.update(tx_hash="c" * 64, value="1000000000")
            rich["asset_list"][0]["quantity"] = "1000000"
            return replace(result, rows=(*result.rows, rich))
        return result

    p.scan = scan
    result = e.tick()
    assert result["action"] == "paused"
    assert result["inventory"]["free_base"] == 600000
    assert result["inventory"]["protected_assets"][MARKET.base.unit] == 1000000
    j.close()
