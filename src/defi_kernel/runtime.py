"""Market configuration and wallet binding shared by shadow and execution."""

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pycardano import Address, Network, ScriptHash, VerificationKeyHash

from .domain import Asset, KernelError, OutRef
from .protocols import DEPLOYMENTS
from .strategy import Settings
from .trading import ExecutionLimits


@dataclass(frozen=True)
class Market:
    base: Asset
    pool: OutRef
    settings: Settings
    assumed_tx_fee_lovelace: int
    pool_nft: str | None = None
    execution: ExecutionLimits = field(default_factory=ExecutionLimits)

    @classmethod
    def load(cls, path, profile):
        value = json.loads(Path(path).read_text())
        required = {
            "chain_id",
            "network",
            "base_unit",
            "pool_tx_hash",
            "pool_index",
            "strategy",
            "assumed_tx_fee_lovelace",
        }
        if (
            not isinstance(value, dict)
            or not required <= value.keys()
            or value.keys() - required - {"pool_nft", "execution"}
        ):
            raise KernelError("Market configuration has missing or unknown fields")
        if value["chain_id"] != profile.chain_id or value["network"] != profile.name:
            raise KernelError("Market configuration chain identity mismatch")
        fee = value["assumed_tx_fee_lovelace"]
        if type(fee) is not int or fee < 0:
            raise KernelError("Invalid assumed transaction fee")
        if value.get("pool_nft") is not None:
            if Asset.from_unit(profile.name, value["pool_nft"]).unit == "lovelace":
                raise KernelError("Pool NFT must identify a native asset")
        return cls(
            Asset.from_unit(profile.name, value["base_unit"]),
            OutRef(value["pool_tx_hash"], value["pool_index"]),
            Settings(**value["strategy"]),
            fee,
            value.get("pool_nft"),
            ExecutionLimits(**value.get("execution", {})),
        )


@contextmanager
def run_lock(state_dir, profile):
    """One active process per wallet ID, including across network switches."""
    root = Path(state_dir) / "runs"
    if root.is_symlink():
        raise KernelError("Run-lock directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid():
        raise KernelError("Run-lock directory must belong to the current user")
    root.chmod(0o700)
    descriptor = os.open(
        root / f"{profile.wallet_id}.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    with os.fdopen(descriptor, "a+") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise KernelError("Run lock must be an owned regular file")
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise KernelError(
                "This wallet has an active run; stop it before switching network or starting another run"
            ) from None
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps({"network": profile.name, "chain_id": profile.chain_id})
            )
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def bind_wallet(profile, journal, market, wallet_address):
    """Bind a single owner to this network/wallet journal."""
    wallet = Address.from_primitive(wallet_address)
    category = (
        Network.MAINNET if profile.address_network == "mainnet" else Network.TESTNET
    )
    if wallet.network != category or not isinstance(
        wallet.payment_part, VerificationKeyHash
    ):
        raise KernelError(
            "Wallet must be a payment-key address on the selected network"
        )
    if not isinstance(wallet.staking_part, VerificationKeyHash):
        raise KernelError(
            "Owner strategy requires a stake key credential; registration/delegation is not required"
        )
    if market.base.network != profile.name:
        raise KernelError("Market asset network mismatch")
    journal.bind_watch_wallet(str(wallet))
    return wallet, Address(
        ScriptHash(bytes.fromhex(DEPLOYMENTS["swaps-v1"]["script_hash"])),
        wallet.staking_part,
        category,
    )
