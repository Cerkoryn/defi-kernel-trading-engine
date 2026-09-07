"""Named chain identities. A testnet address is not a chain identity."""

import math
import re
import tomllib
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from .domain import KernelError

KNOWN_CHAINS = {
    "mainnet": (764824073, 1506203091, "mainnet"),
    "preprod": (1, 1654041600, "testnet"),
    "preview": (2, 1666656000, "testnet"),
}


@dataclass(frozen=True)
class Profile:
    name: str
    network_magic: int
    system_start: int
    koios_url: str
    address_network: str
    wallet_id: str = "watch-only"
    token_env: str | None = None
    page_size: int = 100
    max_pages: int = 100
    request_interval: float = 0.25
    max_request_bytes: int = 1000
    confirmations: int = 3

    def __post_init__(self):
        if any(
            type(getattr(self, name)) is not int
            for name in (
                "network_magic",
                "system_start",
                "page_size",
                "max_pages",
                "max_request_bytes",
                "confirmations",
            )
        ):
            raise KernelError("Chain identity and provider limits require integers")
        if type(self.request_interval) not in (int, float) or not math.isfinite(
            self.request_interval
        ):
            raise KernelError("Request interval must be finite")
        if not 0 <= self.network_magic < 2**32 or self.system_start < 0:
            raise KernelError("Invalid chain identity parameters")
        for name in (self.name, self.wallet_id):
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                raise KernelError("Profile and wallet IDs must be simple names")
        url = urlsplit(self.koios_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise KernelError(
                "Provider requires an HTTPS URL without embedded credentials"
            )
        if self.address_network not in ("mainnet", "testnet"):
            raise KernelError("Invalid address network category")
        if self.name in KNOWN_CHAINS and KNOWN_CHAINS[self.name] != (
            self.network_magic,
            self.system_start,
            self.address_network,
        ):
            raise KernelError(
                "Named public network identity cannot be redefined; use a custom profile"
            )
        if (
            min(
                self.page_size,
                self.max_pages,
                self.max_request_bytes,
                self.confirmations,
            )
            < 1
            or self.request_interval < 0
        ):
            raise KernelError("Invalid provider limits or confirmation policy")

    @property
    def chain_id(self):
        return f"{self.network_magic}:{self.system_start}:{self.address_network}"

    def state_path(self, root: Path) -> Path:
        identity = sha256(self.chain_id.encode()).hexdigest()[:16]
        return root / f"{self.name}-{identity}" / self.wallet_id / "runtime.sqlite3"


def load_profile(path: Path, network: str | None = None) -> Profile:
    with path.open("rb") as f:
        config = tomllib.load(f)
    selected = network or config.get("default_network")
    if not selected or selected not in config.get("networks", {}):
        raise KernelError(
            "Select a configured --network; no mainnet fallback is allowed"
        )
    return Profile(name=selected, **config["networks"][selected])
