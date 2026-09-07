"""Transaction-scoped PyCardano bridge using the shared Koios capabilities."""

from fractions import Fraction

from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.utility import asset_to_value
from pycardano import (
    Address,
    ChainContext,
    DatumHash,
    ExecutionUnits,
    GenesisParameters,
    Network,
    PlutusScript,
    ProtocolParameters,
    TransactionInput,
    TransactionOutput,
    UTxO,
    plutus_script_hash,
)
from pycardano.serialization import RawCBOR

from .domain import KernelError, OutRef
from .protocols import row_assets
from .slots import SlotClock


def protocol_parameters(p):
    """Preserve every cost-model parameter, including newly appended entries.

    Zero-padded ordinal keys preserve ledger array order even for V1's sorted
    language view. Do not zip against PyCardano's older fixed parameter names.
    """
    models = {}
    for language, values in p["plutusCostModels"].items():
        if language not in ("plutus:v1", "plutus:v2", "plutus:v3"):
            raise KernelError("Unsupported Plutus language in protocol parameters")
        if not isinstance(values, list) or any(type(v) is not int for v in values):
            raise KernelError("Invalid protocol cost model")
        models[language.replace("plutus:v", "PlutusV")] = {
            f"{i:06d}": v for i, v in enumerate(values)
        }
    return ProtocolParameters(
        min_fee_constant=p["minFeeConstant"]["ada"]["lovelace"],
        min_fee_coefficient=p["minFeeCoefficient"],
        max_block_size=p["maxBlockBodySize"]["bytes"],
        max_tx_size=p["maxTransactionSize"]["bytes"],
        max_block_header_size=p["maxBlockHeaderSize"]["bytes"],
        key_deposit=p["stakeCredentialDeposit"]["ada"]["lovelace"],
        pool_deposit=p["stakePoolDeposit"]["ada"]["lovelace"],
        pool_influence=Fraction(p["stakePoolPledgeInfluence"]),
        monetary_expansion=Fraction(p["monetaryExpansion"]),
        treasury_expansion=Fraction(p["treasuryExpansion"]),
        decentralization_param=Fraction(0),
        extra_entropy="",
        protocol_major_version=p["version"]["major"],
        protocol_minor_version=p["version"]["minor"],
        min_utxo=p["minUtxoDepositConstant"]["ada"]["lovelace"],
        min_pool_cost=p["minStakePoolCost"]["ada"]["lovelace"],
        price_mem=Fraction(p["scriptExecutionPrices"]["memory"]),
        price_step=Fraction(p["scriptExecutionPrices"]["cpu"]),
        max_tx_ex_mem=p["maxExecutionUnitsPerTransaction"]["memory"],
        max_tx_ex_steps=p["maxExecutionUnitsPerTransaction"]["cpu"],
        max_block_ex_mem=p["maxExecutionUnitsPerBlock"]["memory"],
        max_block_ex_steps=p["maxExecutionUnitsPerBlock"]["cpu"],
        max_val_size=p["maxValueSize"]["bytes"],
        collateral_percent=p["collateralPercentage"],
        max_collateral_inputs=p["maxCollateralInputs"],
        coins_per_utxo_word=p["minUtxoDepositCoefficient"] * 8,
        coins_per_utxo_byte=p["minUtxoDepositCoefficient"],
        cost_models=models,
        maximum_reference_scripts_size=p["maxReferenceScriptsSizePerTransaction"],
        min_fee_reference_scripts=p["minFeeReferenceScripts"],
    )


def to_utxo(row, profile):
    if row.get("is_spent") is not False:
        raise KernelError("Cannot construct an input from unverified/spent state")
    address = Address.from_primitive(row["address"])
    category = (
        Network.MAINNET if profile.address_network == "mainnet" else Network.TESTNET
    )
    if address.network != category:
        raise KernelError("UTxO address network mismatch")
    ref_script = row.get("reference_script")
    script = None
    if ref_script:
        kind = ref_script["type"]
        if kind not in ("plutusV1", "plutusV2", "plutusV3"):
            raise KernelError("Native reference script conversion not implemented")
        script = PlutusScript.from_version(
            int(kind[-1]), bytes.fromhex(ref_script["bytes"])
        )
        if str(plutus_script_hash(script)) != ref_script["hash"]:
            raise KernelError("Reference script hash mismatch")
    inline = (row.get("inline_datum") or {}).get("bytes")
    datum = RawCBOR(bytes.fromhex(inline)) if inline else None
    if datum is not None:
        from pycardano import datum_hash

        if str(datum_hash(datum)) != row["datum_hash"]:
            raise KernelError("Input datum hash mismatch")
    return UTxO(
        TransactionInput.from_primitive(
            [bytes.fromhex(row["tx_hash"]), row["tx_index"]]
        ),
        TransactionOutput(
            address,
            asset_to_value(Assets(root=row_assets(row, profile.name))),
            datum_hash=DatumHash(bytes.fromhex(row["datum_hash"]))
            if row.get("datum_hash") and datum is None
            else None,
            datum=datum,
            script=script,
        ),
    )


class KoiosChainContext(ChainContext):
    """Create a fresh context per plan; cached parameters never cross builds."""

    def __init__(self, provider):
        self.provider = provider
        self.profile = provider.profile
        self._genesis = provider.verify_identity()
        self._tip = provider.tip()
        self.slot_clock = SlotClock(
            self.profile, provider.rpc("queryLedgerState/eraSummaries")
        )
        self._parameters = protocol_parameters(
            provider.rpc("queryLedgerState/protocolParameters")
        )

    @property
    def protocol_param(self):
        return self._parameters

    @property
    def network(self):
        return (
            Network.MAINNET
            if self.provider.profile.address_network == "mainnet"
            else Network.TESTNET
        )

    @property
    def epoch(self):
        return int(self._tip["epoch_no"])

    @property
    def last_block_slot(self):
        return int(self._tip["abs_slot"])

    @property
    def genesis_param(self):
        p = self._genesis
        return GenesisParameters(
            Fraction(p["activeslotcoeff"]),
            int(p["updatequorum"]),
            int(p["maxlovelacesupply"]),
            int(p["networkmagic"]),
            int(p["epochlength"]),
            int(p["systemstart"]),
            int(p["slotsperkesperiod"]),
            int(p["slotlength"]),
            int(p["maxkesrevolutions"]),
            int(p["securityparam"]),
        )

    def _utxos(self, address):
        observation = self.provider.scan(
            "address_utxos", {"_addresses": [address], "_extended": True}
        )
        return [to_utxo(r, self.provider.profile) for r in observation.rows]

    def slot_at_ms(self, timestamp):
        return self.slot_clock.slot_at_ms(timestamp)

    def utxo_by_tx_id(self, tx_hash, index):
        rows = self.provider.utxos([OutRef(tx_hash, index)])
        return to_utxo(rows[0], self.provider.profile) if rows else None

    def evaluate_tx_cbor(self, cbor):
        rows = self.provider.evaluate(cbor.hex() if isinstance(cbor, bytes) else cbor)
        purposes = {
            "spend": "spend",
            "mint": "mint",
            "withdraw": "withdrawal",
            "publish": "certificate",
        }
        result = {}
        for row in rows:
            validator = row["validator"]
            if validator["purpose"] not in purposes:
                raise KernelError("Unsupported evaluated script purpose")
            key = f"{purposes[validator['purpose']]}:{validator['index']}"
            if key in result:
                raise KernelError("Duplicate evaluation budget")
            result[key] = ExecutionUnits(row["budget"]["memory"], row["budget"]["cpu"])
        return result

    def submit_tx_cbor(self, cbor):
        return self.provider.submit(cbor.hex() if isinstance(cbor, bytes) else cbor)
