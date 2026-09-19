"""Body-derived loss reservations and canonical arbitrage drawdown accounting."""

import json

from .domain import KernelError
from .signing import decode_candidate


def candidate_loss(transaction, dependencies):
    """Success and phase-2 failure are alternatives, never additive costs."""
    from .trading import collateral_loss

    body = transaction.transaction_body
    return collateral_loss(body, dependencies) if body.collateral else body.fee


def drawdown(journal, limit, *, exclude_intent=None):
    if type(limit) is not int or limit <= 0:
        raise KernelError("Drawdown limit must be a positive integer")
    net = peak = reserved = 0
    anchor = None
    for row in journal.db.execute(
        "SELECT t.intent,t.status,o.unsigned,o.dependencies,o.block_hash,o.block_height,"
        "a.net_lovelace FROM transactions t JOIN outbox o USING(intent) "
        "LEFT JOIN arbitrage_outcomes a ON a.intent=t.intent AND a.block_hash=o.block_hash "
        "WHERE json_extract(o.metadata,'$.mode')='atomic arbitrage' "
        "ORDER BY o.block_height,o.rowid"
    ):
        if row["status"] in ("confirmed", "failed"):
            if row["net_lovelace"] is None or row["block_height"] is None:
                raise KernelError(
                    "Arbitrage result is unverified; loss accounting paused"
                )
            net += row["net_lovelace"]
            peak = max(peak, net)
            anchor = (row["block_height"], row["block_hash"])
        elif row["status"] not in ("aborted", "expired", "conflicted"):
            if row["intent"] != exclude_intent:
                reserved += candidate_loss(
                    decode_candidate(row["unsigned"]), json.loads(row["dependencies"])
                )
    return {
        "net_lovelace": net,
        "peak_net_lovelace": peak,
        "drawdown_lovelace": peak - net,
        "reserved_loss_lovelace": reserved,
        "loss_headroom_lovelace": limit - (peak - net) - reserved,
        "anchor": anchor,
    }


def check_candidate(journal, transaction, dependencies, metadata, *, intent=None):
    journal.require_no_execution_incident()
    if metadata.get("mode") != "atomic arbitrage":
        return None
    limit = metadata.get("max_drawdown_lovelace")
    if limit is None:
        raise KernelError(
            "Historical arbitrage candidate lacks drawdown authorization; "
            "reconcile its original bytes without submitting"
        )
    risk = drawdown(journal, limit, exclude_intent=intent)
    loss = candidate_loss(transaction, dependencies)
    if loss > risk["loss_headroom_lovelace"]:
        raise KernelError(
            f"Drawdown limit: candidate risks {loss / 1_000_000:.6f} tADA; "
            f"loss headroom {risk['loss_headroom_lovelace'] / 1_000_000:.6f} tADA"
        )
    return risk
