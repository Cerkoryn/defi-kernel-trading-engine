import copy
import json
from pathlib import Path

import pytest
from charli3_dendrite.dexs.ob import cardanoswaps
from pycardano import (
    Address,
    Network,
    PlutusV2Script,
    PlutusV3Script,
    ScriptHash,
    VerificationKeyHash,
    plutus_script_hash,
)

from defi_kernel.config import load_profile
from defi_kernel.domain import KernelError
from defi_kernel.protocols import DEPLOYMENTS, decode_dano, decode_swaps, row_assets


def evidence(name):
    return json.loads(Path(f"evidence/{name}.json").read_text())["data"]


@pytest.mark.parametrize(
    "network,expected", [("mainnet", 10), ("preprod", 58), ("preview", 3)]
)
def test_real_v1_orders_decode(network, expected):
    profile = load_profile(Path("config.example.toml"), network)
    rows = evidence(network + "-swaps-v1-scan")["rows"]
    assert len(rows) == expected
    rejected = []
    for row in rows:
        try:
            order = decode_swaps(row, profile)
        except KernelError as e:
            rejected.append(str(e))
            continue
        assert order.offer.network == network
        assert order.price > 0
        assert order.address == row["address"]
    if network == "preprod":
        assert (
            rejected
            == ["Reference-script output without an inline order/pool datum"] * 2
        )
    else:
        assert rejected == []


def test_order_does_not_require_stake_registration_or_delegation():
    """Decoder needs no stake-account lookup; a key credential alone is enough."""
    profile = load_profile(Path("config.example.toml"), "preprod")
    row = copy.deepcopy(evidence("preprod-swaps-v1-scan")["rows"][0])
    row["address"] = str(
        Address(
            ScriptHash(bytes.fromhex(DEPLOYMENTS["swaps-v1"]["script_hash"])),
            VerificationKeyHash(b"z" * 28),
            Network.TESTNET,
        )
    )
    row["stake_address"] = None  # No registration/delegation observation supplied.
    assert decode_swaps(row, profile).address == row["address"]
    row["address"] = str(
        Address(
            ScriptHash(bytes.fromhex(DEPLOYMENTS["swaps-v1"]["script_hash"])),
            network=Network.TESTNET,
        )
    )
    with pytest.raises(KernelError, match="delegation is not required"):
        decode_swaps(row, profile)


@pytest.mark.parametrize("value", [1.9, True, "1.9", "-1"])
def test_provider_fractional_quantities_are_not_truncated(value):
    with pytest.raises(KernelError, match="integer"):
        row_assets({"value": value}, "preprod")


def test_beacon_and_datum_validation():
    profile = load_profile(Path("config.example.toml"), "preprod")
    original = evidence("preprod-swaps-v1-scan")["rows"][0]
    row = copy.deepcopy(original)
    row["asset_list"][0]["quantity"] = "2"
    with pytest.raises(KernelError, match="beacon"):
        decode_swaps(row, profile)
    row = copy.deepcopy(original)
    row["datum_hash"] = "0" * 64
    with pytest.raises(KernelError, match="hash mismatch"):
        decode_swaps(row, profile)
    with pytest.raises(KernelError, match="Address"):
        decode_swaps(original, profile, "swaps-v2")


def test_legacy_dendrite_is_distinct_historical_script():
    raw = Path("src/defi_kernel/data/swaps-legacy-expiration.hex").read_text().strip()
    assert raw == cardanoswaps.SWAP_VALIDATOR_SCRIPT_HEX
    assert (
        DEPLOYMENTS["swaps-legacy-expiration"]["source_commit"]
        == "75aa2e0f304ab00feb09608c8187140f20cdf74b"
    )
    assert (
        evidence("mainnet-swaps-legacy-expiration-beacon-script")["bytes"]
        == cardanoswaps.BEACON_POLICY_SCRIPT_HEX
    )
    assert cardanoswaps.SWAP_VALIDATOR_HASH != DEPLOYMENTS["swaps-v2"]["script_hash"]


@pytest.mark.parametrize("name", list(DEPLOYMENTS))
def test_packaged_script_hashes(name):
    manifest = DEPLOYMENTS[name]
    cls = PlutusV2Script if manifest["plutus_version"] == 2 else PlutusV3Script
    raw = bytes.fromhex(Path(f"src/defi_kernel/data/{name}.hex").read_text().strip())
    assert str(plutus_script_hash(cls(raw))) == manifest["script_hash"]
    live = evidence("preprod-" + name + "-spend-script")
    assert raw.hex() == live["bytes"]


@pytest.mark.parametrize("network", ["mainnet", "preprod"])
def test_dano_decodes_real_pools_and_rejects_lp_positions(network):
    profile = load_profile(Path("config.example.toml"), network)
    rows = evidence(network + "-dano-scan")["rows"]
    decoded = rejected = 0
    for row in rows:
        try:
            state = decode_dano(row, profile, platform_fee_rate=1000)
            assert min(state.reserve_a, state.reserve_b) >= 0
            assert state.dex_nft.quantity() == 1
            decoded += 1
        except KernelError:
            rejected += 1
    assert decoded >= 40 and rejected > 0
