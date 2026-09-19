"""Independent venue fee checks against built fills and confirmed Preprod evidence."""

import json
from collections import Counter
from pathlib import Path

import cbor2
import pytest
from charli3_dendrite.dexs.amm.dano import DanoPoolDatum
from charli3_dendrite.dexs.amm.splash import SplashCPPPoolDatum
from charli3_dendrite.dexs.ob.geniusyield import GeniusYieldOrder, GeniusYieldSettings
from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapSwapDatumV3
from pycardano import Address, Transaction
from test_venues import EVIDENCE

from defi_kernel.domain import KernelError
from defi_kernel.protocols import (
    DANO_CONFIG,
    SwapsTwoWayDatum,
    SwapsV1Datum,
    checked_datum,
    row_assets,
)
from defi_kernel.signing import value_units
from defi_kernel.venues import GENIUS_CONFIG_TOKEN, SATURN_FEE_ADDRESS


def venue_fees(tx, metadata, dependencies):
    rows = {f"{r['tx_hash']}#{r['tx_index']}": r for r in dependencies}
    outputs = tx.transaction_body.outputs
    result = []
    for hop in metadata.get("hops", []):
        row = rows[hop["ref"]]
        venue, incoming, outgoing = hop["venue"], hop["amount_in"], hop["amount_out"]
        unit_in, unit_out = hop["input_unit"], hop["output_unit"]
        same_address = [
            o for o in outputs if str(o.address) == row["address"] and o.datum
        ]
        if venue in ("swaps-v1", "swaps-v1-two-way"):
            cls = SwapsV1Datum if venue == "swaps-v1" else SwapsTwoWayDatum
            d = cls.from_cbor(checked_datum(row, 10 if venue == "swaps-v1" else 11))
            if venue == "swaps-v1":
                price = d.swap_price
                beacon = d.beacon_id.hex() + d.pair_beacon.hex()
            else:
                a = d.asset1_id.hex() + d.asset1_name.hex() or "lovelace"
                price = d.asset1_price if unit_out == a else d.asset2_price
                beacon = d.beacon_id.hex() + d.pair_beacon.hex()
            candidates = [
                o
                for o in same_address
                if value_units(o.amount).get(beacon) == 1
                and getattr(
                    cls.from_cbor(o.datum.to_cbor()).prev_input, "to_cbor", lambda: b""
                )()
                != d.prev_input.to_cbor()
            ]
            before = row_assets(row, "preprod")
            exact = (
                outgoing * price.numerator + price.denominator - 1
            ) // price.denominator
            if incoming != exact or not any(
                value_units(o.amount).get(unit_out, 0)
                == before.get(unit_out, 0) - outgoing
                and value_units(o.amount).get(unit_in, 0)
                == before.get(unit_in, 0) + incoming
                for o in candidates
            ):
                raise KernelError("Canonical Swaps price/value mismatch")
        elif venue == "splash":
            d = SplashCPPPoolDatum.from_cbor(checked_datum(row, 11))
            nft = d.pool_nft.assets.unit()
            continuation = [
                o for o in same_address if value_units(o.amount).get(nft) == 1
            ]
            if len(continuation) != 1:
                raise KernelError("Splash continuation identity mismatch")
            after = SplashCPPPoolDatum.from_cbor(continuation[0].datum.to_cbor())
            x = unit_in == d.asset_x.assets.unit()
            amount = incoming * d.treasury_fee // 100000
            if (after.treasury_x - d.treasury_x, after.treasury_y - d.treasury_y) != (
                (amount, 0) if x else (0, amount)
            ):
                raise KernelError("Splash treasury fee mismatch")
            reserves = row_assets(row, "preprod")
            rx = reserves[unit_in] - (d.treasury_x if x else d.treasury_y)
            ry = reserves[unit_out] - (d.treasury_y if x else d.treasury_x)
            adjusted = incoming * (d.pool_fee - d.treasury_fee)
            if outgoing != ry * adjusted // (rx * 100000 + adjusted):
                raise KernelError("Splash integer output mismatch")
        elif venue == "dano":
            d = DanoPoolDatum.from_cbor(
                bytes.fromhex((row["inline_datum"] or {})["bytes"])
            )
            config = rows[str(DANO_CONFIG["preprod"])]
            rate, fixed = cbor2.loads(bytes.fromhex(checked_datum(config, 2))).value
            nft = next(
                (
                    u
                    for u, q in row_assets(row, "preprod").items()
                    if q == 1 and u not in (unit_in, unit_out, "lovelace")
                ),
                None,
            )
            continuation = [
                o for o in same_address if nft and value_units(o.amount).get(nft) == 1
            ]
            if len(continuation) != 1:
                raise KernelError("Dano continuation identity mismatch")
            after = DanoPoolDatum.from_cbor(continuation[0].datum.to_cbor())
            lp_fee = incoming * d.lp_fee_rate // 10000
            platform = lp_fee * rate // 10000
            expected = (platform, 0) if unit_in == d.unit_x else (0, platform)
            if (
                after.platform_fee_x - d.platform_fee_x,
                after.platform_fee_y - d.platform_fee_y,
            ) != expected or after.total_swap_fee - d.total_swap_fee != fixed:
                raise KernelError("Dano fee accrual mismatch")
        elif venue == "saturnswap":
            d = SaturnSwapSwapDatumV3.from_cbor(checked_datum(row, 11))
            scale = 10**12
            ratio = (incoming * scale + d.amount_buy - 1) // d.amount_buy
            released = (d.amount_sell * ratio + scale - 1) // scale
            fee = released // 100
            fee_outputs = [
                o for o in outputs if str(o.address) == SATURN_FEE_ADDRESS and o.datum
            ]
            from charli3_dendrite.dexs.ob.saturnswap import SaturnSwapPaymentDatumV3

            matched = []
            for o in fee_outputs:
                payment = SaturnSwapPaymentDatumV3.from_cbor(o.datum.to_cbor())
                ref = payment.output_reference
                if (ref.tx_id.hex(), ref.index) == (row["tx_hash"], row["tx_index"]):
                    matched.append(o)
            if (
                len(matched) != 1
                or value_units(matched[0].amount).get(unit_out, 0) < fee
                or (
                    unit_out != "lovelace"
                    and value_units(matched[0].amount).get(unit_out, 0) != fee
                )
            ):
                raise KernelError("Saturn per-order fee output mismatch")
        elif venue == "genius-yield":
            d = GeniusYieldOrder.from_cbor(checked_datum(row, 15))
            config = next(
                r
                for r in rows.values()
                if row_assets(r, "preprod").get(GENIUS_CONFIG_TOKEN) == 1
            )
            settings = GeniusYieldSettings.from_cbor(checked_datum(config, 8))
            address = settings.fee_address.to_address()
            fee_address = Address(
                address.payment_part,
                address.staking_part,
                Address.from_primitive(row["address"]).network,
            )
            matched = [o for o in outputs if o.address == fee_address]
            if (
                not matched
                or sum(o.amount.coin for o in matched) < d.taker_lovelace_fee
            ):
                raise KernelError("Genius taker fee missing")
            contained = Counter({"lovelace": d.contained_fee.lovelaces})
            contained[unit_out] += d.contained_fee.offered_tokens
            contained[unit_in] += d.contained_fee.asked_tokens
            from charli3_dendrite.dexs.ob.geniusyield import (
                GeniusTxRef,
                GeniusUTxORef,
                GeniusYieldFeeDatum,
            )

            ref = GeniusUTxORef(
                GeniusTxRef(bytes.fromhex(row["tx_hash"])), row["tx_index"]
            )
            fee_datum = GeniusYieldFeeDatum.from_cbor(matched[0].datum.to_cbor())
            paid_map = fee_datum.fees.get(ref)
            expected_map = {}
            if outgoing == d.offered_amount:
                for u, q in contained.items():
                    if q:
                        policy, name = (
                            (b"", b"")
                            if u == "lovelace"
                            else (bytes.fromhex(u[:56]), bytes.fromhex(u[56:]))
                        )
                        expected_map.setdefault(policy, {})[name] = q
            if paid_map != expected_map:
                raise KernelError("Genius contained fee attribution mismatch")
        else:
            raise KernelError("Unqualified venue fee measurement")
        result.append(venue)
    return result


@pytest.mark.parametrize(
    "key,full",
    [
        ("swaps-two-way", False),
        ("splash-0", False),
        ("splash-1", False),
        ("genius", False),
        ("genius", True),
        ("saturn", False),
        ("saturn", True),
    ],
)
def test_independent_venue_fee_audit_on_constructed_fills(key, full):
    from dataclasses import asdict

    from test_venues import VENUES, make_direct_candidate

    from defi_kernel.venues import decode_direct

    tx, _, _, _, ctx, _ = make_direct_candidate(key, full=full)
    row = EVIDENCE["rows"][key][0]
    state = decode_direct(
        VENUES[key], row, ctx.profile, ctx, config_row=EVIDENCE["config"]
    )
    amount = 10**15 if full else 1_000_000
    if not full and key == "genius":
        amount = max(
            1,
            (state.datum.offered_amount // 3)
            * state.datum.price.numerator
            // state.datum.price.denominator,
        )
    if not full and key == "saturn":
        amount = max(state.datum.min_partial_fill, state.datum.amount_buy * 2 // 3)
    hop = state.edges()[0].quote(amount)
    meta = {"hops": [asdict(hop) | {"ref": str(hop.ref)}]}
    audited = venue_fees(tx, meta, [row, EVIDENCE["config"]])
    assert audited[0] == VENUES[key]


def test_fee_audit_on_existing_confirmed_dano_and_swaps_evidence():

    evidence = json.loads(Path("evidence/preprod-arbitrage-execution.json").read_text())
    for entry in evidence["transactions"]:
        audits = venue_fees(
            Transaction.from_cbor(entry["on_chain_cbor"]),
            entry["metadata"],
            entry["dependencies"],
        )
        assert len(audits) == len(entry["metadata"].get("hops", []))
