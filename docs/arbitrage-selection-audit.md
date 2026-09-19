# Profit-first arbitrage selection — September 8, 2026

The latest run selected the highest **evaluated net ADA gain** each time. Its two-hop trades were not chosen to satisfy a hop target. Longer routes compete on profit after all costs, and all legs still share one transaction.

## Latest recorded run

[Read-only audit evidence](../evidence/preprod-arbitrage-selection-audit.json) covers run `7dc6ea4c`, September 8, 13:16–13:42 EDT:

- 11 Dano→Splash two-hop trades confirmed; their expected and confirmed net gains match.
- Total confirmed net: **942.736648 tADA**. Network fees: **7.609495 tADA**, already deducted from that net figure.
- Each of the 11 selected candidates had the highest net gain among that poll's fully evaluated candidates. Several winners changed position after actual fee evaluation.
- Zero pending or unverified outcomes at shutdown. Temporary unknown-status observations subsequently resolved to confirmation.
- One candidate failed the minimum-ADA continuation requirement during construction; it was not submitted.
- All 11 discovery/search polls reported limited coverage. The remaining polls mostly reconciled pending transactions. Logs did not retain every market row or rejected quote, so they cannot establish that an unevaluated route would not have been better.

Median observation/search/build-and-evaluation times were approximately 20.8/0.50/7.96 seconds. This audit checks recorded consistency, not a new independent chain observation. The last FUNDS snapshot preceded the final trade's settlement; shutdown reconciled that trade before the next wallet snapshot.

## Implemented improvements

[Shortlist ranking](../src/defi_kernel/arbitrage.py) previously charged 140,000 lovelace per Swaps hop and 180,000 for every other venue, plus a 155,000 base. That inherited estimate treated the four newly added venues alike and overcounted shared overhead for repeated script spends.

The replacement keeps the shared base and separates script overhead from each additional spend:

| Venue | Overhead per distinct spending script | Marginal per hop |
| --- | ---: | ---: |
| Swaps v1 one-way | 72,000 | 68,000 |
| Dano | 148,000 | 115,000 |
| Swaps v1 two-way | 100,000 | 66,000 |
| Splash | 185,000 | 75,000 |
| Genius Yield | 92,000 | 136,000 |
| SaturnSwap | 223,000 | 92,000 |

Values are lovelace and are **ranking heuristics only**. Inputs come from the recorded [fee-policy evaluations](../evidence/preprod-arbitrage-fee-policy.json) and [direct-venue evaluations](../evidence/preprod-direct-venue-evaluation.json). Different Splash validators count separately. Full Genius burns, Saturn complete-fill output counts, Dano withdrawals, body sizes and future protocol fees can differ from these priors. The model does not claim to predict those costs exactly or to establish an inclusion probability.

Across 20 evaluation samples and the 11 logged trades, mean absolute estimation error fell from 115,965 to 14,271 lovelace. This is retrospective fit using the calibration fixtures, not independent validation or a promise of improved live profit. A regression demonstrates the practical correction: a third same-script hop earning 100,000 extra lovelace now outranks the two-hop alternative when its estimated marginal cost is 68,000; the previous 140,000-per-hop model incorrectly reversed them.

[Final selection](../src/defi_kernel/arbitrage_runtime.py) uses the independently inspected **net gain** after actual fees. A larger transaction with even one additional lovelace of net gain wins. Only exact-profit ties use signed bytes, memory, CPU, hop count and finally route ID as deterministic preferences.

A shortlisted route can skip further work when its final ADA output minus initial ADA input is below an already-evaluated, still-valid candidate's net gain—even assuming zero network and venue fees. This reduces avoidable provider calls without using heuristic fees as an exclusion rule. Expired candidates cannot eliminate alternatives; equality still permits evaluation and resource tie-breaking. The three-candidate cap, expiry checks, asset restrictions, collateral and drawdown limits are unchanged.

JSONL diagnostics now include per-hop-length candidate counts, the shortlist's estimated fees/net gains, actual evaluated alternatives, skip reasons and the selection policy. Terminal output stays concise. The [post-change offline benchmark](../evidence/preprod-arbitrage-ranking-benchmark.json) covered two- through eight-hop candidates in a synthetic 112-edge graph, taking approximately 0.23–0.47 seconds at the soak's 1,024-cycle setting.

## What “best” means here

The objective is highest evaluated after-fee net ADA gain within the operator's limits. It is not maximum hop count, maximum gross proceeds or maximum profit-per-byte. Length-based search allowances provide coverage; they do not reserve execution slots for long routes.

Search is bounded, sizes use a finite integer grid, and only a small shortlist receives expensive evaluation. These choices keep observations fresh and provider traffic bounded, but **cannot guarantee the globally optimal trade**. Increasing work indefinitely can make the quote stale before it is actionable.

Hop count alone does not establish an inclusion advantage. Consensus maintains a ledger-valid mempool sequence and constructs blocks within capacity constraints; competing spends can make an expected input unavailable. See the [node-task description](https://ouroboros-consensus.cardano.intersectmbo.org/docs/explanations/node_tasks/) and [EUTxO explanation](https://docs.cardano.org/about-cardano/learn/eutxo-explainer). This motivates resource preferences for exact-profit ties, not an invented per-hop success probability. All swap legs remain in [one atomic transaction](https://developers.cardano.org/docs/learn/core-concepts/transactions/), so failed inclusion cannot leave a subset of the route's swaps settled.
