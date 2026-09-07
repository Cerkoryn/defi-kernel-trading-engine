# Designated preprod execution

This bounded operator sequence exercises published Swaps v1 and Dano with disposable credentials and test funds. It runs the same contributors, final evaluation, independent signer and durable coordinator used by the runtime. For the complete market-maker scheduler, see [the operator guide](operator.md).

The disposable wallet created for this workspace received 10,000 tADA from the faucet. Its payment and staking keys remain under the private `state/` directory, excluded from packages. No stake registration or delegation was performed. Do not recreate or overwrite that wallet.

For a **new** test wallet, first choose a distinct `wallet_id` in a copy of `examples/preprod-test.toml`, then run:

```bash
.venv/bin/kernel --config examples/preprod-test.toml wallet-create
# Request tADA for the printed address using the official preprod faucet.
.venv/bin/kernel --config examples/preprod-test.toml wallet-status --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json
```

Use the manifest path printed by `wallet-create` when choosing a different wallet ID. The [official faucet](https://faucet.preprod.world.dev.cardano.org/basic-faucet) requires an interactive CAPTCHA. The existing Dano test pool supplies the fUSDA needed for this sequence through a bounded swap.

Run one action at a time, using a new intent ID for each new transaction and waiting for `confirmed` before the next action:

```bash
.venv/bin/kernel --config examples/preprod-test.toml test-execute --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json --action split --intent my-split-001 --submit
.venv/bin/kernel --config examples/preprod-test.toml reconcile --intent my-split-001 --wait
```

Repeat those commands with the following actions and distinct intent IDs:

| Action | Controlled operation |
| --- | --- |
| `split` | Set aside 5 tADA collateral and 30 tADA operating funds; return the remainder to the wallet. |
| `buy-base` | Swap 4 tADA through Dano for at least 900,000 fUSDA base units. |
| `create` | Publish a 250,000-unit token ask and a 1-tADA bid as two one-way orders. |
| `fill` | Partially fill the wallet's own ask for 50,000 units at its exact price, capped at 2 tADA payment. |
| `compose` | Fill another 125,000 units and sell them into Dano in the same transaction, with zero net intermediate tokens. |
| `close` | Cancel both remaining orders, burn their beacons and return their assets. |

Each action caps the transaction fee at 2 tADA and collateral loss at 5 tADA, in addition to exact destination, asset and slippage checks. Sizes and prices are intentionally controlled test parameters. The route is not claimed to be profitable. It follows the configured pool's validity NFT after each spend. Insufficient liquidity, changed dependencies or evaluation failures stop the action.

`test-execute --sign` evaluates and signs locally, storing the exact bytes without submitting. `--submit` also authorizes signing; repeating the same intent with `--submit` submits that candidate once. Omitting both flags fails before accessing keys. For a preview that never signs, use `trade` without `--execute`. After any attempted submission, use `reconcile`; never invent a new intent to bypass an unknown outcome. Expired signed candidates remain reserved until canonical expiry and explicit input evidence allow safe retirement. `abandon-unsigned --intent ID` can release only a never-signed, never-attempted candidate and preserves its history. `--error-details` exposes bounded public RPC rejection details for diagnosis.

Inclusion requires a matching transaction hash, successful validation and a matching canonical block at the configured depth. When Koios omits `valid_contract`, the observer verifies the hash and validity flag from on-chain CBOR. Confirmed spending inputs remain reservation tombstones so an old indexer view cannot make them available again; confirmed collateral is reusable. Positive rollback evidence quarantines further execution. The scheduler also reconciles expiry, competing spends, order continuations and rollback; see the operator guide for the evidence requirements.

Export independently checked public evidence after closing the test orders:

```bash
.venv/bin/python scripts/capture_live_execution.py --config examples/preprod-test.toml --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json --output evidence/preprod-mvp-execution.json
```

The export includes transaction CBOR, dependency observations, canonical inclusion, fees, bounded wallet deltas, staking-account status and the final wallet/order scans. It excludes keys and private configuration. On-chain CBOR must match the persisted candidate, with only key witnesses removed for comparison.

The historical milestone-3 run confirmed all six transactions and left no open test orders. Its final recorded balance is 9,993.701608 tADA and 858,553 fUSDA base units. Ledger transaction fees total 2.611452 tADA, separate from Dano fees and net assets exchanged. The 5-tADA collateral output survived unchanged. See [the transaction IDs](../CHECKPOINT.md) and [public evidence](../evidence/preprod-live-execution.json).

The subsequent MVP run uses `trade --execute` for automatic rebalancing, both-side publication, fill discovery and staged replacement, followed by `cancel --execute` for tracked cleanup. Its expanded record is `evidence/preprod-mvp-execution.json`; preserve the original six-transaction evidence as a historical fixture. `stop` leaves on-chain orders open.
