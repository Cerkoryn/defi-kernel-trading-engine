# Public verification evidence

These JSON files are committed deliberately. They make the protocol qualification, offline tests and historical execution claims reviewable without wallet keys or a live provider.

- `*-identity.json`, `*-tip.json`, `*-scan.json`, script/config/reference responses and era/protocol parameters record the qualified provider and deployment observations. Complete scans are retained even when larger than the small test fixtures.
- `preprod-composition-dependencies.json` and the initial/final evaluation records preserve construction inputs, measured budgets and the resulting evaluated bytes. The initial evaluations are inputs to the final-budget reconstruction, not disposable duplicates.
- `preprod-live-execution.json` and `preprod-mvp-execution.json` preserve six and twelve historical confirmed transactions respectively. Tests replay their hashes, signatures, limits and order lineage. Never overwrite them with a new run.
- `audit-*.json` and `dependency-audit.json` record the audit's read-only shadow check, scoped performance measurement and unresolved advisory findings.

- `preprod-direct-venues.json` records the four added venues' public inputs, scripts and context. `preprod-direct-venue-evaluation.json` contains 17 final hosted evaluations with synthetic inputs (including mixed four-/five-hop routes), replayed byte-for-byte by tests. `preprod-direct-venue-discovery.json` records cold/warm read-only scans under the existing asset allowlist. None of these three files claims signed execution.

- `preprod-arbitrage-selection-audit.json` records historical log consistency and retrospective fee-model fit; it is not a new chain verification. `preprod-arbitrage-ranking-benchmark.json` measures the revised bounded search on synthetic liquidity.


- `preprod-dolos-qualification.json` records the governance diagnosis, hybrid shadow checks and the replacement image's verified startup/restart and short relay-sync smoke test. NAS replacement, rebuilt ledger qualification and live submission remain pending.

All balances and addresses here are public test evidence; transaction CBOR may contain public signatures, never private signing keys. Observation times matter: these files are historical snapshots, not current market data or proof of profitability.

Superseded short discovery probes, duplicate initial genesis responses, old shadow diagnostics and the earlier shadow-only configuration are local research material under ignored `local-reference/`. They are not needed by tests or supported commands. Private keys and the durable wallet journal stay under ignored `state/` and must not be deleted as build artifacts. Generated distributions and bytecode/test caches can be rebuilt.
