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

## Historical MVP test state

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

Execution remains preprod-only. Published v2, two-way maker publication, script/pointer stake authorization and batcher venues are unsupported. Direct two-way arbitrage fills are covered by the later increment below. Second-provider live verification remains outstanding without credentials; the brief explicitly permits proceeding with Koios. Recovery failures are simulated deterministically rather than induced on the public chain. Hosted observations are trusted, non-atomic and can pause the runtime when history, dependencies or canonical blocks disagree. Fee budgets permit cleanup cancellation beyond the admission budget; collateral loss has its own cap. There is no performance/profitability certification.

The subsequent atomic-arbitrage increment is described below; no batcher venue was added.

## Implementation invariants

- Pure Python `cbor2` is required to preserve tagged input ordering and exact transaction hashes; use the repository's uv configuration.
- Dano outputs lead composed transactions; redeeming indices are resolved after ordering. Its isolated Dendrite session never mutates global backend/deployment/cache state.
- Preprod slot conversion includes 20-second Byron slots. Dano protocol epochs last 30 minutes; validity stays inside one protocol epoch. Pool minimum changes and active liquidity are enforced.
- Signing requires independently inspected destinations, asset deltas, input roles, minting, withdrawals, collateral and a fresh final evaluation for exact bytes. Missing collateral return means the entire collateral input is at risk.
- Unknown submissions retain exact candidates. Expiry requires mature canonical and explicit input evidence; positive competing spends permit safe retirement. Expiry anchors themselves remain subject to rollback checks.
- Canonical transaction-body replay tracks roots and continuations, reverses removed fills and does not infer fills from missing UTxOs. Confirmed regular spends and failed collateral spends retain tombstones, including after re-inclusion.
- The anonymous preprod 16-KiB request ceiling was explicitly qualified. The general default remains 1,000 bytes; other endpoints/tiers require their own qualification.

## Atomic arbitrage increment — 2026-09-07

Implemented the accepted [atomic-arbitrage plan](docs/arbitrage.md): bounded allowlisted ADA cycles, exact intermediate balances, composed transactions, shared unsigned inspection/evaluation, provider batching, resource limits, optional Preprod execution, durable recovery and confirmed PnL. No dependency upgrade or new venue was needed.

Confirmed three-hop Swaps/Swaps/Dano transaction `7ecfef2b4190773140455f55a26e42cdb9f612c920c80da91095b7486616a481` gained 168,451 lovelace. Confirmed four-hop Swaps transaction `bf0d9d8381cabb62bb8c29478506595b538a2d999e43276d5812ae8d6091be9f` gained 249,840 lovelace. Both had zero intermediate wallet delta and preserved the original strategy collateral and faucet remainder. These are controlled maker transfers, not organic arbitrage profit.

The separate `arbitrage-maker` wallet was allocated 20 tADA and 600,000 fUSDA. Its public manifest is `state/preprod-e0e0961e12306509/arbitrage-maker/wallet/wallet.json`; keep its private keys and journal. Maker cleanup returns all remaining order assets and burns the order beacons. See the [strategy evidence](evidence/preprod-arbitrage-execution.json), [maker evidence](evidence/preprod-arbitrage-maker.json) and [resource probes](evidence/preprod-arbitrage-benchmark.json) for authoritative final holdings and chain confirmation.

Synthetic hosted evaluation passed 2/3/4/6 Swaps hops, two Dano pools in one batch, and a Swaps/native-token-Dano/Dano cycle. The eight-hop synthetic request exceeded the endpoint's byte ceiling; its execution budgets remain unmeasured. This is a bounded first strategy, not a global optimum or profitability certification. The default remains unsigned evaluated shadow mode; execution is Preprod-only.

Final verification: **155 tests pass**, Ruff lint/format checks and offline source/wheel builds pass. Both journals have zero pending transactions and both wallets have zero open orders. The strategy runtime is stopped. The strategy wallet holds **9,972.377974 tADA**; the maker holds **17.592265 tADA**, 300,000 ArbA, 600,000 ArbB and 300,000 fUSDA base units. Private state is excluded from the verified build archives.

## Arbitrage observability and extended-run readiness — 2026-09-07

Added timestamped terminal events, five-minute health summaries, debug/JSONL output, private rotating diagnostics capped at 256 MiB, and durable per-run history. Verified trade outcomes stay with the originating run, include actual collateral losses, reverse on rollback, and exclude historical fixture trades from new totals. Logging health gates original/recovered submissions after dependency checks and immediately before the wire request; failures cannot reset attempts or obscure a submission result. Ctrl+C, SIGTERM and terminal hangup request a graceful stop.

The separate [extended-run configuration](examples/preprod-arbitrage-soak.json) explicitly permits ADA and seven observed intermediate assets, with a 50-tADA cumulative wallet cost budget and unchanged per-trade safety limits. Quote-only Dano snapshots avoid repeated datum decoding while reusing upstream integer math. Search has a ten-second cooperative deadline and rotating traversal order, with limited coverage reported explicitly.

Three Preprod shadow polls completed without signing or submitting. [Two consecutive recorded polls](evidence/preprod-arbitrage-observability.json) searched 256 cycles each in 0.37–0.49 seconds; observation took 47–49 seconds and build/evaluation about eight seconds. The evaluated ADA/fBTC/fUSDM/ADA opportunities are estimates, not realized profit. Both new runs recorded zero realized profit and no transaction intents; the existing wallet still has zero pending transactions. The engine is stopped. Follow the [foreground run guide](docs/arbitrage.md#running-an-extended-preprod-experiment) for the Herdr terminal workflow.

Validation: **167 tests pass**, including log failure before/after submission, final provider gating, file/link/rotation safety, terminal control escaping, machine-output parsing, graceful interruption, run restart/pruning/rollback accounting and quote parity across reserve/minimum/reward boundaries. Ruff lint/format and offline source/wheel builds pass; private state and caches remain excluded from package archives. No new dependency was added.

## Operator display and multi-pool signing fix — 2026-09-07

The user's subsequent execution run produced four unsigned candidates, zero signatures and zero wire attempts. One unsigned candidate remains prepared in the stopped journal; normal restart reconciliation handles it. The private journal was inspected read-only during this fix.

Fixed pinned PyCardano's loss of long redeemer byte chunks through a shared exact-byte decoder used by shadow/execution validation, signing and submission claims. All four saved candidates now preserve complete bytes through decode and copy. No guard was relaxed and no real wallet key was opened. The [compatibility notes](docs/upstream-contributions.md#long-redeemer-bytes) document removal criteria and offline qualification limits.

Default output now uses compact wrapped blocks, shorter timestamps/IDs, quiet internal preparation/cleanup and deduplicated blockers. Blocked candidates are not shown as actionable opportunities. Collateral failures have a safe explanation, and validity-window errors distinguish unauthorized, expired and not-yet-valid candidates. Full diagnostics and machine output remain available. Validation: **171 tests pass**, Ruff lint/format and offline package builds pass. No new trading run was started.

## First public arbitrage run follow-up — 2026-09-07

Read-only journal inspection of run `6bd5e3e71b6a45af86448a0b771fd664` records one confirmed ADA/fBTC/fUSDM/ADA transaction (`edae1f79b7c8367e4ba0b6458131529b397cb0e3f60c37da820a25b80d522083`) and net gain **128.867992 tADA**. Its network fee was 1.230279 tADA plus 0.300000 tADA in fixed venue fees, already reflected in the net gain. The sole wallet output was 131.417671 tADA, exceeding the 100-tADA operating-input limit; the remaining original faucet output and 5-tADA collateral were untouched. The run stopped with zero pending transactions after operating-input rejections and provider HTTP 429 responses.

Text output now uses terminal width and combines events into single lines when possible. Missing operating ADA pauses before market scanning. Future large-profit builds explicitly preserve their selected operating amount in a separate authorized output, retaining funding/fee/collateral limits and protected-fund exclusion. This does not reclassify the existing oversized output: operating funds still need explicit allocation before another run.

HTTP 429 now applies a provider-wide, stop-aware cooldown, increasing local backoff from 60 to 900 seconds and honoring longer numeric/date Retry-After instructions. Submission retries remain disabled. Offline checks cover both Swaps and Dano operating-change layouts, cross-endpoint cooldowns, interruption and wide/narrow text output. No live run or wallet mutation was performed for this follow-up.

## Deadline safety and automatic ADA allocation — 2026-09-07

Arbitrage and allocation candidates now persist a submission deadline derived from final ledger expiry minus a configurable 30-second margin. The gate runs before signing/reserving, after submission dependency reads, and immediately before the hosted wire request after pacing/logging checks. Evaluation rate limits abort the poll, pending transactions reconcile first, and attempted/uncertain submissions retain their original bytes until canonical recovery. HTTP quotas remain adapter-specific; atomicity and recovery apply regardless of backend.

The ADA-only strategy targets a 10-tADA operating reserve and separate 5-tADA collateral. It restores missing roles automatically with an ordinary, independently authorized self-transfer funded by eligible operating inputs or exact outputs of confirmed, body-verified bot transactions. Unrelated protected funds remain excluded. Maintenance shares the outbox, fee budget, confirmation/rollback accounting and shadow/execute distinction. Its fees reduce confirmed run net. Healthy collateral is preserved. Replenishment currently requires one sufficient source output; insufficient safe capital pauses rather than consuming unrelated reserves.

An offline reconstruction from the recorded 131.417671-tADA proceeds produced 10.000000 tADA operating plus 121.249002 tADA remainder, with a 0.168669-tADA estimated fee and no collateral/faucet input. This is not a live fee quote or submitted transaction. No real keys were opened or wallet state changed. Regression coverage includes exact verified provenance, changed/rolled-back proceeds, missing collateral, actual signing with disposable test keys, allocation shadow mode, expiry during pacing/dependency reads and 429s around submission/confirmation.

## Aligned terminal fields and Koios credentials — 2026-09-07

Recurring human-readable fields now use fixed widths and stable ordering. Health, transactions and stop events share state/net columns; health and stop share pending counts. Unknown headroom/data age occupy the same columns as observed values. Terminal-width wrapping, full JSON fields and private diagnostics remain available.

The supplied Preprod profile now selects `token_env = "KOIOS_API_KEY"`. Missing/empty/malformed tokens fail before requests; raw exported tokens become Bearer headers, including with an injected HTTP client. Startup identifies only the variable name, and 401/403 errors point to credentials/permissions without exposing secrets. The operator guide includes a hidden Bash prompt. Ambient `.env` loading remains disabled. No authenticated live request or trading run was performed; the user will supply the token.

## Compact display and bounded collateral — 2026-09-07

Run `a34b28af` confirmed its reserve self-transfer (10 tADA operating, 0.168669-tADA fee); the existing 5-tADA collateral remained intact. Subsequent candidates failed at PyCardano 0.18.0's maximum-fee collateral estimate before final evaluation/signing. New candidates now size collateral from the authorized fee ceiling and network percentage, rounded up, with explicit owner return and minimum-ADA checks. The budget-estimation copy preserves the composition subclass and Dano output links. Construction rejects fees/losses above authorization; independent final inspection/evaluation/signing remain mandatory. Saved candidates are untouched.

Default events retain compact state/net alignment while placing amounts beside labels. Health keeps pending count, uptime, cost headroom and market age; technical counters/coverage and routine log status remain in debug/structured logs. Allocation omits zero collateral creation, and duplicate pending-state/current-run-ID noise is removed. Error and diagnostic-write failures remain visible.

Validation: 197 offline tests passed, Ruff lint/format passed, and offline source/wheel builds succeeded. Regression tests reproduce the SDK failure under a synthetic higher network resource ceiling, then inspect provisional/final mixed-route collateral with the existing 5-tADA reserve. Fee/loss limits, rounding and dust rejection are covered. Display checks cover narrow/wide terminals and persistent diagnostics. No live evaluation or submission was performed for this fix.

## Event namespaces — 2026-09-07

Terminal columns now read timestamp, namespace, event, severity, message. Lifecycle/access/funds/provider/logging events use SYSTEM; strategy decisions, transactions, allocation and health use ARBITRAGE. Wrapped text retains its namespace for filtering. Persisted/streamed JSONL events add `namespace`; prior records are unchanged. No trading behavior changed. Validation: all 44 reporting/arbitrage tests passed, including namespace filtering across wrapped lines and SYSTEM attribution for logging failures; Ruff lint/format and diff checks passed.

## Clear trade milestones and venue routes — 2026-09-07

Default output now uses TIME NAMESPACE EVENT STATUS | DETAILS, hides routine INFO, and retains explicit warning/error markers. Wallet snapshots share the compact status/separator columns. Submission lines combine expected net, fee, recorded hop venues and transaction reference; opportunity events remain in diagnostics and shadow output. Unknown/included/confirmed-but-unverified transactions display checking/confirming/verifying respectively. Raw event levels/states remain intact, with additive display_status, hop metadata and confirmation fields. Token names omit policy prefixes in normal output; diagnostic mappings retain full units and disambiguate duplicate/unnamed assets with numbered aliases.

Health and stop use journal-derived pending counts and verified run totals; stale pending/submitted stages no longer display alongside zero pending transactions. Saved hop metadata supports restart attribution without guessing venues. No construction/signing/submission/recovery logic or database schema changed.

Validation: all 200 offline tests passed, including lifecycle/restart, verification delay, rollback, logging failures, token labels, narrow/wide filtering and the eight-confirmed/one-pending replay from run 69b11bd3. That replay totals 186.618720 tADA and excludes the pending expected 34.870253 tADA. Ruff lint/format and diff checks passed. A recorded-log render is available locally at /tmp/defi-kernel-event-preview.txt; no live transactions were submitted.

## Compact swap notation — 2026-09-07

Routes now render as `N-way hop · INPUT->OUTPUT(Venue), ...`, with one segment per swap and the return to ADA included in the count. `Swaps` abbreviates Cardano Swaps. The shared formatter covers submitted trades and shadow opportunities; missing/inconsistent venue metadata still displays `venue unavailable`. Structured route data and execution behavior are unchanged. All 22 reporting tests and repository lint/format/diff checks passed, including two-/four-hop rendering, repeated/mixed venues and narrow/wide output.


## Profitability-based fee policy (September 8)

Arbitrage no longer inherits a fixed 1.5-tADA network-fee cap. Each candidate freezes the smaller allowance supported by composed net economics (after retaining the 0.1-tADA floor), collateral with a valid owner return, drawdown headroom, and any explicit optional fee cap. Allocation has its own 1.5-tADA cap. The 5-tADA collateral reserve is unchanged. The composition hook corrects SDK reference-fee overcounting per input occurrence; historical evaluated/signed bytes are not rewritten.

The soak configuration now selects a 50-tADA canonical drawdown limit across arbitrage history. The old gross-fee field is rejected with migration instructions. Verified net includes maintenance fees and collateral loss once; pending outcomes reserve their possible loss and never contribute expected profit. Shared preparation/submission checks enforce risk admission, including a fresh canonical accounting anchor on recovery. Loss-headroom reporting replaces cost-headroom reporting for new runs.

Confirmed script failures create durable wallet execution incidents in the same commit as failure settlement. Reconciliation continues while all new preparation/submission is paused. Restart and rollback cannot clear the incident. `arbitrage-acknowledge` requires the bound wallet manifest, full failed transaction ID and an investigation reason; it does not submit, rewrite bytes or reset accounting. Historical journals backfill incidents for recorded failures.

[Hosted shadow evidence](evidence/preprod-arbitrage-fee-policy.json) covers three Swaps hops, two Dano pools and a mixed Swaps/Dano route. All finalized candidates passed hosted script evaluation within assigned budgets, with fees 0.432623, 0.532299 and 0.676435 tADA respectively. Synthetic inputs only; no wallet keys, signing or submission. The probe refreshed its captured era horizon using live era summaries. Offline tests also qualify profitable fees above 1.5 tADA, exact profit boundaries, drawdown/reservation/restart/rollback behavior, incident acknowledgment and recovery admission.

Validation: 207 tests passed; repository-wide Ruff lint/format checks and `git diff --check` passed.

## Soak audit follow-up — 2026-09-08

Implemented the six approved follow-ups from run `1a100d5d6c5040c1b42163e056c3bd92` (12 confirmed trades, 6.482180 tADA net):

- Liquidity discovery stages provider reads outside row-exclusion handlers. Reward and reference failures invalidate the poll; stop requests remain actionable. Reward accounts are fetched in bounded batches, and malformed individual pool rows remain excluded.
- Shared Dano epoch clipping enables submission-window rejection before construction and balancing. Final evaluation checks the margin again; post-evaluation expiry filtering preserves a valid runner-up without extending validity.
- Canonical history/recovery-anchor reads batch up to 50 heights per request and refresh every poll. Pending work is reconciled first. Reference caching stores identities only and fetches fresh unspent bytes; missing/spent references trigger bounded discovery. Independent submission and chain identity checks remain intact.
- Reconciliation publishes durable transaction changes before market reads. Canonical block changes participate in event deduplication. Repeated identical settlement verification preserves the original timestamp, and cycle logs contain only new/changed settlement outcomes instead of complete historical arrays. SQLite remains authoritative for history, rollback and PnL.
- Profit-floor build failures retain precise numerical diagnostics but display a grouped, stable rejection reason. Search prioritizes distinct directed UTxO cycles over additional sizes, recalibrates Dano fee ranking after the reference-fee fix, and names pre-network-fee candidate counts explicitly.
- Reconciliation and observation timings are separate. The soak example searches up to 1,024 cycles with unchanged expansion, elapsed-time and evaluation bounds. The offline 112-edge mixed-venue benchmark measured 0.26–0.30 seconds at that cap; 4,096 hit the existing expansion limit first. This does not establish live throughput or optimality.

Validation: 222 tests passed; repository Ruff lint/format and diff checks passed. New regressions cover global provider failures, missing/duplicate/unregistered rewards, bounded canonical reads and rollback, fresh reference bytes and spent-reference recovery, epoch deadlines, expiry during evaluation, distinct-cycle selection, prompt/idempotent settlement reporting and phase timing. A bounded anonymous read-only Preprod probe matched batched heights against individual canonical reads, fetched two reward accounts in one request, and verified reference reuse still performs a fresh UTxO read. No wallet access, signing or submission in that probe.

Evidence: [search benchmark](evidence/preprod-arbitrage-search.json), [provider batching](evidence/preprod-arbitrage-provider-batching.json). Operator details: [arbitrage documentation](docs/arbitrage.md#september-8-soak-follow-up). Reproduce the offline benchmark with `.venv/bin/python scripts/benchmark_arbitrage_search.py`.

## Four direct arbitrage venues — 2026-09-08

Added and enabled Swaps v1 two-way, both selected Splash CPP validators, Genius Yield v1.1 and SaturnSwap V3 uncovered orders on Preprod. The existing eight-asset allowlist and risk limits are unchanged. All hops share one transaction, including complete-fill Genius NFT burns and Dano's leading outputs. Explicit deployment validation, integer arithmetic, exact datum witnesses and final index binding avoid the SDK's incompatible defaults.

[Qualification and boundaries](docs/venues.md) document 17 successful final hosted Plutus evaluations, including all four new venues in one four-hop route and a five-hop route adding Dano. Inputs were synthetic; no wallet key was opened and no transaction was signed or submitted. Public discovery confirmed matching Swaps two-way/Splash liquidity; Genius/Saturn require matching allowlisted pairs. Tests cover conservation, independent authorization, multiple fills with equal initial indices, provider datum verification and saved evaluation replay.

Validation: **262 tests passed**, Ruff lint/format and `git diff --check` passed. All 17 evaluated transactions rebuild byte-for-byte. The read-only cold/warm discovery took 42.3/18.9 seconds; bounded search took about 0.34 seconds.

## Preprod hop limit — 2026-09-08

Raised the arbitrage default and both Preprod examples from four to eight hops. Eight is the longest simple cycle possible with the current soak allowlist; all lengths from two through eight remain eligible. Split bounded search allowances across lengths to prevent deeper branches from crowding out short routes, with rotating length order and unused allowances carried forward. Construction now independently checks the operator's hop limit. Risk, transaction-resource and provider limits are unchanged.

[Offline comparison](evidence/preprod-arbitrage-hop-limit.json) used a synthetic 112-edge graph. Eight-hop searches at the 1,024-cycle setting took 0.24–0.48 seconds and returned pre-fee candidates spanning all seven lengths. This is search evidence, not new signed execution or live eight-hop validation.

Validation: **266 tests passed**, including every shorter cycle length, bounded dense-graph coverage across lengths, and rejection of an over-limit route at construction. Ruff lint/format and `git diff --check` passed.

## Profit-first strategy selection audit — 2026-09-08

Reviewed the latest eight-hop-enabled run: 11 confirmed Dano→Splash two-hop routes, 942.736648 tADA net, with zero pending/unverified outcomes at stop. All 11 selected the maximum evaluated net, and expected/confirmed gains matched. Every active search was bounded, so the logs cannot prove global optimality.

Updated shortlist fees to account for shared script overhead and measured venue differences; actual evaluated net remains the decision criterion. Exact-profit ties now prefer fewer bytes/resources. Added conservative pruning only when even a fee-free route cannot beat a still-valid evaluated candidate. JSONL now reports candidate hop coverage, shortlist estimates and skips. [Audit and limitations](docs/arbitrage-selection-audit.md) distinguish calibration, search measurements and historical log consistency from live execution qualification.

Validation: **268 tests passed**, plus Ruff lint/format and `git diff --check`. The post-change synthetic eight-hop search took 0.23–0.47 seconds at the soak budget. No transaction was signed or submitted during the audit.

## Retired Preprod campaign — 2026-09-08

The [campaign review](docs/preprod-campaign-review.md) records eight confirmed atomic trades (2, 4 and 8 hops), no failed trade and no pending submission in the saved records. Recorded expenditure is 48.482379 tADA. Further trials are retired; counters and budgets remain unchanged.

The one-off harness, campaign-only tests and exports are archived under ignored `local-reference/retired-preprod-campaign/`. Production recovery, CBOR, authorization, request-capacity and shared-backoff fixes remain, with independent venue-fee regressions. Wallets, journals and campaign SQLite state remain in their original ignored locations.

Final capital return is outstanding: `KOIOS_API_KEY` is absent from the cleanup session. Use the archived **cleanup** command in the review from a configured terminal; do not resume new trials. No live recovery was attempted during repository retirement.

Retirement validation: 303 maintained tests passed; the eight retained fee checks passed again after removing report-only formatting. Three offline archive recovery checks passed, and report regeneration succeeded against a temporary database copy. Ruff lint/format, local documentation links, archive hashes and `git diff --check` passed.

## Dolos Preprod provider integration — 2026-09-08

Added explicit chain, discovery, evaluation, submission and recovery adapters while preserving legacy Koios configuration and wallet/journal paths. Dolos MiniBlockfrost/MiniKupo supply local data and submission; Koios remains the unsigned evaluator with independent credentials and cooldown. Chain anchors and protocol parameters must agree before building. Missing or malformed evidence cannot release saved transactions, establish spending or authorize a new submission.

The [TrueNAS deployment guide](docs/dolos-preprod.md) includes a pinned-version container configuration, guarded full-snapshot bootstrap, read-only `provider-check`, shadow qualification and bounded execution/restart checks. Configuration binds the APIs to the NAS LAN address, runs without wallet keys or root privileges, and retains archive history for recovery.

Live installation and qualification remain outstanding. Compose configuration and bootstrap shell syntax validate, but this workstation's Docker socket denies access even outside the sandbox; no container was started, wallet opened or transaction submitted during implementation. Resource limits are provisional until measured on the NAS.

Validation: **347 tests passed**, including 44 Dolos/configuration/adapter cases covering incomplete evidence, recovery without resubmission, provider disagreement, credential isolation and evaluator cooldown. Repository Ruff lint/format and `git diff --check` passed.

## Simplified Dolos installation — 2026-09-10

The TrueNAS deployment is now one self-contained Compose YAML: inline Dolos configuration, guarded automatic first-start full-snapshot bootstrap, and persistent restart. Only an empty dataset writable by 568:568 and one source-path edit are required. The separate bootstrap script and TOML file were removed. Previous completed data directories remain usable; interrupted or unrecognized data is preserved and blocks startup.

Validation: 50 Dolos/provider/deployment tests passed, including six startup scenarios executed with stubbed Dolos commands and no downloads. Compose configuration, Ruff and diff checks passed. Actual NAS container startup and live qualification remain outstanding.

### TrueNAS inline-config startup fix — 2026-09-10

The first NAS deployment pulled the image but failed before starting Dolos: Compose rejected `configs.content` with a read-only service. Startup now writes the embedded TOML into the existing `/tmp` tmpfs and passes that file to bootstrap/daemon. No extra files, writable root filesystem, ACL changes or data reset are required. Six deployment regressions pass, exercising the generated TOML and bootstrap/restart guards; Compose validation, Ruff and diff checks pass. NAS startup still needs retrying with the updated YAML.

### Initial live Dolos checks — 2026-09-10

Read-only NAS API checks reached MiniBlockfrost and MiniKupo. Genesis reported Preprod magic 1 and system start 1654041600. Successive tips advanced from height 4,820,011 / slot 125,713,198 to height 4,820,277 / slot 125,719,531; the first block timestamp was 2026-06-14T00:19:58Z, so the node remained about 89 days behind. MiniKupo reported connected, indexes installed and checkpoint 125,716,450 between those samples. This confirms API reachability and forward progress, not current synchronization or trading readiness. Full provider qualification also requires KOIOS_API_KEY, absent in this diagnostic session. No signing, submission or journal changes were performed.

## Dolos live qualification and bounded shadow — 2026-09-12

[Qualification evidence](evidence/preprod-dolos-qualification.json) records 29 wallet outputs and two historical transaction bodies matching Koios byte-for-byte. Discovery completed in 4.81 seconds with 110 edges across Swaps v1 (33), Dano (61), Swaps two-way (6) and Splash (10). Genius Yield/Saturn had no eligible edges under the existing rules. Added 28 independently verified live reference hints to the Dolos example; runtime still rechecks every reference.

Two shadow cycles in run `92079cd535b14c6086b0f8eebdadcf59` paused before building/evaluating because Dolos and Koios disagree on pool cost and transaction/block memory limits. There were no signed/submitted transactions or pending work, and stored transaction status counts stayed unchanged. The public evaluator was used at a two-second request interval because this session lacks KOIOS_API_KEY; normal authenticated configuration remains unchanged. A longer evaluated soak is blocked, not completed. See the [parameter evidence and suspected governance cause](docs/upstream-contributions.md#live-qualification-september-12-2026).

Fixed a discovery exception escaping from malformed Dano SDK rows; known pool-validation exceptions now become row rejections at the shared decoder boundary. Parameter-mismatch errors now name affected fields. Validation: 354 tests passed, including a malformed-NFT/pool-asset regression; the 44 Dolos tests passed again after adding hints. Ruff lint/format and diff checks passed. No NAS image, database or engine safety limit was changed.

## Confirmed Dolos governance gap and interim routing — 2026-09-12

Preprod proposal `e641ec802bb109e150e920c6c0387e85f2efd30944a46d08d08212bde540f69c#0` enacted the disputed values in epoch 305; the equivalent mainnet proposal expired in epoch 653. Dolos's archive contains the proposal (original body hash verified), but v1.6.0's hardcoded Preprod outcomes omit it. Synced blocks therefore coexist with stale ledger parameters. The [diagnosis](docs/upstream-contributions.md#dolos-governance) records primary evidence and upstream repair requirements. No NAS database or container was changed.

Added an optional `ledger` capability (defaults to `chain`) for era history, parameters and rewards. The Dolos example explicitly assigns ledger/evaluation/submission to Koios and leaves chain/UTxOs/discovery/recovery local. Dolos submission also validates against stale state, so retaining it would risk rejecting correctly evaluated transactions. All bound peers retain network and canonical-anchor checks; distinct ledger/evaluator parameters must match exactly. Submission checks run at submission, allowing local recovery during hosted cooldowns.

Validation: **358 tests passed**. Live read-only qualification passed with 29 wallet outputs, historical body comparison and 110 edges. Two shadow cycles (`fa058e497bfe40b5ad6a3951e40f335d`) evaluated two candidates with zero build rejections, no signatures/submissions and no pending transactions. Hosted requests were publicly paced at two seconds because this session lacks the API key. Full evaluated soak and live hybrid submission remain unqualified; Dolos ledger/submission require a governance-correct build plus rebuilt state before restoring those bindings.

## Dolos replacement prepared — 2026-09-12

Updated the single Compose deployment to official `2.0.0-alpha.0`, pinning its verified amd64 image digest and storage v4. The actual registry tag has no `v` prefix. Removed snapshot/bootstrap shell machinery; the daemon opens fresh storage, syncs from genesis and resumes on restart. Existing source path, ports, resource limits and security settings remain in place. The deployment guide now provides one scoped, tested cleanup procedure for the disposable NAS database and failed imports; no backup or alternate installation is planned.

The image's binary and genesis files were extracted with manifest/layer digest verification because local Docker daemon access is unavailable. Native startup, both APIs, clean shutdown and restart passed in temporary local storage. A 12-second Preprod relay probe reached block 99 and shut down cleanly. **361 tests passed**, including cleanup refusal for active containers, nested mounts, symlinks and inspection failures. Ruff lint/format and diff checks passed.

The NAS has not been modified: its stop/cleanup/YAML replacement requires the documented TrueNAS operator steps. Keep Koios ledger/evaluation/submission routing until full sync, parameter parity and shadow checks pass. One bounded qualifying Preprod trade is authorized after these checks; no transaction was signed/submitted during preparation. Evidence records preparation separately from pending live qualification.
