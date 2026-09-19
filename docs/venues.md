# Direct arbitrage venues

The arbitrage strategy enables six integrations on **Preprod**: the existing Swaps v1 one-way orders and Dano pools, plus Swaps v1 two-way, Splash CPP, Genius Yield v1.1 and SaturnSwap V3. Every route settles in **one transaction**, including its protocol fees, order continuations and any required NFT burns. An oversized route is rejected; separate transactions cannot provide the same all-or-nothing settlement.

Both [example strategies](../examples/preprod-arbitrage-soak.json) explicitly select all six through `venues`. The startup `SYSTEM VENUES` line lists them; each JSONL cycle's `venue_edges` shows eligible directed edges, including zero counts. Venue selection and the exact token `assets` allowlist are independent. The existing eight-asset allowlist is retained: the observed Genius and Saturn orders currently use other tokens. Enabling those venues permits matching future liquidity without silently authorizing additional assets.

## Qualified scope

| Venue ID | Supported direct fills | Deliberate boundary |
| --- | --- | --- |
| `swaps-v1` | Existing published v1 one-way orders | Existing maker lifecycle unchanged |
| `dano` | Existing CLMM pools, including reward withdrawals | Existing network/configuration checks unchanged |
| `swaps-v1-two-way` | Either direction at its independent rational price | Published v1 only; filling existing orders, not creating two-way maker orders |
| `splash` | Either direction of the two selected basic CPP deployments | Eleven-field datum; no stable, weighted, balance or newer nonce-bearing pool variants |
| `genius-yield` | Partial and complete v1.1 orders, including hash datums and complete-fill NFT burns | Explicit Preprod config NFT and script identities; no inferred Mainnet deployment |
| `saturnswap` | Uncovered V3 orders, partial and complete ADA/token fills | No receipts, covered orders or legacy validators; token/token completion stays partial |

The default search limit is eight hops (configurable from two to eight), with every shorter arbitrage length eligible. Eight covers every simple cycle length possible under the soak strategy's current eight-asset allowlist; [search budgets are shared across lengths](arbitrage.md#configuration-and-limits). The practical limit also depends on transaction bytes, execution units, collateral, reference-script costs, provider request limits and available profit. Four- and five-hop mixed examples below demonstrate composition, not a universal hop guarantee.

Swaps' beacon policy requires an address staking credential, but no registration or delegation. This requirement is specific to Swaps: credential-less outputs on other venues are not rejected merely for being unstaked.

## Construction and security

[Direct adapters](../src/defi_kernel/venues.py) reuse the pinned Dendrite datum codecs, with exact integer arithmetic and explicit deployment identities. They preserve the observed input values, address credentials and datum representation. Before construction, each hop's input reference, venue, asset direction, amounts and fee are checked again. Authorization still checks complete wallet deltas: only ADA funds the route, every intermediate native-token balance cancels, and the required net ADA gain includes network fees, venue fees and minimum-ADA top-ups.

- **Swaps two-way:** validate both rational prices and all three beacon identities. Preserve the address, tokens and previous-input lineage in the continuation. Reserve minimum ADA before quoting ADA outflows.
- **Splash:** compute the constant-product quote with integer fee/treasury rounding, preserve pool NFT and LP tokens, and bind the embedded input index after the builder finalizes input order.
- **Genius:** resolve datum hashes through bounded Koios batches, verify each content hash and keep the original witness bytes. The bounded cache stores immutable witnesses only. Partial fills preserve contained payments/fees; complete fills pay the maker, attribute fees by input reference and burn the order NFT. Multiple fills merge the protocol fee output because the validator examines the first matching output. Each fill retains its quoted ADA allowance, which conservatively pays more than an optimized shared fee in some batches. Order start/end times intersect the transaction validity interval.
- **Saturn:** use its two-stage ceiling arithmetic, per-order payment datums and final input/output indices. The maker and fee-recipient alias case is excluded after the recorded deployment rejected separate matching outputs. Token/token full fills have an extra ADA condition in the published filler, so this adapter keeps those fills partial and checks the minimum fill again. These exclusions avoid repeatedly building known-invalid transactions.

Saturn's published Preprod reference-script UTxO was unavailable during qualification. The adapter therefore carries the exact deployed 5,233-byte V3 script in [package data](../src/defi_kernel/data/saturn-preprod-v3.hex), verifying its hash before use. This consumes transaction space and fees; ordinary limits still apply. A verified supplied reference script can replace the inline witness. No new script is deployed.

Provider failures during datum/config/reference discovery invalidate the observation, including rate limits. Missing individual datum witnesses make those orders ineligible. Reconciliation, submission deadlines, drawdown limits and collateral checks apply to all venues; no recovery or signing guard was relaxed.

## Sources and reproducibility

Exact spending hashes, policies and config identities are in [venues.py](../src/defi_kernel/venues.py). The pinned dependency graph is unchanged. The Saturn filler reference implementation's [MIT notice](../src/defi_kernel/data/SATURN-LICENSE.txt) accompanies the adapter.

| Primary source | Revision / relevant implementation |
| --- | --- |
| [Cardano Swaps published v1](https://github.com/fallen-icarus/cardano-swaps/tree/9ec41e7619f5ba9d3dd46dd194e2146098093721/aiken) | `9ec41e7`; two-way validator and beacon validation |
| [Splash CPP](https://github.com/splashprotocol/splash-core/blob/9fd951054ac7143de6acf491f36d1073e729ba90/plutarch-validators/WhalePoolsDex/PContracts/PPool.hs) | `9fd9510`; eleven-field pool ABI. Later source revisions have a different datum. |
| [Genius contracts API](https://github.com/geniusyield/dex-contracts-api/tree/421b673bab0d0b3970aa1f30d475384416b87b74) | `421b673`; `PartialOrder.hs`, `Utils.hs`, and Preprod deployment constants |
| [Saturn filler](https://github.com/Flux-Point-Studios/saturnswap-filler/tree/19b46fdb1a124770621cf23d986c083a598e353d) | `19b46fd`; `contract.ts`, `ratio.ts`, `fillV3.ts` and `outputs.ts` |

[Recorded public rows](../evidence/preprod-direct-venues.json) include both Splash scripts, configuration and reference UTxOs, hash-datum witnesses and protocol parameters. [Hosted evaluation evidence](../evidence/preprod-direct-venue-evaluation.json) contains **17 successful final evaluations**: individual/repeated fills, both pool/two-way directions, Genius/Saturn complete fills, a four-hop route using all four additions, and a five-hop route adding Dano. Each final transaction was rebuilt with measured execution budgets and evaluated again. The mixed route fees were 1.095181 and 1.383036 tADA respectively.

These evaluations use **synthetic additional UTxOs**, real deployed validators and unsigned transactions. They establish script acceptance and composition for the fixtures; they are not live fills, a deployment audit or proof of market profitability. [Offline tests](../tests/test_venues.py) replay their authorization, conservation and resource budgets and exercise input/output-index collisions. Existing signed execution evidence remains separate.

```bash
.venv/bin/pytest -q
# Optional public hosted requalification; never signs or submits:
.venv/bin/python scripts/qualify_direct_venues.py --evaluate
```

The harness allows 64-KiB requests for synthetic additional-UTxO overhead only. Production retains its configured request ceiling and final transaction limits. [Read-only discovery evidence](../evidence/preprod-direct-venue-discovery.json) separately records eligible liquidity under the unchanged allowlist: 42.3 seconds for the cold scan, 18.9 seconds with cached immutable datums/references, and about 0.34 seconds for bounded route search. These are two anonymous-provider observations, not sustained throughput measurements.
