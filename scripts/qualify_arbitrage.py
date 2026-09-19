"""Bounded controlled Preprod liquidity, separate from the production strategy.

Fund once from the existing disposable wallet, publish only the two minted test
assets and 600,000 fUSDA, then close every maker order. Uses the runtime's final
inspection, evaluation, signer, journal and single-attempt submission controls.
"""

import argparse
import json
import logging
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.utility import asset_to_value
from pycardano import Address, ScriptHash, ScriptPubkey, TransactionOutput

from defi_kernel.chain_context import KoiosChainContext, to_utxo
from defi_kernel.config import load_profile
from defi_kernel.coordinator import Coordinator
from defi_kernel.domain import Asset, KernelError, OutRef
from defi_kernel.execution import prepare_transaction
from defi_kernel.journal import Journal
from defi_kernel.protocols import DEPLOYMENTS, decode_swaps, row_assets
from defi_kernel.providers import Koios
from defi_kernel.runtime import run_lock
from defi_kernel.transactions import CompositionBuilder, close_order, create_order
from defi_kernel.wallet import create_test_wallet, load_wallet

BASE = "9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5026655534441"
ROOT = Path("state")
PROFILE = load_profile(Path("examples/preprod-test.toml"))
MAKER = replace(PROFILE, wallet_id="arbitrage-maker")
MANIFEST = MAKER.state_path(ROOT).parent / "wallet/wallet.json"
LIMIT = 30_000_000  # Far below the authorized 100-tADA maker budget.


def prepare(action, intent, provider, journal, wallet, directory, maker):
    if journal.db.execute(
        "SELECT 1 FROM transactions WHERE status NOT IN ('confirmed','aborted','expired','conflicted','failed')"
    ).fetchone():
        raise KernelError("Reconcile existing candidates first")
    profile = provider.profile
    owner = Address.from_primitive(wallet["address"])
    context = KoiosChainContext(provider)
    builder = CompositionBuilder(context)
    builder.validity_start, builder.ttl = (
        context.last_block_slot - 60,
        context.last_block_slot + 300,
    )
    observed = provider.scan(
        "address_utxos", {"_addresses": [str(owner)], "_extended": True}
    )
    available = [
        r
        for r in observed.rows
        if r["address"] == str(owner)
        and r.get("is_spent") is False
        and not any(
            r.get(k) for k in ("datum_hash", "inline_datum", "reference_script")
        )
        and type(r.get("block_height")) is int
        and context._tip["block_no"] - r["block_height"] + 1 >= profile.confirmations
    ]
    dependencies = {}

    def remember(row):
        dependencies[OutRef(row["tx_hash"], row["tx_index"])] = row
        return to_utxo(row, profile)

    selected = [
        r
        for r in available
        if int(r["value"]) != 5_000_000 and int(r["value"]) <= LIMIT
    ]
    if sum(int(r["value"]) for r in selected) > LIMIT:
        raise KernelError("Controlled fixture input cap exceeded")
    for row in selected:
        builder.add_input(remember(row))
    if not selected:
        raise KernelError("No bounded operating inputs")
    deltas = []
    if action == "fund":
        if journal.db.execute(
            "SELECT 1 FROM outbox WHERE json_extract(metadata,'$.action')='fixture-fund'"
        ).fetchone():
            raise KernelError("Maker funding is one-time only")
        target = Address.from_primitive(maker["address"])
        # Leave the strategy enough ADA-only operating funds and its collateral.
        builder.add_output(
            TransactionOutput(
                target,
                asset_to_value(Assets(root={"lovelace": 15_000_000, BASE: 600_000})),
            )
        )
        builder.add_output(TransactionOutput(target, 5_000_000))
        deltas = [(BASE, -600_000, -600_000), ("lovelace", -21_500_000, -20_000_000)]
    else:
        collateral = next(
            (
                r
                for r in available
                if int(r["value"]) == 5_000_000 and not r.get("asset_list")
            ),
            None,
        )
        if collateral is None:
            raise KernelError("Dedicated maker collateral unavailable")
        builder.collaterals.append(remember(collateral))
        order_address = Address(
            ScriptHash(bytes.fromhex(DEPLOYMENTS["swaps-v1"]["script_hash"])),
            owner.staking_part,
            owner.network,
        )
        orders = provider.scan(
            "address_utxos", {"_addresses": [str(order_address)], "_extended": True}
        )
        beacon = remember(
            provider.reference_script(DEPLOYMENTS["swaps-v1"]["beacon_policy"])
        )
        policy = ScriptPubkey(owner.payment_part)
        a, b = (
            Asset(profile.name, bytes(policy.hash()), name)
            for name in (b"ArbA", b"ArbB")
        )
        base, ada = Asset.from_unit(profile.name, BASE), Asset(profile.name)
        if action == "publish":
            if (
                orders.rows
                or journal.db.execute(
                    "SELECT 1 FROM outbox WHERE json_extract(metadata,'$.action')='fixture-publish'"
                ).fetchone()
            ):
                raise KernelError("Fixture publication is one-time only")
            builder.native_scripts = [policy]
            minted = {a.unit: 300_000, b.unit: 600_000}
            builder.mint = asset_to_value(Assets(root=minted)).multi_asset
            for offer, ask, quantity, price in (
                (a, ada, 300_000, Fraction(1, 10)),
                (b, ada, 300_000, Fraction(1, 10)),
                (b, a, 300_000, Fraction(1)),
                (base, b, 600_000, Fraction(1)),
            ):
                create_order(
                    builder,
                    profile,
                    owner,
                    offer,
                    ask,
                    quantity,
                    price,
                    beacon_reference=beacon,
                )
            committed = sum(o.amount.coin for o in builder.outputs)
            deltas = [
                (BASE, -600_000, -600_000),
                ("lovelace", -committed - 1_500_000, -committed),
            ]
        elif action == "replace-empty":
            empty = [r for r in orders.rows if decode_swaps(r, profile).held_offer == 0]
            if len(empty) != 1:
                raise KernelError("Expected exactly one depleted fixture order")
            spend = remember(
                provider.reference_script(DEPLOYMENTS["swaps-v1"]["script_hash"])
            )
            returned = close_order(
                builder,
                profile,
                empty[0],
                owner,
                swap_reference=spend,
                beacon_reference=beacon,
            )
            remember(empty[0])
            if returned.amount.multi_asset:
                raise KernelError("Depleted fixture order must return only ADA")
            builder.outputs.remove(returned)
            output = create_order(
                builder,
                profile,
                owner,
                ada,
                base,
                1_000_000,
                Fraction(3, 10),
                beacon_reference=beacon,
            )
            net = returned.amount.coin - output.amount.coin
            deltas = [("lovelace", net - 1_500_000, net)]
        elif action == "close":
            if not orders.rows:
                raise KernelError("No fixture orders remain")
            spend = remember(
                provider.reference_script(DEPLOYMENTS["swaps-v1"]["script_hash"])
            )
            returned = {}
            for row in orders.rows:
                order = decode_swaps(row, profile)
                if not {order.offer, order.ask} <= {a, b, base, ada}:
                    raise KernelError("Unexpected maker order")
                close_order(
                    builder,
                    profile,
                    row,
                    owner,
                    swap_reference=spend,
                    beacon_reference=beacon,
                )
                remember(row)
                for unit, quantity in row_assets(row, profile.name).items():
                    if not unit.startswith(DEPLOYMENTS["swaps-v1"]["beacon_policy"]):
                        returned[unit] = returned.get(unit, 0) + quantity
            # Aggregate reclaimed deposits into inspected wallet change.
            builder.outputs.clear()
            deltas = [
                (unit, q - 1_500_000 if unit == "lovelace" else q, q)
                for unit, q in returned.items()
            ]
        else:
            raise KernelError("Unknown fixture action")
    journal.bind_watch_wallet(str(owner))
    return prepare_transaction(
        provider,
        journal,
        builder,
        owner,
        wallet,
        directory,
        intent,
        deltas,
        {
            "mode": "controlled arbitrage qualification; maker transfers are not organic profit",
            "action": "fixture-" + action,
            "asset_delta_limits": deltas,
            "venue_fee_lovelace": 0,
        },
        list(dependencies.values()),
        max_fee=1_500_000,
        max_collateral=5_000_000,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["wallet", "fund", "publish", "replace-empty", "close"]
    )
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    logging.getLogger("PyCardano").setLevel(logging.ERROR)
    if args.action == "wallet":
        if not MANIFEST.exists():
            create_test_wallet(MAKER, ROOT)
        maker, _ = load_wallet(MAKER, MANIFEST)
        policy = ScriptPubkey(Address.from_primitive(maker["address"]).payment_part)
        config = {
            "chain_id": MAKER.chain_id,
            "network": "preprod",
            "assets": [
                "lovelace",
                BASE,
                *(str(policy.hash()) + n.hex() for n in (b"ArbA", b"ArbB")),
            ],
            "max_hops": 4,
        }
        (ROOT / "preprod-arbitrage-controlled.json").write_text(
            json.dumps(config, indent=2) + "\n"
        )
        (ROOT / "preprod-maker.toml").write_text(
            Path("examples/preprod-test.toml")
            .read_text()
            .replace('wallet_id = "mvp-test"', 'wallet_id = "arbitrage-maker"')
        )
        print(
            json.dumps(
                {
                    "manifest": str(MANIFEST),
                    "address": maker["address"],
                    "strategy": str(ROOT / "preprod-arbitrage-controlled.json"),
                }
            )
        )
        return
    if not args.submit:
        raise KernelError("Fixture signing requires explicit --submit")
    maker, _ = load_wallet(MAKER, MANIFEST)
    profile = PROFILE if args.action == "fund" else MAKER
    path = profile.state_path(ROOT).parent / "wallet/wallet.json"
    wallet, directory = load_wallet(profile, path)
    intent = "arbitrage-fixture-" + args.action
    with run_lock(ROOT, profile):
        provider = Koios(profile, enable_testnet_submission=True)
        journal = Journal(profile.state_path(ROOT), profile)
        try:
            if journal.db.execute(
                "SELECT 1 FROM outbox WHERE intent=?", (intent,)
            ).fetchone():
                entry = journal.outbox_entry(intent)
                print(
                    json.dumps(
                        {
                            "intent": intent,
                            "status": entry["status"],
                            "txid": entry["txid"],
                        }
                    )
                )
                return  # Never implicitly resubmit; reconcile the existing intent.
            result = prepare(
                args.action, intent, provider, journal, wallet, directory, maker
            )
            Coordinator(provider, journal).submit(intent)
            print(json.dumps(result | {"status": "submitted"}))
        finally:
            provider.close()
            journal.close()


if __name__ == "__main__":
    main()
