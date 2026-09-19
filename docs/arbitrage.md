# Atomic arbitrage

`arbitrage` discovers ADA cycles across Swaps v1 one-way/two-way orders, Dano and Splash CPP pools, Genius Yield v1.1 orders and SaturnSwap V3 uncovered orders. Each candidate consumes all hops in **one transaction**. Separate chained transactions cannot provide rollback of earlier trades, so the engine never splits an oversized route into sequential swaps.

```bash
.venv/bin/kernel --config examples/preprod-test.toml arbitrage \
  --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json \
  --strategy examples/preprod-arbitrage.json --iterations 1
```

The default builds, independently inspects and finally evaluates qualifying candidates without opening keys, reserving inputs or writing submission candidates. Add `--execute` for bounded Preprod execution. Omit `--iterations` for continuous polling; the default interval is 30 seconds after each cycle completes. `stop` ends polling; restart reconciles original pending bytes before discovering new work. Existing `trade`, `run` and the original two-leg `route` command remain available.

See [direct venue qualification](venues.md) for deployment boundaries and mixed-route evaluation evidence.

## Configuration and limits

The JSON configuration binds `network` and `chain_id` and requires an explicit `assets` allowlist including `lovelace`. Units are exact policy-ID + asset-name hex, not tickers. Discovery considers every eligible order/pool joining allowlisted assets, excluding the strategy wallet's Swaps stake credential. Adding an asset permits trading through its eligible venues; it does not prescribe a particular route.

| Setting | Default |
| --- | ---: |
| `venues` | All six qualified Preprod integrations; explicit list in both examples |
| `max_hops` | 8; routes of every length from 2 through the configured maximum are eligible |
| `max_expansions` / `max_cycles` | 10,000 / 256; soak example uses 1,024 cycles |
| `size_attempts` per cycle | 64 |
| `max_evaluations` | 3 candidates per poll |
| `max_search_seconds` | 10; cooperative checks during traversal and sizing |
| `max_age_seconds` | 180 |
| `submission_margin_seconds` | 30; stop attempting submission this far before ledger expiry |
| `max_trade_lovelace` | 10,000,000 |
| `max_funding_lovelace` | 100,000,000 |
| `max_fee_lovelace` | `null`; optional additional cap for arbitrage trades |
| `max_maintenance_fee_lovelace` | 1,500,000; allocation transfers only |
| `collateral_lovelace` | 5,000,000 |
| `operating_target_lovelace` | 10,000,000; replenishment target, at most the funding limit |
| `max_drawdown_lovelace` | 20,000,000; the soak example explicitly selects 50,000,000 |
| `min_profit_lovelace` | 100,000 |

ADA notional limits the first hop, even when protocol inputs supply the route's net funding. Coin selection uses confirmed ADA-only operating inputs and a separate exact-size collateral output. Large UTxOs, datum/script-bearing outputs and intermediate-token wallet inventory are unavailable to the route. The loss limit measures drawdown from the highest verified cumulative arbitrage result, starting at zero and including earlier arbitrage runs in the same journal. Deposits and unrelated strategies do not replenish it. Admission reserves the candidate's possible collateral loss, or the actual network fee for allocation. A separate maker wallet has its own journal and allocation.

## ADA allocation and recovery

The strategy's asset allocation is 100% ADA: intermediate assets net to zero inside each arbitrage transaction. Operating, collateral and reserve outputs are different uses of the same ADA balance. Healthy collateral is not spent on successful trades. Trade outputs preserve an operating reserve when the proceeds can cover the extra output minimum; otherwise bounded ADA change remains usable.

Before market discovery, the engine can restore the operating target and missing collateral with a separate ordinary ADA transfer. It uses eligible operating funds or an exact wallet output from a confirmed, canonically reconciled and body-verified bot transaction. Only this self-transfer may consume verified bot proceeds larger than the trade funding limit. Unrelated protected outputs—including the original faucet reserve—remain excluded. The transfer has no scripts or collateral at risk, returns all inputs to the same wallet minus its bounded network fee, and passes the normal independent signing and resource checks.

Allocation fees reduce cumulative net and therefore available loss headroom, as well as confirmed run net; `ALLOCATION` identifies these maintenance plans separately from opportunities. Shadow mode only builds and inspects the plan. Execution uses the same durable outbox, confirmation and rollback handling as trades, and waits for pending work before acting again. Allocation currently uses one sufficient source output; if none can safely fund the target and fees, it leaves funds untouched. Missing usable operating inputs or collateral then pauses trading. Existing unrelated native assets are not automatically sold.

## Sizing, ranking and authorization

[The planner](../src/defi_kernel/arbitrage.py) searches simple directed cycles: no repeated intermediate asset, repeated consumed UTxO, branching, split fills or bundled independent cycles. It samples an integer grid and the first edge's capacity boundary. Forward quotes and monotone backward searches reduce ADA input when actual integer payments leave a remainder. A candidate is rejected if the attempt budget cannot find an exact fit. The search is deliberately bounded and does **not** prove a globally optimal size or route, even when cycle enumeration completes.

The Preprod default is **eight hops**, including in both examples. This covers the longest simple ADA cycle possible with the soak configuration's eight allowed assets: seven distinct intermediate assets, then ADA. A smaller allowlist naturally reduces the reachable length (the two-asset example can only form two-hop cycles). Increasing the number above eight would not add routes for the current allowlist; eight is the engine's current search ceiling, not a Cardano protocol limit. Reassess it when expanding the allowlist.

The hop limit is a maximum, never an exact target. Discovery shares its expansion/cycle budgets across lengths and rotates their order between polls, so long routes do not consume the entire budget before shorter ones are considered. Unused allowances carry forward to subsequent lengths. Sparse graphs, rounding, profitability and bounded sampling mean not every length will yield a candidate on every poll. Construction independently enforces the configured hop maximum, and all candidates still face transaction-size, execution-budget, provider-request, collateral, freshness and after-fee profit checks. Routes remain atomic in one transaction.

The [hop-limit benchmark](../evidence/preprod-arbitrage-hop-limit.json) compares four/eight-hop settings on a synthetic 112-edge mixed graph. With the soak's 1,024-cycle budget, eight-hop searches took 0.24–0.48 seconds across three rotations and produced pre-fee candidates of every length from two through eight. These are offline search measurements, not live profitability or eight-hop script-execution evidence. Reproduce with `scripts/benchmark_arbitrage_search.py --max-hops 4 8 --output evidence/preprod-arbitrage-hop-limit.json` using the project Python environment.

Ranking estimates network fees as a shared 155,000-lovelace base, a per-spending-script overhead charged once, and a marginal cost per hop. The [selection audit](arbitrage-selection-audit.md) records the six venue priors and their calibration limits. Venue fees and minimum-ADA allowances already reduce each route's quoted gain and are not charged again by this estimate. Final built fees and independently inspected wallet deltas alone authorize execution.

The best estimated size of each distinct directed UTxO cycle gets an evaluation slot before additional sizes of those cycles. The winner has the **highest evaluated net ADA gain**, regardless of hop count or profit-per-byte. Exact net ties prefer fewer signed bytes, then lower memory/CPU, then fewer hops and a deterministic route ID. A shortlisted route can skip construction only if its ADA output minus ADA input, even ignoring all network/venue fees, is below the net gain of a still-valid evaluated candidate. Heuristic fee estimates never prune or authorize execution.

`pre_fee_candidates` counts quotes that clear the profit floor before network fees; it is not a count of executable profitable trades. Diagnostics record `candidate_cycles_by_hops`, each `shortlist` estimate, actual `evaluated_candidates`, `skipped_candidates` and `selection_policy`. Search budgets, coarse integer sizing and limited evaluations can still miss a better route. `search_truncated`, `stop_reason`, `sizing_exhausted`, counts and individual build rejections expose those limits. Rotating bounded sampling is not proof of exhaustive coverage. Time limits are cooperative, with bounded sorting/reporting afterward; stop requests are checked during search and between provider reads.

Dano discovery constructs private quote snapshots containing a copied datum and integer reserves. The pinned SDK's integer quote methods run against these values without repeatedly decoding CBOR. Snapshots never construct transactions. Rewards refresh in bounded account batches on each observation. Reference-script caching retains only UTxO identities: every use fetches and validates fresh unspent bytes, with spent references rediscovered. Chain identity checks, final dependency rechecks and signing remain independent. Provider failures invalidate the entire discovery poll; malformed individual liquidity rows are still excluded.

[The runtime](../src/defi_kernel/arbitrage_runtime.py) composes the existing protocol builders, nets every protocol input/output and withdrawal, and requires every intermediate asset to balance exactly before wallet funding. The final inspector independently requires ADA gain above the configured floor and zero change in every other wallet asset. It commits protocol continuations, destinations, input roles, minting, withdrawals, signers and validity before balancing. Minimum-ADA deposits and Dano fixed fees affect the actual net delta; pool fees are already in the integer quotes.

Dano pools share their batch withdrawal and reference inputs, retain leading continuation outputs, and resolve packed indices after sorting. Each pool's minimum input, reserve capacity, rewards and protocol-epoch validity apply to the whole transaction. Reference scripts, output value size, collateral count, total execution budgets, estimated signed size and provider request size are checked before signing; exact signed size is checked afterward. Hosted evaluation covers the finalized scripts but is not phase-1 validation or chain confirmation. A script failure can still consume authorized collateral.

Unchanged rows reuse decoded state, while rewards, fees, carrier sizing and freshness are refreshed. Lookups are batched under the selected provider's byte ceiling and reject duplicate/unrequested identities; missing dependencies fail final rechecks. Context resolution is cached only within a build context. Fresh coordinator and signer checks bypass that cache. REST traversals remain non-atomic and trust the selected hosted provider.

## Qualification evidence

The funded test used a separate maker allocated **20 tADA and 600,000 fUSDA**, plus two disposable native assets. The strategy retained its original 5-tADA collateral and large faucet remainder. Maker orders were controlled; wallet gains below are transfers from that fixture, not evidence of organic profitability or a long-running strategy's performance.

| Confirmed route | Signed bytes | Network fee | Net ADA gain |
| --- | ---: | ---: | ---: |
| ADA → ArbB → fUSDA → ADA, Swaps/Swaps/Dano | 2,166 | 912,461 | 168,451 |
| ADA → ArbA → ArbB → fUSDA → ADA, four Swaps orders | 2,550 | 720,160 | 249,840 |

Both used 30,000-lovelace initial notional and had zero intermediate-token wallet delta. The first trade is `7ecfef2b4190773140455f55a26e42cdb9f612c920c80da91095b7486616a481`; the second is `bf0d9d8381cabb62bb8c29478506595b538a2d999e43276d5812ae8d6091be9f`. The four-hop Dano alternative failed the original profit floor; replacing a depleted maker order supplied a qualifying four-hop Swaps cycle without relaxing that floor.

[Execution evidence](../evidence/preprod-arbitrage-execution.json) records chain CBOR, canonical inclusion, input observations, final wallet deltas and decisions. [Maker evidence](../evidence/preprod-arbitrage-maker.json) records publication/replacement/cleanup and final holdings; the funding transfer is in the strategy evidence. [Resource probes](../evidence/preprod-arbitrage-benchmark.json) distinguish synthetic evaluation from funded execution:

| Synthetic Swaps hops | Estimated signed bytes | Assigned memory | Assigned CPU | Result |
| --- | ---: | ---: | ---: | --- |
| 2 | 1,404 | 916,979 | 286,213,852 | Final hosted evaluation passed |
| 3 | 1,961 | 1,431,305 | 454,509,245 | Passed |
| 4 | 2,518 | 1,960,448 | 633,272,083 | Passed |
| 6 | 3,632 | 3,063,187 | 1,022,200,095 | Passed |
| 8 | 4,746 | Unmeasured | Unmeasured | Synthetic request was 17,952 bytes; rejected before sending |

The eight-hop ordinary transaction request would be 9,591 bytes, but the probe additionally supplies synthetic UTxOs and exceeds this endpoint's 16,384-byte request ceiling. Its placeholder budgets are not measured results. The captured ledger limits are 16,384 transaction bytes, 17,500,000 memory units, 10,000,000,000 CPU units and 204,800 reference-script bytes. There is no universal supported hop count: datum size, protocol combination, script costs and provider limits matter.

A two-Dano-pool batch and a Swaps/native-token-Dano/Dano cycle also passed final hosted evaluation with synthetic inputs, including the chunked 70-byte packed batch redeemer. These paths have structural/script qualification; they were not submitted against actual public pools. No mainnet or sequential-chain execution is enabled.


## Profit authorization and loss controls

[September 8 hosted qualification](../evidence/preprod-arbitrage-fee-policy.json) evaluated finalized synthetic three-hop Swaps, two-pool Dano, and mixed Swaps/Dano candidates with corrected reference fees and dynamic fee allowances. All measured execution budgets fit the assigned budgets. This is shadow evaluation, not evidence of submission, inclusion, or realized profit.

A trade must return at least `min_profit_lovelace` more ADA than it consumes from the wallet, after network fees, venue costs, and minimum-ADA deposits, with no intermediate-token balance change. Profitability applies to the completed transaction body, not just the search quote. Fee allowances are frozen before balancing and checked again independently before signing. Reference-script fees count distinct input occurrences, not repeated uses of a shared input; see [the compatibility note](upstream-contributions.md#reference-script-fee-accounting).

`drawdown = peak_verified_cumulative_net - verified_cumulative_net`. Loss headroom subtracts both drawdown and pending loss reservations from the configured limit. Network and venue fees already embedded in wallet deltas are never subtracted again. For example, a 100-tADA verified profit followed by a 1-tADA allocation cost has 1 tADA of drawdown and 49 tADA of headroom under the soak limit, before reserving a new transaction. A profitable trade restores headroom up to the limit; it never increases the maximum collateral exposure. Canonical outcome order determines the peak, and rollbacks remove invalidated results before recomputing it. Unverified outcomes block new execution.

The first confirmed on-chain script failure creates a durable incident and pauses new preparation and submission across the wallet, including recovery of saved transactions. Reconciliation and diagnostics continue. An incident remains pending across restarts and rollbacks until explicitly acknowledged. A verified arbitrage body/delta mismatch also blocks execution and must be fixed; it cannot be waived by an incident acknowledgment. Rejected builds, submission expiry, 429 responses and delayed chain visibility do not create loss incidents.

After investigating and correcting a script failure, stop the running process and record the resolution:

```bash
.venv/bin/kernel --config examples/preprod-test.toml arbitrage-acknowledge \
  --manifest PATH_TO_WALLET_MANIFEST --txid FULL_FAILED_TRANSACTION_ID \
  --reason 'Cause identified, corrected, and independently qualified'
```

This command records an acknowledgment under the wallet lock. It does not submit, alter transaction bytes, reset drawdown, or override any other execution check. Ordinary `status` output includes pending incidents.

For an older strategy file, replace `max_total_fees_lovelace` with an explicitly chosen `max_drawdown_lovelace`; the loader rejects the old field rather than silently reinterpreting a spending cap as a loss limit. Historical reports keep their original cost fields. Older pending transactions continue to reconcile unchanged. A historical signed-but-never-submitted arbitrage candidate without drawdown authorization cannot be newly submitted: let it expire and reconcile before creating a fresh candidate.

## Reproducing and observing

The default terminal columns are `TIME NAMESPACE EVENT STATUS | DETAILS`. A compact shared status column aligns wallet `snapshot` rows with trading milestones. Routine `INFO` is hidden; warnings and errors retain explicit `[WARN]`/`[ERROR]` markers. Debug output includes all severity levels. Lines use the current terminal width and wrap without truncation; redirected output defaults to 120 characters. Wrapped lines repeat timestamp and namespace, so `awk '$2 == "ARBITRAGE"'` retains complete messages.

`SYSTEM` covers lifecycle, access, wallet balances, provider requests and logging failures. `ARBITRAGE` covers decisions, opportunities, transactions, strategy allocation and health. Persisted and streamed JSONL events retain uppercase `namespace`, `level`, event names and raw states; `display_status` supplies the human label. Filter JSONL with `jq 'select(.namespace == "ARBITRAGE")'`. Older saved records remain unchanged and may lack newer fields.

Execution emits one submitted transaction line with **expected net**, fee, venue-labeled route and transaction reference. The corresponding opportunity event remains in diagnostics. Shadow mode displays the opportunity without implying submission. Routes show each recorded swap as `INPUT->OUTPUT(Venue)`, separated by commas, such as `2-way hop · ADA->MIN(Dano), MIN->ADA(Swaps)`. The count includes every swap, including the return to ADA; `Swaps` abbreviates Cardano Swaps. Historical records lacking hop information say `venue unavailable`. Token names are escaped; policy prefixes appear only in debug output. Duplicate or unnamed tokens receive deterministic numbered aliases, with the full unit-to-label mapping in diagnostic `asset_labels` events. Exact configured asset units remain authoritative; display names never authorize a trade.

| Terminal status | Meaning |
| --- | --- |
| `submitted` | Submission acknowledged; the displayed net is an expectation. |
| `checking` | Chain evidence is unavailable; monitor the original saved transaction. This does not imply failure or a resubmission. |
| `confirming` | Inclusion observed; shows confirmation depth, e.g. `1/3`. |
| `verifying` | Confirmed on-chain, but the wallet result has not yet been independently verified. |
| `confirmed` | Verified net is available for this transaction. |
| `failed` / `rolled back` | Failure or rollback remains visible with a warning; an unverified collateral loss is not presented as a known amount. |

Five-minute health and stop summaries show **run net** from verified outcomes only. Pending transaction counts are distinct from results still being verified; health checks current journal status rather than repeating a stale pending state. Health also shows uptime, loss headroom and market age. `market not observed` means no snapshot exists yet. Funds rows are wallet observations, not confirmed profit. Monetary values retain six decimal places. Short transaction/run references identify terminal events; full references remain in diagnostics and `status`.

`--debug` adds preparation, cleanup, phase, search, rejection and provider diagnostics. `--output json` retains polling report objects; `--output jsonl` emits versioned events with UTC timestamps and no ANSI styling. Repeated unchanged milestones/blockers are quiet. Reserve maintenance remains identified separately from trading and omits collateral creation when none is needed. Logging failures remain visible and retain their submission gate.

A candidate that expires or has not reached its validity window is rejected; the next poll searches again. Collateral is sized against the authorized fee ceiling, rounded up using the network collateral percentage. The fee allowance is the smallest of the composed ADA surplus minus minimum profit, the fee supported by available collateral/loss headroom, and any explicitly configured operator cap. It reserves enough ADA for a valid collateral return; it never automatically enlarges the collateral input or forfeits dust. Collateral therefore follows the bounded allowance, not the entire expected profit. For example, a 1.5-tADA allowance at 150% requires 2.25 tADA and returns 2.75 tADA from the existing 5-tADA input. Successful script execution leaves that input untouched. Insufficient collateral or a return below minimum ADA is rejected with numeric diagnostics; fee and loss limits are still checked independently. See [the bounded collateral integration](upstream-contributions.md#bounded-collateral). A CBOR round-trip mismatch blocks signing and is now checked during shadow evaluation as well. See [the long-redeemer compatibility fix](upstream-contributions.md#long-redeemer-bytes).

Each run gets a persistent ID. New transaction intents retain that ID across restarts; a later confirmation belongs to its originating run. Confirmed net results come from independently verified transaction bodies, and confirmed script failures subtract actual collateral loss. Fees embedded in wallet deltas are not subtracted again. Positive rollback evidence removes previously counted outcomes. Pending/unknown transactions and shadow opportunities are not realized profit. Historical transactions without run IDs remain separate, including the earlier maker fixtures. A saved report is only as current as its last successful reconciliation.

Diagnostics are automatically written to `logs/arbitrage.jsonl` beside the wallet journal: one 16-MiB file and fifteen backups, bounded to 256 MiB total. The directory is private (0700), files are private (0600), and symlinks/hardlinks are rejected. Polling decisions, transitions and metrics are recorded by default; `--debug` adds safe request status/latency/retry fields and exception stack locations, never request/response bodies, credentials, keys or SDK object dumps. Oversized diagnostic records explicitly report truncation. Run summaries and trade evidence stay in SQLite independently of log rotation and the existing 1,000-poll retention.

HTTP 429 starts a provider-wide cooldown: 60 seconds initially, increasing to at most 15 minutes of local backoff, with longer numeric/date `Retry-After` instructions honored. Successful reads do not reset escalation until five minutes beyond the prior cooldown. No endpoint is queried during the cooldown; the polling wait remains interruptible and health output continues. Submission attempts are never retried. Cooldown state is local to the process, so avoid restarting repeatedly to bypass it.

A 429 during candidate evaluation stops that poll immediately. Each new candidate stores an absolute submission deadline derived from its final ledger expiry and the configured margin. It is checked before signing/reserving, after submission dependency reads, and immediately before the hosted submission request after request pacing and logging gates. Recovery cannot extend that deadline. An attempted or uncertain submission retains its exact bytes and reservations until canonical inclusion, expiry or conflict is proven; an unavailable confirmation lookup never authorizes a duplicate. Pending transactions are reconciled before historical ones.

No hosted API can promise quota or timely inclusion. A rejected or delayed submission can miss an opportunity, but cannot execute only some legs: all hops are in one ledger transaction. A 429 after transmission can delay knowledge of its result, not interrupt the ledger's execution. HTTP cooldowns belong to the Koios adapter; local-node integrations would still need the same validity deadlines, atomic construction and uncertain-submission recovery for transport failures. No local-node backend or sequential multi-transaction arbitrage is added here.

An unavailable diagnostic log pauses new submissions while reconciliation continues. Every submission, including original prepared bytes recovered after restart, must pass a successful flushed/synced log-write check after dependency reads and immediately before the provider sends it. Healthy writes restore eligibility; all ordinary execution checks still apply. If the final provider check fails after the journal has claimed an attempt, the candidate remains unknown for reconciliation even though no bytes were sent; the attempt is never reset. Diagnostic failures after transmission do not alter its recorded result or permit retries. An unavailable authoritative journal stops execution.

## Running an extended Preprod experiment

The supplied `examples/preprod-test.toml` now requires `KOIOS_API_KEY`. In the same Bash/Herdr terminal where you will run the bot, enter the raw token (without `Bearer `):

```bash
read -rsp "Koios API token: " KOIOS_API_KEY
export KOIOS_API_KEY
printf '\n'
```

The hidden prompt keeps the token out of terminal output and shell command history. It applies to programs started from this shell; repeat in a new shell. No `.env` file is automatically loaded. The config contains only `token_env = "KOIOS_API_KEY"`; a missing, empty or malformed value fails before requests. `ACCESS` shows the variable name, never the token. Other profiles can select their own variable with `token_env`, or omit that field for intentional anonymous access.

Koios uses `Authorization: Bearer <token>` as described in the [Cardano provider guide](https://developers.cardano.org/docs/get-started/koios/). The client adds the prefix itself. HTTP 401/403 errors identify the variable to check without printing credentials or response bodies. Authenticated access still follows the token's provider limits; the existing cooldown and submission guards stay enabled.

Use [the expanded configuration](../examples/preprod-arbitrage-soak.json) for public liquidity. Its drawdown limit is **50 tADA**, measured across existing and future arbitrage runs in this wallet's journal; the baseline example and engine default use 20 tADA. Profitable trades can continue even when their cumulative fees exceed that amount. The minimum net profit remains 0.1 tADA and collateral remains 5 tADA. An omitted `max_fee_lovelace` no longer imposes a fixed 1.5-tADA trade ceiling; explicitly supplied caps remain binding. The September 7 read-only scan found ADA cycles involving the following exact Preprod units:

| Label (not an issuer attestation) | Unit | Observed venue |
| --- | --- | --- |
| fBTC | `007c4fc75b7662fc735177aa714da9d1b06af5644df199fef39e5fc166425443` | Dano |
| fUSDM | `834a15101873b4e1ddfaa830df46792913995d8738dcde34eda27905665553444d` | Dano |
| fUSDA | `9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5026655534441` | Dano |
| tMILKv2 | `bd976e131cfc3956b806967b06530e48c20ed5498b46a5eb836b61c2744d494c4b7632` | Swaps v1 |
| `OtherToken\n` | `c0f8644a01a6bf5db02f4afe30d604975e63dd274f1098a1738e561d4f74686572546f6b656e0a` | Swaps v1 |
| USDM | `d4fece6b39f7cd78a3f036b2ae6508c13524b863922da80f68dd9ab75553444d` | Swaps v1 |
| MIN | `e16c2dc8ae937e8d3790c7fd7168d7b994621ba14ca11415f39fed724d494e` | Dano, Swaps v1 |

These passed supported deployment/datum/value discovery checks and appeared in ADA cycles. That is not evidence that every route is executable or profitable: exact sizing, fee/resource limits and final transaction validation still decide. No discovered token automatically enters the allowlist. The probe had 113 eligible directed edges and 3,382 structural cycles; the default search intentionally samples fewer.

[The extended shadow evidence](../evidence/preprod-arbitrage-observability.json) records two consecutive polls with rotated traversal order. Both searched 256 cycles in 0.37–0.49 seconds, versus approximately 72 seconds in the earlier uncached probe. Observation took 47–49 seconds and candidate build/evaluation took about 8 seconds. Both evaluated a three-Dano ADA/fBTC/fUSDM/ADA opportunity; expected net gains around 128.87 tADA were **not realized**. No transaction intent, signature or submission was created, the wallet retained zero pending transactions, and recorded run profit stayed zero. These measurements describe this hosted endpoint and observed graph, not a throughput guarantee.

From the repository root, try one shadow cycle in your Herdr terminal:

```bash
.venv/bin/kernel --config examples/preprod-test.toml arbitrage \
  --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json \
  --strategy examples/preprod-arbitrage-soak.json --iterations 1
```

Replace `--iterations 1` with `--iterations 10` for a bounded shadow trial. Review operating versus protected funds, allocation plans, data age, search coverage, rejection reasons and log health. The large faucet output remains protected; a large headline balance does not imply spendable operating inputs. Verified bot proceeds can now automatically replenish operating funds and missing collateral before trading. Shadow mode previews that transfer without signing it.

When ready, start continuous execution explicitly:

```bash
.venv/bin/kernel --config examples/preprod-test.toml arbitrage \
  --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json \
  --strategy examples/preprod-arbitrage-soak.json --execute
```

Keep the process running and the machine awake. The default 30-second interval is a delay **after** each cycle; discovery/evaluation takes additional time. Ctrl+C, terminal hangup and SIGTERM request a graceful stop, checked between provider reads and during search. A submitted transaction can confirm after stopping. The original journal bytes and reservations survive interruption; restarting reconciles them before new work. A changed strategy fingerprint leaves old prepared work pending rather than reinterpreting its authorization. This foreground workflow does not install an automatic restart service.

Inspect saved state and history from another window or after stopping:

```bash
.venv/bin/kernel --config examples/preprod-test.toml status --output text
.venv/bin/kernel --config examples/preprod-test.toml status --output text --run all
.venv/bin/kernel --config examples/preprod-test.toml stop
```

Pass `--run RUN_ID` for one run, or omit `--output text` for JSON. Five-minute terminal summaries show pending trades, confirmed net results, market-observation age and loss headroom. Search counts, routine log health, phase timings and rejection details remain in diagnostics and debug output. Repeated errors/opportunities are deduplicated, while each polling report remains in the diagnostic log. No opportunity is a valid result; shadow gains are estimates, and public Preprod liquidity does not guarantee profitable execution.

```bash
.venv/bin/pytest -q
.venv/bin/python scripts/benchmark_arbitrage.py --evaluate
```

The optional [maker helper](../scripts/qualify_arbitrage.py) is an explicit test-only flow, with one-time `wallet`, `fund`, `publish`, optional `replace-empty`, and `close` actions. Signing requires `--submit`. Its input cap is 30 tADA, publication mints only the two fixture assets, and cleanup aggregates reclaimed deposits into independently inspected wallet change. Existing intents are reported rather than automatically retried. Preserve both wallet manifests, keys and journals in ignored `state/`; never delete them to repeat a test.

After cleanup, the maker holds 17.592265 tADA, 300,000 ArbA, 600,000 ArbB and 300,000 fUSDA base units, with no open orders. The two final unsigned polls reported no opportunity under the default allowlist, confirmed both trade deltas, and then stopped.

## September 8 soak follow-up

The [offline mixed-venue benchmark](../evidence/preprod-arbitrage-search.json), reproducible with `.venv/bin/python scripts/benchmark_arbitrage_search.py`, exercised a synthetic 112-edge graph at three traversal rotations. Increasing the cycle cap from 256 to 1,024 took 0.26–0.30 seconds and found 144–154 distinct candidate cycles versus 3–49. A 4,096 cap hit the unchanged 10,000-expansion limit first. The soak example now uses 1,024; the default stays 256. These are CPU measurements on synthetic liquidity, not live throughput or profitability guarantees.

[Read-only Preprod qualification](../evidence/preprod-arbitrage-provider-batching.json) verified batched canonical heights against individual reads, two reward accounts in one request, and reference reuse with fresh UTxO validation. Terminal transaction and recovery-anchor blocks are checked in batches of at most 50 every poll; nothing is cached across polls. Missing canonical evidence pauses execution, and conflicting hashes trigger the existing rollback handling. Pending transactions are reconciled before historical batches. Submission retains its own fresh checks.

Dano routes intersect the observation deadline and protocol epoch before construction. Submission-margin checks run again before balancing and final evaluation. Candidates that expire while other candidates are evaluated are excluded before selecting the winner; validity is never extended. Below-floor built fees now report `profit_floor`, with numerical details in diagnostics and a grouped terminal reason, rather than an apparently arbitrary fee ceiling.

Transaction transitions publish after reconciliation, before market reads/builds. Unchanged transitions are deduplicated; verification timestamps remain stable on repeated reads. `reconciliation_seconds` is recorded separately from `observation_seconds`. New cycle records contain only newly verified or changed `settlement_changes`, plus journal-derived risk totals; historical cycle `settled` arrays remain readable as historical records. SQLite and `status --run all` retain complete canonical outcomes, including restart attribution and rollback handling.
