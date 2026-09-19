"""Durable, bounded execution of independently authorized transaction candidates."""

from .domain import KernelError
from .execution import validate_candidate
from .providers import ProviderLag, UnknownSubmission


class Coordinator:
    def __init__(self, provider, journal, *, before_submit=None):
        self.provider, self.journal = provider, journal
        self.before_submit = before_submit

    def prepare(
        self,
        intent,
        transaction,
        authorization,
        context,
        dependencies,
        signer,
        metadata,
    ):
        from .arbitrage_risk import check_candidate

        self.journal.require_no_execution_incident()
        if (
            context.profile != self.provider.profile
            or context.profile != self.journal.profile
        ):
            raise KernelError("Execution context/provider profile mismatch")
        if self.journal.db.execute(
            "SELECT 1 FROM transactions WHERE status='rolled_back'"
        ).fetchone():
            raise KernelError(
                "Wallet requires rollback reconciliation before new execution"
            )
        receipt, _, _ = validate_candidate(
            self.provider, transaction, authorization, context, dependencies
        )
        check_candidate(self.journal, transaction, dependencies, metadata)
        if (
            metadata.get("submit_before") is not None
            and self.provider.clock() >= metadata["submit_before"]
        ):
            raise KernelError("Candidate submission deadline elapsed before signing")
        from dataclasses import asdict

        self.journal.prepare_candidate(
            intent,
            transaction,
            dependencies,
            {**metadata, "final_evaluation": asdict(receipt)},
        )
        signed = signer.sign(transaction, authorization, context, dependencies, receipt)
        self.journal.attach_signature(intent, signed)
        return str(signed.transaction_body.id)

    def submit(self, intent):
        import json

        from .arbitrage_risk import check_candidate
        from .signing import decode_candidate

        entry = self.journal.outbox_entry(intent)
        metadata = json.loads(entry["metadata"])
        deadline = metadata.get("submit_before")
        risk = check_candidate(
            self.journal,
            decode_candidate(entry["unsigned"]),
            json.loads(entry["dependencies"]),
            metadata,
            intent=intent,
        )
        self.provider.verify_identity()
        tip = self.provider.tip()
        if risk and risk["anchor"]:
            # A canonical descendant commits its ancestry: one lookup validates
            # the settled accounting anchor without rereading every old trade.
            height, block_hash = risk["anchor"]
            canonical = self.provider.block_at_height(height)
            if (
                not canonical
                or canonical["hash"] != block_hash
                or int(tip["block_no"]) - height + 1
                < self.provider.profile.confirmations
            ):
                raise KernelError(
                    "Loss accounting anchor changed; reconcile before submission"
                )
        if int(tip["abs_slot"]) >= entry["expires_slot"]:
            raise KernelError(
                "Prepared transaction expired; do not submit or replace automatically"
            )
        self.provider.recheck_dependencies(json.loads(entry["dependencies"]))
        if deadline is not None and self.provider.clock() >= deadline:
            raise KernelError(
                "Submission deadline elapsed; retain original candidate for reconciliation"
            )
        # Last gate after read/evaluation diagnostics, before claiming a wire attempt.
        if self.before_submit is not None:
            self.before_submit(intent)
        check_candidate(
            self.journal,
            decode_candidate(entry["unsigned"]),
            json.loads(entry["dependencies"]),
            metadata,
            intent=intent,
        )
        cbor = self.journal.claim_submission(intent)
        try:
            result = self.provider.submit(
                cbor.hex(), **({"deadline": deadline} if deadline is not None else {})
            )
            if result != entry["txid"]:
                raise UnknownSubmission("Provider returned a different transaction ID")
        except Exception as error:
            self.journal.mark_transaction(intent, "unknown")
            self.journal.db.execute(
                "UPDATE outbox SET last_error=? WHERE intent=?", (str(error), intent)
            )
            raise
        self.journal.mark_transaction(intent, "submitted")
        return result

    def reconcile_all(self, *, tip):
        import json

        entries = list(
            self.journal.db.execute(
                "SELECT t.intent,t.status,o.block_height,o.last_error FROM transactions t "
                "JOIN outbox o USING(intent) WHERE t.status != 'aborted' ORDER BY t.rowid"
            )
        )
        terminal = {"confirmed", "failed", "expired", "conflicted"}
        for entry in entries:
            if entry["status"] not in terminal:
                self.reconcile(entry["intent"], tip=tip)
        heights = {
            json.loads(e["last_error"])["anchor_height"]
            if e["status"] in ("expired", "conflicted")
            else e["block_height"]
            for e in entries
            if e["status"] in terminal
        }
        blocks = self.provider.blocks_at_heights(heights) if heights else {}
        for entry in entries:
            if entry["status"] in terminal:
                self.reconcile(entry["intent"], tip=tip, canonical_blocks=blocks)

    def reconcile(self, intent, *, tip=None, canonical_blocks=None):
        p, j = self.provider, self.journal
        if tip is None:
            p.verify_identity()
            tip = p.tip()
        entry = j.outbox_entry(intent)
        if entry["status"] == "aborted":
            return entry["status"]
        if entry["status"] in ("expired", "conflicted"):
            import json

            evidence = json.loads(entry["last_error"])
            anchor = (
                p.block_at_height(evidence["anchor_height"])
                if canonical_blocks is None
                else canonical_blocks.get(evidence["anchor_height"])
            )
            if not anchor:
                raise KernelError("Recovery anchor unavailable; pause execution")
            if anchor["hash"] == evidence["anchor"]["hash"]:
                return entry["status"]
            j.record_transaction_rollback(intent)
            entry = j.outbox_entry(intent)
        # An unchanged canonical block preserves its transaction bodies. Check
        # the block every cycle instead of downloading the same CBOR repeatedly.
        if entry["status"] in ("confirmed", "failed"):
            canonical = (
                p.block_at_height(entry["block_height"])
                if canonical_blocks is None
                else canonical_blocks.get(entry["block_height"])
            )
            if not canonical:
                raise KernelError("Inclusion block unavailable; pause execution")
            if canonical["hash"] == entry["block_hash"]:
                confirmations = int(tip["block_no"]) - entry["block_height"] + 1
                if confirmations < p.profile.confirmations:
                    raise KernelError(
                        "Inclusion lost confirmation depth; pause execution"
                    )
                return entry["status"]
            j.record_transaction_rollback(intent)
            entry = j.outbox_entry(intent)
        info = p.transaction_info(entry["txid"])
        if info is None:
            # Absence alone is indexer lag/unknown. A conflicting canonical block
            # is positive rollback evidence, unlike a missing transaction lookup.
            if entry["block_height"] is not None:
                canonical = p.block_at_height(entry["block_height"])
                if canonical is not None and canonical["hash"] != entry["block_hash"]:
                    j.record_transaction_rollback(intent)
            elif entry["status"] in ("submitting", "submitted"):
                j.mark_transaction(intent, "unknown")
            current = j.outbox_entry(intent)
            if (
                current["status"] in ("prepared", "unknown", "submitted", "rolled_back")
                and int(tip["abs_slot"]) >= current["expires_slot"]
            ):
                self._retire_expired(intent, current, tip)
            return j.outbox_entry(intent)["status"]
        if type(info.get("valid_contract")) is not bool:
            raise KernelError(
                "Included transaction did not prove successful script validation; retain reservations"
            )
        height, block_hash = info.get("block_height"), info.get("block_hash")
        if type(height) is not int or not isinstance(block_hash, str):
            raise KernelError("Incomplete transaction inclusion evidence")
        # REST reads are not one snapshot: inclusion can arrive after the tip.
        # Refresh once, then defer if the provider still cannot prove any depth.
        if height > int(tip["block_no"]):
            tip = p.tip()
        canonical = p.block_at_height(height)
        if not canonical or canonical["hash"] != block_hash:
            raise KernelError(
                "Inclusion block is not verified canonical; resynchronize"
            )
        confirmations = int(tip["block_no"]) - height + 1
        if confirmations <= 0:
            raise ProviderLag(
                "Provider tip is behind transaction inclusion; waiting for consistent chain evidence"
            )
        if entry["block_hash"] and entry["block_hash"] != block_hash:
            j.record_transaction_rollback(intent)
        confirmed = confirmations >= p.profile.confirmations
        if not info["valid_contract"]:
            if not confirmed:
                return "included"
            if entry["status"] == "failed" and entry["block_hash"] == block_hash:
                return "failed"
            from pycardano import Transaction

            from .signing import ref_text

            tx = p.transaction_cbor(entry["txid"])
            if (
                tx.valid
                or tx.transaction_body.to_cbor()
                != Transaction.from_cbor(entry["unsigned"]).transaction_body.to_cbor()
            ):
                raise KernelError(
                    "Failed transaction evidence does not match candidate"
                )
            j.retire_candidate(
                intent,
                "failed",
                [ref_text(i) for i in tx.transaction_body.inputs],
                {
                    "reason": "phase-2 failure",
                    "block_hash": block_hash,
                    "block_height": height,
                    "confirmations": confirmations,
                },
            )
            return "failed"
        j.record_inclusion(
            intent, block_hash, height, confirmations, confirmed=confirmed
        )
        return "confirmed" if confirmed else "included"

    def _retire_expired(self, intent, entry, tip):
        """Expiry alone is insufficient: require mature chain and input evidence."""
        from pycardano import Transaction

        from .domain import OutRef
        from .signing import ref_text

        p = self.provider
        height = int(tip["block_no"]) - p.profile.confirmations + 1
        anchor = p.block_at_height(height)
        if not anchor or anchor.get("abs_slot", -1) < entry["expires_slot"]:
            return
        body = Transaction.from_cbor(entry["unsigned"]).transaction_body
        refs = [
            OutRef(str(i.transaction_id), i.index)
            for i in [*body.inputs, *(body.collateral or [])]
        ]
        states = p.input_states(refs)
        if set(states) != set(map(str, refs)) or any(
            type(row.get("is_spent")) is not bool for row in states.values()
        ):
            raise KernelError("Recovery requires explicit states for every input")
        spent = {ref for ref, row in states.items() if row["is_spent"]}
        proof = None
        if spent:
            # Positive conflicting-spend evidence is needed before treating a
            # timed-out action as replaced by someone else (e.g. a racing fill).
            for ref in spent & {ref_text(i) for i in body.inputs}:
                proof = p.confirmed_spender(ref, states[ref], exclude=entry["txid"])
                if proof:
                    break
            if not proof:
                return
        if p.transaction_info(entry["txid"]) is not None:
            return
        again = p.block_at_height(height)
        if not again or again["hash"] != anchor["hash"]:
            raise KernelError("Recovery anchor changed; retain reservations")
        self.journal.retire_candidate(
            intent,
            "conflicted" if spent else "expired",
            set(states) - spent,
            {
                "anchor": anchor,
                "anchor_height": height,
                "input_states": {r: v["is_spent"] for r, v in states.items()},
                "conflicting_spend": proof,
            },
        )
