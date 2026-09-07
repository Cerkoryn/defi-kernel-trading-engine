"""Hosted capabilities over one bounded Koios client; no acquired ledger state."""

import json
import os
import threading
import time

import httpx

from .config import Profile
from .domain import KernelError, Observation, OutRef, Unsupported


class ProviderError(KernelError):
    def __init__(self, message, *, rpc_error=None):
        super().__init__(message)
        self.rpc_error = rpc_error


class UnknownSubmission(ProviderError):
    """The original transaction must be reconciled; rebuilding is unsafe."""


class Koios:
    def __init__(
        self,
        profile: Profile,
        *,
        client=None,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
        enable_testnet_submission=False,
    ):
        self.profile = profile
        self.enable_testnet_submission = enable_testnet_submission
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        headers = {}
        if profile.token_env:
            token = os.environ.get(profile.token_env)
            if not token:
                raise ProviderError(
                    f"Missing credential environment variable {profile.token_env}"
                )
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.Client(
            timeout=30, follow_redirects=False, headers=headers
        )
        self._lock = threading.Lock()
        self._last_request = -float("inf")

    def close(self):
        self.client.close()

    def request(self, endpoint, *, body=None, params=None, retry=True):
        payload = (
            None if body is None else json.dumps(body, separators=(",", ":")).encode()
        )
        if payload and len(payload) > self.profile.max_request_bytes:
            raise ProviderError(
                f"Request exceeds configured {self.profile.max_request_bytes}-byte limit"
            )
        for attempt in range(3 if retry else 1):
            with self._lock:
                self.sleep(
                    max(
                        0,
                        self.profile.request_interval
                        - (self.monotonic() - self._last_request),
                    )
                )
                self._last_request = self.monotonic()
                try:
                    with self.client.stream(
                        "GET" if body is None else "POST",
                        self.profile.koios_url.rstrip("/") + "/" + endpoint,
                        content=payload,
                        params=params,
                        headers={"Content-Type": "application/json"} if payload else {},
                    ) as r:
                        chunks, size = [], 0
                        # ponytail: 8 MiB per response; lower page size if a scan exceeds it.
                        for chunk in r.iter_bytes(65536):
                            size += len(chunk)
                            if size > 8 * 1024 * 1024:
                                error = ProviderError if retry else UnknownSubmission
                                raise error("Provider response exceeds 8 MiB limit")
                            chunks.append(chunk)
                        response_body = b"".join(chunks)
                except httpx.TransportError:
                    if not retry:
                        raise UnknownSubmission(
                            "Submission transport failure; reconcile original transaction"
                        ) from None
                    if attempt < 2:
                        self.sleep(2**attempt)
                        continue
                    raise ProviderError(f"{endpoint}: transport unavailable") from None
            if r.status_code in (429, 500, 502, 503, 504) and retry and attempt < 2:
                try:
                    delay = min(
                        30, max(0, float(r.headers.get("Retry-After", 2**attempt)))
                    )
                except ValueError:
                    delay = 2**attempt
                self.sleep(delay)
                continue
            if r.status_code >= 300:
                if endpoint == "ogmios" and r.status_code == 400:
                    try:
                        error_body = json.loads(response_body)
                        if (
                            isinstance(error_body, dict)
                            and error_body.get("id") == "kernel"
                            and "error" in error_body
                        ):
                            return error_body
                    except ValueError:
                        pass
                # Never echo response bodies or authenticated request URLs into logs.
                error = ProviderError if retry else UnknownSubmission
                raise error(f"{endpoint}: HTTP {r.status_code}")
            try:
                return json.loads(response_body)
            except ValueError:
                error = ProviderError if retry else UnknownSubmission
                raise error(f"{endpoint}: invalid JSON response") from None
        raise ProviderError(f"{endpoint}: retry budget exhausted")

    def verify_identity(self):
        data = self.request("genesis")
        if not isinstance(data, list) or len(data) != 1:
            raise ProviderError("Unverified chain identity: invalid genesis response")
        row = data[0]
        try:
            actual = (
                int(row["networkmagic"]),
                int(row["systemstart"]),
                row["networkid"].lower(),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError(
                "Unverified chain identity: missing genesis fields"
            ) from None
        expected = (
            self.profile.network_magic,
            self.profile.system_start,
            self.profile.address_network,
        )
        if actual != expected:
            raise ProviderError(
                f"Chain identity mismatch for {self.profile.name}: expected {expected}, got {actual}"
            )
        return row

    def tip(self):
        data = self.request("tip")
        if not isinstance(data, list) or len(data) != 1 or "hash" not in data[0]:
            raise ProviderError("Invalid tip response")
        return data[0]

    def scan(self, endpoint: str, body: dict) -> Observation:
        if endpoint not in (
            "credential_utxos",
            "address_utxos",
            "asset_utxos",
            "address_txs",
        ):
            raise Unsupported(f"Unsupported scan: {endpoint}")
        self.verify_identity()
        before = self.tip()
        # Timestamp the oldest request, not completion: large scans age while running.
        observed_at = self.clock()
        rows, offset = {}, 0
        for _ in range(self.profile.max_pages):
            page = self.request(
                endpoint,
                body=body,
                params={
                    "limit": self.profile.page_size,
                    "offset": offset,
                    "order": "tx_hash.asc"
                    if endpoint == "address_txs"
                    else "tx_hash.asc,tx_index.asc",
                },
            )
            if not isinstance(page, list):
                raise ProviderError("Invalid scan response; incomplete observation")
            if not page:
                after = self.tip()
                if int(after["block_no"]) < int(before["block_no"]) or (
                    after["block_no"] == before["block_no"]
                    and after["hash"] != before["hash"]
                ):
                    raise ProviderError(
                        "Chain inconsistency during scan; resynchronize"
                    )
                return Observation(
                    self.profile.name,
                    self.profile.koios_url,
                    observed_at,
                    tuple(rows.values()),
                    True,
                    before,
                    after,
                )
            added = 0
            for row in page:
                ref = str(
                    OutRef(
                        row["tx_hash"],
                        row.get("tx_index", 0)
                        if endpoint == "address_txs"
                        else row["tx_index"],
                    )
                )
                if ref in rows:
                    if rows[ref] != row:
                        raise ProviderError(
                            "Conflicting UTxO observations; resynchronize"
                        )
                else:
                    rows[ref] = row
                    added += 1
            if not added:
                raise ProviderError("Pagination made no progress; incomplete scan")
            offset += len(page)  # The server can cap below our requested limit.
        raise ProviderError("Scan page limit reached; refusing incomplete observation")

    def credential_utxos(self, credential):
        return self.scan(
            "credential_utxos",
            {"_payment_credentials": [credential], "_extended": True},
        )

    def utxos(self, refs):
        # One ref per request stays inside anonymous-tier request size limits.
        results = []
        for ref in refs:
            data = self.request(
                "utxo_info", body={"_utxo_refs": [str(ref)], "_extended": True}
            )
            if not isinstance(data, list) or len(data) > 1:
                raise ProviderError("Invalid UTxO lookup response")
            for row in data:
                if str(OutRef(row["tx_hash"], row["tx_index"])) != str(ref):
                    raise ProviderError("Provider returned an unrequested UTxO")
                if row.get("is_spent") is False:
                    results.append(row)
                elif row.get("is_spent") is not True:
                    raise ProviderError("UTxO spend state unverified")
        return results

    def script_info(self, script_hash):
        data = self.request("script_info", body={"_script_hashes": [script_hash]})
        if (
            not isinstance(data, list)
            or len(data) != 1
            or data[0].get("script_hash") != script_hash
        ):
            raise ProviderError("Script unavailable or identity mismatch")
        return data[0]

    def reference_script(self, script_hash):
        """Find one usable reference; this is not a complete market scan."""
        from .chain_context import to_utxo

        self.verify_identity()
        data = self.request(
            "reference_script_utxos",
            body={"_script_hashes": [script_hash]},
            params={"limit": 10},
        )
        if not isinstance(data, list):
            raise ProviderError("Invalid reference-script discovery response")
        for candidate in data:
            if candidate.get("script_hash") != script_hash:
                raise ProviderError("Reference-script discovery identity mismatch")
            rows = self.utxos([OutRef(candidate["tx_hash"], candidate["tx_index"])])
            if not rows:
                continue
            row = rows[0]
            if (row.get("reference_script") or {}).get("hash") != script_hash:
                raise ProviderError("Reference-script UTxO identity mismatch")
            to_utxo(row, self.profile)
            return row
        raise ProviderError("No available reference-script UTxO in bounded discovery")

    def stake_rewards(self, reward_address):
        data = self.request("account_info", body={"_stake_addresses": [reward_address]})
        if (
            not isinstance(data, list)
            or len(data) != 1
            or data[0].get("stake_address") != reward_address
        ):
            raise ProviderError("Stake reward account is unverified")
        row = data[0]
        reward = row.get("rewards_available")
        if (
            row.get("status") != "registered"
            or not isinstance(reward, str)
            or not reward.isascii()
            or not reward.isdigit()
        ):
            raise ProviderError(
                "Stake withdrawal requires a registered account and verified rewards"
            )
        return int(reward)

    def recheck_dependencies(self, rows: list[dict]):
        """Reject disappeared or changed dependencies before planning/signing.

        This focused recheck still does not acquire shared ledger state. The
        ledger resolves races after evaluation; reservations cover local races.
        """
        self.verify_identity()
        refs = [OutRef(r["tx_hash"], r["tx_index"]) for r in rows]
        if len(set(refs)) != len(refs):
            raise ProviderError("Plan contains duplicate dependencies")
        fresh = self.utxos(refs)
        by_ref = {OutRef(r["tx_hash"], r["tx_index"]): r for r in fresh}
        if set(by_ref) != set(refs):
            raise ProviderError(
                "Plan dependency disappeared or was consumed; invalidate plan"
            )
        for old, ref in zip(rows, refs, strict=True):
            for key in (
                "address",
                "value",
                "datum_hash",
                "inline_datum",
                "asset_list",
                "reference_script",
            ):
                left, right = old.get(key), by_ref[ref].get(key)
                if key == "asset_list":
                    from .protocols import row_assets

                    left, right = (
                        row_assets(old, self.profile.name),
                        row_assets(by_ref[ref], self.profile.name),
                    )
                elif key == "inline_datum":
                    left, right = (left or {}).get("bytes"), (right or {}).get("bytes")
                if left != right:
                    raise ProviderError("Plan dependency changed; resynchronize")
        return fresh

    def rpc(self, method, params=None, *, retry=True):
        if method == "submitTransaction" and not (
            self.enable_testnet_submission
            and self.profile.name in ("preprod", "preview")
        ):
            raise Unsupported(
                "Submission disabled until execution qualification passes"
            )
        data = self.request(
            "ogmios",
            body={
                "jsonrpc": "2.0",
                "id": "kernel",
                "method": method,
                "params": params or {},
            },
            retry=retry,
        )
        if isinstance(data, dict) and data.get("id") == "kernel" and "error" in data:
            error = ProviderError if retry else UnknownSubmission
            raise error(
                f"Ogmios {method} rejected the request (code {data['error'].get('code')})",
                rpc_error=data["error"],
            )
        if (
            not isinstance(data, dict)
            or data.get("id") != "kernel"
            or "result" not in data
            or "error" in data
        ):
            error = ProviderError if retry else UnknownSubmission
            raise error(f"Ogmios {method} failed or returned an invalid response")
        return data["result"]

    def evaluate(self, cbor_hex):
        try:
            if not bytes.fromhex(cbor_hex):
                raise ValueError
        except ValueError:
            raise ProviderError(
                "Evaluation requires nonempty transaction CBOR hex"
            ) from None
        self.verify_identity()
        data = self.rpc("evaluateTransaction", {"transaction": {"cbor": cbor_hex}})
        if not isinstance(data, list) or not data:
            raise ProviderError("Evaluation returned no script budgets")
        for result in data:
            if (
                not isinstance(result, dict)
                or not {"validator", "budget"} <= result.keys()
            ):
                raise ProviderError("Invalid evaluation budget")
            budget = result["budget"]
            if not isinstance(budget, dict) or any(
                type(budget.get(k)) is not int or budget[k] < 0
                for k in ("memory", "cpu")
            ):
                raise ProviderError("Invalid evaluation units")
        return data

    def submit(self, cbor_hex):
        if not self.enable_testnet_submission or self.profile.name not in (
            "preprod",
            "preview",
        ):
            raise Unsupported(
                "Submission disabled: explicit testnet execution is required"
            )
        from pycardano import Transaction

        transaction = Transaction.from_cbor(cbor_hex)
        if (
            not transaction.valid
            or not transaction.transaction_witness_set.vkey_witnesses
        ):
            raise ProviderError("Submission requires a signed, valid transaction")
        self.verify_identity()
        result = self.rpc(
            "submitTransaction", {"transaction": {"cbor": cbor_hex}}, retry=False
        )
        expected = str(transaction.transaction_body.id)
        if (
            not isinstance(result, dict)
            or result.get("transaction", {}).get("id") != expected
        ):
            raise UnknownSubmission(
                "Submission response hash mismatch; reconcile original transaction"
            )
        return expected

    def transaction_info(self, tx_hash):
        data = self.request(
            "tx_info",
            body={
                "_tx_hashes": [tx_hash],
                "_inputs": True,
                "_assets": True,
                "_scripts": True,
            },
        )
        if not isinstance(data, list) or len(data) > 1:
            raise ProviderError("Invalid transaction information response")
        if not data:
            return None
        if data[0].get("tx_hash") != tx_hash:
            raise ProviderError("Transaction information identity mismatch")
        if "valid_contract" not in data[0]:
            # Current tx_info omits the top-level validity flag. Resolve the
            # actual on-chain transaction bytes instead of assuming success.
            tx = self.transaction_cbor(tx_hash)
            data[0]["valid_contract"] = tx.valid
            data[0]["valid_contract_source"] = "verified on-chain transaction CBOR"
        return data[0]

    def transaction_cbor(self, tx_hash):
        from pycardano import Transaction

        rows = self.request("tx_cbor", body={"_tx_hashes": [tx_hash]})
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or rows[0].get("tx_hash") != tx_hash
        ):
            raise ProviderError("Transaction CBOR unavailable or identity mismatch")
        tx = Transaction.from_cbor(rows[0]["cbor"])
        if str(tx.transaction_body.id) != tx_hash:
            raise ProviderError("Transaction CBOR body hash mismatch")
        return tx

    def input_states(self, refs):
        """Explicit spent/unspent evidence; absence is never a spend assertion."""
        result = {}
        for ref in refs:
            rows = self.request(
                "utxo_info", body={"_utxo_refs": [str(ref)], "_extended": True}
            )
            if not isinstance(rows, list) or len(rows) != 1:
                raise ProviderError("Input state unavailable")
            row = rows[0]
            if (
                f"{row['tx_hash']}#{row['tx_index']}" != str(ref)
                or type(row.get("is_spent")) is not bool
            ):
                raise ProviderError("Input state identity/validity mismatch")
            result[str(ref)] = row
        return result

    def confirmed_spender(self, ref, row, *, exclude):
        from .signing import ref_text

        history = self.scan(
            "address_txs",
            {
                "_addresses": [row["address"]],
                "_after_block_height": max(0, row["block_height"] - 1),
            },
        )
        for item in sorted(history.rows, key=lambda r: r["block_height"], reverse=True):
            if (
                item["tx_hash"] == exclude
                or history.tip_after["block_no"] - item["block_height"] + 1
                < self.profile.confirmations
            ):
                continue
            tx = self.transaction_cbor(item["tx_hash"])
            consumed = (
                tx.transaction_body.inputs
                if tx.valid
                else tx.transaction_body.collateral or []
            )
            if ref not in set(map(ref_text, consumed)):
                continue
            info = self.transaction_info(item["tx_hash"])
            block = self.block_at_height(item["block_height"])
            if (
                info
                and block
                and info["block_height"] == item["block_height"]
                and info["block_hash"] == block["hash"]
            ):
                return {
                    "txid": item["tx_hash"],
                    "consumed": ref,
                    "block_hash": block["hash"],
                    "block_height": item["block_height"],
                }
        return None

    def block_at_height(self, height):
        data = self.request(
            "blocks", params={"block_height": f"eq.{height}", "limit": 2}
        )
        if not isinstance(data, list) or len(data) > 1:
            raise ProviderError("Ambiguous canonical block observation")
        if data and data[0].get("block_height") != height:
            raise ProviderError("Block height identity mismatch")
        return data[0] if data else None

    def transaction_status(self, tx_hash):
        data = self.request("tx_status", body={"_tx_hashes": [tx_hash]})
        if not isinstance(data, list) or len(data) > 1:
            raise ProviderError("Invalid transaction status response")
        if not data:
            return None
        if data[0].get("tx_hash") != tx_hash:
            raise ProviderError("Transaction status identity mismatch")
        return data[0]
