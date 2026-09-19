"""Dolos REST adapters. Missing evidence is never proof of spending.

MiniKupo supplies live UTxOs; MiniBlockfrost supplies historical evidence.
Wire rows are normalized to the engine's existing validated row representation.
"""

import re
from copy import deepcopy
from fractions import Fraction
from hashlib import blake2b
from urllib.parse import quote

from pycardano import Address, Network, PlutusScript, plutus_script_hash

from .config import KNOWN_CHAINS
from .domain import Asset, Observation, OutRef, Unsupported
from .providers import (
    BoundedHTTP,
    EvidenceUnavailable,
    ProviderChecks,
    ProviderError,
    ProviderLag,
    UnknownSubmission,
)


def integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise ProviderError("Dolos returned an invalid nonnegative integer")


def hash_hex(value, size=32):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-f]{" + str(size * 2) + "}", value
    ):
        raise ProviderError("Dolos returned an invalid content hash")
    return value


def record(value):
    if not isinstance(value, dict):
        raise ProviderError("Dolos returned an invalid record")
    return value


def verified_datum(raw, expected):
    hash_hex(expected)
    try:
        if (
            not isinstance(raw, str)
            or blake2b(bytes.fromhex(raw), digest_size=32).hexdigest() != expected
        ):
            raise ValueError()
    except ValueError:
        raise ProviderError("Dolos datum witness hash mismatch") from None
    return raw


def block_row(value):
    p = record(value)
    return {
        "hash": hash_hex(p.get("hash")),
        "block_height": integer(p.get("height")),
        "abs_slot": integer(p.get("slot")),
        "block_time": integer(p.get("time")),
        "epoch_no": integer(p.get("epoch")),
    }


class Dolos(ProviderChecks):
    def __init__(self, profile, settings, *, client=None, kupo_client=None, **kwargs):
        self.profile, self.settings = profile, settings
        self.http = BoundedHTTP(
            profile,
            base_url=settings.url,
            anonymous=True,
            request_interval=settings.request_interval,
            client=client,
            **kwargs,
        )
        try:
            self.kupo = BoundedHTTP(
                profile,
                base_url=settings.minikupo_url,
                anonymous=True,
                request_interval=settings.request_interval,
                client=kupo_client,
                **kwargs,
            )
        except Exception:
            self.http.close()
            raise
        self.clock = self.http.clock
        self._blocks = {}
        self._scripts = {}
        self._datums = {}

    @property
    def observer(self):
        return self.http.observer

    @observer.setter
    def observer(self, value):
        self.http.observer = self.kupo.observer = value

    def close(self):
        self.http.close()
        self.kupo.close()

    def verify_identity(self):
        p = record(self.http.request("genesis"))
        actual = (integer(p.get("network_magic")), integer(p.get("system_start")))
        expected = (self.profile.network_magic, self.profile.system_start)
        if actual != expected or KNOWN_CHAINS.get(self.profile.name) != (
            *expected,
            self.profile.address_network,
        ):
            raise ProviderError(
                "Dolos chain identity mismatch or unqualified custom chain"
            )
        names = {
            "activeslotcoeff": "active_slots_coefficient",
            "maxlovelacesupply": "max_lovelace_supply",
            "epochlength": "epoch_length",
            "slotlength": "slot_length",
            "slotsperkesperiod": "slots_per_kes_period",
            "updatequorum": "update_quorum",
            "maxkesrevolutions": "max_kes_evolutions",
            "securityparam": "security_param",
        }
        if not set(names.values()) <= p.keys():
            raise ProviderError("Dolos genesis fields are incomplete")
        return {
            **{k: str(p[v]) for k, v in names.items()},
            "networkmagic": actual[0],
            "systemstart": actual[1],
            "networkid": self.profile.address_network,
        }

    def tip(self):
        p = block_row(self.http.request("blocks/latest"))
        age = self.clock() - p["block_time"]
        if not 0 <= age <= self.settings.max_tip_age_seconds:
            raise ProviderLag(
                f"Dolos chain tip is not fresh ({int(age)}s); waiting for synchronization"
            )
        return {**p, "block_no": p["block_height"]}

    def _block(self, block_hash):
        hash_hex(block_hash)
        if block_hash not in self._blocks:
            p = self.http.request("blocks/" + block_hash, missing_ok=True)
            if p is None:
                raise EvidenceUnavailable(
                    "Dolos archive block unavailable; full history is required"
                )
            p = block_row(p)
            if p["hash"] != block_hash:
                raise ProviderError("Dolos block identity mismatch")
            self._blocks[block_hash] = p
            if len(self._blocks) > 4096:
                self._blocks.pop(next(iter(self._blocks)))
        return self._blocks[block_hash]

    def block_at_height(self, height):
        height = integer(height)
        p = self.http.request(f"blocks/{height}", missing_ok=True)
        if p is None:
            return None
        p = block_row(p)
        if p["block_height"] != height:
            raise ProviderError("Dolos canonical block height mismatch")
        return p

    def blocks_at_heights(self, heights):
        return {h: self.block_at_height(h) for h in sorted(set(heights))}

    def _observation(self, read):
        self.verify_identity()
        before, observed_at = self.tip(), self.clock()
        rows = read()
        after = self.tip()
        anchor = self.block_at_height(before["block_no"])
        if (
            after["block_no"] < before["block_no"]
            or not anchor
            or anchor["hash"] != before["hash"]
        ):
            raise ProviderLag("Dolos chain changed during observation; resynchronize")
        return Observation(
            self.profile.name,
            self.settings.url,
            observed_at,
            tuple(rows),
            True,
            before,
            after,
        )

    def _script(self, h, value=None):
        hash_hex(h, 28)
        if h not in self._scripts:
            p = record(
                value if value is not None else self.kupo.request(f"scripts/{h}")
            )
            language = p.get("language")
            if language not in ("plutus:v1", "plutus:v2", "plutus:v3"):
                raise Unsupported(
                    "Dolos native reference script conversion is not qualified"
                )
            try:
                raw = bytes.fromhex(p["script"])
                script = PlutusScript.from_version(int(language[-1]), raw)
                if str(plutus_script_hash(script)) != h:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ProviderError("Dolos reference script hash mismatch") from None
            self._scripts[h] = {
                "hash": h,
                "type": "plutusV" + language[-1],
                "bytes": raw.hex(),
                "size": len(raw),
            }
            if len(self._scripts) > 256:
                self._scripts.pop(next(iter(self._scripts)))
        return deepcopy(self._scripts[h])

    def script_info(self, script_hash):
        p = self._script(script_hash)
        return {**p, "script_hash": p["hash"]}

    def _row(
        self,
        ref,
        address,
        coins,
        assets,
        *,
        datum_hash=None,
        inline=None,
        script=None,
        block,
    ):
        from .chain_context import to_utxo

        address = Address.from_primitive(address)
        expected = (
            Network.MAINNET
            if self.profile.address_network == "mainnet"
            else Network.TESTNET
        )
        if address.network != expected or address.payment_part is None:
            raise ProviderError("Dolos output address network/credential mismatch")
        asset_list = []
        for unit, q in sorted(assets.items()):
            a = Asset.from_unit(self.profile.name, unit)
            if not a.policy:
                raise ProviderError("Dolos asset list duplicates ADA")
            asset_list.append(
                {
                    "policy_id": a.policy.hex(),
                    "asset_name": a.name.hex(),
                    "quantity": str(integer(q)),
                }
            )
        if datum_hash is not None:
            hash_hex(datum_hash)
        if inline is not None:
            verified_datum(inline, datum_hash)
        result = {
            "tx_hash": ref.tx_hash,
            "tx_index": ref.index,
            "address": str(address),
            "payment_cred": str(address.payment_part),
            "value": str(integer(coins)),
            "asset_list": asset_list,
            "datum_hash": datum_hash,
            "inline_datum": {"bytes": inline} if inline is not None else None,
            "reference_script": script,
            "is_spent": False,
            "block_height": block["block_height"],
            "block_time": block["block_time"],
            "block_hash": block["hash"],
        }
        to_utxo(result, self.profile)
        return result

    def _matches(self, pattern, *, params=None, requested=None):
        health = record(self.kupo.request("health"))
        checkpoint = integer(health.get("most_recent_checkpoint"))
        tip = self.tip()
        if (
            health.get("connection_status") != "connected"
            or checkpoint != tip["abs_slot"]
        ):
            # Both APIs must expose the same local state cursor. Retry a moving tip;
            # do not accept stale positive UTxO evidence from an index still catching up.
            raise ProviderLag(
                "Dolos index/chain tips differ; waiting for synchronization"
            )
        data = self.kupo.request(
            "matches/" + quote(pattern, safe="/@.*"),
            params={"unspent": "", "resolve_hashes": "", **(params or {})},
        )
        if (
            not isinstance(data, list)
            or len(data) > self.profile.max_pages * self.profile.page_size
        ):
            raise ProviderError("Dolos match limit exceeded or incomplete response")
        rows = {}
        try:
            for p in data:
                record(p)
                if "spent_at" not in p or p["spent_at"] is not None:
                    raise ProviderError(
                        "Dolos unspent match has unverified spend state"
                    )
                ref = OutRef(p["transaction_id"], integer(p["output_index"]))
                if requested is not None:
                    if ref.tx_hash not in {r.tx_hash for r in requested}:
                        raise ProviderError("Dolos output lookup identity mismatch")
                    if ref not in requested:
                        continue
                if ref in rows:
                    raise ProviderError("Duplicate Dolos match; incomplete observation")
                point = record(p["created_at"])
                block = self._block(point["header_hash"])
                if block["abs_slot"] != integer(point["slot_no"]):
                    raise ProviderError("Dolos output inclusion mismatch")
                if any(
                    not re.fullmatch(r"[0-9a-f]{56}\.([0-9a-f]{2}){0,32}", unit)
                    for unit in record(p["value"]["assets"])
                ):
                    raise ProviderError("Malformed Dolos asset identity")
                assets = {
                    k.replace(".", ""): v
                    for k, v in record(p["value"]["assets"]).items()
                }
                if len(assets) != len(p["value"]["assets"]):
                    raise ProviderError("Duplicate Dolos asset identity")
                kind = p.get("datum_type")
                if p.get("datum_hash") and kind not in ("hash", "inline"):
                    raise ProviderError("Dolos datum form is unknown")
                if kind == "inline" and p.get("datum") is None:
                    raise EvidenceUnavailable(
                        "Dolos inline datum bytes are unavailable"
                    )
                row = self._row(
                    ref,
                    p["address"],
                    p["value"]["coins"],
                    assets,
                    datum_hash=p.get("datum_hash"),
                    inline=p.get("datum") if kind == "inline" else None,
                    script=self._script(p["script_hash"], p.get("script"))
                    if p.get("script_hash")
                    else None,
                    block=block,
                )
                if kind == "hash" and p.get("datum") is not None:
                    row["datum_cbor"] = verified_datum(p["datum"], p["datum_hash"])
                rows[ref] = row
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError("Malformed Dolos UTxO response") from error
        return list(rows.values())

    def address_utxos(self, address):
        address = str(Address.from_primitive(address))

        def read():
            rows = self._matches(address)
            if any(r["address"] != address for r in rows):
                raise ProviderError("Dolos address lookup returned a foreign output")
            return rows

        return self._observation(read)

    def credential_utxos(self, credential):
        hash_hex(credential, 28)

        def read():
            rows = self._matches(credential + "/*")
            if any(r["payment_cred"] != credential for r in rows):
                raise ProviderError("Dolos credential lookup returned a foreign output")
            return rows

        return self._observation(read)

    def asset_utxos(self, policy, name):
        unit = Asset.from_unit(self.profile.name, policy + name).unit

        def read():
            rows = self._matches(
                policy + ".*", params={"policy_id": policy, "asset_name": name}
            )
            if any(
                not any(
                    a["policy_id"] + a["asset_name"] == unit for a in r["asset_list"]
                )
                for r in rows
            ):
                raise ProviderError("Dolos asset lookup returned a foreign output")
            return rows

        return self._observation(read)

    def utxos(self, refs):
        refs = list(refs)
        if len(set(refs)) != len(refs):
            raise ProviderError("Duplicate requested UTxO")
        rows = []
        # Group references by transaction: one local index query for all its outputs.
        for txid in dict.fromkeys(r.tx_hash for r in refs):
            wanted = {r for r in refs if r.tx_hash == txid}
            found = self._matches("*@" + txid, requested=wanted)
            if any(r["tx_hash"] != txid for r in found):
                raise ProviderError("Dolos output lookup identity mismatch")
            rows.extend(
                r for r in found if OutRef(r["tx_hash"], r["tx_index"]) in wanted
            )
        by_ref = {OutRef(r["tx_hash"], r["tx_index"]): r for r in rows}
        return [by_ref[r] for r in refs if r in by_ref]

    def resolve_datums(self, rows):
        result = []
        for row in rows:
            h = row.get("datum_hash")
            if h and not row.get("inline_datum"):
                hash_hex(h)
                if row.get("datum_cbor") is not None:
                    self._datums[h] = verified_datum(row["datum_cbor"], h)
                if h not in self._datums:
                    p = self.kupo.request(f"datums/{h}")
                    if p is not None:
                        self._datums[h] = verified_datum(record(p).get("datum"), h)
                if h in self._datums:
                    row = dict(row, datum_cbor=self._datums[h])
            result.append(row)
        self._datums = dict(list(self._datums.items())[-4096:])
        return result

    def reference_scripts(self, script_hashes):
        result = {}
        for h in sorted(set(script_hashes)):
            value = self.settings.reference_scripts.get(h)
            if value is None:
                raise EvidenceUnavailable(
                    f"Dolos reference hint missing for script {h}; configure a qualified unspent reference"
                )
            txid, idx = value.split("#")
            rows = self.utxos([OutRef(txid, int(idx))])
            if (
                len(rows) != 1
                or (rows[0].get("reference_script") or {}).get("hash") != h
            ):
                raise EvidenceUnavailable(
                    f"Dolos reference hint spent or mismatched for script {h}; refresh reference_scripts"
                )
            result[h] = rows[0]
        return result

    def stake_rewards_many(self, addresses):
        result = {}
        for address in sorted(set(addresses)):
            p = record(
                self.http.request(
                    "accounts/" + quote(address, safe=""), missing_ok=True
                )
            )
            if p.get("stake_address") != address or p.get("registered") is not True:
                raise ProviderError(
                    "Stake withdrawal requires a registered account and verified rewards"
                )
            result[address] = integer(p.get("withdrawable_amount"))
        return result

    def transaction_cbor(self, tx_hash):
        from .signing import decode_transaction

        hash_hex(tx_hash)
        p = self.http.request(f"txs/{tx_hash}/cbor", missing_ok=True)
        if p is None:
            raise EvidenceUnavailable(
                "Dolos transaction bytes unavailable; retain saved transaction and retry"
            )
        tx = decode_transaction(record(p).get("cbor"))
        if str(tx.transaction_body.id) != tx_hash:
            raise ProviderError("Dolos transaction CBOR body hash mismatch")
        return tx

    def transaction_info(self, tx_hash):
        hash_hex(tx_hash)
        p = self.http.request(f"txs/{tx_hash}", missing_ok=True)
        if p is None:
            return None
        p = record(p)
        if p.get("hash") != tx_hash:
            raise ProviderError("Dolos transaction identity mismatch")
        block_hash = hash_hex(p.get("block"))
        block = self._block(block_hash)
        if block["block_height"] != integer(p.get("block_height")) or block[
            "abs_slot"
        ] != integer(p.get("slot")):
            raise ProviderError("Dolos transaction inclusion mismatch")
        tx = self.transaction_cbor(tx_hash)
        return {
            "tx_hash": tx_hash,
            "block_height": block["block_height"],
            "block_hash": block_hash,
            "block_time": block["block_time"],
            "absolute_slot": block["abs_slot"],
            "tx_block_index": integer(p.get("index")),
            "valid_contract": tx.valid,
            "fee": str(tx.transaction_body.fee),
        }

    def address_transactions(self, address, *, after_height=0):
        address = str(Address.from_primitive(address))
        integer(after_height)

        def read():
            rows = {}
            for page in range(1, self.profile.max_pages + 1):
                data = self.http.request(
                    "addresses/" + quote(address, safe="") + "/transactions",
                    params={
                        "count": min(self.profile.page_size, 100),
                        "page": page,
                        "order": "asc",
                        **({"from": after_height} if after_height else {}),
                    },
                    missing_ok=True,
                )
                if data is None and page == 1:
                    # MiniBF returns 404 for never-used addresses. Existing journal
                    # history is separately protected by OrderObserver's canonical checks.
                    return []
                if not isinstance(data, list):
                    raise ProviderError("Invalid Dolos history page")
                if not data:
                    return list(rows.values())
                for p in data:
                    p = record(p)
                    h = hash_hex(p.get("tx_hash"))
                    if h in rows:
                        raise ProviderError(
                            "Dolos history pagination repeated an entry"
                        )
                    height = integer(p.get("block_height"))
                    if height < after_height:
                        raise ProviderError(
                            "Dolos history ignored the requested height bound"
                        )
                    rows[h] = {"tx_hash": h, "block_height": height}
            raise ProviderError(
                "Dolos history page limit reached; refusing incomplete observation"
            )

        return self._observation(read)

    def input_states(self, refs):
        result = {}
        for value in refs:
            txid, idx = str(value).split("#")
            ref = OutRef(txid, int(idx))
            info = self.transaction_info(txid)
            if info is None:
                raise EvidenceUnavailable("Dolos input creation history unavailable")
            block = self.block_at_height(info["block_height"])
            if not block or block["hash"] != info["block_hash"]:
                raise ProviderLag("Dolos input creation is not canonical")
            p = record(self.http.request(f"txs/{txid}/utxos", missing_ok=True))
            if p.get("hash") != txid or not isinstance(p.get("outputs"), list):
                raise ProviderError("Dolos historical output identity mismatch")
            outputs = [
                r for r in p["outputs"] if record(r).get("output_index") == ref.index
            ]
            if len(outputs) != 1 or "consumed_by_tx" not in outputs[0]:
                raise EvidenceUnavailable("Dolos explicit input state unavailable")
            output = outputs[0]
            spender = output["consumed_by_tx"]
            if spender is not None:
                hash_hex(spender)
            else:
                # Null historical spend metadata is corroborated by positive live evidence.
                if len(self.utxos([ref])) != 1:
                    raise EvidenceUnavailable(
                        "Dolos input state is unresolved; absence does not prove spending"
                    )
            result[str(ref)] = {
                "tx_hash": txid,
                "tx_index": ref.index,
                "address": output["address"],
                "block_height": info["block_height"],
                "is_spent": spender is not None,
                "consumed_by_tx": spender,
            }
        return result

    def confirmed_spender(self, ref, row, *, exclude):
        from .signing import ref_text

        txid = row.get("consumed_by_tx")
        if txid is None or txid == exclude:
            return None
        hash_hex(txid)
        info = self.transaction_info(txid)
        if not info:
            return None
        tx = self.transaction_cbor(txid)
        consumed = (
            tx.transaction_body.inputs
            if tx.valid
            else tx.transaction_body.collateral or []
        )
        if ref not in set(map(ref_text, consumed)):
            raise ProviderError("Dolos spender does not consume the reserved input")
        block, tip = self.block_at_height(info["block_height"]), self.tip()
        if (
            not block
            or block["hash"] != info["block_hash"]
            or tip["block_no"] - info["block_height"] + 1 < self.profile.confirmations
        ):
            return None
        return {
            "txid": txid,
            "consumed": ref,
            "block_hash": block["hash"],
            "block_height": info["block_height"],
        }

    def submit(self, cbor_hex, *, deadline=None):
        from .signing import decode_transaction

        if not self.http.enable_testnet_submission or self.profile.name != "preprod":
            raise Unsupported(
                "Dolos submission requires explicit qualified Preprod execution"
            )
        tx = decode_transaction(cbor_hex)
        if not tx.valid or not tx.transaction_witness_set.vkey_witnesses:
            raise ProviderError("Submission requires a signed, valid transaction")
        self.verify_identity()
        self.tip()
        result = self.http.request(
            "tx/submit", raw=bytes.fromhex(cbor_hex), retry=False, deadline=deadline
        )
        expected = str(tx.transaction_body.id)
        if result != expected:
            raise UnknownSubmission(
                "Dolos submission hash mismatch; reconcile original transaction"
            )
        return expected

    def era_summaries(self):
        data = self.http.request("network/eras")
        if not isinstance(data, list) or not data:
            raise ProviderError("Dolos era history is unavailable")
        result = []
        try:
            for i, p in enumerate(data):
                start, end, params = p["start"], dict(p["end"]), p["parameters"]
                length = integer(params["slot_length"])
                if length < 1:
                    raise ProviderError("Dolos era has invalid slot length")
                if i == len(data) - 1:
                    # MiniBF ends the last era at the tip, not its forecast horizon.
                    # Bound extension by the ledger safe zone and one hour. Never unbounded.
                    # https://github.com/txpipe/dolos/blob/v2.0.0-alpha.0/crates/minibf/src/routes/network.rs
                    horizon = min(integer(params["safe_zone"]), 3600 // length)
                    end["slot"] = integer(end["slot"]) + horizon
                    end["time"] = integer(end["time"]) + horizon * length
                    end["epoch"] = integer(start["epoch"]) + (
                        end["slot"] - integer(start["slot"])
                    ) // integer(params["epoch_length"])
                if start["slot"] == end["slot"]:
                    continue  # Simultaneous historical hard forks have zero duration.

                def bound(value):
                    return {
                        "slot": integer(value["slot"]),
                        "epoch": integer(value["epoch"]),
                        "time": {"seconds": integer(value["time"])},
                    }

                result.append(
                    {
                        "start": bound(start),
                        "end": bound(end),
                        "parameters": {
                            "slotLength": {"milliseconds": length * 1000},
                            "epochLength": integer(params["epoch_length"]),
                            "safeZone": integer(params["safe_zone"]),
                        },
                    }
                )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            raise ProviderError("Dolos era history is malformed") from error
        from .slots import SlotClock

        SlotClock(self.profile, result)
        return result

    def protocol_parameters(self):
        """Keep complete raw cost arrays; the bundle cross-checks exact evaluator values."""
        p = record(self.http.request("epochs/latest/parameters"))
        try:
            major = integer(p["protocol_major_ver"])
            if major not in (9, 10, 11):
                raise Unsupported(
                    "Dolos protocol version requires renewed fee-rule qualification"
                )

            def ada(key):
                return {"ada": {"lovelace": integer(p[key])}}

            def size(key):
                return {"bytes": integer(p[key])}

            def fraction(key):
                return str(Fraction(str(p[key])))

            models = p["cost_models_raw"]
            if (
                not isinstance(models, dict)
                or not models
                or set(models) - {"PlutusV1", "PlutusV2", "PlutusV3"}
            ):
                raise ProviderError("Dolos raw protocol cost models are unavailable")
            result = {
                "minFeeCoefficient": integer(p["min_fee_a"]),
                "minFeeConstant": ada("min_fee_b"),
                "maxBlockBodySize": size("max_block_size"),
                "maxBlockHeaderSize": size("max_block_header_size"),
                "maxTransactionSize": size("max_tx_size"),
                "maxValueSize": size("max_val_size"),
                "stakeCredentialDeposit": ada("key_deposit"),
                "stakePoolDeposit": ada("pool_deposit"),
                "stakePoolPledgeInfluence": fraction("a0"),
                "monetaryExpansion": fraction("rho"),
                "treasuryExpansion": fraction("tau"),
                "minStakePoolCost": ada("min_pool_cost"),
                "minUtxoDepositCoefficient": integer(p["coins_per_utxo_size"]),
                "scriptExecutionPrices": {
                    "memory": fraction("price_mem"),
                    "cpu": fraction("price_step"),
                },
                "maxExecutionUnitsPerTransaction": {
                    "memory": integer(p["max_tx_ex_mem"]),
                    "cpu": integer(p["max_tx_ex_steps"]),
                },
                "maxExecutionUnitsPerBlock": {
                    "memory": integer(p["max_block_ex_mem"]),
                    "cpu": integer(p["max_block_ex_steps"]),
                },
                "collateralPercentage": integer(p["collateral_percent"]),
                "maxCollateralInputs": integer(p["max_collateral_inputs"]),
                "version": {"major": major, "minor": integer(p["protocol_minor_ver"])},
                "plutusCostModels": {"plutus:v" + k[-1]: v for k, v in models.items()},
                # Conway ledger constants omitted by MiniBF. Exact parity is mandatory
                # against the evaluator before building; changes fail closed.
                "minUtxoDepositConstant": {"ada": {"lovelace": 0}},
                "maxReferenceScriptsSizePerTransaction": {"bytes": 204800},
                "minFeeReferenceScripts": {
                    "base": float(p["min_fee_ref_script_cost_per_byte"]),
                    "range": 25600,
                    "multiplier": 1.2,
                },
            }
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            raise ProviderError(
                "Dolos protocol parameters are incomplete or invalid"
            ) from error
        from .chain_context import protocol_parameters

        protocol_parameters(result)
        return result
