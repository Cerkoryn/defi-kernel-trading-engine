"""Operator CLI. Every command uses the same explicit network context."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import typer

from .backends import create_provider
from .config import load_profile
from .domain import KernelError, json_value
from .journal import Journal
from .reporting import Reporter, configure_sdk_logging, error_details, status_text

app = typer.Typer(
    no_args_is_help=True,
    help="Self-hosted Cardano liquidity runtime; defaults to shadow mode.",
)


@app.command("wallet-create")
def wallet_create(ctx: typer.Context):
    """Create a new disposable test wallet; refuses to overwrite existing keys."""
    from .wallet import create_test_wallet

    try:
        path, manifest = create_test_wallet(ctx.obj["profile"], ctx.obj["state_dir"])
        render(
            {
                "manifest": str(path),
                "address": manifest["address"],
                "network": manifest["network"],
            }
        )
    except (KernelError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)


@app.command("wallet-status")
def wallet_status(ctx: typer.Context, manifest: Annotated[Path, typer.Option()]):
    """Verify chain identity and show the local wallet's public UTxOs."""
    from .protocols import row_assets
    from .wallet import load_wallet

    provider = None
    try:
        wallet, _ = load_wallet(ctx.obj["profile"], manifest)
        provider = create_provider(ctx.obj["profile"])
        observation = provider.address_utxos(wallet["address"])
        totals = {}
        for row in observation.rows:
            for unit, quantity in row_assets(row, provider.profile.name).items():
                totals[unit] = totals.get(unit, 0) + quantity
        render(
            {
                "network": provider.profile.name,
                "address": wallet["address"],
                "observed_at": observation.observed_at,
                "balances": totals,
                "utxos": observation.rows,
            }
        )
    except (KernelError, OSError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    finally:
        if provider is not None:
            provider.close()


@app.command("test-execute")
def test_execute(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Option()],
    action: Annotated[
        str, typer.Option(help="split, buy-base, create, fill, compose, or close")
    ],
    intent: Annotated[
        str,
        typer.Option(
            help="Durable unique action ID; never reuse for a different transaction"
        ),
    ],
    submit: Annotated[
        bool, typer.Option(help="Submit the inspected signed candidate to preprod")
    ] = False,
    sign: Annotated[
        bool,
        typer.Option(help="Explicitly sign and store a candidate without submitting"),
    ] = False,
    error_details: Annotated[
        bool, typer.Option(help="Show bounded public provider rejection details")
    ] = False,
):
    """Prepare a bounded disposable-wallet test; explicitly opt in to submission."""
    # Upstream's failure decorator dumps the entire builder and reference scripts.
    # Keep normal operator errors concise and use explicit provider diagnostics.
    import logging

    from .coordinator import Coordinator
    from .runtime import run_lock
    from .test_execution import prepare_test_action

    logging.getLogger("PyCardano").setLevel(logging.ERROR)

    provider, journal = None, None
    try:
        if not sign and not submit:
            raise KernelError(
                "Test signing requires --sign or --submit; use trade without --execute for shadow mode"
            )
        profile = ctx.obj["profile"]
        with run_lock(ctx.obj["state_dir"], profile):
            provider = create_provider(profile, enable_testnet_submission=submit)
            journal = Journal(profile.state_path(ctx.obj["state_dir"]), profile)
            existing = journal.db.execute(
                "SELECT 1 FROM outbox WHERE intent=?", (intent,)
            ).fetchone()
            if existing:
                entry = journal.outbox_entry(intent)
                if json.loads(entry["metadata"])["action"] != action:
                    raise KernelError("Intent already belongs to a different action")
                result = {
                    "intent": intent,
                    "transaction_id": entry["txid"],
                    "status": entry["status"],
                }
            else:
                result = prepare_test_action(
                    provider, journal, manifest, action, intent
                )
            if submit:
                result["submitted_transaction_id"] = Coordinator(
                    provider, journal
                ).submit(intent)
                result["status"] = "submitted"
            render(result)
    except (KernelError, OSError, ValueError) as error:
        typer.echo(str(error), err=True)
        if error_details and getattr(error, "rpc_error", None):
            typer.echo(json.dumps(error.rpc_error)[:6000], err=True)
        raise typer.Exit(1)
    finally:
        if provider is not None:
            provider.close()
        if journal is not None:
            journal.close()


@app.command("abandon-unsigned")
def abandon_unsigned(ctx: typer.Context, intent: Annotated[str, typer.Option()]):
    """Release an unsigned, never-submitted candidate while retaining its history."""
    from .runtime import run_lock

    profile = ctx.obj["profile"]
    journal = None
    try:
        with run_lock(ctx.obj["state_dir"], profile):
            journal = Journal(profile.state_path(ctx.obj["state_dir"]), profile)
            journal.abandon_unsigned(intent, "Operator abandoned unsigned candidate")
            render({"intent": intent, "status": "aborted"})
    except (KernelError, OSError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    finally:
        if journal is not None:
            journal.close()


@app.command()
def reconcile(
    ctx: typer.Context,
    intent: Annotated[str, typer.Option()],
    wait: Annotated[
        bool, typer.Option(help="Poll for confirmation without resubmitting")
    ] = False,
    timeout: Annotated[int, typer.Option(min=1, max=1800)] = 180,
):
    """Reconcile the original transaction; never resubmit or rebuild it."""
    import time

    from .coordinator import Coordinator
    from .runtime import run_lock

    provider, journal = None, None
    try:
        provider = create_provider(ctx.obj["profile"])
        journal = Journal(
            ctx.obj["profile"].state_path(ctx.obj["state_dir"]), ctx.obj["profile"]
        )
        with run_lock(ctx.obj["state_dir"], ctx.obj["profile"]):
            deadline = time.monotonic() + timeout
            while True:
                status = Coordinator(provider, journal).reconcile(intent)
                if (
                    not wait
                    or status
                    in (
                        "confirmed",
                        "rolled_back",
                        "expired",
                        "conflicted",
                        "failed",
                        "aborted",
                    )
                    or time.monotonic() >= deadline
                ):
                    break
                time.sleep(5)
        entry = journal.outbox_entry(intent)
        render(
            {
                "intent": intent,
                "transaction_id": entry["txid"],
                "status": status,
                "block_hash": entry["block_hash"],
                "confirmations": entry["confirmations"],
            }
        )
    except (KernelError, OSError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    finally:
        if provider is not None:
            provider.close()
        if journal is not None:
            journal.close()


def render(value):
    typer.echo(json.dumps(value, indent=2, default=json_value))


@app.command("provider-check")
def provider_check(
    ctx: typer.Context,
    address: Annotated[
        str | None, typer.Option(help="Public wallet address; no key files are opened")
    ] = None,
    transaction: Annotated[
        list[str] | None,
        typer.Option(help="Existing transaction hash to verify; repeatable"),
    ] = None,
    strategy: Annotated[
        Path | None,
        typer.Option(
            help="Also qualify discovery for the configured venues; requires --address"
        ),
    ] = None,
    compare_koios: Annotated[
        bool,
        typer.Option(
            help="Compare selected immutable records with the configured Koios evaluator"
        ),
    ] = False,
):
    """Read-only adapter qualification: synchronization, parameters, history and discovery."""
    import time

    from pycardano import Address

    from .backends import ProviderBundle
    from .chain_context import ProviderChainContext, to_utxo

    provider = None
    started = time.monotonic()
    try:
        if strategy is not None and address is None:
            raise KernelError("--strategy qualification requires a public --address")
        provider = create_provider(ctx.obj["profile"])
        context = ProviderChainContext(provider)
        comparison = None
        if compare_koios:
            if not isinstance(provider, ProviderBundle):
                raise KernelError(
                    "--compare-koios requires explicit capability bindings"
                )
            comparison = provider.bindings["evaluation"]
        report = {
            "mode": "read-only; no signing, submission or journal writes",
            "network": provider.profile.name,
            "capabilities": provider.profile.capabilities or {"all": "koios"},
            "tip": context._tip,
            "tip_age_seconds": provider.clock() - context._tip["block_time"],
            "protocol_major": context.protocol_param.protocol_major_version,
            "cost_model_lengths": {
                k: len(v) for k, v in context.protocol_param.cost_models.items()
            },
            "transactions": [],
        }
        for txid in transaction or []:
            info = provider.transaction_info(txid)
            if info is None:
                raise KernelError(
                    "Requested transaction history is unavailable; qualify full archive retention"
                )
            tx = provider.transaction_cbor(txid)
            block = provider.block_at_height(info["block_height"])
            if not block or block["hash"] != info["block_hash"]:
                raise KernelError("Requested transaction is not verified canonical")
            if comparison:
                remote = comparison.transaction_cbor(txid)
                if (
                    remote.valid != tx.valid
                    or remote.transaction_body.to_cbor()
                    != tx.transaction_body.to_cbor()
                ):
                    raise KernelError("Dolos/Koios historical transaction disagreement")
            report["transactions"].append(
                {"txid": txid, "valid": tx.valid, "block_hash": block["hash"]}
            )
        if address:
            address = str(Address.from_primitive(address))
            observed = provider.address_utxos(address)
            if not observed.complete:
                raise KernelError("Incomplete wallet observation")
            if comparison:
                from .domain import OutRef

                # Compare immutable contents of outputs positively present on both;
                # a disappearing output is inconclusive, never evidence of a spend.
                refs = [OutRef(r["tx_hash"], r["tx_index"]) for r in observed.rows]
                remote = {
                    OutRef(r["tx_hash"], r["tx_index"]): r
                    for r in comparison.utxos(refs)
                }
                if set(remote) != set(refs):
                    raise KernelError(
                        "Comparison UTxO set changed or is incomplete; repeat qualification"
                    )
                for ref, row in zip(refs, observed.rows, strict=True):
                    if (
                        to_utxo(row, provider.profile).output.to_cbor()
                        != to_utxo(remote[ref], provider.profile).output.to_cbor()
                    ):
                        raise KernelError("Dolos/Koios UTxO content disagreement")
            report["wallet_utxos"] = len(observed.rows)
        if strategy is not None:
            from collections import Counter

            from .arbitrage import ArbitrageConfig
            from .arbitrage_runtime import Liquidity

            selected = ArbitrageConfig.load(strategy, provider.profile)
            edges, rows, _, _, rejected = Liquidity().observe(
                provider, selected, Address.from_primitive(address), context
            )
            report["discovery"] = {
                "enabled_venues": selected.venues,
                "edges_by_venue": dict(Counter(e.venue for e in edges)),
                "dependencies": len(rows),
                "rejected": rejected,
            }
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["next_step"] = (
            "Evaluated shadow; this read-only check does not qualify live submission"
        )
        render(report)
    except (KernelError, OSError, ValueError, TypeError) as error:
        typer.echo(error_details(error)["reason"], err=True)
        raise typer.Exit(1)
    finally:
        if provider is not None:
            provider.close()


@app.callback()
def context(
    ctx: typer.Context,
    network: Annotated[
        str | None, typer.Option(help="Named network; defaults to configured default")
    ] = None,
    config: Annotated[Path, typer.Option()] = Path("config.example.toml"),
    state_dir: Annotated[Path, typer.Option()] = Path("state"),
):
    configure_sdk_logging()
    try:
        ctx.obj = {"profile": load_profile(config, network), "state_dir": state_dir}
    except (KernelError, OSError, TypeError, ValueError) as e:
        typer.echo(f"Configuration error: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def diagnostics(ctx: typer.Context):
    """Verify hosted chain identity and selected deployment scripts without trading."""
    from pycardano import PlutusV2Script, PlutusV3Script, plutus_script_hash

    from .protocols import DEPLOYMENTS

    provider = create_provider(ctx.obj["profile"])
    try:
        identity = provider.verify_identity()
        report = {
            "network": provider.profile.name,
            "chain_magic": identity["networkmagic"],
            "system_start": identity["systemstart"],
            "tip": provider.tip(),
            "mode": "shadow",
            "state_path": str(provider.profile.state_path(ctx.obj["state_dir"])),
            "limits": {
                "request_bytes": provider.profile.max_request_bytes,
                "page_size": provider.profile.page_size,
                "max_pages": provider.profile.max_pages,
            },
            "deployments": {},
            "execution": "preprod ADA/fUSDA Swaps v1 + Dano qualified; explicit --execute required"
            if provider.profile.name == "preprod"
            else "read-only: execution is qualified only on preprod",
        }
        for name, manifest in DEPLOYMENTS.items():
            try:
                script = provider.script_info(manifest["script_hash"])
                cls = (
                    PlutusV2Script
                    if manifest["plutus_version"] == 2
                    else PlutusV3Script
                )
                if (
                    script["type"] != f"plutusV{manifest['plutus_version']}"
                    or str(plutus_script_hash(cls(bytes.fromhex(script["bytes"]))))
                    != manifest["script_hash"]
                ):
                    raise KernelError("Script bytes/language mismatch")
                report["deployments"][name] = {
                    "read_script": "hash verified",
                    "audit": manifest["audit"],
                    "execution_qualified": provider.profile.name == "preprod"
                    and name == "swaps-v1",
                }
            except KernelError as e:
                report["deployments"][name] = {"available": False, "reason": str(e)}
        render(report)
    except KernelError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    finally:
        provider.close()


@app.command()
def simulate(
    ctx: typer.Context,
    fixture: Annotated[Path, typer.Option()] = Path("examples/preprod-market.json"),
):
    from .simulation import fixture_decision

    """Run the reference strategy on recorded data with explicitly synthetic inventory."""
    try:
        result = fixture_decision(fixture, ctx.obj["profile"])
        journal = Journal(
            ctx.obj["profile"].state_path(ctx.obj["state_dir"]), ctx.obj["profile"]
        )
        try:
            journal.record_shadow(
                result["observed_at"], json.dumps(result, default=json_value)
            )
        finally:
            journal.close()
        render(result)
    except (KernelError, OSError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)


@app.command()
def status(
    ctx: typer.Context,
    details: Annotated[
        bool,
        typer.Option(help="Include full lineage, reservations and latest diagnostics"),
    ] = False,
    output: Annotated[Literal["text", "json"], typer.Option()] = "json",
    run: Annotated[
        str, typer.Option(help="Arbitrage history: latest, all, or a run ID")
    ] = "latest",
):
    """Show last reconciled state, not a fresh chain query."""
    import time

    journal = Journal(
        ctx.obj["profile"].state_path(ctx.obj["state_dir"]), ctx.obj["profile"]
    )
    try:
        history = journal.arbitrage_history(run)
        if output == "text":
            typer.echo(status_text(history))
            return
        report = journal.status()
        report["arbitrage"] = history
        latest = report["latest_shadow"]
        report["observation_age_seconds"] = (
            max(0, time.time() - latest["observed_at"]) if latest else None
        )
        if not details:
            transactions = report["transactions"]
            report["transaction_count"] = len(transactions)
            report["transactions"] = [
                t
                for t in transactions
                if t["status"]
                not in ("confirmed", "aborted", "expired", "conflicted", "failed")
            ]
            report["reservation_count"] = len(report.pop("reservations"))
            ledger = report.pop("order_ledger")
            report.pop("outbox")
            report["orders"] = (
                [
                    {k: o[k] for k in ("id", "ref", "status", "fills")}
                    for o in ledger["orders"]
                    if o["live"]
                ]
                if ledger
                else []
            )
            report["fill_count"] = len(ledger["fills"]) if ledger else 0
            if latest:
                latest["decision"] = {
                    k: v
                    for k, v in latest["decision"].items()
                    if k
                    not in ("fills", "orders", "sell_quote", "buy_quote", "proposals")
                }
        render(
            {
                "network": ctx.obj["profile"].name,
                "wallet": ctx.obj["profile"].wallet_id,
                **report,
            }
        )
    except (KernelError, OSError) as error:
        typer.echo(error_details(error)["reason"], err=True)
        raise typer.Exit(1)
    finally:
        journal.close()


@app.command()
def markets(ctx: typer.Context, venue: str = "swaps-v1"):
    """Discover and decode complete REST traversals, with rejected candidate counts."""
    from .protocols import (
        DANO_HASH,
        DANO_PREPROD_HASH,
        DEPLOYMENTS,
        dano_config,
        decode_dano,
        decode_swaps,
    )

    provider = create_provider(ctx.obj["profile"])
    try:
        if venue == "dano":
            provider.verify_identity()
            fee_rate, fixed_fee = dano_config(provider)
            h = DANO_HASH if provider.profile.name == "mainnet" else DANO_PREPROD_HASH
        elif venue in DEPLOYMENTS:
            h = DEPLOYMENTS[venue]["script_hash"]
            fixed_fee = 0
        else:
            raise KernelError(
                "Supported read venues: swaps-v1, swaps-legacy-expiration, swaps-v2, dano"
            )
        observation = provider.credential_utxos(h)
        decoded, rejected = [], {}
        for row in observation.rows:
            try:
                if venue == "dano":
                    s = decode_dano(row, provider.profile, platform_fee_rate=fee_rate)
                    decoded.append(
                        {
                            "ref": f"{s.tx_hash}#{s.tx_index}",
                            "asset_x": s.unit_a,
                            "asset_y": s.unit_b,
                            "active_x": s.reserve_a,
                            "active_y": s.reserve_b,
                            "lp_fee_bps": s.fee,
                            "platform_fee_rate": fee_rate,
                            "fixed_fee_lovelace": fixed_fee,
                            "execution_qualified": False,
                        }
                    )
                else:
                    decoded.append(asdict(decode_swaps(row, provider.profile, venue)))
            except (KernelError, ValueError, TypeError, KeyError) as e:
                # A failed candidate is reported, never silently accepted as a market.
                key = type(e).__name__ + ": " + str(e).splitlines()[0][:160]
                rejected[key] = rejected.get(key, 0) + 1
        render(
            {
                "network": provider.profile.name,
                "venue": venue,
                "provider": observation.provider,
                "observed_at": observation.observed_at,
                "complete_rest_traversal": observation.complete,
                "atomic_snapshot": False,
                "tip_before": observation.tip_before,
                "tip_after": observation.tip_after,
                "decoded": decoded,
                "rejected_candidates": rejected,
            }
        )
    except KernelError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    finally:
        provider.close()


@app.command()
def run(
    ctx: typer.Context,
    wallet_address: Annotated[
        str, typer.Option(help="Public address; no signing credentials")
    ],
    market: Annotated[Path, typer.Option()] = Path("examples/preprod-mvp.json"),
    interval: Annotated[float, typer.Option(min=1)] = 30,
    iterations: Annotated[int | None, typer.Option(min=1)] = None,
):
    """Run the shared strategy in shadow mode using only a public address."""
    _trade_command(
        ctx, None, market, False, interval, iterations, wallet_address=wallet_address
    )


@app.command()
def stop(ctx: typer.Context):
    """Request that this profile's run stop polling; does not cancel open orders."""
    journal = Journal(
        ctx.obj["profile"].state_path(ctx.obj["state_dir"]), ctx.obj["profile"]
    )
    try:
        journal.request_stop()
        render({"stop_requested": True, "orders_cancelled": False, **journal.status()})
    finally:
        journal.close()


def _trade_command(
    ctx,
    manifest,
    market,
    execute,
    interval,
    iterations,
    *,
    cancel=False,
    route=False,
    wallet_address=None,
):
    import logging

    from .engine import TradingEngine
    from .runtime import Market, run_lock
    from .wallet import load_wallet

    logging.getLogger("PyCardano").setLevel(logging.ERROR)
    profile = ctx.obj["profile"]
    provider, journal = None, None
    try:
        wallet, key_dir = (
            load_wallet(profile, manifest)
            if manifest is not None
            else ({"address": wallet_address}, None)
        )
        if execute and manifest is None:
            raise KernelError("Execution requires a wallet manifest")
        selected = Market.load(market, profile)
        with run_lock(ctx.obj["state_dir"], profile):
            provider = create_provider(profile, enable_testnet_submission=execute)
            journal = Journal(profile.state_path(ctx.obj["state_dir"]), profile)
            engine = TradingEngine(
                provider, journal, selected, wallet, key_dir, execute=execute
            )
            if cancel:
                if execute:
                    engine.request_cancel()
                else:
                    engine.preview_cancel = True
            result = engine.run(
                interval=interval,
                iterations=iterations,
                cancel_only=cancel,
                route_only=route,
                emit=render,
            )
            if result.get("action") == "paused":
                raise typer.Exit(2)
    except KeyboardInterrupt:
        typer.echo(
            "Stopped. Open orders remain on-chain; use status or the separate cancel command."
        )
    except (KernelError, OSError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    finally:
        if provider is not None:
            provider.close()
        if journal is not None:
            journal.close()


@app.command()
def trade(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Option()],
    market: Annotated[Path, typer.Option()] = Path("examples/preprod-mvp.json"),
    execute: Annotated[
        bool, typer.Option(help="Enable bounded preprod signing/submission")
    ] = False,
    interval: Annotated[float, typer.Option(min=1)] = 30,
    iterations: Annotated[int | None, typer.Option(min=1)] = None,
):
    """Reconcile, quote, publish, reprice and rebalance; defaults to shadow mode."""
    _trade_command(ctx, manifest, market, execute, interval, iterations)


@app.command()
def cancel(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Option()],
    market: Annotated[Path, typer.Option()] = Path("examples/preprod-mvp.json"),
    execute: Annotated[
        bool, typer.Option(help="Submit cancellation and track confirmation")
    ] = False,
    interval: Annotated[float, typer.Option(min=1)] = 15,
    iterations: Annotated[int | None, typer.Option(min=1)] = None,
):
    """Separately cancel the wallet's selected-pair orders; resumes racing fills."""
    _trade_command(
        ctx,
        manifest,
        market,
        execute,
        interval,
        iterations if execute else 1,
        cancel=True,
    )


@app.command()
def route(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Option()],
    market: Annotated[Path, typer.Option()] = Path("examples/preprod-mvp.json"),
    execute: Annotated[
        bool, typer.Option(help="Submit only a qualifying atomic route")
    ] = False,
):
    """Discover one bounded Swaps/Dano route; reject unprofitable/incompatible legs."""
    _trade_command(ctx, manifest, market, execute, 30, 1, route=True)


@app.command()
def arbitrage(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Option()],
    strategy: Annotated[Path, typer.Option()],
    execute: Annotated[
        bool, typer.Option(help="Sign and submit bounded atomic Preprod arbitrage")
    ] = False,
    interval: Annotated[float, typer.Option(min=1)] = 30,
    iterations: Annotated[int | None, typer.Option(min=1)] = None,
    output: Annotated[Literal["text", "json", "jsonl"], typer.Option()] = "text",
    debug: Annotated[
        bool, typer.Option(help="Include bounded decision and provider diagnostics")
    ] = False,
):
    """Search ADA cycles; default shadow builds, inspects and evaluates without keys."""
    import logging
    import signal

    from .arbitrage import ArbitrageConfig
    from .arbitrage_runtime import ArbitrageEngine
    from .runtime import run_lock
    from .wallet import load_wallet

    logging.getLogger("PyCardano").setLevel(logging.ERROR)
    provider, journal, reporter = None, None, None
    handlers = {}
    try:
        profile = ctx.obj["profile"]
        selected = ArbitrageConfig.load(strategy, profile)
        wallet, key_dir = load_wallet(profile, manifest)
        with run_lock(ctx.obj["state_dir"], profile):
            provider = create_provider(profile, enable_testnet_submission=execute)
            journal = Journal(profile.state_path(ctx.obj["state_dir"]), profile)
            reporter = Reporter(
                journal, selected, output=output, debug=debug, write=typer.echo
            )
            provider.observer = reporter

            def request_stop(signum, frame):
                # A flag avoids interrupting journal commits or a submission response.
                reporter.stop_requested = True

            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                handlers[signum] = signal.signal(signum, request_stop)
            result = ArbitrageEngine(
                provider,
                journal,
                selected,
                wallet,
                key_dir,
                execute=execute,
                reporter=reporter,
            ).run(interval=interval, iterations=iterations)
            if result["stage"] == "paused":
                raise typer.Exit(2)
    except KeyboardInterrupt:
        typer.echo("Stopped. Reconcile any pending transaction before new execution.")
    except typer.Exit:
        raise
    except Exception as error:
        detail = error_details(error)
        if reporter:
            reporter.event(
                "error",
                detail,
                namespace="SYSTEM",
                level="ERROR",
                message=detail["reason"],
            )
        else:
            typer.echo(detail["reason"], err=True)
        raise typer.Exit(1)
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if reporter and reporter.handler:
            reporter.handler.close()
        if provider is not None:
            provider.close()
        if journal is not None:
            journal.close()


@app.command("arbitrage-acknowledge")
def arbitrage_acknowledge(
    ctx: typer.Context,
    manifest: Annotated[Path, typer.Option()],
    txid: Annotated[
        str, typer.Option(help="Full transaction ID of the investigated failure")
    ],
    reason: Annotated[
        str, typer.Option(help="Investigation result and corrective action")
    ],
):
    """Acknowledge an investigated script failure; never submit or reset losses."""
    import time

    from .runtime import bind_wallet, run_lock
    from .wallet import load_wallet

    journal = None
    try:
        profile = ctx.obj["profile"]
        wallet, _ = load_wallet(profile, manifest)
        with run_lock(ctx.obj["state_dir"], profile):
            journal = Journal(profile.state_path(ctx.obj["state_dir"]), profile)
            bind_wallet(profile, journal, None, wallet["address"])
            journal.acknowledge_execution_incident(txid, reason, time.time())
            render(
                {"txid": txid, "status": "acknowledged", "submission_enabled": False}
            )
    except (KernelError, OSError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1)
    finally:
        if journal is not None:
            journal.close()
