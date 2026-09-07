# Public verification evidence

These JSON files are committed deliberately. They make the protocol qualification, offline tests and historical execution claims reviewable without wallet keys or a live provider.

- `*-identity.json`, `*-tip.json`, `*-scan.json`, script/config/reference responses and era/protocol parameters record the qualified provider and deployment observations. Complete scans are retained even when larger than the small test fixtures.
- `preprod-composition-dependencies.json` and the initial/final evaluation records preserve construction inputs, measured budgets and the resulting evaluated bytes. The initial evaluations are inputs to the final-budget reconstruction, not disposable duplicates.
- `preprod-live-execution.json` and `preprod-mvp-execution.json` preserve six and twelve historical confirmed transactions respectively. Tests replay their hashes, signatures, limits and order lineage. Never overwrite them with a new run.
- `audit-*.json` and `dependency-audit.json` record the audit's read-only shadow check, scoped performance measurement and unresolved advisory findings.

All balances and addresses here are public test evidence; transaction CBOR may contain public signatures, never private signing keys. Observation times matter: these files are historical snapshots, not current market data or proof of profitability.

Superseded short discovery probes, duplicate initial genesis responses, old shadow diagnostics and the earlier shadow-only configuration are local research material under ignored `local-reference/`. They are not needed by tests or supported commands. Private keys and the durable wallet journal stay under ignored `state/` and must not be deleted as build artifacts. Generated distributions and bytecode/test caches can be rebuilt.
