"""Confirmed Swaps order lineage derived from canonical transaction bodies.

The projection is replaced atomically from replayable chain events. Neither a
missing UTxO nor an outbox status is treated as a fill. REST still has provider
trust and consistency limits; incomplete history pauses execution.
"""

import json
from collections import Counter
from dataclasses import replace

import cbor2
from pycardano import Address, Transaction, datum_hash

from .domain import KernelError, ceil_fraction
from .protocols import SwapsV1Datum, decode_swaps, row_assets
from .signing import ref_text, value_units


def output_row(txid, index, output, info):
    units = value_units(output.amount)
    return {
        "tx_hash": txid,
        "tx_index": index,
        "address": str(output.address),
        "value": str(output.amount.coin),
        "is_spent": False,
        "block_height": info["block_height"],
        "block_time": info.get("tx_timestamp", info.get("block_time", 0)),
        "datum_hash": str(datum_hash(output.datum))
        if output.datum is not None
        else None,
        "inline_datum": {"bytes": output.datum.to_cbor_hex()}
        if output.datum is not None
        else None,
        "reference_script": None,
        "asset_list": [
            {"policy_id": u[:56], "asset_name": u[56:], "quantity": str(q)}
            for u, q in units.items()
            if u != "lovelace"
        ],
    }


def spend_constructor(tx, index):
    redeemers = tx.transaction_witness_set.redeemer
    if not hasattr(redeemers, "items"):
        raise KernelError("Order settlement requires indexed redeemer evidence")
    for key, value in redeemers.items():
        if key.tag.name == "SPEND" and key.index == index:
            data = cbor2.loads(value.data.to_cbor())
            if (
                isinstance(data, cbor2.CBORTag)
                and 121 <= data.tag <= 127
                and not data.value
            ):
                return data.tag - 121
    raise KernelError("Order spend redeemer is unavailable or unsupported")


def project_history(events, profile, order_address, metadata=None):
    """Deterministic rebuild, including reversals when canonical events disappear."""
    metadata = metadata or {}
    roots, active, fills = {}, {}, []
    for event in sorted(
        events,
        key=lambda e: (
            e["info"]["block_height"],
            e["info"].get("tx_block_index", 0),
            e["txid"],
        ),
    ):
        tx = Transaction.from_cbor(event["cbor"])
        body, info = tx.transaction_body, event["info"]
        txid = str(body.id)
        if txid != event["txid"] or tx.valid != info["valid_contract"]:
            raise KernelError("Settlement bytes do not match inclusion evidence")
        if not tx.valid:
            continue  # Regular inputs/outputs were not applied by the ledger.
        outputs = []
        for index, output in enumerate(body.outputs):
            if str(output.address) != str(order_address):
                continue
            row = output_row(txid, index, output, info)
            try:
                order = decode_swaps(row, profile)
            except KernelError:
                # Reference outputs are not positions. Beacon-bearing malformed
                # positions must not disappear from inventory accounting.
                from .protocols import DEPLOYMENTS

                if any(
                    a["policy_id"] == DEPLOYMENTS["swaps-v1"]["beacon_policy"]
                    for a in row["asset_list"]
                ):
                    raise
                continue
            outputs.append((row, order))
        consumed = []
        for index, item in enumerate(body.inputs):
            ref = ref_text(item)
            if ref not in active:
                continue
            root_id = active.pop(ref)
            current = roots[root_id]
            old = decode_swaps(current["row"], profile)
            purpose = spend_constructor(tx, index)
            matches = [(r, o) for r, o in outputs if o.previous == old.ref]
            if purpose == 2:
                if len(matches) != 1:
                    raise KernelError("Fill has missing or ambiguous continuation")
                row, new = matches[0]
                before = SwapsV1Datum.from_cbor(old.datum_cbor)
                after = SwapsV1Datum.from_cbor(new.datum_cbor)
                if (
                    replace(after, prev_input=before.prev_input).to_cbor()
                    != before.to_cbor()
                    or new.address != old.address
                ):
                    raise KernelError("Fill changed immutable order terms")
                delta = Counter(row_assets(row, profile.name))
                delta.subtract(row_assets(current["row"], profile.name))
                taken, paid = -delta[old.offer.unit], delta[old.ask.unit]
                if (
                    taken <= 0
                    or paid < ceil_fraction(taken * old.price)
                    or any(
                        q
                        for u, q in delta.items()
                        if u not in (old.offer.unit, old.ask.unit)
                    )
                ):
                    raise KernelError(
                        "Fill settlement violates price or asset conservation"
                    )
                fill = {
                    "event": f"{txid}#{index}",
                    "order_id": root_id,
                    "txid": txid,
                    "block_hash": info["block_hash"],
                    "taken": taken,
                    "paid": paid,
                    "delta": {u: q for u, q in delta.items() if q},
                    "previous": ref,
                    "continuation": str(new.ref),
                }
                fills.append(fill)
                current.update(
                    row=row,
                    ref=str(new.ref),
                    status="partial",
                    fills=current["fills"] + 1,
                )
                if (
                    new.held_offer == 0
                    or new.offer.unit == "lovelace"
                    and current["carrier_lovelace"] is not None
                    and new.held_offer <= current["carrier_lovelace"]
                ):
                    current["status"] = "filled"
                active[str(new.ref)] = root_id
                consumed.append(str(new.ref))
            elif purpose == 0 and Address.from_primitive(old.address).staking_part in (
                body.required_signers or []
            ):
                if matches:
                    raise KernelError(
                        "Owner close unexpectedly declares a fill continuation"
                    )
                current.update(status="cancelled", live=False, closed_txid=txid)
            else:
                raise KernelError("Unsupported owner operation requires reconciliation")
        for row, order in outputs:
            ref = str(order.ref)
            if ref in consumed:
                continue
            if order.previous is not None:
                raise KernelError("Order history is incomplete: predecessor missing")
            planned = next(
                (
                    o
                    for o in metadata.get(txid, {}).get("order_outputs", [])
                    if o["index"] == order.ref.index
                ),
                None,
            )
            carrier = (
                int(row["value"])
                if order.offer.unit != "lovelace"
                else planned["carrier_lovelace"]
                if planned
                else None
            )
            roots[ref] = {
                "id": ref,
                "ref": ref,
                "status": "open",
                "live": True,
                "created_at": row["block_time"],
                "row": row,
                "fills": 0,
                "carrier_lovelace": carrier,
            }
            active[ref] = ref
    balances = Counter()
    for fill in fills:
        balances.update(fill["delta"])
    return {
        "orders": list(roots.values()),
        "fills": fills,
        "fill_asset_delta": dict(balances),
    }


class OrderObserver:
    def __init__(self, provider, journal, order_address):
        self.provider, self.journal, self.address = (
            provider,
            journal,
            str(order_address),
        )

    def sync(self):
        p, j = self.provider, self.journal
        history = p.scan("address_txs", {"_addresses": [self.address]})
        if not history.complete:
            raise KernelError("Incomplete order history; retain accounting")
        tip = history.tip_after
        blocks = {}

        def canonical(height):
            if height not in blocks:
                blocks[height] = p.block_at_height(height)
            return blocks[height]

        old = {
            r["txid"]: json.loads(r["payload"])
            for r in j.db.execute(
                "SELECT txid,payload FROM chain_events WHERE address=?", (self.address,)
            )
        }
        seen = {r["tx_hash"] for r in history.rows}
        for txid, event in old.items():
            block = canonical(event["info"]["block_height"])
            if txid not in seen and (
                not block or block["hash"] == event["info"]["block_hash"]
            ):
                raise KernelError(
                    "History temporarily omits a canonical transaction; retain accounting"
                )
        events, pending = [], []
        for row in history.rows:
            txid, height = row["tx_hash"], row["block_height"]
            block = canonical(height)
            if not block:
                raise KernelError("History block is not verified canonical")
            if tip["block_no"] - height + 1 < p.profile.confirmations:
                pending.append(txid)
                continue
            cached = old.get(txid)
            if cached and cached["info"]["block_hash"] == block["hash"]:
                event = cached
            else:
                info = p.transaction_info(txid)
                if (
                    not info
                    or info["block_height"] != height
                    or info["block_hash"] != block["hash"]
                ):
                    raise KernelError("History/inclusion inconsistency; resynchronize")
                tx = p.transaction_cbor(txid)
                event = {"txid": txid, "info": info, "cbor": tx.to_cbor_hex()}
            events.append(event)
        metadata = {
            r["txid"]: json.loads(r["metadata"])
            for r in j.db.execute(
                "SELECT txid,metadata FROM transactions JOIN outbox USING(intent)"
            )
        }
        projection = project_history(events, p.profile, self.address, metadata)
        observation = p.scan(
            "address_utxos", {"_addresses": [self.address], "_extended": True}
        )
        if not observation.complete:
            raise KernelError("Incomplete order UTxO observation; retain accounting")
        expected = {o["ref"]: o["row"] for o in projection["orders"] if o["live"]}
        actual = set()
        for row in observation.rows:
            if row.get("address") != self.address:
                raise KernelError("Order observation contains a foreign address")
            try:
                order = decode_swaps(row, p.profile)
            except KernelError:
                from .protocols import DEPLOYMENTS

                if any(
                    a["policy_id"] == DEPLOYMENTS["swaps-v1"]["beacon_policy"]
                    for a in row.get("asset_list") or []
                ):
                    raise
                continue
            ref = str(order.ref)
            if ref in actual:
                raise KernelError("Duplicate order output in observation")
            actual.add(ref)
            if ref in expected and (
                order != decode_swaps(expected[ref], p.profile)
                or Counter(row_assets(row, p.profile.name))
                != Counter(row_assets(expected[ref], p.profile.name))
            ):
                raise KernelError(
                    "Order value or terms disagree with confirmed history; retain accounting"
                )
        # A recent fill/create/cancel may explain the difference; pause until its
        # canonical evidence reaches the configured confirmation depth.
        if actual != set(expected) and not pending:
            raise KernelError(
                "Order UTxOs disagree with confirmed history; retain accounting"
            )
        after = p.tip()
        if after["block_no"] < tip["block_no"]:
            raise KernelError("Provider tip moved backwards during reconciliation")
        for height, block in blocks.items():
            again = p.block_at_height(height)
            if not again or again["hash"] != block["hash"]:
                raise KernelError("Chain changed during order reconciliation")
        projection.update(
            observed_at=p.clock(),
            pending=pending,
            address=self.address,
            tip=after,
            complete=True,
        )
        j.replace_order_projection(self.address, events, projection)
        return projection
