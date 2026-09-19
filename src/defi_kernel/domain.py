"""Exact execution units and observations with explicit provenance."""

import math
import re
from dataclasses import dataclass
from fractions import Fraction


class KernelError(Exception):
    """An operator-actionable failure."""


class Unsupported(KernelError):
    pass


class RequestTooLarge(KernelError):
    """A local capacity rejection, not a provider outage or a retryable request."""

    reason_code = "request_size"

    def __init__(self, actual, limit):
        self.actual, self.limit = actual, limit
        super().__init__(
            f"Request is {actual} bytes; provider request limit is {limit} bytes"
        )


@dataclass(frozen=True)
class Asset:
    network: str
    policy: bytes = b""
    name: bytes = b""

    def __post_init__(self):
        if len(self.policy) not in (0, 28) or len(self.name) > 32:
            raise ValueError("Invalid asset identity")
        if not self.policy and self.name:
            raise ValueError("ADA has an empty policy and name")

    @classmethod
    def from_unit(cls, network: str, unit: str):
        if unit == "lovelace":
            return cls(network)
        if not re.fullmatch(r"[0-9a-f]{56}([0-9a-f]{2}){0,32}", unit):
            raise ValueError("Expected policy ID followed by raw asset-name hex")
        return cls(network, bytes.fromhex(unit[:56]), bytes.fromhex(unit[56:]))

    @property
    def unit(self):
        return self.policy.hex() + self.name.hex() if self.policy else "lovelace"


@dataclass(frozen=True, order=True)
class OutRef:
    tx_hash: str
    index: int

    def __post_init__(self):
        if (
            not re.fullmatch(r"[0-9a-f]{64}", self.tx_hash)
            or type(self.index) is not int
            or self.index < 0
        ):
            raise ValueError("Invalid transaction output reference")

    def __str__(self):
        return f"{self.tx_hash}#{self.index}"


@dataclass(frozen=True)
class Observation:
    network: str
    provider: str
    observed_at: float
    rows: tuple[dict, ...]
    # Completion of REST traversal does not imply a ledger snapshot.
    complete: bool
    tip_before: dict | None = None
    tip_after: dict | None = None


@dataclass(frozen=True)
class Order:
    ref: OutRef
    address: str
    offer: Asset
    ask: Asset
    price: Fraction
    held_offer: int
    lovelace: int
    datum_cbor: str
    # held_offer includes any carrier ADA; execution must calculate min-ADA.
    previous: OutRef | None = None
    expires_at_ms: int | None = None


@dataclass(frozen=True)
class Quote:
    input_asset: Asset
    output_asset: Asset
    amount_in: int
    amount_out: int
    fee_lovelace: int
    dependencies: tuple[OutRef, ...]
    observed_at: float
    settlement: str = "atomic"

    def __post_init__(self):
        if type(self.observed_at) not in (int, float) or not math.isfinite(
            self.observed_at
        ):
            raise ValueError("Quote observation time must be finite")
        if any(
            type(q) is not int
            for q in (self.amount_in, self.amount_out, self.fee_lovelace)
        ):
            raise ValueError("Quote quantities must be integer base units")
        if self.input_asset.network != self.output_asset.network:
            raise ValueError("Cross-network quote")
        if min(self.amount_in, self.amount_out) <= 0 or self.fee_lovelace < 0:
            raise ValueError("Non-positive quote or negative fee")


def ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def json_value(value):
    """Stable JSON representation for raw identifiers and exact prices."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    return str(value)
