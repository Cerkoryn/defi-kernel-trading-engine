"""Transaction-local Dendrite environment; no mutation of its global backend.

The pinned Dano contribution function is reused verbatim with a private globals
dictionary. Only deployment, clock and backend lookups are rebound. This narrow
compatibility seam can disappear when Dendrite accepts these dependencies.
"""

from copy import deepcopy
from types import FunctionType, SimpleNamespace

from charli3_dendrite.dataclasses.models import Assets, PoolSelector
from charli3_dendrite.dexs.amm import dano
from pycardano import Address, ScriptHash, plutus_script_hash

from .chain_context import to_utxo
from .domain import KernelError, OutRef, Unsupported
from .protocols import (
    DANO_CONFIG,
    DANO_HASH,
    DANO_PREPROD_HASH,
    decode_dano,
    dendrite_row,
    row_assets,
)

DANO_REFERENCE = {
    "mainnet": OutRef(
        "64d111b957e7d7848ffdde5149aa77fa4090a7fa1ad0ac108067900614848501", 0
    ),
    "preprod": OutRef(
        "2e19cca74e3badcab26aef7574aa1885ba97228a254ca227ba2f79f2b75fd136", 0
    ),
}


def protocol_epoch(profile, ms):
    if profile.name not in DANO_CONFIG:
        raise Unsupported("No Dano epoch configuration for this network")
    length = 432_000_000 if profile.name == "mainnet" else 1_800_000
    return (ms - 1_647_899_091_000) // length + 328


def clip_dano_validity(builder, profile, now, slot):
    """Intersect contributor deadlines with the observed Dano protocol epoch."""
    now_ms = int(now * 1000)
    length = 432_000_000 if profile.name == "mainnet" else 1_800_000
    start_ms = 1_647_899_091_000 + ((now_ms - 1_647_899_091_000) // length) * length
    lower = max(slot(now_ms) - 120, slot(start_ms), builder.validity_start or 0)
    upper = min(
        slot(now_ms) + 240,
        slot(start_ms + length) - 1,
        builder.ttl or 2**63,
    )
    if not lower <= slot(now_ms) < upper:
        raise KernelError(
            "No usable Dano validity interval; refresh near epoch boundary"
        )
    builder.validity_start, builder.ttl = lower, upper


# Replace the private globals/subclass bridge only after explicit dependencies qualify.
# https://github.com/Charli3-Official/charli3-dendrite/issues/224
class DanoSession:
    """One network, observation and clock per build, including all dependencies.

    Rows must be rechecked by the execution coordinator before evaluation/signing.
    Unknown rewards are an error whenever the pool requires a withdrawal.
    """

    def __init__(self, profile, rows, *, now: float, rewards=None):
        if profile.name not in DANO_CONFIG:
            raise Unsupported("No qualified Dano deployment manifest for this network")
        self.profile, self.now = profile, now
        self.rows = {}
        for row in rows:
            ref = OutRef(row["tx_hash"], row["tx_index"])
            if ref in self.rows:
                raise KernelError("Duplicate Dano session dependency")
            to_utxo(row, profile)  # Validate bytes, hash, value and network category.
            self.rows[ref] = deepcopy(row)
        self.rewards = dict(rewards or {})
        self.hash = DANO_HASH if profile.name == "mainnet" else DANO_PREPROD_HASH
        self.config_ref = DANO_CONFIG[profile.name]
        self.script_ref = DANO_REFERENCE[profile.name]

    def resolve(self, ref):
        if ref not in self.rows:
            raise KernelError(f"Missing Dano build dependency: {ref}")
        return to_utxo(self.rows[ref], self.profile)

    def script_reference(self, script_hash, *, required_ref=None):
        candidates = [
            ref
            for ref, row in self.rows.items()
            if (row.get("reference_script") or {}).get("hash") == script_hash
            and (required_ref is None or ref == required_ref)
        ]
        if not candidates:
            raise KernelError(f"Missing Dano reference script: {script_hash}")
        utxo = self.resolve(sorted(candidates, key=str)[0])
        if not isinstance(utxo.output.script, dano.PlutusV3Script):
            raise KernelError("Dano reference script must be Plutus V3")
        if str(plutus_script_hash(utxo.output.script)) != script_hash:
            raise KernelError("Dano reference script identity mismatch")
        return utxo

    def get_pool_in_tx(self, tx_hash, *, assets, addresses):
        rows = [
            row
            for ref, row in self.rows.items()
            if ref.tx_hash == tx_hash
            and row["address"] in addresses
            and all(row_assets(row, self.profile.name).get(a) == 1 for a in assets)
        ]
        if len(rows) != 1:
            raise KernelError("Dano pool identity is missing or ambiguous")
        return [SimpleNamespace(address=rows[0]["address"])]

    def get_stake_rewards(self, reward_address):
        reward = self.rewards.get(str(reward_address))
        if type(reward) is not int or reward < 0:
            raise KernelError("Dano staking rewards are unverified")
        return reward

    def epoch(self, ms):
        return protocol_epoch(self.profile, ms)

    def slot(self, ms):
        return self._slot_at_ms(ms)

    def contribute(self, builder, pool_ref, input_unit, quantity, output_unit, min_out):
        if getattr(builder.context, "profile", None) != self.profile:
            raise KernelError("Dano builder chain/wallet context mismatch")
        self._slot_at_ms = builder.context.slot_at_ms
        if any(type(q) is not int or q <= 0 for q in (quantity, min_out)):
            raise KernelError("Dano amounts must be positive integer base units")
        pool = self.resolve(pool_ref)
        if any(u.input == pool.input for u in builder.inputs):
            raise KernelError("Dano pool is already consumed by this plan")
        config = self.resolve(self.config_ref)
        if config.output.datum is None:
            raise KernelError("Dano config datum is missing")
        import cbor2

        from .protocols import checked_datum

        rate, fee = cbor2.loads(
            bytes.fromhex(checked_datum(self.rows[self.config_ref], 2))
        ).value
        if (
            type(rate) is not int
            or not 0 <= rate < 10000
            or type(fee) is not int
            or fee < 0
        ):
            raise KernelError("Invalid Dano protocol fee configuration")
        decoded = decode_dano(self.rows[pool_ref], self.profile, platform_fee_rate=rate)
        if {input_unit, output_unit} != {
            decoded.unit_a,
            decoded.unit_b,
        } or input_unit == output_unit:
            raise KernelError("Dano trade does not match the pool pair")
        # Upstream must reject below-minimum inputs before mutating the builder.
        # https://github.com/Charli3-Official/charli3-dendrite/issues/226
        minimum = (
            decoded._datum.min_x_change
            if input_unit == decoded.unit_a
            else decoded._datum.min_y_change
        )
        if quantity < minimum:
            raise KernelError(
                f"Dano input is below the pool's minimum change: {quantity} < {minimum}"
            )
        session = self

        class SessionState(dano.DanoCLMMState):
            @classmethod
            def dex_policy(cls):
                return [session.hash]

            @classmethod
            def pool_selector(cls):
                return PoolSelector(
                    addresses=[str(pool.output.address)], assets=[session.hash]
                )

            @classmethod
            def reference_utxo(cls):
                return session.script_reference(
                    session.hash, required_ref=session.script_ref
                )

            def _staking_reference(self, pool_address):
                address = Address.from_primitive(pool_address)
                stake = address.staking_part
                if not isinstance(stake, ScriptHash):
                    raise KernelError(
                        "Overdue Dano ADA pool requires a script stake credential"
                    )
                return session.script_reference(str(stake)), Address(
                    staking_part=stake, network=address.network
                )

        state = SessionState.model_validate(
            dendrite_row(self.rows[pool_ref], self.profile)
        )
        state.platform_fee_rate = rate
        # No fabricated fallback value bag: the upstream builder must resolve the
        # same pool input from its context. Compare the complete serialized output.
        actual = builder.context.utxo_by_tx_id(pool_ref.tx_hash, pool_ref.index)
        if actual is None or actual.output.to_cbor() != pool.output.to_cbor():
            raise KernelError("Dano pool changed between observation and construction")
        builder.reference_inputs.add(config)
        original = dano.DanoCLMMState.swap_utxo
        environment = {
            **original.__globals__,
            "get_backend": lambda: session,
            "time": SimpleNamespace(time=lambda: session.now),
            "_current_epoch_mainnet": session.epoch,
            "_current_slot_mainnet": session.slot,
            "_find_protocol_config": lambda b: next(
                (u for u in b.reference_inputs if u.input == config.input), None
            ),
            "PROTOCOL_CONFIG_OUT_REF_MAINNET": (
                self.config_ref.tx_hash,
                self.config_ref.index,
            ),
            "DANO_POOL_REWARD_ADDRESS_MAINNET": str(
                Address(
                    staking_part=ScriptHash(bytes.fromhex(self.hash)),
                    network=builder.context.network,
                )
            ),
        }
        contribute = FunctionType(
            original.__code__,
            environment,
            original.__name__,
            original.__defaults__,
            original.__closure__,
        )
        # Clip to one protocol epoch without extending other contributors' deadlines.
        # https://github.com/Charli3-Official/charli3-dendrite/issues/227
        clip_dano_validity(builder, self.profile, self.now, self.slot)
        output, _ = contribute(
            state,
            pool.output.address,
            Assets(root={input_unit: quantity}),
            Assets(root={output_unit: min_out}),
            tx_builder=builder,
        )
        # Dano's batch index assumes leading pool outputs; replace with a qualified helper.
        # https://github.com/Charli3-Official/charli3-dendrite/issues/12
        outputs = getattr(builder, "_kernel_dano_outputs", [])
        builder.outputs.insert(len(outputs), output)
        outputs.append(output)
        builder._kernel_dano_outputs = outputs
        return output
