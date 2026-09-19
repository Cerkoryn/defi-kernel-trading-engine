# DeFi Kernel runtime

A self-hosted Python CLI for the [Cardano trading MVP](cardano-trading-mvp-plan.md), inspired by [fallen-icarus's vision](docs/vision.md). The selected preprod flow runs a two-sided market maker using published Cardano-Swaps v1 orders and Dendrite's Dano integration: inventory-aware quotes, publication, fill accounting, staged repricing, bounded rebalancing, tracked cancellation and atomic cross-protocol routes.

Use the [operator guide](docs/operator.md) for configuration, execution, recovery and custom Python strategies; [qualification](docs/qualification.md) records pinned sources and deployment support. [CHECKPOINT.md](CHECKPOINT.md) records completion evidence and test-wallet state.

[Atomic arbitrage](docs/arbitrage.md) adds bounded multi-hop ADA cycle search with exact intermediate balances, full unsigned evaluation and optional Preprod execution. Confirmed three- and four-hop fixtures and resource probes are recorded separately from market-making evidence.

The retired [Preprod campaign results](docs/preprod-campaign-review.md) document confirmed 2-, 4- and 8-hop atomic trades, measured fees and the limits of the inclusion-timing evidence.

For a foreground terminal experiment, follow [the extended Preprod run guide](docs/arbitrage.md#running-an-extended-preprod-experiment). Arbitrage now provides timestamped events, five-minute health summaries, rotating diagnostics, optional debug/JSONL output and persistent per-run accounting. Its separate expanded configuration has an explicit asset allowlist and 50-tADA drawdown limit; signing still requires `--execute`.

Arbitrage keeps strategy assets in ADA and can automatically restore operating funds and missing collateral from verified bot proceeds. Hosted rate limits pause work; fixed submission deadlines and durable recovery prevent stale attempts or duplicate trades. See [allocation and recovery](docs/arbitrage.md#ada-allocation-and-recovery).

The [security and organization audit](docs/audit.md) records implemented fixes, measured reconciliation costs, verification and unresolved dependency/provider risks.

A [Dolos Preprod deployment for TrueNAS](docs/dolos-preprod.md) and explicit provider adapters are available. Start with `examples/preprod-dolos.toml` and read-only `kernel provider-check`. Dolos handles chain data, discovery and recovery; Koios supplies ledger state, unsigned evaluation and submission until [Dolos's governance state is repaired](docs/upstream-contributions.md#dolos-governance). Live submission qualification is still required.

## Setup and first run

Python 3.12 is pinned. The supported installation is `uv sync` from this repository: it applies the patched transitive dependency override and required pure Python `cbor2` build settings. A generic binary decoder can reorder Cardano input sets and change transaction hashes. Ambient `.env` loading is disabled; explicitly export configured credential environment variables.

```bash
uv sync --locked --cache-dir .uv-cache
uv run --locked --cache-dir .uv-cache kernel --network preprod diagnostics
```

If uv is absent, the tested workspace bootstrap is:

```bash
python3 -m venv /tmp/kernel-tools
/tmp/kernel-tools/bin/python -m pip install uv==0.12.10
/tmp/kernel-tools/bin/uv sync --locked --cache-dir .uv-cache
```

For the existing disposable test wallet, first export `KOIOS_API_KEY` using [the hidden token prompt](docs/arbitrage.md#running-an-extended-preprod-experiment), then inspect one live-data shadow cycle:

```bash
.venv/bin/kernel --config examples/preprod-test.toml trade --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json --market examples/preprod-mvp.json --iterations 1
.venv/bin/kernel --config examples/preprod-test.toml status
```

`trade` defaults to shadow mode; `--execute` enables bounded preprod execution. See the operator guide before starting a continuous run. `stop` leaves orders open; `cancel --execute` separately tracks their closure. A new wallet needs its own wallet ID, generated manifest and designated test funds. Global `--network`, `--config` and `--state-dir` apply consistently, with preprod as the default and no mainnet fallback.

`markets` and `diagnostics` support qualified mainnet/preprod/preview reads. `simulate` uses recorded data and synthetic inventory. `run --wallet-address ADDRESS` runs the same reconciled scheduler in shadow mode using only a public address. It applies the same inventory and collateral requirements. The CLI filters Dendrite's known default-backend warning because the runtime injects an isolated transaction environment.

## Evidence and validation

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
uv build --offline --cache-dir .uv-cache
```

Historical [fixture evaluations](evidence/preprod-composition-final-evaluation.json) use synthetic additional UTxOs. The separate [six-transaction execution record](evidence/preprod-live-execution.json) proves real create/fill/close, standalone Dano and atomic Swaps/Dano execution. The [MVP execution record](evidence/preprod-mvp-execution.json) extends that evidence through automatic rebalancing, publication, observed controlled fill, cancellation/replacement and cleanup. Tests replay hashes, signatures, economic limits and lineage, and exercise restart, unknown outcomes, expiry, contention, cancellation races and rollback using deterministic fixtures.

The chain fills were controlled self-fills; they demonstrate execution and management, not organic demand or profitable trading. The wallet's stake credential remained unregistered and undelegated throughout: registration/delegation is not required for orders. Published v1's beacon policy does require an address staking credential; credential-less reference-script outputs without order datums/beacons are not orders.

## Scope and limits

The MVP selects one preprod ADA/fUSDA pair, two one-way v1 orders and one direct venue. Execution is restricted to preprod; no mainnet transactions were signed or submitted. Source and deployment qualification do not certify unattended financial performance. Canonical observations still depend on provider evidence and are not atomic snapshots. Koios remains the default; the Dolos configuration adds explicit capability routing and an optional read-only comparison against Koios. Read-only and bounded shadow checks passed with Koios ledger routing; live hybrid submission remains unqualified.

Arbitrage now also fills Swaps v1 two-way, Splash CPP, Genius Yield v1.1 and SaturnSwap V3 uncovered orders; see [venue scope and qualification](docs/venues.md). Published Swaps v2, a production two-way maker strategy, script-based stake signing and batcher execution remain unsupported. Two-way and Saturn publication/cancellation are restricted to the controlled test harness. Keep private wallet files under ignored `state/`; packages and public evidence exclude them. The qualified preprod request ceiling is 16 KiB; the general default remains 1,000 bytes and must not be generalized to other endpoints or tiers.
