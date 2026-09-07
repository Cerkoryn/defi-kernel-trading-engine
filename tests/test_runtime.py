from copy import deepcopy
from pathlib import Path

import pytest
from pycardano import Address, VerificationKeyHash
from test_engine import MARKET, OWNER, PROFILE, Provider, engine

from defi_kernel.domain import KernelError
from defi_kernel.journal import Journal
from defi_kernel.runtime import Market, bind_wallet, run_lock
from defi_kernel.trading import observe_pool


def test_wallet_binding_and_run_lock_prevent_context_switch(tmp_path):
    from defi_kernel.config import load_profile

    journal = Journal(PROFILE.state_path(tmp_path), PROFILE)
    bind_wallet(PROFILE, journal, MARKET, str(OWNER))
    wrong = Address(VerificationKeyHash(b"x" * 28), OWNER.staking_part, OWNER.network)
    with pytest.raises(KernelError, match="different address"):
        bind_wallet(PROFILE, journal, MARKET, str(wrong))
    mainnet = load_profile(Path("config.example.toml"), "mainnet")
    with run_lock(tmp_path, PROFILE):
        with (
            pytest.raises(KernelError, match="active run"),
            run_lock(tmp_path, mainnet),
        ):
            pass
    with run_lock(tmp_path, mainnet):
        pass
    alias_root = tmp_path / "alias"
    alias_root.mkdir()
    (alias_root / "runs").symlink_to(tmp_path / "runs")
    with pytest.raises(KernelError, match="symlink"), run_lock(alias_root, PROFILE):
        pass
    journal.close()


def test_pool_continuation_is_selected_by_nft_and_ambiguity_pauses():
    provider = Provider()
    provider.rows[0]["tx_hash"] = "c" * 64
    market = observe_pool(provider, MARKET)
    assert market.quotes(MARKET)[0].dependencies[0].tx_hash == "c" * 64
    original = provider.scan
    from dataclasses import replace

    def duplicate(endpoint, body):
        observation = original(endpoint, body)
        return replace(
            observation, rows=(*observation.rows, deepcopy(observation.rows[0]))
        )

    provider.scan = duplicate
    with pytest.raises(KernelError, match="ambiguous"):
        observe_pool(provider, MARKET)


def test_shared_engine_journals_pause_and_stops_without_cancelling(tmp_path):
    runner, provider, journal, _ = engine(tmp_path)

    def unavailable():
        raise KernelError("Unavailable chain evidence")

    runner.reconcile = unavailable
    runner.sleep = lambda _: journal.request_stop()
    runner.run()
    status = journal.status()
    assert status["run"]["state"] == "stopped"
    assert status["latest_shadow"]["decision"]["action"] == "paused"
    assert status["shadow_decisions"] == 1
    assert not runner.cancelling()
    journal.close()


def test_market_cannot_be_reinterpreted_on_mainnet():
    from defi_kernel.config import load_profile

    with pytest.raises(KernelError, match="chain identity"):
        Market.load(
            Path("examples/preprod-mvp.json"),
            load_profile(Path("config.example.toml"), "mainnet"),
        )
