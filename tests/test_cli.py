from pathlib import Path

from typer.testing import CliRunner

from defi_kernel.cli import app
from defi_kernel.config import load_profile
from defi_kernel.simulation import fixture_decision


def test_recorded_reference_strategy_produces_both_sides():
    p = load_profile(Path("config.example.toml"), "preprod")
    result = fixture_decision(Path("examples/preprod-market.json"), p)
    assert result["mode"] == "recorded-fixture simulation"
    assert [p["side"] for p in result["decision"]["proposals"]] == ["bid", "ask"]
    assert result["execution_qualified"] is False


def test_cli_default_simulation_and_status(tmp_path):
    runner = CliRunner()
    prefix = ["--state-dir", str(tmp_path)]
    result = runner.invoke(app, prefix + ["simulate"])
    assert result.exit_code == 0, result.output
    assert '"network": "preprod"' in result.output
    result = runner.invoke(app, prefix + ["status"])
    assert result.exit_code == 0
    assert '"shadow_decisions": 1' in result.output
    assert '"transactions": []' in result.output
    assert '"transaction_count": 0' in result.output
    details = runner.invoke(app, prefix + ["status", "--details"])
    assert details.exit_code == 0
    assert '"reservations": []' in details.output


def test_mainnet_cannot_reinterpret_preprod_fixture(tmp_path):
    result = CliRunner().invoke(
        app, ["--network", "mainnet", "--state-dir", str(tmp_path), "simulate"]
    )
    assert result.exit_code == 1
    assert "chain identity mismatch" in result.output
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_test_harness_requires_explicit_signing_opt_in(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "--state-dir",
            str(tmp_path),
            "test-execute",
            "--manifest",
            "unused.json",
            "--action",
            "split",
            "--intent",
            "audit",
        ],
    )
    assert result.exit_code == 1
    assert "--sign or --submit" in result.output
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_shadow_alias_cannot_enable_execution(monkeypatch):
    import defi_kernel.cli as module

    calls = []
    monkeypatch.setattr(
        module, "_trade_command", lambda *a, **kw: calls.append((a, kw))
    )
    result = CliRunner().invoke(
        app, ["run", "--wallet-address", "public-address", "--iterations", "1"]
    )
    assert result.exit_code == 0
    assert calls[0][0][3] is False
    assert calls[0][1]["wallet_address"] == "public-address"


def test_bounded_paused_run_has_distinct_exit_status(tmp_path, monkeypatch):
    from test_engine import OWNER

    monkeypatch.setattr(
        "defi_kernel.engine.TradingEngine.run", lambda *a, **kw: {"action": "paused"}
    )
    result = CliRunner().invoke(
        app,
        [
            "--state-dir",
            str(tmp_path),
            "run",
            "--wallet-address",
            str(OWNER),
            "--iterations",
            "1",
        ],
    )
    assert result.exit_code == 2, result.output
