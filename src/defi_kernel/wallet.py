"""Disposable test-wallet creation. Secret material never leaves local files."""

import json
import os
import stat
from pathlib import Path

from pycardano import Address, Network, PaymentSigningKey, StakeSigningKey

from .domain import KernelError


def create_test_wallet(profile, state_dir):
    if (
        profile.name not in ("preprod", "preview")
        or profile.address_network != "testnet"
    ):
        raise KernelError("Disposable wallet creation is restricted to public testnets")
    root = profile.state_path(Path(state_dir)).parent / "wallet"
    root.parent.mkdir(parents=True, exist_ok=True)
    # Refuse an existing/partial wallet; never regenerate keys behind an address.
    root.mkdir(mode=0o700)
    payment, stake = PaymentSigningKey.generate(), StakeSigningKey.generate()
    address = Address(
        payment.to_verification_key().hash(),
        stake.to_verification_key().hash(),
        Network.TESTNET,
    )
    manifest = {
        "purpose": "disposable runtime integration tests",
        "network": profile.name,
        "chain_id": profile.chain_id,
        "wallet_id": profile.wallet_id,
        "address": str(address),
        "payment_key": "payment.skey",
        "stake_key": "stake.skey",
    }
    # Bypass SDK save until exclusive private writes qualify; retain fsync and read checks.
    # https://github.com/Python-Cardano/pycardano/pull/495
    for name, content in [
        ("payment.skey", payment.to_json()),
        ("stake.skey", stake.to_json()),
        ("wallet.json", json.dumps(manifest, indent=2) + "\n"),
        ("address.txt", str(address) + "\n"),
    ]:
        descriptor = os.open(
            root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    return root / "wallet.json", manifest


def load_wallet(profile, path):
    path = Path(path)
    manifest = json.loads(path.read_text())
    for field in ("payment_key", "stake_key"):
        name = manifest.get(field)
        if (
            not isinstance(name, str)
            or not name.endswith(".skey")
            or Path(name).name != name
        ):
            raise KernelError("Wallet keys must be local .skey filenames")
    if (manifest["chain_id"], manifest["wallet_id"], manifest["network"]) != (
        profile.chain_id,
        profile.wallet_id,
        profile.name,
    ):
        raise KernelError("Wallet manifest chain/account mismatch")
    address = Address.from_primitive(manifest["address"])
    category = (
        Network.MAINNET if profile.address_network == "mainnet" else Network.TESTNET
    )
    if address.network != category:
        raise KernelError("Wallet address network mismatch")
    return manifest, path.parent


def load_private_key(path, key_type):
    """Read only an owned, private regular file; never echo secret parser input."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise KernelError(
                    "Signing key must be an owned regular file with mode 0600 or 0400"
                )
            content = handle.read(16385)
            if len(content) > 16384:
                raise KernelError("Signing key file exceeds size limit")
            return key_type.from_json(content)
    except KernelError:
        raise
    except Exception:
        raise KernelError(
            "Cannot read private signing key; check its path, permissions and format"
        ) from None
