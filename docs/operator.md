# Run the MVP

The selected execution path is preprod ADA/fUSDA, published Cardano-Swaps v1 one-way orders and Dano. `trade` uses the complete scheduler and defaults to live-data shadow mode. Execution requires `--execute`; mainnet, preview and custom profiles remain read-only for this strategy.

For the arbitrage strategy, use [the extended-run guide](arbitrage.md#running-an-extended-preprod-experiment). It covers readable terminal events, bounded diagnostic logs, debug/JSONL output, per-run profit accounting, and the expanded Preprod allowlist. `status --output text --run all` shows arbitrage history; the existing `status` JSON view remains available.

## Configure and inspect

Install Python 3.12 and run `uv sync --locked --cache-dir .uv-cache` from the repository. Use its pinned pure Python `cbor2` build; installing a generic binary wheel can change Cardano transaction hashes. Copy `examples/preprod-test.toml` and `examples/preprod-mvp.json` for a new configuration. Give each wallet a distinct `wallet_id`. Credential settings contain environment-variable names, never credentials themselves.

This uv installation also overrides Dendrite's vulnerable `python-dotenv` pin. Ambient `.env` loading is disabled; export credential variables explicitly before starting the process. Signing keys must be owned private regular files (0600 or 0400), with local `.skey` filenames in the manifest. Symlink keys are rejected. Journals and run locks use private directories and files; do not share writable state directories across users.

```bash
.venv/bin/kernel --config examples/preprod-test.toml diagnostics
.venv/bin/kernel --config examples/preprod-test.toml markets --venue dano
.venv/bin/kernel --config examples/preprod-test.toml wallet-create
```

`wallet-create` prints a public address and manifest path, creates private payment/stake key files, and refuses to overwrite them. Request designated tADA using the [preprod faucet](https://faucet.preprod.world.dev.cardano.org/basic-faucet). Follow the [bounded funding sequence](preprod-execution.md) to split out exactly 5 tADA collateral and 30 tADA operating funds. For a new wallet, the market maker can obtain inventory through bounded Dano rebalances; the historical `buy-base` action is optional. Liquidity must be sufficient at execution time.

The existing workspace wallet is already funded. Its manifest is `state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json`; do not recreate it. The examples below use this manifest and its public profile. Substitute your generated manifest when using a different wallet ID.

```bash
.venv/bin/kernel --config examples/preprod-test.toml wallet-status --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json
.venv/bin/kernel --config examples/preprod-test.toml trade --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json --market examples/preprod-mvp.json --iterations 1
```

Review the shadow decision, both quotes, inventory, costs and configured limits. All token quantities are raw base units; ADA quantities are lovelace. The sample quotes 200,000 token units per side, targets 600,000, caps inventory at 1,500,000, and rebalances in steps of at most 500,000 when imbalance reaches 200,000. Prices include size-dependent Dano math, the fixed venue fee and an explicit estimated ledger fee. They are not guaranteed profits.

The sample caps each fee at 1.5 tADA, collateral at 5 tADA, selected operating inputs at 100 tADA and ADA trade size at 10 tADA. Wallet UTxOs larger than the operating cap remain protected; their tokens still count toward inventory exposure. The dedicated collateral is excluded from free inventory. Bid exposure conservatively includes ADA carrier excess the contract could permit a taker to exchange. The actual carrier deposit must fit each proposal's allocation.

Datum-bearing and reference-script wallet outputs are also protected and counted toward token exposure. Unconfirmed or incomplete wallet observations pause decisions. `run --wallet-address ADDRESS` uses the same scheduler in shadow mode without a manifest; the wallet must satisfy the same inventory and collateral requirements to produce actionable decisions.

## Execute, monitor, stop and cancel

```bash
.venv/bin/kernel --config examples/preprod-test.toml trade --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json --market examples/preprod-mvp.json --execute
# From another terminal, with the same config and state directory:
.venv/bin/kernel --config examples/preprod-test.toml status
.venv/bin/kernel --config examples/preprod-test.toml stop
# After the trade process releases its lock:
.venv/bin/kernel --config examples/preprod-test.toml cancel --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json --execute
```

The scheduler confirms previous work before creating new work. It publishes independently funded bids and asks, follows confirmed partial-fill continuations, and reprices when fills, age or movement warrant it. Repricing is staged: cancel, confirm, then compute and publish fresh prices. Inventory rebalancing likewise closes resting orders before a bounded Dano swap. No sequential fallback substitutes for an atomic route.

`stop` cooperatively stops new actions; existing orders remain on-chain. `cancel --execute` persists a cancellation request, tracks confirmations and follows racing fills until the selected wallet has no remaining orders. Without `--execute`, cancellation is a one-cycle preview. A wallet with another asset pair at the same order address pauses for explicit configuration rather than guessing how to manage it. Ctrl-C or a bounded `--iterations` run also leaves orders open. Starting another run clears the cooperative stop flag; a pending cancellation request persists until completed.

Status includes the latest observation/decision, free and committed inventory, protected assets, carrier deposits, order roots and current references, confirmed fills, pending transactions, costs and reservations. It is the last reconciled state, not a fresh chain query; check its observation time and heartbeat. Fill asset deltas describe the maker's order assets, not portfolio profit. Controlled self-fills transfer assets within the same owner. Reported venue costs are fixed Dano fees; pool trading fees and price impact are embedded in executed asset deltas. Pending ledger fees are reserved separately. The lifetime fee setting is an admission budget across this journal; cleanup cancellation is allowed beyond it, and a failed script can incur up to the separate collateral cap.

`status` defaults to a compact view with pending transactions, active orders, counts and observation age. Use `status --details` for the full ledger and reservation references. Decisions include at most the latest 20 fills; the durable fill ledger remains complete. Only the latest 1,000 diagnostic decisions are retained, so `shadow_decisions` counts retained records. Financial records are never pruned. A bounded run whose last decision is `paused` exits with code 2; configuration/command errors exit 1 and normal completion exits 0. Continuous runs record pauses and retry on the next cycle.

For one bounded external route scan:

```bash
.venv/bin/kernel --config examples/preprod-test.toml route --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json
# Add --execute only to submit a qualifying route with the configured test limits.
```

Routes consider confirmed external v1 token asks, exclude this wallet's orders, and require the slippage-adjusted Dano return to cover payment, venue fee, maximum ledger fee and minimum net gain. The final inspector requires zero intermediate token delta and the configured ADA gain. No qualifying route is a normal `hold` result.

## Recover without duplicate trades

The journal stores exact candidate bytes and reservations before signing, then records one wire submission attempt per intent. Restarting reconciles those same bytes. Never create a replacement intent to bypass a timeout or manually delete reservations.

```bash
.venv/bin/kernel --config examples/preprod-test.toml reconcile --intent ORIGINAL_INTENT --wait
.venv/bin/kernel --config examples/preprod-test.toml abandon-unsigned --intent NEVER_SIGNED_INTENT
```

An unsigned, unattempted candidate can be abandoned; the executing scheduler does this during recovery. A signed, unattempted strategy candidate can resume only with matching market configuration and fresh dependencies/validity. A changed configuration waits for reconciliation or expiry. Attempted submissions are never automatically retransmitted.

Expiry alone does not release funds. Recovery requires a canonical block beyond the validity deadline at confirmation depth, the original transaction still absent, and explicit unspent input observations. A consumed input also requires a confirmed different spending transaction before retiring the candidate as conflicted. Unspent inputs are released; consumed references remain tombstones. A racing cancellation can then be rebuilt against the independently observed continuation. Missing or inconsistent evidence keeps the runtime paused.

Canonical transaction bodies reconstruct order roots, fills and closures. Replay is idempotent; positive rollback evidence reverses removed fills and restores predecessors. Temporary indexer absence does not erase canonical accounting. Script failures consume collateral rather than regular inputs, and their rollback is reconciled separately. These paths are tested with injected provider/chain changes; no real network rollback or script loss was deliberately induced.

Hosted observations still trust the selected provider and are not atomic snapshots. Bounded full address history, UTxO comparison, canonical-block rechecks and pending overlays detect inconsistencies; an incomplete history or changed dependency pauses execution. Network identity is checked independently of address prefix. State, manifests and locks isolate networks and wallets. Do not move a preprod journal into another profile.

Order UTxO comparison checks values and datum terms as well as references. A disagreement, duplicate order reference or foreign address leaves saved accounting intact and pauses the runtime. A pending transaction may explain a new or missing reference; it cannot explain changed contents at the same reference. The final signing authorization also fixes selected spending, reference and collateral inputs before balancing.

## Custom Python strategies

Pass a strategy object to `TradingEngine(..., strategy=my_strategy)`. Implement `decide(sell_quote, buy_quote, inventory, now) -> Decision` and `should_reprice(old_price, new_price, created_at, now) -> bool`, following `MarketMaker` in `src/defi_kernel/strategy.py`. The runtime supplies observations, manages order lifecycle and applies the same execution limits, final evaluation and independent signing policy. Strategy callbacks receive no provider or signer. Run custom code as trusted local Python; it is not a security sandbox. Protocol and provider interfaces are separate from these callbacks.

Second-provider live verification awaits credentials. Published Swaps v2, two-way orders, script-based stake authorization and batcher venues are unsupported. See [atomic arbitrage](arbitrage.md) for multi-hop search, configuration, evaluated shadow mode and funded qualification.

## Multi-hop arbitrage

Use `arbitrage --manifest WALLET --strategy examples/preprod-arbitrage.json` for a continuous evaluated shadow run; add `--iterations 1` for one poll or `--execute` for bounded Preprod submission. The [arbitrage guide](arbitrage.md) lists allowlist/search limits, exact balance requirements, cost accounting, evidence and recovery semantics. This command shares wallet locking, polling, the signer and durable recovery with the market maker.
