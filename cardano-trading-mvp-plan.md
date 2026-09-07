# Cardano trading runtime: MVP implementation brief

## Mission

Build a lightweight, self-hostable Python runtime that makes automated market-making on Cardano easy to start and extensible for advanced users. The ultimate goal is to **connect liquidity across Cardano**, with DeFi Kernel orders as the primary place to provide liquidity.

The first release must run a real reference strategy, not merely expose SDK methods or display quotes. Reuse **Charli3 Dendrite** for supported protocol discovery, decoding, quote math, and transaction construction. Build the missing runtime around it. Use hosted infrastructure, initially Koios; the user cannot run an additional cardano-node, Ogmios, or Kupo.

This brief consolidates decisions made through September 5, 2026. Treat repository observations below as research leads to recheck, not certifications of deployed behavior. Work within the target repository's instructions, preserve existing work, make routine implementation decisions autonomously, and keep a short progress/checkpoint file. Ask only when a material product choice, missing credential, or genuine blocker cannot be resolved locally.

## 1. Agreed scope

**Initial MVP:** one asset pair, one qualified Cardano-Swaps deployment, one qualified direct-execution DEX path, one reference market-making strategy, and a CLI. Demonstrate an atomic transaction combining the two protocols as well as sustained order management.

- Start with Dendrite's existing integrations. Qualify Dano or Splash as the external venue; choose based on executable support, deployment availability, and suitable liquidity rather than brand preference.
- Support providing bids and asks through Kernel orders, external pricing, inventory-aware quoting, and bounded rebalancing.
- Keep protocol integrations and infrastructure providers independently replaceable through small internal interfaces.
- Preserve the user's staking credential on Swaps orders using the existing contracts. A new custody or smart-wallet contract is not an MVP requirement.
- Use the same runtime for strategy presets and custom Python strategies.
- Make mainnet, preprod, preview, and custom testnets selectable through named network profiles and a consistent CLI option.
- Design for asynchronous DEX orders now. Implement a contrasting batcher-based venue, such as Minswap V2, in the next increment after the initial MVP; do not claim this support before it works.

**Deferred:** lending, Cardano-Loans, options, AMM liquidity-position management, hosted multi-user execution, custom session-key contracts, web UI, Hummingbot/Nautilus bridges, a comprehensive backtester, and exhaustive DEX coverage. A Textual TUI is optional after the CLI works.

Do not start a separate comprehensive DeFi Kernel SDK or rewrite Dendrite's adapter layer. Extract a public SDK later if actual reuse warrants it. Do not promise that every DEX combination can settle atomically.

## 2. First task: qualify reuse before building abstractions

Inspect current Dendrite, PyCardano, upstream Swaps, and candidate venue implementations. Pin compatible versions or commits in the project lockfile. Record a compact support matrix: network, deployment/version, operation, required dependencies, and demonstrated validation level.

Research found the following at Dendrite commit `0a1e02505af9d92506e8adbaf42307d923b1f061`:

- Existing protocol builders include caller-owned transaction-builder paths useful for composition.
- Built-in data backends include db-sync, Blockfrost, and Ogmios/Kupo; no built-in Koios backend was found.
- The Cardano-Swaps module describes v2 one-way swaps and expiration, but uses `PlutusV2Script` and a spending hash beginning `1d6cff26`. Upstream's published v2 uses Plutus V3 and a hash beginning `ef69e7b2`; v1 begins `01fa3646`. Reconcile this before use. Do not repair it by swapping hash constants without matching script bytes and semantics.
- Existing Swaps tests were structural, not full transaction evaluation. Two-way support was not established.
- Upstream reports Swaps v1 audited without expiration and v2 unaudited with expiration. Recheck the chosen version and audit scope; do not silently select a deployment.

Use authoritative deployment manifests, script bytes/hashes, datum/redeemer schemas, and reference UTxOs. The Kernel registry contains placeholders and is not sufficient deployment evidence. Maintain separate network configurations; do not assume every protocol is available on public testnets.

Prefer small upstream-compatible patches or a pinned fork when necessary. Keep adapter shims narrow. Do not implement unrelated historical methods just to imitate an existing broad interface; report unsupported capabilities explicitly.

## 3. Architecture and stack

Use one Python project and one runtime process initially. Use `uv` if repository conventions permit, a Python version compatible with the pinned dependencies, PyCardano for transaction construction, SQLite for durable state, and a small CLI using Typer/Rich or an existing project equivalent. Avoid unnecessary services and dependencies.

| Boundary | Responsibility |
| --- | --- |
| Strategy | Consume normalized observations and inventory; propose constrained actions. No provider calls or signing. |
| Runtime | Schedule strategies, enforce risk, reserve inventory/UTxOs, journal actions, reconcile outcomes and recover. |
| Protocol integration | Reuse Dendrite to decode, quote, contribute transaction requirements/build operations, and interpret settlement. |
| Infrastructure | Retrieve chain data, evaluate transactions, submit signed bytes, and observe inclusion. |
| Signer | Inspect and authorize the finalized transaction independently of strategy code. |

Suggested modules are `domain`, `strategies`, `runtime`, `protocols`, `providers`, `signing`, and `cli`; adapt to the existing repository. Use explicit dependency injection. Isolate Dendrite's global backend selection rather than allowing strategies to switch it during execution.

Define only the models the first working flow needs:

- Asset identity: network, policy ID, raw asset-name bytes; quantities in integer base units. Use exact rational/decimal arithmetic for execution math.
- Observed state: UTxO references and decoded state, provider provenance, observation time, and chain point where actually supplied. Do not claim atomic snapshots from unrelated REST responses.
- Quote: direction, size, executable amounts, dependencies, freshness, fees/deposits, and settlement type.
- Intent: desired operation with amount, price/slippage, fee, inventory, and deadline constraints.
- Execution plan: contributing actions, authorization requirements, expected balance changes, and explicitly atomic or staged settlement.

Keep trading orders/fills in the trading domain. Let future modules introduce loan offers, obligations, and deadlines while reusing execution plans, authorization, infrastructure, and accounting. Do not implement speculative lending machinery now.

## 4. Network profiles

Provide named profiles for `mainnet`, `preprod`, `preview`, and configurable custom testnets. Use one consistent option such as `--network preprod` across commands, with an explicit configured default. Mainnet execution must never be the implicit fallback.

Each profile defines expected chain identity (network magic/genesis identity as appropriate), provider endpoints/credential references, protocol deployment manifests and reference UTxOs, asset IDs, signer/account references, and execution limits. Different testnets share the testnet address category: address prefixes or a mainnet/testnet boolean are not enough to identify the chain. Validate provider identity against the selected profile before execution, including after failover. Fail clearly on a mismatch or unverified identity.

Keep databases, reservations, pending transactions, order lineage, caches, and checkpoints isolated per network and wallet. Changing networks starts or resumes that profile's context; it must not reinterpret another network's state. Require stopping the active run before switching its network, while preserving pending work for reconciliation when that profile resumes.

Provide example testnet configurations and document how to obtain test ADA. Reuse strategy settings where appropriate, but qualify protocol availability, token identities, liquidity, and reference scripts separately on each network. Report unsupported deployments explicitly; switching profiles does not magically deploy contracts or reproduce mainnet liquidity.

## 5. Hosted infrastructure and Koios

Implement a shared Koios client with small capability interfaces for **chain data, evaluation, submission, and observation**. One provider may implement all four; configuration should permit different providers per capability later. Keep credentials out of configuration examples and logs.

Koios documents asset/address/credential/UTxO queries, datum/script retrieval, protocol parameters, transaction status, submission, and an `/ogmios` endpoint supporting `evaluateTransaction` and `submitTransaction`. Validate actual endpoint availability, response shapes, request-size limits, and authentication needs. Bridge the shared client into Dendrite's data backend and PyCardano's `ChainContext` as needed; those are distinct integration points.

The public Ogmios forwarding interface does not provide a shared acquired state across calls and excludes `queryLedgerState/utxo`. Do not assume chain-sync streaming or mempool access. Start with focused polling, caching, pagination, bounded concurrency, backoff, and centrally managed rate limits.

Track the consistency and freshness of observations; deduplicate by UTxO reference, detect incomplete scans, recheck dependencies, and invalidate plans when inputs disappear. Resynchronize after provider changes or detected chain inconsistencies. Keep a local pending-transaction/UTxO reservation overlay so indexer lag cannot make committed funds look available.

Prove replaceability using a second concrete hosted configuration for a narrow common flow, preferably existing Blockfrost support if credentials are available. Lack of a second provider credential must not block Koios development; record that verification as outstanding. Local Ogmios/Kupo remains an optional future configuration.

## 6. Reference strategy and composition

Implement a conservative, configurable two-sided market maker:

1. Observe Kernel orders, the selected external venue, and wallet/committed inventory.
2. Derive a size-aware reference price from executable external quotes, accounting for price impact and known fees.
3. Set bid/ask prices using configurable spread, order size, inventory target/skew, and exposure limits.
4. Publish orders and reprice only when movement or order age justifies transaction costs. Pause on stale data or breached limits.
5. Reconcile fills; rebalance excess inventory through the external venue when configured thresholds are crossed.

Use a qualified two-way Swaps operation if practical. Otherwise use two one-way orders with explicitly allocated inventory. Do not let both quotes reserve the same funds, or present two-way support as implemented when it is not.

Also implement a bounded cross-protocol route executor: discover a Kernel fill that can be paired with the external venue and assemble both legs in **one transaction**, subject to explicit asset-delta and cost limits. Two legs are enough; do not build an unrestricted routing engine. If no attractive live route exists, prove composition using controlled test liquidity and fixtures rather than fabricating profitability.

Reuse compatible Dendrite builders, but coordinate all inputs, outputs, minting, reference scripts, withdrawals/observers, redeemers, fees, minimum ADA, collateral, and change. Resolve index-sensitive references after transaction ordering is finalized. Evaluate the composed transaction, not just each leg. Evaluation is not full ledger validation or a guarantee of inclusion.

Reject incompatible atomic plans with a useful reason. Never silently replace an atomic request with sequential trades. Confirming a batcher-order submission will eventually mean “open order,” not “filled”; preserve that distinction even before adding an asynchronous venue.

## 7. Execution, signing, and operator experience

Persist intent IDs, transaction IDs, attempts, reserved UTxOs, order continuation lineage, fills, and resulting balances. Distinguish free inventory, committed inventory, minimum-ADA deposits, fees, and unsettled exposure. Separate transaction lifecycle from order lifecycle.

Recover from restarts, UTxO contention, partial fills, cancellation races, provider lag, and chain rollback. A timeout is an unknown submission outcome: reconcile the original transaction before rebuilding. A missing order must be investigated as spent, filled, cancelled, rolled back, or temporarily unobserved. Repeated observations must not duplicate fills or transactions.

Support a configurable confirmation policy. A stop command must stop new actions and clearly report remaining open orders; cancellation is a separate action whose completion is tracked.

Use a replaceable local signing interface. Swaps owner actions require the order's staking credential authorization; an unrelated payment key is insufficient. Preserve the selected staking credential on order/continuation outputs. Remember that a stake key authorizing these contracts is asset-control authority: a separate payment wallet alone does not isolate positions sharing that stake credential.

Validate final transaction destinations, asset deltas, mint/burn activity, fees, and authorization against the plan before requesting signatures. Do not log or commit key material. New on-chain permission contracts are deferred.

Provide a short operator path: configure provider/network/market/signer, run diagnostics, inspect markets, start in shadow mode, monitor status, and stop/cancel. Diagnostics should expose unsupported operations and provider limits. Status should show inventory, orders, pending transactions, freshness, fills, and costs; do not equate captured spread with net profit.

Distinguish recorded-fixture simulation, live-data shadow operation, and actual on-chain execution. Default to shadow mode. Build live-execution capability, but do not spend mainnet funds while implementing this brief without separate explicit authorization. Use disposable credentials and designated test funds for integration execution.

## 8. Build order and completion gates

| Milestone | Completion evidence |
| --- | --- |
| 1. Qualify integrations | Pinned dependencies, selected pair/deployments, support matrix, and explained Swaps discrepancies. |
| 2. Read through Koios | Correctly discover/decode real orders and pool state, including complete pagination and provenance; working CLI diagnostics and network profiles. |
| 3. Prove execution | Reproducible individual and composed transactions, full evaluation, and a confirmed test execution where compatible deployments are available. |
| 4. Run the strategy | Publish/manage both sides, observe fills, reprice, and rebalance using one configuration and the shared runtime. |
| 5. Prove recovery | Restart with pending work, reconcile unknown submission, handle consumed inputs, cancellation races and rollback without duplicate accounting or unintended replacement trades. |
| 6. Package the MVP | Reproducible setup, sample configuration/strategy, operator guide, validation evidence, and explicit remaining limitations. |

Use focused tests for economic invariants, transaction composition, and recovery. Structural builder tests alone do not satisfy execution gates. Use an injected clock and recorded fixtures for deterministic strategy/lifecycle tests; avoid building a general backtesting platform.

Verify network selection against preprod and preview endpoints where available; require chain-identity mismatch rejection and isolated state in tests. Exercise a strategy on a qualified test deployment and demonstrate that switching profiles cannot load its orders, keys, or pending transactions into mainnet execution. Mainnet read-only diagnostics may be tested without placing trades.

If a public test deployment is unavailable, use compatible lightweight ledger/emulator evaluation where feasible and recorded fixtures. Continue independent work, but report the missing end-to-end chain evidence honestly. Do not install a full node stack or label mocked execution as live validation.

Finish with a concise handoff: implemented capabilities, exact run commands, validated networks/deployments, evidence, known blockers, and the next smallest increment. The next increment should exercise a batcher-based order lifecycle before expanding venue count broadly.

## Primary references

- [DeFi Kernel](https://defikernel.org/) and [CIP-89](https://cips.cardano.org/cip/CIP-0089): design motivation, personal dApp addresses, and beacons.
- [Charli3 Dendrite](https://github.com/Charli3-Official/charli3-dendrite): first integration foundation; inspect code and tests, not just README coverage claims.
- [Cardano-Swaps](https://github.com/fallen-icarus/cardano-swaps) and [versions](https://github.com/fallen-icarus/cardano-swaps/blob/main/VERSIONS.md): authoritative protocol behavior and deployment leads.
- [PyCardano](https://github.com/Python-Cardano/pycardano): transaction construction and provider context.
- [Koios API specification](https://github.com/cardano-community/koios-artifacts/blob/main/specs/results/koiosapi-mainnet.yaml): provider capabilities and limits.
- [Ogmios evaluation](https://ogmios.dev/mini-protocols/local-tx-submission/): evaluation semantics and limitations.
- [P2P wallet](https://github.com/fallen-icarus/p2p-wallet): reference for composing Kernel actions; not a required runtime dependency.
