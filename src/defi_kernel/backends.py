"""Explicit capability routing. No provider failover and no provider-specific strategy I/O."""

from dataclasses import fields
from typing import Protocol

from .domain import Observation, OutRef
from .providers import Koios, ProviderChecks, ProviderError, ProviderLag


class ProviderIdentity(Protocol):
    """Every bound adapter must supply its own chain identity and anchor evidence."""

    def verify_identity(self) -> dict: ...
    def tip(self) -> dict: ...
    def block_at_height(self, height: int) -> dict | None: ...


class ChainQueries(Protocol):
    def verify_identity(self) -> dict: ...
    def tip(self) -> dict: ...
    def utxos(self, refs: list[OutRef]) -> list[dict]: ...


class LedgerQueries(Protocol):
    """Governance-derived state can require a different source from block/UTxO data."""

    def era_summaries(self) -> list[dict]: ...
    def protocol_parameters(self) -> dict: ...
    def stake_rewards_many(self, addresses: list[str]) -> dict[str, int]: ...


class IndexedDiscovery(Protocol):
    def address_utxos(self, address: str) -> Observation: ...
    def credential_utxos(self, credential: str) -> Observation: ...
    def asset_utxos(self, policy: str, name: str) -> Observation: ...
    def resolve_datums(self, rows: list[dict]) -> list[dict]: ...
    def reference_scripts(self, hashes: list[str]) -> dict[str, dict]: ...
    def script_info(self, script_hash: str) -> dict: ...


class Evaluation(Protocol):
    def protocol_parameters(self) -> dict: ...
    def evaluate(self, cbor_hex: str) -> list[dict]: ...


class Submission(Protocol):
    def submit(self, cbor_hex: str, *, deadline: float | None = None) -> str: ...


class ChainEvidence(Protocol):
    def transaction_info(self, tx_hash: str) -> dict | None: ...
    def transaction_cbor(self, tx_hash: str): ...
    def input_states(self, refs: list[str]) -> dict[str, dict]: ...
    def confirmed_spender(
        self, ref: str, row: dict, *, exclude: str
    ) -> dict | None: ...
    def block_at_height(self, height: int) -> dict | None: ...
    def blocks_at_heights(self, heights: list[int]) -> dict[int, dict | None]: ...
    def address_transactions(
        self, address: str, *, after_height: int = 0
    ) -> Observation: ...


_INTERFACES = {
    "chain": ChainQueries,
    "ledger": LedgerQueries,
    "index": IndexedDiscovery,
    "evaluation": Evaluation,
    "submission": Submission,
    "observation": ChainEvidence,
}
_ROUTES = {
    name: capability
    for capability, interface in _INTERFACES.items()
    for name in interface.__dict__
    if not name.startswith("_")
}


class ProviderBundle(ProviderChecks):
    def __init__(self, profile, providers):
        self.profile, self.providers = profile, providers
        bindings = {"ledger": profile.capabilities["chain"], **profile.capabilities}
        self.bindings = {c: providers[n] for c, n in bindings.items()}
        self.clock = self.bindings["chain"].clock
        for capability, interface in _INTERFACES.items():
            if any(
                not callable(getattr(self.bindings[capability], name, None))
                for required in (ProviderIdentity, interface)
                for name in required.__dict__
                if not name.startswith("_")
            ):
                raise ProviderError(f"Provider does not implement {capability}")

    def __getattr__(self, name):
        if name not in _ROUTES:
            raise AttributeError(name)
        return getattr(self.bindings[_ROUTES[name]], name)

    @property
    def observer(self):
        return self.bindings["chain"].observer

    @observer.setter
    def observer(self, value):
        for provider in self.providers.values():
            provider.observer = value

    def close(self):
        for provider in self.providers.values():
            provider.close()

    def verify_identity(self):
        # Reconciliation must remain usable while ledger/evaluation/submission is down.
        data = self.bindings["chain"].verify_identity()
        for p in {self.bindings[c] for c in ("index", "observation")} - {
            self.bindings["chain"]
        }:
            p.verify_identity()
            self._check_anchor(p)
        return data

    def confirmed_spender(self, ref, row, *, exclude):
        return self.bindings["observation"].confirmed_spender(ref, row, exclude=exclude)

    def _evaluation_anchor(self):
        self._check_bound("ledger", "evaluation")

    def _check_bound(self, *capabilities):
        for peer in dict.fromkeys(self.bindings[c] for c in capabilities):
            if peer is not self.bindings["chain"]:
                peer.verify_identity()
                self._check_anchor(peer)

    def era_summaries(self):
        self._check_bound("ledger")
        return self.bindings["ledger"].era_summaries()

    def stake_rewards_many(self, addresses):
        self._check_bound("ledger")
        return self.bindings["ledger"].stake_rewards_many(addresses)

    def _check_anchor(self, peer):
        chain = self.bindings["chain"]
        local, remote = chain.tip(), peer.tip()
        age = self.clock() - remote["block_time"]
        if not 0 <= age <= 300:
            raise ProviderLag(
                "Bound provider chain tip is not fresh; waiting for synchronization"
            )
        height = min(local["block_no"], remote["block_no"])
        a, b = chain.block_at_height(height), peer.block_at_height(height)
        if not a or not b or a["hash"] != b["hash"]:
            raise ProviderLag(
                "Bound providers disagree on the canonical anchor; waiting for synchronization"
            )

    def protocol_parameters(self):
        from .chain_context import protocol_parameters

        self._evaluation_anchor()
        ledger, evaluator = self.bindings["ledger"], self.bindings["evaluation"]
        local = ledger.protocol_parameters()
        if evaluator is not ledger:
            remote = evaluator.protocol_parameters()
            a, b = protocol_parameters(local), protocol_parameters(remote)
            # MiniBF rounds some rational values. Never build with prices or cost
            # models that disagree with the unsigned evaluator's exact parameters.
            differences = [
                f.name for f in fields(a) if getattr(a, f.name) != getattr(b, f.name)
            ]
            if differences:
                raise ProviderError(
                    "Ledger/evaluator protocol parameters differ "
                    f"({', '.join(differences)}); new builds paused"
                )
        return local

    def evaluate(self, cbor_hex):
        self._evaluation_anchor()
        return self.bindings["evaluation"].evaluate(cbor_hex)

    def submit(self, cbor_hex, *, deadline=None):
        self._check_bound("submission")
        return self.bindings["submission"].submit(cbor_hex, deadline=deadline)


def create_provider(profile, **kwargs):
    """Legacy configuration selects Koios; explicit bindings select named adapters."""
    if not profile.providers:
        return Koios(profile, **kwargs)
    providers = {}
    try:
        for name in dict.fromkeys(profile.capabilities.values()):
            setting = profile.providers[name]
            if setting.kind == "koios":
                providers[name] = Koios(
                    profile,
                    base_url=setting.url,
                    credential_env=setting.token_env,
                    request_interval=setting.request_interval,
                    **kwargs,
                )
            else:
                from .dolos import Dolos

                providers[name] = Dolos(profile, setting, **kwargs)
        return ProviderBundle(profile, providers)
    except Exception:
        for provider in providers.values():
            provider.close()
        raise
