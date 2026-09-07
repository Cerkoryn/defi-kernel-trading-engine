# Security, performance and organization audit

Completed 2026-09-07 against the implemented preprod MVP. Priority order: protect funds, reduce execution overhead, explain decisions clearly, then simplify maintenance. The Ponytail skill was applied to shared boundaries and deletion of duplicate paths; no new runtime dependency or speculative framework was added.

This is a source review with implemented fixes, regression checks and a live-data shadow check. It is not an independent smart-contract audit, formal verification, penetration test or guarantee that funds cannot be lost. Execution remains restricted to the qualified preprod flow.

## Scope and architecture

Reviewed configuration and CLI entry points; provider identity, pagination and submission; datum/asset and deployment validation; Dendrite isolation; transaction composition and final evaluation; private-key handling and independent authorization; durable reservations, recovery and rollback; canonical order replay; inventory, pricing, routing and scheduling; dependencies, tests, public evidence and packaging.

There is now one scheduler for `trade` and public-address `run`. Both strategy execution and the bounded test harness use `execution.prepare_transaction`, then `Coordinator`, the final evaluator and `LocalSigner`. Protocol contributors remain responsible for protocol-specific outputs; signing policy checks complete candidates independently. Shadow mode cannot load signing keys or submit transactions.

## Security findings and changes

Severity below describes potential impact in the affected path, not evidence of exploitation. No loss or unauthorized transaction was observed during this audit.

| Priority | Finding | Implemented control and verification |
| --- | --- | --- |
| High | Wallet outputs with datums/reference scripts could disappear from inventory calculations, understating token exposure. | Count their tokens as protected inventory. Reject foreign, spent, incomplete or insufficiently confirmed wallet observations. Six adversarial output cases exercise these boundaries. |
| High | A new independent intent could bypass pending signed work when its inputs did not overlap. | The shared durable preparation transaction refuses new work until existing nonterminal work is reconciled. Existing matching intent recovery remains possible. Regression proves disjoint replacements are blocked. |
| High | Stop could arrive during dependency checks and still permit a subsequent submission claim. | Check cooperative stop inside the database transaction that claims the one wire attempt. A stop injected during dependency rechecking leaves attempts at zero. An already claimed/in-flight submission still requires reconciliation. |
| High | Signed outbox bytes were not compared with their authorized candidate immediately before wire submission. | Reparse the stored signed candidate, require its identity/validity/witness presence, and compare exact bytes after removing key witnesses. Corruption is rejected before claiming an attempt. Ledger and signing checks still validate actual signatures. |
| High | Expiry recovery assumed every input had an explicit state. | Require an exact complete set of spend/collateral references with boolean spent states before retirement. Missing evidence retains reservations; expiry and conflicting-spend proofs remain mandatory. |
| High | Local key reads allowed symlinks/public permissions, and parser errors could disclose secret input. | Read owned private regular files without following the final symlink, bound file size, sanitize parser errors, and restrict manifest entries to local `.skey` filenames. Key permission, symlink, traversal and secret-sentinel regressions pass. This is not protection against malicious code running as the same user. |
| Medium | Journals and run locks could be created with permissive modes or unsafe final paths. | Private directories/files, ownership checks, no-follow opens, regular-file validation and non-truncating lock acquisition. Reconciliation uses the same wallet run lock as scheduling. Existing journal identity checks remain mandatory. |
| Medium | Rebalance math could round a buy above the inventory cap. | Check actual computed token output plus free/protected inventory before construction. The bounded-buy regression rejects the over-limit result. |
| Medium | Separate finalization code could drift; builder-added authorities were captured after construction. | One shared finalizer captures outputs, minting, withdrawals, validity and expected signers before build. It explicitly accounts for pinned PyCardano's automatic payment signers, including reference scripts. Publish/cancel/both rebalances/atomic route pass the independent policy checks. |
| Medium | NaN, floats and booleans could bypass intended integer settings; misspelled market fields were ignored. | Require integer economic limits, finite times/intervals, exact market fields and valid pool asset identity. Invalid economic inputs fail before planning. |
| Medium | One provider response could allocate unbounded application response storage; single-reference reads could be ambiguous. | Stream responses with an 8-MiB decoded-byte ceiling, reject duplicate single-reference results and require complete settlement observations. Oversized submission responses preserve unknown-outcome semantics and never retry. This is an application payload bound, not a process-wide memory bound. |
| Medium | `test-execute` signed by default even without submission opt-in. | Require explicit `--sign` or `--submit`; omission fails before key access. `trade` remains the unsigned preview path. |
| Medium | An outdated transitive dotenv pin remained installed; upstream imports attempted implicit `.env` loading. | Locked uv override updates `python-dotenv` to 1.2.3, and package initialization disables implicit loading. Explicit exported credential variables still work. The no-implicit-loading regression passes. |

Retained protections include exact integer/rational economics; network/profile isolation; script/datum identity checks; disjoint input roles; bounded fees and collateral; owner-only change; mint/withdrawal/output authorization; final full-transaction evaluation; dependency rechecks; one wire attempt; SQLite FULL/WAL durability; tombstones; and canonical rollback/continuation replay. Security and financial tests were retained when deleting obsolete APIs.

### Follow-up hardening

The follow-up review closed two additional authorization and accounting gaps:

- **Freeze input roles before building.** The first audit froze outputs and signing authorities before construction but still read the spending/reference/collateral sets afterward. Both callers already complete coin selection before finalization, so authorization now captures those sets before `build` too. Balancing cannot silently enlarge the authorized input set. Three regressions inject an additional input during build, one for each role, and verify rejection by the independent inspector before signing preparation.
- **Check order contents against canonical replay.** A matching output reference alone no longer establishes agreement between current UTxO observations and confirmed history. Reconciliation also compares decoded terms and all asset quantities, rejects duplicate order references and foreign addresses, and retains the prior ledger on disagreement. The check applies to shared references even when a recent transaction is pending: a pending spend can replace a reference but cannot change its immutable contents. Regression cases change ADA, tokens and correctly hashed datum terms, inject duplicate/foreign rows, and verify accounting survives unchanged. Existing confirmation, rollback and re-inclusion checks still pass.

Follow-up validation: **137 tests pass**, Ruff lint/format checks pass, and the current reconciliation benchmark remains 14 requests. Locked offline installation and source/wheel builds pass; archive checks exclude private state, keys and caches. The historical six- and twelve-transaction evidence files match their pre-follow-up SHA-256 hashes. No transaction was signed or submitted for this follow-up; chain execution is covered by historical evidence and offline regression replay, not a new on-chain execution claim.

## Performance

[Recorded benchmark](../evidence/audit-performance.json): reconciling twelve already-confirmed transactions takes **14 provider requests instead of 60**, a **76.7% reduction for this operation**. Identity and tip are fetched once for the reconciliation pass. Each recorded inclusion still gets its canonical block checked; unchanged block hashes avoid downloading and decoding the same transaction again. Missing blocks, lost confirmation depth or changed hashes pause or enter rollback recovery.

The benchmark uses the twelve real recorded transactions through the real Koios adapter with mocked HTTP responses. It excludes order-history polling and real network latency. Offline elapsed timings are supporting diagnostics, not a claim that the whole engine is 76.7% faster. Reproduce the current count with:

```bash
.venv/bin/python scripts/benchmark_reconcile.py
```

The optional `--baseline` loads an explicitly supplied trusted pre-audit `coordinator.py`. The workspace baseline used for the recorded comparison was extracted from `/tmp/defi-kernel-audit-before.tar.gz`; this local backup is not a distributed dependency.

Additional changes: aged/filled-order cancellation no longer waits for fresh venue quotes; diagnostic records retain only the latest 1,000 decisions; each new decision includes at most 20 recent fills plus the full count. Complete financial evidence is retained. Full bounded order-history replay and transaction cost accounting remain linear in journal history. Incremental replay/checkpointing is deferred until measured history costs justify its additional rollback complexity; reaching provider pagination limits pauses instead of truncating financial history.

## Usability and maintenance

- `run --wallet-address` and `trade` now share lifecycle, inventory and pause behavior. Public-address shadow use has the same collateral/funding requirements.
- Compact `status` shows active/pending work, counts, inventory/costs and observation age. `status --details` retains full financial lineage and reservation references. Status is a stored observation, not a new chain query.
- A bounded run ending paused returns exit code 2. Normal completion returns 0; configuration/command failures return 1. Continuous runs persist pause reasons and retry later.
- Documentation now distinguishes signing from submitting, explains protected assets and diagnostic retention, and specifies uv installation, explicit credential exports and private key modes.
- Removed the separate `ShadowRunner`, unused provider/signer protocols, old direct journal mutation methods, unused routing helper and dead bookkeeping. Tests coupled only to those removed paths were replaced with checks of the shared engine and actual fund boundaries.
- Runtime Python source decreased from 5,689 to 5,653 lines at the audit checkpoint despite added controls. Test code and two reproducible audit utilities grew; this is consolidation, not a claim that total repository size decreased.

## Dependency findings and remaining trust boundaries

[OSV results](../evidence/dependency-audit.json) cover 81 locked registry packages, including development/platform dependencies. Git-pinned Dendrite and this repository are listed separately and are not covered by registry-version matching. The scan can be repeated with `.venv/bin/python scripts/audit_dependencies.py`; it deliberately exits nonzero while advisories or incomplete result pages remain.

The old `python-dotenv` pin was affected by a symlink overwrite advisory in its file-writing helpers; the advisory identifies 1.2.2 as fixed. The lock now selects 1.2.3. No runtime call to those writing helpers was found. [Upstream advisory](https://github.com/advisories/GHSA-mf9w-mj56-hr94).

**One package remains flagged:** `ecdsa==0.19.2`, brought in through `pycardano -> cose -> ecdsa`. Its timing-attack advisory has no patched version listed. The runtime's payment/stake signing uses PyCardano's Ed25519/PyNaCl path; a regression disables ECDSA signing/key generation and verifies a real Cardano key signature still succeeds. This supports the conclusion that the affected signing path is unused by this runtime; it does not remove the vulnerable dependency or authorize adding ECDSA-based signing. Do not suppress the scanner result. [Upstream advisory](https://github.com/advisories/GHSA-wj6h-64fc-37mp).

The `cbor2` lower bound is now 5.9.0, which includes the decoder depth-limit fix; the pure Python build remains mandatory for correct Cardano set ordering. A version match is not proof that hostile CBOR parsing is risk-free. [Upstream releases](https://github.com/agronholm/cbor2/releases).

Hosted Koios observations remain trusted and non-atomic. Second-provider verification still awaits credentials. Local Python strategies, dependencies, the operating-system user and writable parent directories remain trusted; this process is not a plugin sandbox or hardware wallet. Contract bugs, compromised providers/hosts, adverse fills, fees, price movement and phase-2 collateral loss remain possible. No mainnet readiness or profitable/unattended-operation certification is implied.

## Verification and live state

The full automated suite, lint/format checks, locked offline installation and source/wheel build results are recorded in [CHECKPOINT.md](../CHECKPOINT.md). Regression coverage includes money limits, key handling, outbox corruption, stop races, incomplete recovery, rollback, composition, shared shadow behavior and historical signature/hash/lineage replay.

One live-data preprod shadow cycle completed with submission disabled and proposed both sides. [Audit status export](../evidence/audit-shadow.json) shows no open orders or pending transactions, 13 historical intents (12 confirmed and one unsigned aborted), unchanged cumulative fees and 600,000 token units. Operational ADA plus protected ADA and collateral totals **9,992.137900 tADA**. No transaction was signed or submitted for this audit. The original six-transaction and twelve-transaction execution evidence files were preserved unchanged. This audit does not claim a new on-chain execution test of the refactor.
