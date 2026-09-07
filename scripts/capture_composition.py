"""Read-only capture of current Dano dependencies for the recorded market."""

import json
import time
from pathlib import Path

from pycardano import Address

from defi_kernel.config import load_profile
from defi_kernel.dendrite_bridge import DANO_REFERENCE
from defi_kernel.domain import OutRef
from defi_kernel.protocols import DANO_CONFIG
from defi_kernel.providers import Koios

profile = load_profile(Path("config.example.toml"), "preprod")
fixture = json.loads(Path("examples/preprod-market.json").read_text())
provider = Koios(profile)
try:
    identity = provider.verify_identity()
    before = provider.tip()
    refs = [
        OutRef(fixture["pool"]["tx_hash"], fixture["pool"]["tx_index"]),
        DANO_REFERENCE[profile.name],
        DANO_CONFIG[profile.name],
    ]
    rows = provider.utxos(refs)
    if len(rows) != 3:
        raise RuntimeError(
            "Selected fixture pool or manifest reference is no longer unspent"
        )
    address = Address.from_primitive(rows[0]["address"])
    reward_address = str(
        Address(staking_part=address.staking_part, network=address.network)
    )
    rows.append(provider.reference_script(str(address.staking_part)))
    rewards = {reward_address: provider.stake_rewards(reward_address)}
    Path("evidence/preprod-composition-dependencies.json").write_text(
        json.dumps(
            {
                "endpoint": profile.koios_url,
                "observed_at": time.time(),
                "chain_id": profile.chain_id,
                "identity": identity,
                "tip_before": before,
                "tip_after": provider.tip(),
                "atomic_snapshot": False,
                "rows": rows,
                "rewards": rewards,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        "Captured",
        len(rows),
        "verified unspent dependencies and stake rewards; no transaction submitted",
    )
finally:
    provider.close()
