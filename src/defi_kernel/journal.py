"""Durable transaction reservations and order accounting, isolated per chain/wallet.

This journal does not infer a fill from a missing UTxO or a submitted transaction.
Only evidence-bearing settlement events affect balances.
"""

import json
import os
import sqlite3
import stat
from pathlib import Path

from .config import Profile
from .domain import KernelError


class Journal:
    def __init__(self, path: Path, profile: Profile):
        self.profile = profile
        if path.parent.is_symlink():
            raise KernelError("Journal directory must not be a symlink")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_uid != os.getuid():
            raise KernelError("Journal directory must belong to the current user")
        path.parent.chmod(0o700)
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise KernelError("Journal must be an owned regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), chain TEXT, wallet TEXT);
            CREATE TABLE IF NOT EXISTS transactions (intent TEXT PRIMARY KEY, txid TEXT UNIQUE NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations (ref TEXT PRIMARY KEY, intent TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, status TEXT NOT NULL, ref TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS fills (event TEXT PRIMARY KEY, order_id TEXT NOT NULL, txid TEXT NOT NULL, block_hash TEXT NOT NULL, delta TEXT NOT NULL, reversed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS shadow (id INTEGER PRIMARY KEY, observed_at REAL, decision TEXT);
            CREATE TABLE IF NOT EXISTS run_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), stop_requested INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'stopped', heartbeat REAL);
            CREATE TABLE IF NOT EXISTS watch_wallet (singleton INTEGER PRIMARY KEY CHECK(singleton=1), address TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (intent TEXT PRIMARY KEY, unsigned BLOB NOT NULL, signed BLOB, dependencies TEXT NOT NULL, metadata TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, expires_slot INTEGER NOT NULL, block_hash TEXT, block_height INTEGER, confirmations INTEGER NOT NULL DEFAULT 0, last_error TEXT);
            CREATE TABLE IF NOT EXISTS chain_events (txid TEXT PRIMARY KEY, address TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS order_projection (address TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS engine_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), cancel_requested INTEGER NOT NULL DEFAULT 0);
        """)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO identity VALUES(1,?,?)",
                (profile.chain_id, profile.wallet_id),
            )
        row = self.db.execute("SELECT chain,wallet FROM identity").fetchone()
        if tuple(row) != (profile.chain_id, profile.wallet_id):
            self.db.close()
            raise KernelError(
                "Journal identity mismatch; refusing cross-network/wallet state"
            )

    def close(self):
        self.db.close()

    def replace_order_projection(self, address, events, projection):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("DELETE FROM chain_events WHERE address=?", (address,))
            self.db.executemany(
                "INSERT INTO chain_events VALUES(?,?,?)",
                [(e["txid"], address, json.dumps(e)) for e in events],
            )
            self.db.execute(
                "INSERT OR REPLACE INTO order_projection VALUES(?,?)",
                (address, json.dumps(projection)),
            )
            self.db.execute("DELETE FROM orders")
            self.db.executemany(
                "INSERT INTO orders VALUES(?,?,?)",
                [(o["id"], o["status"], o["ref"]) for o in projection["orders"]],
            )
            self.db.execute("UPDATE fills SET reversed=1")
            self.db.executemany(
                "INSERT INTO fills VALUES(?,?,?,?,?,0) ON CONFLICT(event) DO UPDATE SET block_hash=excluded.block_hash,delta=excluded.delta,reversed=0",
                [
                    (
                        f["event"],
                        f["order_id"],
                        f["txid"],
                        f["block_hash"],
                        json.dumps(f["delta"]),
                    )
                    for f in projection["fills"]
                ],
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def mark_transaction(self, intent: str, status: str):
        transitions = {
            "prepared": {"submitting"},
            "submitting": {"unknown", "submitted", "included"},
            "unknown": {"submitted", "included"},
            "submitted": {"unknown", "included"},
            "included": {"confirmed", "rolled_back"},
            "confirmed": {"rolled_back"},
            "rolled_back": {"unknown", "included"},
        }
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT status FROM transactions WHERE intent=?", (intent,)
            ).fetchone()
            if not row or (
                status != row[0] and status not in transitions.get(row[0], set())
            ):
                raise KernelError(
                    "Unsafe transaction transition; reconciliation evidence required"
                )
            self.db.execute(
                "UPDATE transactions SET status=? WHERE intent=?", (status, intent)
            )
            # Reservations are retained even after confirmation. Explicit spent-input
            # reconciliation must precede release; indexer absence is insufficient.
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def balances_delta(self):
        result = {}
        for row in self.db.execute("SELECT delta FROM fills WHERE reversed=0"):
            for asset, q in json.loads(row[0]).items():
                result[asset] = result.get(asset, 0) + q
        return result

    def status(self):
        control = self.db.execute(
            "SELECT stop_requested,state,heartbeat FROM run_control WHERE singleton=1"
        ).fetchone()
        latest = self.db.execute(
            "SELECT observed_at,decision FROM shadow ORDER BY id DESC LIMIT 1"
        ).fetchone()
        ledger = self.db.execute("SELECT payload FROM order_projection").fetchone()
        return {
            "run": dict(control) if control else {"state": "stopped"},
            "latest_shadow": {
                "observed_at": latest[0],
                "decision": json.loads(latest[1]),
            }
            if latest
            else None,
            "transactions": [
                dict(r) for r in self.db.execute("SELECT * FROM transactions")
            ],
            "reservations": [
                dict(r) for r in self.db.execute("SELECT * FROM reservations")
            ],
            "orders": [dict(r) for r in self.db.execute("SELECT * FROM orders")],
            "order_ledger": json.loads(ledger[0]) if ledger else None,
            "outbox": [
                dict(r)
                for r in self.db.execute(
                    "SELECT intent,attempts,expires_slot,block_hash,block_height,confirmations,last_error FROM outbox"
                )
            ],
            "settled_balance_delta": self.balances_delta(),
            "shadow_decisions": self.db.execute(
                "SELECT count(*) FROM shadow"
            ).fetchone()[0],
        }

    def record_shadow(self, observed_at, decision_json):
        self.db.execute(
            "INSERT INTO shadow(observed_at,decision) VALUES(?,?)",
            (observed_at, decision_json),
        )
        # ponytail: retain 1,000 diagnostics; trade/fill evidence is never pruned.
        self.db.execute(
            "DELETE FROM shadow WHERE id <= (SELECT max(id)-1000 FROM shadow)"
        )

    def bind_watch_wallet(self, address):
        self.db.execute("INSERT OR IGNORE INTO watch_wallet VALUES(1,?)", (address,))
        if (
            self.db.execute(
                "SELECT address FROM watch_wallet WHERE singleton=1"
            ).fetchone()[0]
            != address
        ):
            raise KernelError(
                "Wallet ID is already bound to a different address; select a distinct wallet ID"
            )

    def start_run(self, now):
        self.db.execute(
            "INSERT INTO run_control VALUES(1,0,'shadow',?) ON CONFLICT(singleton) DO UPDATE SET stop_requested=0,state='shadow',heartbeat=excluded.heartbeat",
            (now,),
        )

    def request_stop(self):
        self.db.execute("UPDATE run_control SET stop_requested=1 WHERE singleton=1")

    def stop_requested(self):
        row = self.db.execute(
            "SELECT stop_requested FROM run_control WHERE singleton=1"
        ).fetchone()
        return bool(row and row[0])

    def heartbeat(self, now, state="shadow"):
        self.db.execute(
            "UPDATE run_control SET heartbeat=?,state=? WHERE singleton=1", (now, state)
        )

    def prepare_candidate(self, intent, transaction, dependencies, metadata):
        """Atomically persist the exact candidate and reserve spend/collateral inputs."""
        from .signing import ref_text

        body = transaction.transaction_body
        if body.ttl is None:
            raise KernelError("Durable candidates require an expiry slot")
        refs = [ref_text(i) for i in [*body.inputs, *(body.collateral or [])]]
        if not refs or len(refs) != len(set(refs)):
            raise KernelError("Candidate input roles overlap or are empty")
        encoded = transaction.to_cbor()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute(
                "SELECT unsigned,metadata,status FROM outbox JOIN transactions USING(intent) WHERE intent=?",
                (intent,),
            ).fetchone()
            payload = json.dumps(metadata, sort_keys=True)
            if old:
                if old[0] != encoded or old[1] != payload or old[2] != "prepared":
                    raise KernelError(
                        "Intent already prepared; reconcile the original candidate"
                    )
            else:
                if self.db.execute(
                    "SELECT 1 FROM transactions WHERE status NOT IN ('confirmed','aborted','expired','conflicted','failed') LIMIT 1"
                ).fetchone():
                    raise KernelError(
                        "Reconcile pending work before preparing another transaction"
                    )
                self.db.execute(
                    "INSERT INTO transactions VALUES(?,?,?)",
                    (intent, str(body.id), "prepared"),
                )
                self.db.executemany(
                    "INSERT INTO reservations VALUES(?,?)", [(r, intent) for r in refs]
                )
                self.db.execute(
                    "INSERT INTO outbox(intent,unsigned,dependencies,metadata,expires_slot) VALUES(?,?,?,?,?)",
                    (intent, encoded, json.dumps(dependencies), payload, body.ttl),
                )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def attach_signature(self, intent, transaction):
        from copy import deepcopy

        candidate = deepcopy(transaction)
        candidate.transaction_witness_set.vkey_witnesses = None
        row = self.db.execute(
            "SELECT o.unsigned,o.signed,t.status FROM outbox o JOIN transactions t USING(intent) WHERE intent=?",
            (intent,),
        ).fetchone()
        if not row or row[2] != "prepared" or candidate.to_cbor() != row[0]:
            raise KernelError(
                "Signed candidate differs from the durable prepared transaction"
            )
        encoded = transaction.to_cbor()
        if row[1] is not None and row[1] != encoded:
            raise KernelError("A different signed transaction already exists")
        updated = self.db.execute(
            "UPDATE outbox SET signed=? WHERE intent=? AND attempts=0 AND EXISTS (SELECT 1 FROM transactions t WHERE t.intent=outbox.intent AND t.status='prepared') AND (signed IS NULL OR signed=?)",
            (encoded, intent, encoded),
        )
        if updated.rowcount != 1:
            raise KernelError("Candidate changed while attaching its signature")

    def abandon_unsigned(self, intent, reason):
        """Release only a candidate that has never been signed or submitted."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            entry = self.outbox_entry(intent)
            if (
                entry["status"] != "prepared"
                or entry["signed"] is not None
                or entry["attempts"] != 0
            ):
                raise KernelError(
                    "Only an unsigned, unattempted candidate can be abandoned"
                )
            self.db.execute(
                "UPDATE transactions SET status='aborted' WHERE intent=?", (intent,)
            )
            self.db.execute(
                "UPDATE outbox SET last_error=? WHERE intent=?", (reason, intent)
            )
            self.db.execute("DELETE FROM reservations WHERE intent=?", (intent,))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def claim_submission(self, intent):
        """One wire attempt per intent, including across concurrent/restarted processes."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.stop_requested():
                raise KernelError(
                    "Stop requested before submission; signed candidate retained"
                )
            row = self.db.execute(
                "SELECT o.signed,o.attempts,t.status,o.unsigned,t.txid FROM outbox o JOIN transactions t USING(intent) WHERE intent=?",
                (intent,),
            ).fetchone()
            if not row or not row[0] or row[1] or row[2] != "prepared":
                raise KernelError(
                    "Original transaction needs reconciliation; submission is not repeatable"
                )
            from pycardano import Transaction

            candidate = Transaction.from_cbor(row[0])
            if (
                not candidate.valid
                or not candidate.transaction_witness_set.vkey_witnesses
                or str(candidate.transaction_body.id) != row[4]
            ):
                raise KernelError(
                    "Stored signed candidate identity or witnesses are invalid"
                )
            candidate.transaction_witness_set.vkey_witnesses = None
            if candidate.to_cbor() != row[3]:
                raise KernelError(
                    "Stored signed bytes differ from the authorized candidate"
                )
            self.db.execute(
                "UPDATE outbox SET attempts=attempts+1 WHERE intent=?", (intent,)
            )
            self.db.execute(
                "UPDATE transactions SET status='submitting' WHERE intent=?", (intent,)
            )
            self.db.execute("COMMIT")
            return row[0]
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def outbox_entry(self, intent):
        row = self.db.execute(
            "SELECT o.*,t.txid,t.status FROM outbox o JOIN transactions t USING(intent) WHERE intent=?",
            (intent,),
        ).fetchone()
        if not row:
            raise KernelError("Unknown durable intent")
        return dict(row)

    def retire_candidate(self, intent, status, release, evidence):
        if status not in ("expired", "conflicted", "failed"):
            raise KernelError("Invalid terminal recovery status")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            entry = self.outbox_entry(intent)
            if entry["status"] in (
                "confirmed",
                "aborted",
                "expired",
                "conflicted",
                "failed",
            ):
                raise KernelError("Terminal candidate cannot be replaced by recovery")
            self.db.execute(
                "UPDATE transactions SET status=? WHERE intent=?", (status, intent)
            )
            self.db.execute(
                "UPDATE outbox SET last_error=? WHERE intent=?",
                (json.dumps(evidence), intent),
            )
            if status == "failed":
                from pycardano import Transaction

                from .signing import ref_text

                body = Transaction.from_cbor(entry["unsigned"]).transaction_body
                self.db.executemany(
                    "INSERT OR IGNORE INTO reservations VALUES(?,?)",
                    [(ref_text(i), intent) for i in body.collateral or []],
                )
                self.db.execute(
                    "UPDATE outbox SET block_hash=?,block_height=?,confirmations=? WHERE intent=?",
                    (
                        evidence["block_hash"],
                        evidence["block_height"],
                        evidence["confirmations"],
                        intent,
                    ),
                )
            self.db.executemany(
                "DELETE FROM reservations WHERE intent=? AND ref=?",
                [(intent, ref) for ref in release],
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def record_inclusion(self, intent, block_hash, height, confirmations, *, confirmed):
        """Commit chain evidence and release collateral only after confirmation.

        Spending reservations remain permanent tombstones: an old provider view
        cannot reintroduce a confirmed-spent input as new free funds.
        """
        from pycardano import Transaction

        from .signing import ref_text

        self.db.execute("BEGIN IMMEDIATE")
        try:
            entry = self.outbox_entry(intent)
            self.db.execute(
                "UPDATE transactions SET status=? WHERE intent=?",
                ("confirmed" if confirmed else "included", intent),
            )
            self.db.execute(
                "UPDATE outbox SET block_hash=?,block_height=?,confirmations=? WHERE intent=?",
                (block_hash, height, confirmations, intent),
            )
            if confirmed:
                body = Transaction.from_cbor(entry["unsigned"]).transaction_body
                self.db.executemany(
                    "INSERT OR IGNORE INTO reservations VALUES(?,?)",
                    [(ref_text(i), intent) for i in body.inputs],
                )
                self.db.executemany(
                    "DELETE FROM reservations WHERE ref=? AND intent=?",
                    [(ref_text(i), intent) for i in body.collateral or []],
                )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def record_transaction_rollback(self, intent):
        # Retain spending tombstones and quarantine the context before any new
        # plan; released collateral may already belong to a later transaction.
        self.db.execute(
            "UPDATE transactions SET status='rolled_back' WHERE intent=?", (intent,)
        )
        self.db.execute("UPDATE outbox SET confirmations=0 WHERE intent=?", (intent,))
