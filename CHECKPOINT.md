# MVP implementation checkpoint

The selected initial MVP is implemented: one preprod ADA/fUSDA pair, published Swaps v1 one-way orders, Dendrite Dano direct execution, one continuous strategy runtime and a CLI. The repository originally contained only the brief; source pins and deployment discrepancies are documented in [qualification](docs/qualification.md), and the interpretation of fallen-icarus's writings is in [vision](docs/vision.md).

| Milestone | Completion evidence |
| --- | --- |
| 1. Qualify reuse | Pinned Dendrite/PyCardano, verified script bytes and deployment identities, separate published v1/historical expiration/published v2 classification. |
| 2. Hosted reads | Koios chain identity, bounded complete pagination, pool/order decoding, CLI diagnostics, mainnet/preprod/preview reads and isolated state. |
| 3. Execution | Hosted final evaluation plus six real confirmed funding/order/fill/Dano/atomic-composition/close transactions in the original evidence. |
| 4. Strategy | Automatic bounded rebalance, both-side publication, confirmed controlled-fill discovery, staged cancellation/replacement, restart hold without duplication and tracked final cancellation. Six additional confirmed transactions and persisted decisions. |
| 5. Recovery | Deterministic tests for durable restart, one-attempt submission, unknown outcomes, signed expiry, competing spends/cancellation continuations, phase-2 failure, canonical rollback/re-inclusion, expiry-anchor rollback and idempotent fill accounting. |
| 6. Packaging | Locked setup, sample market/profile, operator guide, source/wheel builds, public chain evidence and explicit limitations. |

The historical six-transaction fixture remains [preprod-live-execution.json](evidence/preprod-live-execution.json). The expanded [preprod-mvp-execution.json](evidence/preprod-mvp-execution.json) includes all twelve transactions, canonical inclusion, CBOR/signatures, wallet deltas, runtime decisions, order lineage and final wallet/order scans. The fills were controlled self-fills, not organic demand or evidence of profit. The recorded atomic composition has zero intermediate token wallet delta and cost 804,698 lovelace net; no attractive external live route was found under the configured limits.

Post-audit validation: **137 tests pass**; Ruff lint and formatting pass. Locked offline uv sync and source/wheel builds pass. Archive inspection confirms runtime modules, upstream script license, source documentation/evidence, and exclusion of private keys, wallet databases and caches. Build artifacts are generated under ignored `dist/` and removed after verification; rebuild them with the README command.

The [2026-09-07 audit](docs/audit.md) implements security, performance, usability and maintenance improvements. It consolidates shadow/execution scheduling and finalization, closes inventory/signing/recovery gaps and reduces the twelve-confirmed-transaction reconciliation benchmark from 60 provider requests to 14 while retaining canonical checks. One live shadow cycle completed without signing or submitting; historical transaction evidence and balances are unchanged. The dependency scan upgraded `python-dotenv` and still flags transitive `ecdsa`; the affected ECDSA signing path is unused by the tested Ed25519 transaction signer. This remaining advisory is recorded, not suppressed.

Follow-up hardening freezes spending/reference/collateral authorization before transaction building and compares observed order values and terms with canonical replay before replacing accounting. Added mutation checks cover all three input roles; inconsistent ADA/token/datum, duplicate-reference and foreign-address observations preserve the existing ledger. The benchmark remains 14 requests, and no follow-up transaction was signed or submitted.

## Final test state

The runtime is stopped, all twelve submitted intents are confirmed, and no open orders remain. One historical unsigned candidate remains aborted with zero wire attempts. Final balance is **9,992.137900 tADA and 600,000 fUSDA base units**. Ledger fees across all twelve transactions total **5.129168 tADA**; fixed Dano fees total 0.300000 tADA, separate from pool trading fees/price impact and exchanged assets. The initial 5-tADA collateral and 9,964.828295-tADA remainder outputs survived unchanged. Spent-input reservation tombstones are intentional.

Profile: `examples/preprod-test.toml` (same as local `state/preprod-test.toml`). Manifest: `state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json`. Market: `examples/preprod-mvp.json`. Public address:

`addr_test1qq7ryz6elnkr7taz0ausqhq7aj2c9l605a34jcq9qhkrzvyg4crtgfpvxyrg6zqlfgq409m7ydc2a6smvy4mrp0g29msz8vk5s`

Private payment/stake keys remain in ignored local state with restricted permissions. Do not recreate this wallet, overwrite its keys or reuse confirmed intent IDs. No mainnet transaction was signed or submitted. The wallet stake key remained unregistered and undelegated: an address staking credential is required by v1's beacon policy, but active staking is not required.

| New strategy action | Transaction ID |
| --- | --- |
| Rebalance to 600,000 base units | `c8fb996bcde3df0b954a75ece13ba7820181b89936445d6fe4ab92bc5d312b70` |
| Publish both sides | `9ec00b87db82d0bd85f720fb6e682a2f852b3baa4a6821170167862fb485cdc8` |
| Controlled 50,000-unit fill | `424ad68d5c5be72f445eef5b89d5b1edd84ac380fdcbd1e27967952c721f70fd` |
| Automatic reprice cancellation | `b7e0f9b924f1e2788c9c7955dcc19a8b1eccff47e7695bb9ea63b01c13deede5` |
| Publish replacements | `2d4d2110b4e62aea8664c0645abe5d07aae75ec5017fbf50a2c135d6d6737db8` |
| Tracked final cancellation | `0cf073048f55b8e0a78dea3bcf5494ba3ab4b10722fad2a2f74bb6f40a26065e` |

## Run and extend

Follow [docs/operator.md](docs/operator.md). `trade` defaults to shadow mode; `trade --execute` runs the qualified preprod strategy. `stop` leaves orders open; `cancel --execute` separately tracks closure. `route` scans without executing by default. A custom trusted Python strategy can use the same `TradingEngine` through injected `decide`/`should_reprice` callbacks.

Execution remains preprod-only. Published v2, two-way orders, script/pointer stake authorization and batcher venues are unsupported. Second-provider live verification remains outstanding without credentials; the brief explicitly permits proceeding with Koios. Recovery failures are simulated deterministically rather than induced on the public chain. Hosted observations are trusted, non-atomic and can pause the runtime when history, dependencies or canonical blocks disagree. Fee budgets permit cleanup cancellation beyond the admission budget; collateral loss has its own cap. There is no performance/profitability certification.

Next smallest increment: qualify one batcher venue and exercise its asynchronous order lifecycle before expanding venue count.

## Implementation invariants

- Pure Python `cbor2` is required to preserve tagged input ordering and exact transaction hashes; use the repository's uv configuration.
- Dano outputs lead composed transactions; redeeming indices are resolved after ordering. Its isolated Dendrite session never mutates global backend/deployment/cache state.
- Preprod slot conversion includes 20-second Byron slots. Dano protocol epochs last 30 minutes; validity stays inside one protocol epoch. Pool minimum changes and active liquidity are enforced.
- Signing requires independently inspected destinations, asset deltas, input roles, minting, withdrawals, collateral and a fresh final evaluation for exact bytes. Missing collateral return means the entire collateral input is at risk.
- Unknown submissions retain exact candidates. Expiry requires mature canonical and explicit input evidence; positive competing spends permit safe retirement. Expiry anchors themselves remain subject to rollback checks.
- Canonical transaction-body replay tracks roots and continuations, reverses removed fills and does not infer fills from missing UTxOs. Confirmed regular spends and failed collateral spends retain tombstones, including after re-inclusion.
- The anonymous preprod 16-KiB request ceiling was explicitly qualified. The general default remains 1,000 bytes; other endpoints/tiers require their own qualification.
