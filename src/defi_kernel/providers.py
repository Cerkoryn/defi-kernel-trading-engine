"""Bounded HTTP transport, shared recovery checks and the Koios adapter."""

import json
import math
import os
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import httpx

from .config import Profile
from .domain import KernelError, Observation, OutRef, RequestTooLarge, Unsupported


class ProviderError(KernelError):
    def __init__(self, message, *, rpc_error=None):
        super().__init__(message)
        self.rpc_error = rpc_error


class UnknownSubmission(ProviderError):
    """The original transaction must be reconciled; rebuilding is unsafe."""


class ProviderLag(ProviderError):
    """Inclusion is ahead of the provider tip; retain reservations and retry reads."""


class EvidenceUnavailable(ProviderError):
    """Indexer has not supplied transaction bytes; retry reads, retaining reservations."""


class RateLimited(ProviderError):
    def __init__(self, seconds, *, provider="Provider"):
        super().__init__(f"{provider} rate limited; waiting before further requests")
        self.retry_after_seconds = max(1, math.ceil(seconds))


@dataclass
class _RequestState:
    lock: object = field(default_factory=threading.Lock)
    last_request: float = -float("inf")
    cooldown_until: float = 0
    rate_limits: int = 0


class BoundedHTTP:
    def __init__(
        self,
        profile: Profile,
        *,
        client=None,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
        enable_testnet_submission=False,
        share_requests_with=None,
        base_url=None,
        anonymous=False,
        request_interval=None,
        credential_env=None,
    ):
        self.profile = profile
        self.base_url = base_url or profile.koios_url
        if not self.base_url:
            raise ProviderError(
                "Explicit capability profiles require create_provider; Koios-only capture tools need a legacy Koios profile"
            )
        self.label = next(
            (
                name
                for name, setting in profile.providers.items()
                if self.base_url in (setting.url, setting.minikupo_url)
            ),
            "Koios",
        )
        self.token_env = None if anonymous else credential_env or profile.token_env
        self._request_interval = request_interval
        self.enable_testnet_submission = enable_testnet_submission
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        headers = {}
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ProviderError(
                    f"Missing credential environment variable {self.token_env}; export it before starting"
                )
            if not token.isascii() or any(not 33 <= ord(c) <= 126 for c in token):
                raise ProviderError(
                    f"Credential in {self.token_env} must be a raw token without whitespace"
                )
            headers["Authorization"] = f"Bearer {token}"
        self.client = client or httpx.Client(
            timeout=30, follow_redirects=False, headers=headers
        )
        if client is not None:
            self.client.headers.update(headers)
        if share_requests_with is not None and (
            self.base_url != share_requests_with.base_url
            or self.client.headers.get("Authorization")
            != share_requests_with.client.headers.get("Authorization")
            or monotonic is not share_requests_with.monotonic
        ):
            self.client.close()
            raise ProviderError(
                "Shared request pacing requires the same endpoint, credential and clock"
            )
        self._requests = (
            share_requests_with._requests
            if share_requests_with is not None
            else _RequestState()
        )
        self.observer = None
        self._reference_refs = {}

    def _diagnostic(self, **data):
        if self.observer is not None:
            try:
                self.observer.provider_event({"provider": self.base_url, **data})
            except Exception:
                # Diagnostics must not replace a provider result, especially after submit.
                self.observer.healthy = False

    def close(self):
        self.client.close()

    @property
    def request_interval(self):
        return (
            self.profile.request_interval
            if self._request_interval is None
            else self._request_interval
        )

    def request(
        self,
        endpoint,
        *,
        body=None,
        params=None,
        retry=True,
        deadline=None,
        raw=None,
        missing_ok=False,
    ):
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline)
        ):
            raise KernelError("Invalid submission deadline")
        payload = (
            raw
            if raw is not None
            else (
                None
                if body is None
                else json.dumps(body, separators=(",", ":")).encode()
            )
        )
        if payload and len(payload) > self.profile.max_request_bytes:
            raise RequestTooLarge(len(payload), self.profile.max_request_bytes)
        for attempt in range(3 if retry else 1):
            if retry and self.observer is not None and self.observer.stop_requested:
                raise KernelError("Stop requested between provider operations")
            with self._requests.lock:
                if self.monotonic() < self._requests.cooldown_until:
                    raise RateLimited(
                        self._requests.cooldown_until - self.monotonic(),
                        provider=self.label,
                    )
                self.sleep(
                    max(
                        0,
                        self.request_interval
                        - (self.monotonic() - self._requests.last_request),
                    )
                )
                self._requests.last_request = self.monotonic()
                if (
                    self.observer is not None
                    and not retry
                    and (
                        raw is not None
                        or isinstance(body, dict)
                        and body.get("method") == "submitTransaction"
                    )
                ):
                    # submit() performs another identity read after the coordinator claim.
                    # A failed gate retains that claim as unknown, never retried.
                    self.observer.guard("provider-submit")
                if deadline is not None and self.clock() >= deadline:
                    raise KernelError("Submission deadline elapsed before transmission")
                started = self.monotonic()
                # Wall time anchors inclusion measurements; duration uses a monotonic clock.
                # Capture after pacing/guards. Never record headers or request payloads.
                wire = {"started_at": self.clock()}
                if endpoint == "ogmios" and isinstance(body, dict):
                    method = body.get("method")
                    if method in ("submitTransaction", "evaluateTransaction"):
                        wire["method"] = method
                try:
                    with self.client.stream(
                        "GET" if payload is None else "POST",
                        self.base_url.rstrip("/") + "/" + endpoint,
                        content=payload,
                        params=params,
                        headers={
                            "Content-Type": "application/cbor"
                            if raw is not None
                            else "application/json"
                        }
                        if payload is not None
                        else {},
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
                    self._diagnostic(
                        endpoint=endpoint,
                        attempt=attempt + 1,
                        result="transport_error",
                        seconds=self.monotonic() - started,
                        finished_at=self.clock(),
                        **wire,
                    )
                    if not retry:
                        raise UnknownSubmission(
                            "Submission transport failure; reconcile original transaction"
                        ) from None
                    if attempt < 2:
                        self.sleep(2**attempt)
                        continue
                    raise ProviderError(f"{endpoint}: transport unavailable") from None
                if r.status_code == 429:
                    self._requests.rate_limits = min(self._requests.rate_limits + 1, 5)
                    delay = min(900, 60 * 2 ** (self._requests.rate_limits - 1))
                    header = r.headers.get("Retry-After", "")
                    try:
                        try:
                            requested = float(header)
                        except ValueError:
                            requested = (
                                parsedate_to_datetime(header).timestamp() - self.clock()
                            )
                        if math.isfinite(requested):
                            delay = max(delay, requested)
                    except (ValueError, TypeError, OverflowError):
                        pass
                    self._requests.cooldown_until = self.monotonic() + delay
                    self._diagnostic(
                        endpoint=endpoint,
                        status=429,
                        retry_after_seconds=delay,
                        seconds=self.monotonic() - started,
                        finished_at=self.clock(),
                        **wire,
                    )
                    if not retry:
                        raise UnknownSubmission(
                            "Submission HTTP 429; reconcile original transaction after provider cooldown"
                        )
                    raise RateLimited(delay, provider=self.label)
                if (
                    r.status_code < 300
                    and self.monotonic() - self._requests.cooldown_until >= 300
                ):
                    self._requests.rate_limits = 0
            self._diagnostic(
                endpoint=endpoint,
                attempt=attempt + 1,
                status=r.status_code,
                request_bytes=len(payload or b""),
                response_bytes=size,
                seconds=self.monotonic() - started,
                finished_at=self.clock(),
                **wire,
            )
            if r.status_code in (500, 502, 503, 504) and retry and attempt < 2:
                try:
                    delay = min(
                        30, max(0, float(r.headers.get("Retry-After", 2**attempt)))
                    )
                except ValueError:
                    delay = 2**attempt
                self._diagnostic(
                    endpoint=endpoint, retry_after_seconds=delay, attempt=attempt + 1
                )
                self.sleep(delay)
                continue
            if r.status_code == 404 and missing_ok and retry:
                return None
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
                if r.status_code in (401, 403) and self.token_env:
                    raise error(
                        f"Provider access rejected (HTTP {r.status_code}); check {self.token_env} and token permissions"
                    )
                raise error(f"{endpoint}: HTTP {r.status_code}")
            try:
                return json.loads(response_body)
            except ValueError:
                error = ProviderError if retry else UnknownSubmission
                raise error(f"{endpoint}: invalid JSON response") from None
        raise ProviderError(f"{endpoint}: retry budget exhausted")


class ProviderChecks:
    """Shared safety checks over named capabilities, independent of HTTP format."""

    def reference_script(self, script_hash):
        return self.reference_scripts([script_hash])[script_hash]

    def stake_rewards(self, reward_address):
        return self.stake_rewards_many([reward_address])[reward_address]

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

    def confirmed_spender(self, ref, row, *, exclude):
        from .signing import ref_text

        history = self.address_transactions(
            row["address"], after_height=max(0, row["block_height"] - 1)
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


class Koios(BoundedHTTP, ProviderChecks):
    def address_utxos(self, address):
        return self.scan(
            "address_utxos", {"_addresses": [str(address)], "_extended": True}
        )

    def address_transactions(self, address, *, after_height=0):
        return self.scan(
            "address_txs",
            {"_addresses": [str(address)], "_after_block_height": after_height},
        )

    def asset_utxos(self, policy, name):
        return self.scan(
            "asset_utxos", {"_asset_list": [[policy, name]], "_extended": True}
        )

    def era_summaries(self):
        return self.rpc("queryLedgerState/eraSummaries")

    def protocol_parameters(self):
        return self.rpc("queryLedgerState/protocolParameters")

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
                    self.base_url,
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
        requested = list(map(str, refs))
        if len(set(requested)) != len(requested):
            raise ProviderError("Duplicate requested UTxO")
        batches, batch = [], []
        for ref in requested:
            body = {"_utxo_refs": [*batch, ref], "_extended": True}
            if (
                len(json.dumps(body, separators=(",", ":")).encode())
                > self.profile.max_request_bytes
            ):
                if not batch:
                    raise ProviderError("Single UTxO lookup exceeds request limit")
                batches.append(batch)
                batch = []
            batch.append(ref)
        if batch:
            batches.append(batch)
        found = {}
        for batch in batches:
            data = self.request(
                "utxo_info", body={"_utxo_refs": batch, "_extended": True}
            )
            if not isinstance(data, list):
                raise ProviderError("Invalid UTxO lookup response")
            seen = set()
            for row in data:
                ref = str(OutRef(row["tx_hash"], row["tx_index"]))
                if ref not in batch or ref in seen:
                    raise ProviderError(
                        "Provider returned a duplicate or unrequested UTxO lookup"
                    )
                seen.add(ref)
                if row.get("is_spent") is False:
                    found[ref] = row
                elif row.get("is_spent") is not True:
                    raise ProviderError("UTxO spend state unverified")
        # Missing/spent outputs remain absent; dependency rechecks require the full set.
        return [found[ref] for ref in requested if ref in found]

    def script_info(self, script_hash):
        data = self.request("script_info", body={"_script_hashes": [script_hash]})
        if (
            not isinstance(data, list)
            or len(data) != 1
            or data[0].get("script_hash") != script_hash
        ):
            raise ProviderError("Script unavailable or identity mismatch")
        return data[0]

    def resolve_datums(self, rows):
        """Attach verified witnesses without converting hash datums to inline datums."""
        from hashlib import blake2b

        cache = getattr(self, "_datum_witnesses", {})
        hashes = set()
        for row in rows:
            h = row.get("datum_hash")
            if h is None or row.get("inline_datum"):
                continue
            if not isinstance(h, str) or len(h) != 64:
                raise ProviderError("Invalid datum hash")
            try:
                if len(bytes.fromhex(h)) != 32:
                    raise ValueError()
            except ValueError as error:
                raise ProviderError("Invalid datum hash") from error
            if h not in cache:
                hashes.add(h)
        hashes = sorted(hashes)
        found = {}
        # Each quoted 64-character hash plus comma costs at most 67 bytes.
        batch_size = min(50, max(1, (self.profile.max_request_bytes - 20) // 67))
        for offset in range(0, len(hashes), batch_size):
            batch = hashes[offset : offset + batch_size]
            data = self.request("datum_info", body={"_datum_hashes": batch})
            if not isinstance(data, list):
                raise ProviderError("Invalid datum lookup response")
            for item in data:
                if not isinstance(item, dict):
                    raise ProviderError("Invalid datum lookup response")
                h, raw = item.get("datum_hash"), item.get("bytes")
                if (
                    not isinstance(h, str)
                    or h not in batch
                    or h in found
                    or not isinstance(raw, str)
                ):
                    raise ProviderError("Unrequested or duplicate datum witness")
                try:
                    valid = blake2b(bytes.fromhex(raw), digest_size=32).hexdigest() == h
                except ValueError:
                    valid = False
                if not valid:
                    raise ProviderError("Datum witness hash mismatch")
                found[h] = raw
        found = {**cache, **found}
        # Content hashes are immutable. Bound memory without expiring valid bytes.
        self._datum_witnesses = dict(list(found.items())[-4096:])
        # Unavailable witnesses make individual orders ineligible, not fabricated.
        return [
            dict(r, datum_cbor=found[r["datum_hash"]])
            if r.get("datum_hash") in found
            else r
            for r in rows
        ]

    def reference_scripts(self, script_hashes):
        """Cache identities only; re-read and validate every unspent reference."""
        from .chain_context import to_utxo

        hashes = sorted(set(script_hashes))
        if not hashes:
            return {}
        self.verify_identity()
        cached = {
            h: self._reference_refs[h] for h in hashes if h in self._reference_refs
        }
        fresh = (
            {
                OutRef(r["tx_hash"], r["tx_index"]): r
                for r in self.utxos(list(cached.values()))
            }
            if cached
            else {}
        )
        result = {}
        for script_hash in hashes:
            row = fresh.get(cached.get(script_hash))
            if row is None:
                self._reference_refs.pop(script_hash, None)
                data = self.request(
                    "reference_script_utxos",
                    body={"_script_hashes": [script_hash]},
                    params={"limit": 10},
                )
                if not isinstance(data, list) or len(data) > 10:
                    raise ProviderError("Invalid reference-script discovery response")
                refs = []
                for candidate in data:
                    if (
                        not isinstance(candidate, dict)
                        or candidate.get("script_hash") != script_hash
                    ):
                        raise ProviderError(
                            "Reference-script discovery identity mismatch"
                        )
                    try:
                        refs.append(OutRef(candidate["tx_hash"], candidate["tx_index"]))
                    except (KeyError, TypeError, ValueError, KernelError) as error:
                        raise ProviderError(
                            "Invalid reference-script identity"
                        ) from error
                rows = self.utxos(list(dict.fromkeys(refs))) if refs else []
                if not rows:
                    raise ProviderError(
                        "No available reference-script UTxO in bounded discovery"
                    )
                row = rows[0]
            if (row.get("reference_script") or {}).get("hash") != script_hash:
                raise ProviderError("Reference-script UTxO identity mismatch")
            to_utxo(row, self.profile)
            self._reference_refs[script_hash] = OutRef(row["tx_hash"], row["tx_index"])
            result[script_hash] = row
        return result

    def stake_rewards_many(self, reward_addresses):
        accounts = sorted(set(reward_addresses))
        rewards = {}
        for offset in range(0, len(accounts), 50):
            batch = accounts[offset : offset + 50]
            data = self.request(
                "account_info",
                body={"_stake_addresses": batch},
                params={"limit": len(batch) + 1},
            )
            if not isinstance(data, list):
                raise ProviderError("Stake reward accounts are unverified")
            for row in data:
                account = row.get("stake_address") if isinstance(row, dict) else None
                if account not in batch or account in rewards:
                    raise ProviderError("Stake reward account is unverified")
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
                rewards[account] = int(reward)
            if not set(batch) <= rewards.keys():
                raise ProviderError("Stake reward accounts are incomplete")
        return rewards

    def rpc(self, method, params=None, *, retry=True, deadline=None):
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
            **({"deadline": deadline} if deadline is not None else {}),
        )
        if isinstance(data, dict) and data.get("id") == "kernel" and "error" in data:
            error = ProviderError if retry else UnknownSubmission
            code = (
                data["error"].get("code") if isinstance(data["error"], dict) else None
            )
            code = code if type(code) is int else "unknown"
            raise error(
                f"Ogmios {method} rejected the request (code {code})",
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

    def submit(self, cbor_hex, *, deadline=None):
        if not self.enable_testnet_submission or self.profile.name not in (
            "preprod",
            "preview",
        ):
            raise Unsupported(
                "Submission disabled: explicit testnet execution is required"
            )
        from .signing import decode_transaction

        transaction = decode_transaction(cbor_hex)
        if (
            not transaction.valid
            or not transaction.transaction_witness_set.vkey_witnesses
        ):
            raise ProviderError("Submission requires a signed, valid transaction")
        self.verify_identity()
        result = self.rpc(
            "submitTransaction",
            {"transaction": {"cbor": cbor_hex}},
            retry=False,
            deadline=deadline,
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
        from .signing import decode_transaction

        rows = self.request("tx_cbor", body={"_tx_hashes": [tx_hash]})
        if rows == []:
            raise EvidenceUnavailable(
                "Transaction bytes not yet available; waiting for indexed chain evidence"
            )
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or rows[0].get("tx_hash") != tx_hash
        ):
            raise ProviderError("Transaction CBOR unavailable or identity mismatch")
        tx = decode_transaction(rows[0].get("cbor"))
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

    def blocks_at_heights(self, heights):
        """Fresh canonical checks, bounded per request; missing heights stay unknown."""
        heights = sorted(set(heights))
        if any(type(h) is not int or h < 0 for h in heights):
            raise ProviderError("Invalid block height")
        blocks = dict.fromkeys(heights)
        for offset in range(0, len(heights), 50):
            batch = heights[offset : offset + 50]
            data = self.request(
                "blocks",
                params={
                    "block_height": "in.(" + ",".join(map(str, batch)) + ")",
                    "limit": len(batch) + 1,
                },
            )
            if not isinstance(data, list):
                raise ProviderError("Invalid canonical block observation")
            for row in data:
                height = row.get("block_height") if isinstance(row, dict) else None
                if (
                    type(height) is not int
                    or height not in batch
                    or blocks[height] is not None
                ):
                    raise ProviderError("Ambiguous canonical block observation")
                if not isinstance(row.get("hash"), str) or len(row["hash"]) != 64:
                    raise ProviderError("Invalid canonical block hash")
                blocks[height] = row
        return blocks

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
