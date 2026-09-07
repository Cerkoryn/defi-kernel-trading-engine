# Integration qualification — 2026-09-05

This records source, read-path and execution qualification. Published Swaps v1 and Dano have passed hosted fixture evaluation and designated preprod execution, including an atomic two-leg transaction. The source repository initially contained only the implementation brief. See [live execution evidence](../evidence/preprod-live-execution.json) and [operator commands](preprod-execution.md).

## Pins and reproducibility

`uv.lock` pins the full dependency graph. Tested with Python 3.12.14, Dendrite 1.5.10 at `0a1e02505af9d92506e8adbaf42307d923b1f061`, PyCardano 0.18.0, and cbor2 5.9.0. PyCardano's datum deserializer requires `typing.Union`; lint modernization to `A | B` breaks decoding and is disabled specifically for the datum adapter.

Authoritative source revisions inspected:

| Source | Revision |
| --- | --- |
| Dendrite | `0a1e02505af9d92506e8adbaf42307d923b1f061` |
| Swaps current head | `5657d09c2f85dec74b28f57ec6e71cfd283620e9` |
| Published Swaps v1 | `9ec41e7619f5ba9d3dd46dd194e2146098093721` |
| Historical expiration deployment | `75aa2e0f304ab00feb09608c8187140f20cdf74b` |
| Published Swaps v2 | `0b24fc374c8b30ca5f46b70ab4e078cdd7333e2f` |
| Dano SDK | `feaf32051d91c0a5b928c080133d88fb55c8e3a8` |
| Meditations blog | `8bf6b41d8588cf447e5f5f5ce72ca1cfe7d86f2e` |

The downloaded [Koios specification](https://github.com/cardano-community/koios-artifacts/blob/main/specs/results/koiosapi-mainnet.yaml) had SHA-256 `da687af20ad6bffda6266ca723705b9de619da5b35ec827023752a7fd1f6c9e1`. Recorded response envelopes contain endpoint and observation times. `*-scan.json` records complete paginated REST traversal and both surrounding tips. Earlier short probes (`sample_only: true`) are retained only in ignored `local-reference/evidence/`; they are not complete scans. See the [committed evidence inventory](../evidence/README.md).

## Swaps discrepancy resolved

Dendrite's inlined spending script is **byte-for-byte identical** to the upstream blueprint at the historical expiration commit above. Its `1d6cff26…` hash and `c4d7d117…` beacon policy describe a separate Plutus V2 deployment. Koios returned matching spending and applied beacon script bytes on mainnet and preprod. This is not a typo in a constant.

Published v1 uses spending hash `01fa3646…`, beacon `47cec2a1…`, a ten-field datum, and beacon redeemers 0=create/close and 1=update. The historical expiration deployment adds an eleventh field and shifts beacon redeemer dispatch with an explicit registration constructor. Published v2 migrates to Plutus V3 (`ef69e7b2…` / `4557249e…`), dispatches beacon behavior by script purpose, and its `OutputReference.transaction_id` is direct bytes rather than the older wrapped transaction ID. Reusing Dendrite's V2 datum/builders for published v2 would therefore be incorrect even after replacing script hashes.

Packaged spending scripts are copied from pinned upstream blueprints and hash-checked with the proper Plutus language. All full hashes and source links are in `src/defi_kernel/data/deployments.json`. Upstream [VERSIONS.md](https://github.com/fallen-icarus/cardano-swaps/blob/5657d09c2f85dec74b28f57ec6e71cfd283620e9/VERSIONS.md) reports v1 audited and published v2 unaudited; this work is not an independent audit. The historical expiration code is outside the reported v1 scope.

**Selected Swaps target: published v1 one-way orders**, using two independently funded orders for bids/asks. Its missing expiration requires runtime order-age management. The adapter reuses Dendrite beacon algorithms, field types and redeemers with the ten-field v1 datum and v1 beacon dispatch. Create-both-sides, fill and close passed hosted preprod evaluation, as did the composed fixture below. Each was rebuilt with measured budgets and evaluated again. Two-way operations remain unsupported.

## Staking credential versus delegation

Published v1's [beacon-output validation](https://github.com/fallen-icarus/cardano-swaps/blob/9ec41e7619f5ba9d3dd46dd194e2146098093721/aiken/lib/cardano_swaps/one_way_swap/utils.ak#L339) requires `is_some(stake)`. This checks the address credential, **not stake registration, pool delegation, or active staking**. The decoder accepts an undelegated/unregistered key credential without querying stake-account status. A script credential is also distinct from a local stake-key signer and will need its own authorization implementation.

The two credential-less preprod records are `9fecc1d2…#0` and `#1`: reference-script outputs with no inline order datums or beacons. Diagnostics classify them accordingly. The owner-authorization helper's `None -> True` recovery branch does not relax the beacon minting invariant; its source comment explains recovery of otherwise stranded non-order funds.

## Support matrix

| Integration / network | Operation and dependencies | Evidence | Status |
| --- | --- | --- | --- |
| Swaps v1 / mainnet | Decode one-way datums and beacons; Koios | 10/10 candidates decoded; spending and beacon script bytes hash-checked | Read qualified; some orders have zero remaining offer |
| Swaps v1 / preprod | Decode, create, partial fill, close and atomic Dano composition | Historical scan plus confirmed designated test transactions | Narrow one-way execution qualified |
| Swaps v1 / preview | Same | 3/3 records decoded, including a depleted order | Read qualified |
| Historical expiration Swaps / mainnet, preprod | Existing Dendrite eleven-field schema | 1 and 392 candidates observed; source and script identities matched | Research/read adapter only; no execution selection |
| Published Swaps v2 / all three | V3 scripts and changed datum ABI | Zero resting UTxOs in complete scans; scripts retrievable on preprod | Script identity only; datum adapter explicitly unsupported |
| Dano CLMM / mainnet | Pool datum, validity NFT, active reserve math, protocol config | 65 pools decoded from 126 records; others have different/no datums; V3 bytes hash-checked | Selected external candidate; read/math qualified |
| Dano CLMM / preprod | Network-specific NFT, refs, rewards and era-aware slots | Historical scan, hosted evaluation, confirmed standalone and composed swaps | Narrow execution qualified |
| Dano / preview | No qualified deployment supplied | Mainnet-hash probe returned no records; not evidence of universal absence | Unsupported |
| Splash CPP | Caller-owned direct-spend builder, script lookup, index-sensitive redeemer | Source inspected; no live evaluation or pool scan in this increment | Alternative, not selected |
| PyCardano 0.18 | Koios ChainContext, balancing, execution budgets, script hashes | Final evaluation and confirmed preprod ledger transitions; pure-Python cbor2 required | Context and composition integrated |
| Koios / mainnet, preprod, preview | Genesis, tip, paginated credential UTxOs, UTxO/script lookup, Ogmios tip forwarding | Successful unauthenticated public reads with recorded responses | Read path qualified |
| Koios evaluation/submission | Ogmios `evaluateTransaction` / `submitTransaction` | Fixture evaluation and actual signed preprod submission/inclusion | Qualified at explicit 16 KiB test profile ceiling; default remains 1,000 bytes |
| Blockfrost | Second hosted provider | No credential supplied; existing upstream implementation inspected | Replaceability demonstration outstanding |
| Minswap V2 | Asynchronous/batcher order lifecycle | No implementation here | Next venue increment after initial MVP gates |

## External venue and pair selection

Dano is the leading candidate because direct-spend support, real pool state, and authoritative mainnet/preprod references are all available. [Its SDK constants](https://github.com/dano-finance/clmm-sdk/blob/feaf32051d91c0a5b928c080133d88fb55c8e3a8/src/constants.ts) match Dendrite's mainnet pool/config references. The current protocol config reports platform fee rate 1000 and a fixed fee of 100,000 lovelace on both inspected networks. Dendrite must receive that config; assuming its default zero platform fee is unsafe.

The mainnet research pair is ADA / asset `1f3aec8bfe7ea4fe14c5f121e2a92e301afe414147860d557cac7e345553444378` (raw name `USDCx`). Multiple ranges have both active reserves. This is an asset identifier selection, not a claim about backing or financial suitability. Published v1 currently has no corresponding resting order in the recorded scan; controlled maker liquidity will be required.

The preprod fixture pair is ADA / `9a614be30284aa88eb845da7657b5d0a235f1b95628b23c08050d5026655534441` (raw name `fUSDA`). Its real pool supports the fixture's 500,000-base-unit quote size in both directions under explicit simulation assumptions. It is a different test asset, not a mainnet token remapping.

Dano composition now resolves network-specific config/script references, packed redeemer indices, protocol withdrawal and overdue per-pool rewards. The SDK's preprod protocol epochs last 30 minutes; they are distinct from Cardano chain epochs. Slot conversion uses node-provided era summaries, including preprod's 20-second Byron slots. Validity intervals stay within one protocol epoch.

## Controlled composed evaluation

`evidence/preprod-composition-final-evaluation.json` records the complete request, additional UTxOs, response and transaction ID. The candidate spends a recorded real Dano ADA/fUSDA pool and a synthetic published-v1 order, funded by a synthetic wallet input, in one transaction. It takes 500,000 fUSDA units from the order and exchanges those through Dano; the intermediate token nets to zero. Prices are controlled test data, not a discovered profitable route.

All four scripts passed hosted preprod evaluation: Dano spend (64,580 memory / 23,051,977 CPU), Swaps spend (418,449 / 133,764,864), Dano protocol withdrawal (829,454 / 270,822,208), and pool staking withdrawal (57,670 / 22,520,271). The candidate was rebuilt using measured budgets with PyCardano's margin and evaluated again; all measured costs fit the assigned budgets. Its transaction ID is `2b9cae6b41ef3bf291a147e2ec29007d2a8d0c35b4002914fec4a43d80794b64`—this identifies an **unsigned evaluated fixture, not a submitted transaction**.

Evaluation exposed and helped fix two integration errors: using one-second slots from preprod genesis, and appending Dano's pool output behind a Swaps output. The SDK uses pool batch positions as output indices because it emits pool outputs first. The composition wrapper now preserves that layout and rejects subsequent reordering. Reference inputs are serialized deterministically. These fixes are covered by regression tests.

PyCardano's [documented cbor2 C-decoder limitation](https://github.com/Python-Cardano/pycardano/issues/311) was reproduced: decoding tag-258 input sets changed ordering and transaction hashes. `pyproject.toml` now requires a source build with `CBOR2_BUILD_C_EXTENSION=0`; the signer also rejects any CBOR round trip that changes the candidate. The final evaluation fixture reproduces byte-for-byte in tests.

[Ogmios additional UTxOs](https://ogmios.dev/typescript/api/interfaces/_cardano_ogmios_schema.EvaluateTransaction.html) permit evaluation of synthetic funding and controlled orders. They do not create real UTxOs, mint test liquidity, prove signatures or confirm ledger inclusion. Individual create-both-sides, fill, close and Dano-swap candidates also passed evaluation and subsequent budgeted final evaluation; their requests/responses are in `evidence/preprod-{create,fill,close,dano}-final-evaluation.json`. The subsequent real execution sequence is recorded separately in `evidence/preprod-live-execution.json`.

## Real preprod execution findings

The user funded a newly generated disposable wallet with faucet tADA. The runtime bought test fUSDA through Dano, published two Swaps orders, partially filled its own ask, composed another fill with a Dano swap, and closed the remainder. All six transactions, including the initial funding split, confirmed on-chain. The atomic transaction is `6f9736aa94d4fe9e70ff38cf145aa5ae48cdc27010c8ca2be31710a5a1ef0214`. Both legs are in its single body; intermediate fUSDA has zero net wallet change. These are controlled own-order tests, not evidence of profitable trading.

The first 100,000-unit composed candidate failed Dano's protocol withdrawal evaluation before signing. Its input was below the pool datum's 109,086 minimum Y change. A 125,000-unit candidate passed full evaluation and confirmed on-chain. The compatibility layer now rejects inputs below the recorded minimum before adding contributions. Final evaluation remains mandatory.

PyCardano can omit both collateral-return fields when the return would be too small. The inspector now charges the full collateral input against the authorized loss limit, consistent with the no-return rule in [CIP-40](https://cips.cardano.org/cip/CIP-0040). Token collateral still has to be conserved. The failed unsigned candidate was explicitly abandoned without a wire attempt; its history remains in the outbox.

The live Koios `tx_info` response omitted `valid_contract`. The observer therefore retrieves `tx_cbor`, verifies its body hash and reads the actual validity flag before accepting canonical inclusion. The wallet's staking credential was never registered or delegated; successful order creation validates that distinction.

## Limits and remaining gates

The client bounds requests, retries, pagination and rate; all scans preserve observation time and surrounding tips. Offset pagination over a changing index can miss rows despite reaching the end. A complete traversal is not an atomic snapshot. Dependency rechecking rejects missing/spent/changed inputs, but cannot eliminate ledger races. No automatic failover, chain-sync stream, shared acquired state, or mempool access is claimed.

The runtime retains its conservative 1,000-byte default request budget. The separate, bounded qualification harness successfully sent a 14,356-byte request to the anonymous preprod Ogmios endpoint (16 KiB probe ceiling). The explicit test profile also successfully submitted actual signed transactions. These are observed capabilities of that endpoint at capture time, not universal tier guarantees; rejection or provider changes require requalification.

Dendrite's contribution function runs with a private dependency dictionary and a transaction-local subclass. Deployment, backend and clock lookups are bound per session; global backend selection, mainnet constants and reference caches remain untouched. Only exact observed pool values, script references and verified rewards are exposed. No unrelated historical backend interface has been imitated. This compatibility seam is tied to the pinned source and should be replaced by upstream dependency injection when available.

The CLI runs continuous live-data shadow observations with actual address inventory, rewards, local reservations, freshness checks, durable decisions and cooperative stop. It follows a configured pool NFT when the initial input is consumed. The complete `trade` scheduler now reconciles canonical order history, follows continuations, manages both sides and waits for pending transactions before new actions. The original empty-address shadow evidence remains separate from the funded operator execution sequence.

The independent final inspector checks exact inputs/references, committed protocol outputs, change destinations, asset deltas, mint/burn, withdrawals, signer identities, fees, collateral and validity. Local signing requires a fresh final-evaluation receipt for the exact bytes, dependency rechecks, the correct payment/stake keys and a separate mainnet gate. The authorized disposable preprod keys remain private local files.

Automatic publication, fill/continuation accounting, staged repricing, inventory rebalancing and tracked cancellation are connected through one market configuration. The expanded `evidence/preprod-mvp-execution.json` records actual strategy execution and decisions, separately from synthetic fixtures. Confirmed collateral is released; spent-input tombstones prevent stale reuse. Signed expiry requires mature canonical and explicit input evidence; competing spends and phase-2 failures have distinct recovery paths. Canonical order replay reverses rollback effects without duplicate fills. Restart, unknown outcomes, cancellation races and rollback are exercised with deterministic provider fixtures; real chain failures were not deliberately induced. Second-provider live verification remains outstanding without credentials and does not block the selected Koios MVP, as specified in the brief.
