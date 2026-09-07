"""Bounded preprod integration sequence using the real runtime contributors.

This is an operator test flow, not the automatic market-making strategy. Every
payment returns to this disposable wallet or a validated protocol continuation.
"""

import json
from fractions import Fraction
from pathlib import Path

from pycardano import Address, ScriptHash, TransactionOutput

from .chain_context import KoiosChainContext, to_utxo
from .dendrite_bridge import DANO_REFERENCE, DanoSession, protocol_epoch
from .domain import Asset, KernelError, OutRef
from .protocols import (
    DANO_CONFIG,
    DEPLOYMENTS,
    dano_config,
    decode_dano,
    decode_swaps,
    row_assets,
)
from .transactions import CompositionBuilder, close_order, create_order, fill_order
from .wallet import load_wallet

MAX_FEE = 2_000_000
MAX_FILL_PAYMENT = 2_000_000
COLLATERAL = 5_000_000


def prepare_test_action(
    provider,
    journal,
    manifest_path,
    action,
    intent,
    *,
    fixture=Path("examples/preprod-market.json"),
):
    profile = provider.profile
    if profile.name != "preprod" or profile.address_network != "testnet":
        raise KernelError("This bounded integration flow only supports preprod")
    if action not in ("split", "buy-base", "create", "fill", "compose", "close"):
        raise KernelError("Unknown preprod integration action")
    if journal.db.execute(
        "SELECT 1 FROM transactions WHERE status NOT IN ('confirmed', 'aborted', 'expired', 'conflicted', 'failed')"
    ).fetchone():
        raise KernelError("Reconcile existing intents before preparing another action")
    wallet, key_dir = load_wallet(profile, manifest_path)
    owner = Address.from_primitive(wallet["address"])
    journal.bind_watch_wallet(wallet["address"])
    data = json.loads(fixture.read_text())
    if data["chain_id"] != profile.chain_id:
        raise KernelError("Test market chain identity mismatch")
    base, ada = Asset.from_unit(profile.name, data["base_unit"]), Asset(profile.name)
    observed = provider.scan(
        "address_utxos", {"_addresses": [wallet["address"]], "_extended": True}
    )
    reserved = {r[0] for r in journal.db.execute("SELECT ref FROM reservations")}
    available = [
        r
        for r in observed.rows
        if f"{r['tx_hash']}#{r['tx_index']}" not in reserved
        and not r.get("reference_script")
        and not r.get("datum_hash")
    ]
    if not available:
        raise KernelError(
            "No confirmed free wallet UTxOs; wait for faucet funding or reconciliation"
        )
    context = KoiosChainContext(provider)
    builder = CompositionBuilder(context)
    builder.validity_start, builder.ttl = (
        context.last_block_slot - 60,
        context.last_block_slot + 600,
    )
    dependencies = {}

    def remember(row):
        dependencies[OutRef(row["tx_hash"], row["tx_index"])] = row
        return to_utxo(row, profile)

    def confirmed(row):
        height = row.get("block_height")
        return (
            type(height) is int
            and int(context._tip["block_no"]) - height + 1 >= profile.confirmations
        )

    available = [r for r in available if confirmed(r)]
    if action != "split":
        candidates = [
            r
            for r in available
            if not r.get("asset_list") and int(r["value"]) == COLLATERAL
        ]
        if not candidates:
            raise KernelError(
                "Create the dedicated 5-tADA collateral output with the split action first"
            )
        collateral_row = candidates[0]
        builder.collaterals.append(remember(collateral_row))
        available.remove(collateral_row)
    if action == "create":
        available = [
            r
            for r in available
            if row_assets(r, profile.name).get(base.unit, 0) >= 250_000
        ]
    min_funding = 40_000_000 if action == "split" else 8_000_000
    available = [r for r in available if int(r["value"]) >= min_funding]
    if not available:
        raise KernelError("Insufficient confirmed funding for this bounded test action")
    funding = min(available, key=lambda r: int(r["value"]))
    builder.add_input(remember(funding))
    limits = [("lovelace", -MAX_FEE, 0)]
    order_rows = []
    order_address = Address(
        ScriptHash(bytes.fromhex(DEPLOYMENTS["swaps-v1"]["script_hash"])),
        owner.staking_part,
        owner.network,
    )

    if action in ("create", "fill", "compose", "close"):
        observation = provider.scan(
            "address_utxos", {"_addresses": [str(order_address)], "_extended": True}
        )
        for row in observation.rows:
            decoded = decode_swaps(row, profile)
            if {decoded.offer, decoded.ask} != {base, ada}:
                raise KernelError(
                    "Unexpected pair at the disposable wallet's personal order address"
                )
            order_rows.append((row, decoded))
        if action == "create" and order_rows:
            raise KernelError(
                "Existing orders must be closed before creating the test pair"
            )

    if action == "split":
        builder.add_output(TransactionOutput(owner, COLLATERAL))
        builder.add_output(TransactionOutput(owner, 30_000_000))
    elif action == "create":
        beacon = remember(
            provider.reference_script(DEPLOYMENTS["swaps-v1"]["beacon_policy"])
        )
        create_order(
            builder,
            profile,
            owner,
            base,
            ada,
            250_000,
            Fraction(4),
            beacon_reference=beacon,
        )
        create_order(
            builder,
            profile,
            owner,
            ada,
            base,
            1_000_000,
            Fraction(1, 4),
            beacon_reference=beacon,
        )
        limits = [(base.unit, -250_000, -250_000), ("lovelace", -8_000_000, -1_000_000)]
    elif action in ("fill", "compose"):
        size = 50_000 if action == "fill" else 125_000
        choices = [
            (r, o) for r, o in order_rows if o.offer == base and o.held_offer >= size
        ]
        if len(choices) != 1:
            raise KernelError("Expected exactly one funded test ask order")
        row, order = choices[0]
        remember(row)
        reference = remember(
            provider.reference_script(DEPLOYMENTS["swaps-v1"]["script_hash"])
        )
        _, payment = fill_order(
            builder,
            profile,
            row,
            size,
            MAX_FILL_PAYMENT if action == "fill" else 4 * size,
            swap_reference=reference,
        )
        limits = [(base.unit, size, size), ("lovelace", -payment - MAX_FEE, -payment)]
    elif action == "close":
        if not order_rows:
            raise KernelError("No test orders to close")
        spend = remember(
            provider.reference_script(DEPLOYMENTS["swaps-v1"]["script_hash"])
        )
        beacon = remember(
            provider.reference_script(DEPLOYMENTS["swaps-v1"]["beacon_policy"])
        )
        returned_ada, returned_base = 0, 0
        for row, _ in order_rows:
            remember(row)
            values = row_assets(row, profile.name)
            returned_ada += values["lovelace"]
            returned_base += values.get(base.unit, 0)
            close_order(
                builder,
                profile,
                row,
                owner,
                swap_reference=spend,
                beacon_reference=beacon,
            )
        limits = [
            (base.unit, returned_base, returned_base),
            ("lovelace", returned_ada - MAX_FEE, returned_ada),
        ]

    if action in ("buy-base", "compose"):
        rate, fixed = dano_config(provider)
        original = decode_dano(data["pool"], profile, platform_fee_rate=rate)
        nft = original.pool_id
        observation = provider.scan(
            "asset_utxos", {"_asset_list": [[nft[:56], nft[56:]]], "_extended": True}
        )
        if len(observation.rows) != 1:
            raise KernelError("Dano validity NFT continuation is absent or ambiguous")
        pool_row = observation.rows[0]
        pool = decode_dano(pool_row, profile, platform_fee_rate=rate)
        remember(pool_row)
        rows = [
            pool_row,
            *provider.utxos([DANO_CONFIG[profile.name], DANO_REFERENCE[profile.name]]),
        ]
        reward = 0
        rewards = {}
        now = provider.clock()
        if protocol_epoch(profile, int(now * 1000)) > pool._datum.last_withdraw_epoch:
            address = Address.from_primitive(pool_row["address"])
            reward_address = str(
                Address(staking_part=address.staking_part, network=address.network)
            )
            reward = provider.stake_rewards(reward_address)
            rewards[reward_address] = reward
            rows.append(provider.reference_script(str(address.staking_part)))
        for row in rows:
            remember(row)
        if action == "buy-base":
            amount, min_out = 4_000_000, 900_000
            _, y = pool.compute_pool_change(amount, reward)
            limits = [
                (base.unit, min_out, -y),
                ("lovelace", -amount - fixed - MAX_FEE, -amount - fixed),
            ]
            input_unit, output_unit = "lovelace", base.unit
        else:
            amount, min_out = 125_000, 400_000
            x, _ = pool.compute_pool_change(-amount, reward)
            limits = [
                (base.unit, 0, 0),
                ("lovelace", min_out - payment - fixed - MAX_FEE, -x - payment - fixed),
            ]
            input_unit, output_unit = base.unit, "lovelace"
        DanoSession(profile, rows, now=now, rewards=rewards).contribute(
            builder,
            OutRef(pool_row["tx_hash"], pool_row["tx_index"]),
            input_unit,
            amount,
            output_unit,
            min_out,
        )

    metadata = {
        "action": action,
        "mode": "designated preprod integration test",
        "wallet": wallet["address"],
        "asset_delta_limits": limits,
        "order_inputs": [str(o.ref) for _, o in order_rows]
        if action == "close"
        else [],
        "unsigned_cbor_digest_note": "Public transaction bytes remain in the local journal",
    }
    from .execution import prepare_transaction

    result = prepare_transaction(
        provider,
        journal,
        builder,
        owner,
        wallet,
        key_dir,
        intent,
        limits,
        metadata,
        list(dependencies.values()),
        max_fee=MAX_FEE,
        max_collateral=COLLATERAL,
    )
    return {"transaction_id": result["txid"], **result, **metadata}
