"""Named chain identities. A testnet address is not a chain identity."""

import math
import re
import tomllib
from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit

from .domain import KernelError

KNOWN_CHAINS = {
    "mainnet": (764824073, 1506203091, "mainnet"),
    "preprod": (1, 1654041600, "testnet"),
    "preview": (2, 1666656000, "testnet"),
}

CAPABILITIES = frozenset({"chain", "index", "evaluation", "submission", "observation"})


def provider_url(value, *, allow_private_http=False):
    """Cleartext is an explicit opt-in for literal private/loopback addresses only."""
    if not isinstance(value, str):
        raise KernelError("Provider URL requires a string")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise KernelError("Invalid provider URL port") from None
    try:
        address = ip_address(url.hostname or "")
        private = address.is_loopback or any(
            address in ip_network(cidr)
            for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        )
    except ValueError:
        private = False
    if (
        any(c.isspace() for c in value)
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or (port is not None and port == 0)
        or not (
            url.scheme == "https"
            or allow_private_http
            and private
            and url.scheme == "http"
        )
    ):
        raise KernelError(
            "Provider requires HTTPS; private literal HTTP needs allow_private_http=true (no embedded credentials)"
        )
    return value


@dataclass(frozen=True)
class ProviderSettings:
    kind: str
    url: str
    minikupo_url: str | None = None
    token_env: str | None = None
    allow_private_http: bool = False
    request_interval: float = 0
    max_tip_age_seconds: int = 300
    reference_scripts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in ("koios", "dolos"):
            raise KernelError("Unsupported provider kind")
        if type(self.allow_private_http) is not bool:
            raise KernelError("allow_private_http requires a boolean")
        provider_url(self.url, allow_private_http=self.allow_private_http)
        if self.kind == "dolos":
            if not self.minikupo_url or self.token_env:
                raise KernelError(
                    "Dolos requires minikupo_url and does not accept hosted credentials"
                )
            provider_url(self.minikupo_url, allow_private_http=self.allow_private_http)
        elif self.minikupo_url or self.reference_scripts:
            raise KernelError("Koios does not use MiniKupo or reference hints")
        if self.token_env is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.token_env
        ):
            raise KernelError("token_env must name an environment variable")
        if (
            type(self.request_interval) not in (int, float)
            or not math.isfinite(self.request_interval)
            or self.request_interval < 0
            or type(self.max_tip_age_seconds) is not int
            or self.max_tip_age_seconds < 1
        ):
            raise KernelError("Invalid provider timing bounds")
        if not isinstance(self.reference_scripts, dict) or any(
            not re.fullmatch(r"[0-9a-f]{56}", h)
            or not re.fullmatch(r"[0-9a-f]{64}#[0-9]+", ref)
            for h, ref in self.reference_scripts.items()
        ):
            raise KernelError("Invalid reference-script lookup hints")


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
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    capabilities: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.token_env is not None and (
            not isinstance(self.token_env, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.token_env)
        ):
            raise KernelError(
                "token_env must name an environment variable, not contain a credential"
            )
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
        if self.providers:
            if self.koios_url or self.token_env:
                raise KernelError(
                    "Use provider-specific URLs and credentials with capability bindings"
                )
            if not (
                CAPABILITIES <= self.capabilities.keys() <= CAPABILITIES | {"ledger"}
            ):
                raise KernelError(
                    "Explicit providers require all five capability bindings; ledger is optional"
                )
            if any(
                not re.fullmatch(r"[a-zA-Z0-9_-]+", n)
                or not isinstance(p, ProviderSettings)
                for n, p in self.providers.items()
            ):
                raise KernelError("Invalid named provider")
            if not set(self.capabilities.values()) <= self.providers.keys():
                raise KernelError("Capability references an unknown provider")
            if self.providers[self.capabilities["evaluation"]].kind != "koios":
                raise KernelError(
                    "Dolos unsigned evaluation is not qualified; select Koios evaluation"
                )
        else:
            if self.capabilities:
                raise KernelError("Capability bindings require named providers")
            provider_url(self.koios_url)
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
    data = dict(config["networks"][selected])
    try:
        if "providers" in data:
            data["providers"] = {
                n: ProviderSettings(**p) for n, p in data["providers"].items()
            }
            data.setdefault("koios_url", "")
        return Profile(name=selected, **data)
    except (TypeError, ValueError, AttributeError) as error:
        raise KernelError("Invalid network/provider configuration") from error
