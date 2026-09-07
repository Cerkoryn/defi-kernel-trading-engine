"""Capture public, read-only evidence using the runtime's provider implementation.

Usage: python scripts/capture_public.py --network preprod
"""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from defi_kernel.config import load_profile
from defi_kernel.protocols import DANO_CONFIG, DANO_HASH, DANO_PREPROD_HASH, DEPLOYMENTS
from defi_kernel.providers import Koios, ProviderError

parser = argparse.ArgumentParser()
parser.add_argument("--network", required=True)
parser.add_argument("--config", type=Path, default=Path("config.example.toml"))
parser.add_argument(
    "--config-only",
    action="store_true",
    help="Capture only Dano config after verifying chain identity",
)
args = parser.parse_args()
profile = load_profile(args.config, args.network)
provider = Koios(profile)
root = Path("evidence")
root.mkdir(exist_ok=True)


def save(name, data):
    (root / f"{profile.name}-{name}.json").write_text(
        json.dumps(
            {
                "endpoint": profile.koios_url,
                "observed_at": time.time(),
                "data": data,
            },
            indent=2,
        )
        + "\n"
    )


try:
    save("identity", provider.verify_identity())
    if args.config_only:
        save("dano-config", provider.utxos([DANO_CONFIG[profile.name]]))
        raise SystemExit(0)
    save("tip", provider.tip())
    for name, manifest in DEPLOYMENTS.items():
        observation = provider.credential_utxos(manifest["script_hash"])
        save(name + "-scan", asdict(observation))
        print(name, len(observation.rows), "complete REST traversal", flush=True)
        for role, script_hash in [
            ("spend", manifest["script_hash"]),
            ("beacon", manifest["beacon_policy"]),
        ]:
            try:
                save(f"{name}-{role}-script", provider.script_info(script_hash))
            except ProviderError as e:
                save(f"{name}-{role}-script", {"error": str(e)})
    if profile.name != "preview":
        h = DANO_HASH if profile.name == "mainnet" else DANO_PREPROD_HASH
        observation = provider.credential_utxos(h)
        save("dano-scan", asdict(observation))
        save("dano-script", provider.script_info(h))
        print("dano", len(observation.rows), "complete REST traversal", flush=True)
    if profile.name in DANO_CONFIG:
        save("dano-config", provider.utxos([DANO_CONFIG[profile.name]]))
    if profile.name == "mainnet":
        from defi_kernel.domain import OutRef

        save(
            "dano-reference",
            provider.utxos(
                [
                    OutRef(
                        "64d111b957e7d7848ffdde5149aa77fa4090a7fa1ad0ac108067900614848501",
                        0,
                    )
                ]
            ),
        )
    # Valid read request tests forwarding without submitting even an invalid tx.
    try:
        save("ogmios-tip", provider.rpc("queryNetwork/tip"))
    except ProviderError as e:
        save("ogmios-tip", {"error": str(e)})
finally:
    provider.close()
