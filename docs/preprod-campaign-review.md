# Retired Preprod campaign — September 8, 2026

Testing is concluded. Saved records show **eight confirmed atomic trades, no failed
trade and no pending submission**. The next case stopped during public-liquidity
discovery, before publication. This is historical evidence, not a fresh chain query
or a claim that the full 78-trade schedule completed.

## Results

| Case | Hops | Network fee (tADA) | Submission to inclusion slot (s) |
| --- | ---: | ---: | ---: |
| Swaps one-way | 2 | 0.369824 | 17.6 |
| Swaps two-way + one-way | 2 | 0.452267 | 12.1 |
| Saturn partial + Swaps | 2 | 0.621370 | 68.9 |
| Genius partial + Swaps | 2 | 0.519254 | 6.5 |
| Splash deployment 0 + Swaps | 2 | 0.542157 | 17.4 |
| Swaps one-way | 8 | 0.792152 | 11.0 |
| Genius + Saturn + Swaps | 4 | 0.906034 | 24.4 |
| Competition: 2-hop beats 4-hop | 2 | 0.370961 | 4.1 |

Measured trader deltas were ADA-only and independent venue-fee checks passed.
Gains were manufactured transfers from our maker, not organic profit. Network fees
in the table exclude venue fees and setup/cleanup costs.

Eight Swaps hops used 4,929 bytes; four mixed hops used 8,373. Script costs, datums
and outputs matter more than hop count alone. One sample per case in sparse blocks
cannot establish congestion performance. The eight-hop observation included a
restart; only its slot-inclusion timing is comparable, not its raw confirmation delay.

Recorded expenditure was **48.482379 tADA**, including acquired public inventory,
against the 200-tADA cap. Cleanup cost 17.069527 tADA across 51 confirmed operations
and one aborted preparation. Mixed-6 exhausted its five-attempt allowance. Costs
and attempt counters are preserved. Of 13,008 provider events, about 72% were
identity/tip/block requests, largely campaign bookkeeping across three wallets.

## Retained improvements

Production retains controlled malformed-CBOR failures, exact transaction identity
checks, recovery without duplicate submission, native-policy authorization,
shared provider pacing/backoff and candidate-local request-capacity handling.
Independent fee checks remain in [test_venue_fees.py](../tests/test_venue_fees.py).
The [request-capacity evidence](../evidence/preprod-request-capacity.json) supports
the 64-KiB provider request limit; the ledger transaction-size limit remains separate.
Normal arbitrage benchmarks and venue qualification fixtures remain maintained.

## Local archive and remaining capital

The one-off runner, helpers, campaign-only tests, qualification exports and reports
are archived under ignored `local-reference/retired-preprod-campaign/`, with verified
SHA-256 copies. They are not distributed with the project. Wallet keys/manifests,
signed transaction bytes, journals and `state/arbitrage-campaign-pilot/campaign.sqlite3`
remain at their original ignored paths. Regenerable reports were removed from the
active results directory after archiving. The archive README describes recovery.

**Capital return is still outstanding.** This session lacked `KOIOS_API_KEY`, so no
live cleanup was attempted. After provider access recovers, stop the ordinary soak
and run from the project root in a terminal with that variable exported:

```sh
.venv/bin/python local-reference/retired-preprod-campaign/scripts/benchmark_arbitrage_live.py cleanup --submit
```

Use `cleanup`, not `run` or `resume`: further trials are retired. Cleanup first
reconciles saved intents, cancels remaining owned orders, burns returned artificial
tokens and returns available ADA to the original source wallet within existing
budgets. Public native assets and a small ADA reserve remain in their wallet;
keep its keys. If access is blocked, retain the archive and state and retry the same
cleanup command later. A backoff interval does not establish the provider quota's
reset time. Inspect final inventory and `cleanup_verified` before declaring fund
recovery complete; never reset journals or attempt counters.
