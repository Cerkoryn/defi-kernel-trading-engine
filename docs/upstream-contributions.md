# Upstream contribution opportunities

Research date: 2026-09-07. Recommendation: contribute small regression-backed fixes and help validate existing PRs first, then introduce explicit Dendrite transaction dependencies. Keep signing policy and recovery controls in the engine. Reducing compatibility code is valuable only when the upstream replacement preserves those guarantees.

The initial research changed no runtime dependency, production source or lockfile. Subsequently authorized comments and issues are linked in the published follow-up below. Source checkouts, public API responses and diagnostic scripts remain under ignored `local-reference/upstream/`; the compact research results are in [upstream-research.json](../evidence/upstream-research.json).

## What is current, and what we reproduced

| Component | Inspected revision/status |
| --- | --- |
| Dendrite | Default branch `0a1e02505af9d92506e8adbaf42307d923b1f061`, version 1.5.10; identical to our pin. [Source](https://github.com/Charli3-Official/charli3-dendrite/tree/0a1e02505af9d92506e8adbaf42307d923b1f061). |
| PyCardano | Default branch `3bc5677d4db6c5e49de11bffd302a7ffe4e6bcff`; latest GitHub release v0.19.2, published March 1, 2026. Our runtime remains on 0.18.0 because Dendrite pins it. [Source](https://github.com/Python-Cardano/pycardano/tree/3bc5677d4db6c5e49de11bffd302a7ffe4e6bcff), [release](https://github.com/Python-Cardano/pycardano/releases/tag/v0.19.2). |

GitHub API checks covered all open items, recent issue activity and the specific PRs below. Open/merged labels are a snapshot, not a promise of eventual acceptance. Search did not cover every historical discussion.

Offline probes ran on Python 3.12.14 against our pinned environment and current PyCardano source. The latter used isolated `cbor2pure==5.8.0` and `crc8==0.2.1` alongside existing dependencies; it was not a full clean-install compatibility test.

| Probe | Observed result | Meaning |
| --- | --- | --- |
| Same newly constructed reference-input set, five `PYTHONHASHSEED` values | Five reference orders and five body hashes, on both PyCardano versions | A deterministic construction contribution remains relevant. This is not proof that unsorted CBOR itself is ledger-invalid. |
| Equivalent `Union[A, B]` and `A \| B` PlutusData annotations | Legacy union round-trips; modern union raises `DeserializeException`, on both versions | There is a reproducible modern-union gap beyond the older list example in issue #287. |
| Plain PyCardano redeemer-index assignment with Dendrite's Dano redeemer | Ledger redeemer index becomes 1, but Dano's embedded index remains 0 until `set_idx` is explicitly called | Dendrite's protocol payload needs an integration step that the base builder does not provide. This probe inspects internals, not an evaluated transaction. |
| Dendrite `ScriptReference.to_utxo`, supplied seven units of a native token | Converted output has no native assets; script is constructed as Plutus V2 | A shared conversion fix can benefit multiple integrations. |
| Twelve recorded signed transactions, decode/re-encode | Exact CBOR and body hashes preserved on both versions | Useful upgrade evidence, insufficient to qualify building/signing against a new SDK. |
| Current PyCardano Ogmios cost-model parser, captured preprod parameters | V1 retains 166/332 parameters; V2 175/332; V3 350/350 | Independently confirms truncation relevant to open PR #497; retained prefixes preserve their supplied order. |

## Recommended contribution sequence

### 1. Help complete existing PyCardano correctness and security work

The most immediate contribution is independent validation of [PR #497](https://github.com/Python-Cardano/pycardano/pull/497), currently open. Its cost-model parser changes overlap directly with our `chain_context.protocol_parameters`: preserve every ledger-supplied parameter in its supplied order. Current upstream still zips V1/V2 arrays against historical fixed name lists. Our captured protocol parameters and final-evaluation fixtures provide additional regression inputs. Crucially, successful script evaluation is not a substitute for checking the resulting script-data hash against ledger evidence.

An offline probe of the current Ogmios parser using our captured preprod parameter arrays retained 166 of 332 V1 parameters and 175 of 332 V2 parameters; all 350 V3 parameters survived. The retained prefixes preserved their supplied order. This independently reproduces truncation, not the PR's additional ordering claim or a ledger rejection. Add a known script-data hash vector before treating the fix as qualified.

Also review the existing open [private-key redaction PR #494](https://github.com/Python-Cardano/pycardano/pull/494), [atomic private key-file creation PR #495](https://github.com/Python-Cardano/pycardano/pull/495), and [CBOR opt-in self-test PR #496](https://github.com/Python-Cardano/pycardano/pull/496). These deserve separate reviews, not another overlapping patch series.

Our contribution could add adversarial regression cases and compatibility feedback: existing empty-file behavior, write failures, symlinks, restrictive umasks, Windows behavior, non-secret serialization callers, parser/repr error paths, and original transaction hashes. PR #495 intentionally changes whether existing empty files may be overwritten; document that migration instead of calling it behavior-neutral. Engine-side private key validation should remain even after upstream improves saving.

**Value:** high security/reliability, modest deletion initially. A corrected Ogmios parser does not replace our hosted Koios context, identity checks or request limits.

### 2. Make Dendrite reference conversion preserve the observed UTxO

[`ScriptReference.to_utxo`](https://github.com/Charli3-Official/charli3-dendrite/blob/0a1e02505af9d92506e8adbaf42307d923b1f061/src/charli3_dendrite/dataclasses/models.py#L274) constructs an ADA-only value and assumes Plutus V2. We reproduced the asset loss. The model also exposes a datum hash that this conversion does not independently preserve for a hash-only datum. This is a narrower and more broadly reusable target than transplanting our full provider adapter.

Split the work: first preserve the supplied complete value and datum representation; then add explicit script-language metadata and propagate it through backend readers. Preserve existing V2 behavior when legacy metadata is absent, with a documented transition; require verified language for new V3 paths. Never infer script language from the address network or guess from opaque script bytes.

**Required checks:** ADA-only output byte parity; multiple native assets including raw/non-UTF8 names; inline versus hash-only datums; V1/V2/V3 script hashes; missing metadata; and all backend constructors. New fields need defaults compatible with existing serialized model dictionaries. Validate unknown versions explicitly. Test the supported optimized-Python path too: safety must not depend on removable `assert` statements.

**Value:** high reuse, moderate compatibility risk. It could remove Dano's script retyping and portions of our conversion code; engine-side identity/value comparisons remain necessary.

### 3. Give Dano an explicit transaction context

This is the largest potential reduction in our compatibility layer. Dendrite's [Dano builder](https://github.com/Charli3-Official/charli3-dendrite/blob/0a1e02505af9d92506e8adbaf42307d923b1f061/src/charli3_dendrite/dexs/amm/dano.py) combines global backend lookup, mainnet deployment/timing helpers, class-level script caching and wall-clock reads. Our `DanoSession` substitutes these through a private function environment and subclass.

Propose an optional, keyword-only context carrying the selected deployment, observed pool/config/reference UTxOs, verified rewards and one clock/slot mapping. Keep existing call signatures operational; make the explicit path avoid global lookups and caches entirely. A mainnet/preview/preprod distinction cannot come from `Network.TESTNET` alone. Resolve chain slots and Dano protocol epochs separately.

Submit boundary validation independently: minimum pool changes, unknown versus zero rewards, raw-value availability and validity intervals intersected with both existing deadlines and the protocol epoch. The current builder comments acknowledge an epoch-boundary exposure; merely preserving a caller-supplied interval does not establish that it matches the datum epoch.

**Required checks:** old mainnet fixture parity away from corrected invalid cases; interleaved mainnet/preprod builds; no global mutation; no cross-network cache reuse; wrong-script/config rejection; rewards absent/zero/positive; epoch boundaries; minimum change minus one/exactly minimum; and both ADA/token and token/token continuations. New strict failures need explicit release notes. Never preserve an unsafe fallback merely to avoid a documented bug-fix behavior change.

**Value:** very high reduction in function rebinding. Moderate-to-high implementation risk; agree the small API shape with maintainers before a broad refactor.

### 4. Establish a supported composition/finalization contract

Our `CompositionBuilder` overrides private PyCardano methods to resolve protocol payload indices and stabilize reference ordering. Dendrite's [Dano batch support PR #216](https://github.com/Charli3-Official/charli3-dendrite/pull/216) is already merged; reimplementing multi-pool support would duplicate work. The remaining gap is integration with a real builder. Its existing batch tests populate resolved entries directly; our callback probe shows why payload-only tests are insufficient. Dendrite already has a related broad [builder request #12](https://github.com/Charli3-Official/charli3-dendrite/issues/12).

Start with an end-to-end unsigned construction regression and a documented Dendrite composition helper. For reusable PyCardano support, propose a narrowly defined explicit finalization hook, not automatic execution of any object's coincidentally named `set_idx` method. Specify when input/reference/output order is stable, what the hook may modify, and how it behaves during copied builders, fee estimation and repeated build passes. No input/output insertion after authorization.

Handle deterministic reference ordering as a separate change. Normalize only newly built candidates before evaluation and authorization. Do not reorder a transaction decoded from someone else's signed CBOR. Existing consumers that compare constructed hashes need a compatibility note or explicit opt-in mode.

**Required checks:** hash-seed/insertion-order permutations; mixed `UTxO`/`TransactionInput` references; duplicate representations of the same out-ref; ordinary transactions without hooks; repeated/copy builds; one/two Dano pools; other protocol outputs before/after pools; and matching embedded spend/withdrawal/reference indices. The qualified Dano layout requires leading pool outputs: validate or enforce that layout rather than assuming arbitrary output placement is valid. Final full-transaction evaluation must follow resolution.

**Value:** high, but transaction-byte changes make this a higher-risk contribution than its line count suggests.

### 5. Add published Swaps v1 alongside the historical adapter

The existing Dendrite adapter uses the historical eleven-field expiration deployment, while our selected published v1 uses ten fields and different deployed scripts. [The original upstream addition](https://github.com/Charli3-Official/charli3-dendrite/pull/180) calls its integration “v2”; our [qualification](qualification.md) records the pinned deployment distinctions.

Contribute explicit deployment naming/documentation first, then an additional published-v1 adapter. Preserve the existing class, scripts, datum ABI and import names; do not fix the naming ambiguity by silently repointing them. Keep published Plutus-V3 v2 separate and unsupported until qualified.

**Required checks:** known script/beacon hashes, cross-deployment rejection, exact datum bytes, all beacon redeemers, create/partial fill/close, owner authorization, undelegated stake credentials, ADA carrier deposits and native assets. Our public six- and twelve-transaction fixtures are useful evidence; portable tests should not depend on this engine's private wallet or journal.

**Value:** potentially replaces much of our Swaps contribution code. Higher scope; sequence after smaller shared fixes.

### 6. Pursue small dependency and annotation improvements

- **Dendrite dotenv pin:** replace the exact 1.0.1 pin with a tested supported patched range. The advisory identifies versions before 1.2.2 as affected; our lock already uses 1.2.3. This could remove our uv override. Preserve documented configuration behavior in that dependency-only PR; separating import-time initialization is another change. The advisory affects file-writing helpers, not every dotenv import. [Advisory](https://github.com/advisories/GHSA-mf9w-mj56-hr94), [current dependency declaration](https://github.com/Charli3-Official/charli3-dendrite/blob/0a1e02505af9d92506e8adbaf42307d923b1f061/pyproject.toml).
- **PyCardano modern unions:** extend the work associated with [issue #287](https://github.com/Python-Cardano/pycardano/issues/287). Use normalized typing origins/arguments while retaining union branch order and existing error behavior. Cover CBOR and JSON decode, nesting, optional values, explicit constructor IDs and invalid input. Keep existing `typing.Union` output byte-identical. Our lint exception can disappear only after this passes the supported runtime version matrix. Coordinate with PR #492's decoder changes.

These are good small first authored contributions. Moving release-only tooling to development dependencies is a secondary packaging cleanup; optionalizing whole backends or replacing the COSE stack is much broader and does not directly remove the critical composition seams.

## Work already underway: validate rather than duplicate

| Upstream work | Checked status | Suggested involvement |
| --- | --- | --- |
| [PyCardano #475](https://github.com/Python-Cardano/pycardano/pull/475): default `cbor2pure` | Merged | Upgrade research, not a new default-decoder patch. |
| [#498](https://github.com/Python-Cardano/pycardano/issues/498), [#500](https://github.com/Python-Cardano/pycardano/pull/500), [#503](https://github.com/Python-Cardano/pycardano/pull/503): cbor2 6 compatibility | Issue and PRs open; #499 closed without merging | Offer our transaction/datum corpus and clean-install tests. The new API/Python-floor changes require a matrix, not an unbounded dependency bump. |
| [#492](https://github.com/Python-Cardano/pycardano/pull/492): serialization performance | Open | Benchmark our workload and review semantic equivalence before endorsing speed claims. |
| [Dendrite #221](https://github.com/Charli3-Official/charli3-dendrite/pull/221): Blockfrost network selection | Open | Useful cross-network test contribution; lower direct impact because our runtime uses Koios. |

PR #492 explicitly notes a change from CBOR-byte deduplication to Python equality/hash semantics for hashable ordered-set members. Treat that as a compatibility question despite the PR's broad “no behavior change” description. Test custom objects and ambiguous equality, mutation/aliasing, malformed data and dynamic class caches. Consider reviewing introspection caching separately from changes to equality or copy behavior. Its published benchmark numbers have not been independently reproduced here. [PR details](https://github.com/Python-Cardano/pycardano/pull/492).

PyCardano's newer decoder does not by itself justify deleting our cbor2 build configuration: Dendrite remains pinned to 0.18.0, other modules import cbor2 directly, and the cbor2 6 transition is open. The twelve successful round-trips are a useful first gate, not upgrade approval. Keep the existing exact-byte signing checks regardless of decoder improvements.

## Compatibility gates for every proposed PR

1. Attach one minimal failing reproduction against a pinned upstream base. Keep network captures public and explain synthetic versus ledger-confirmed data.
2. Compare old and new behavior: exact bytes for unchanged valid cases; explicit, documented rejection for invalid cases being fixed. Cover all supported Python/backend combinations affected by the patch.
3. Avoid combining deployment changes, math corrections, parser optimization and API redesign in one PR. Add new capabilities without silently replacing existing adapters or globally changing backend selection.
4. For transaction changes, independently inspect conservation, owner destinations, mint/withdrawals, collateral, fees, validity and indices; then evaluate complete candidates. Qualification of our replacement requires bounded preprod execution after offline gates, separately authorized and tracked.
5. Keep our local compatibility code until a pinned upstream release passes the engine's regression suite and applicable ledger gates. Preserve reservations, reconciliation, submission policy, final authorization and chain-identity checks: those are application responsibilities, not expendable duct tape.

The safest starting sequence is: validate #497 and the key-safety PRs; propose the patched dotenv dependency and lossless reference-conversion fixes; add the composition reproducer; then design the explicit Dano context. Modern union support is a useful parallel-sized task for a later work session. Published-v1 integration and broad serialization optimization follow after these smaller changes are independently reviewed.

## Published follow-up — 2026-09-07

At the user's request, posted ten short downstream-impact comments and opened seven focused issues under `Cerkoryn`. Read-back checks confirmed every published body. New issues follow the repository templates, describe compatibility checks, and end with `Created by gpt-6-astra high under supervision of @cerkoryn`. No implementation PRs, dependency upgrades or runtime changes were made.

### Existing tracking: comments only

| Topic | Published comment |
| --- | --- |
| Ogmios cost models | [pycardano #497](https://github.com/Python-Cardano/pycardano/pull/497#issuecomment-5572831783) |
| Signing-key redaction | [pycardano #494](https://github.com/Python-Cardano/pycardano/pull/494#issuecomment-5572834964) |
| Private key-file creation | [pycardano #495](https://github.com/Python-Cardano/pycardano/pull/495#issuecomment-5572835253) |
| CBOR decoder self-test | [pycardano #496](https://github.com/Python-Cardano/pycardano/pull/496#issuecomment-5572835502) |
| cbor2 6 compatibility | [pycardano #503](https://github.com/Python-Cardano/pycardano/pull/503#issuecomment-5572835841) |
| Serialization performance | [pycardano #492](https://github.com/Python-Cardano/pycardano/pull/492#issuecomment-5572836052) |
| Modern type hints | [pycardano #287](https://github.com/Python-Cardano/pycardano/issues/287#issuecomment-5572836277) |
| Composition/finalization contract | [charli3-dendrite #12](https://github.com/Charli3-Official/charli3-dendrite/issues/12#issuecomment-5572836617) |
| Compatible PyCardano dependency path | [charli3-dendrite #16](https://github.com/Charli3-Official/charli3-dendrite/issues/16#issuecomment-5572836866) |
| Blockfrost testnet selection | [charli3-dendrite #221](https://github.com/Charli3-Official/charli3-dendrite/pull/221#issuecomment-5572837074) |

Commented on #503 once for the related CBOR compatibility issue/PR chain rather than repeating the same message on #498 and #500. The Blockfrost comment explicitly identifies an alternative-backend qualification obstacle, not a blocker for the current Koios MVP. No new optimization fork or general trading framework was proposed.

### New issues

| Scope | Issue |
| --- | --- |
| ScriptReference.to_utxo loses native assets and datum hashes; script language is fixed to V2 | [charli3-dendrite #222](https://github.com/Charli3-Official/charli3-dendrite/issues/222) |
| Allow a patched python-dotenv version without downstream dependency overrides | [charli3-dendrite #223](https://github.com/Charli3-Official/charli3-dendrite/issues/223) |
| Allow explicit deployment and chain dependencies for Dano swap construction | [charli3-dendrite #224](https://github.com/Charli3-Official/charli3-dendrite/issues/224) |
| Provide opt-in deterministic reference-input ordering for newly built transactions | [pycardano #504](https://github.com/Python-Cardano/pycardano/issues/504) |
| Add published Cardano-Swaps v1 as a distinct deployment without replacing the existing adapter | [charli3-dendrite #225](https://github.com/Charli3-Official/charli3-dendrite/issues/225) |
| Reject Dano swap inputs below the datum's minimum change before mutating the builder | [charli3-dendrite #226](https://github.com/Charli3-Official/charli3-dendrite/issues/226) |
| Keep Dano transaction validity within the protocol epoch used in its continuation datum | [charli3-dendrite #227](https://github.com/Charli3-Official/charli3-dendrite/issues/227) |

The new reference-order policy is explicitly opt-in and excludes normalization of imported transaction bytes. Dano dependency injection, minimum-input checks and epoch-interval checks have separate issues. Published Swaps v1 must remain a distinct adapter with existing deployments preserved. These requests describe acceptance criteria; they do not establish that any future implementation is safe or ready to adopt.

## Local workarounds and removal criteria

Source comments link to the owning upstream thread. A closed issue or merged PR is a reason to reassess, not permission to delete a workaround: first pin a compatible release and verify the replacement through the applicable gates above. Remove the obsolete comment and update this table in the same change. Preserve independent authorization, validation and recovery checks.

| Upstream tracking | Local code | What can be retired, and when |
| --- | --- | --- |
| [Dendrite #16](https://github.com/Charli3-Official/charli3-dendrite/issues/16), [PyCardano #503](https://github.com/Python-Cardano/pycardano/pull/503) | [SDK pin](../pyproject.toml#L10), [CBOR build configuration](../pyproject.toml#L25) | Reassess the 0.18 pin, cbor2 ceiling and pure-Python build together after a compatible Dendrite release and byte-exact decode/build qualification, including direct cbor2 imports. |
| [Dendrite #223](https://github.com/Charli3-Official/charli3-dendrite/issues/223) | [dotenv override](../pyproject.toml#L22) | Remove the resolver override once upstream allows a qualified patched version. Keep [explicit environment-only loading](../src/defi_kernel/__init__.py): it is our configuration policy. |
| [PyCardano #287](https://github.com/Python-Cardano/pycardano/issues/287) | [Union field](../src/defi_kernel/protocols.py#L94), [UP007 exception](../pyproject.toml#L59) | Modernize the annotation and remove the lint exception together after datum decode/encode parity, including branch selection. |
| [Dendrite #222](https://github.com/Charli3-Official/charli3-dendrite/issues/222) | [to_utxo](../src/defi_kernel/chain_context.py#L79) | Reuse qualified conversion for complete values, datums and script languages; keep Koios row adaptation and spent-state, network, datum and script-hash validation. |
| [PyCardano #497](https://github.com/Python-Cardano/pycardano/pull/497) | [protocol_parameters](../src/defi_kernel/chain_context.py#L28) | Reuse cost-model parsing only if exposed through a suitable supported API with full-array and script-data-hash parity. An Ogmios backend fix alone does not replace the Koios context. |
| [Dendrite #224](https://github.com/Charli3-Official/charli3-dendrite/issues/224) | [DanoSession](../src/defi_kernel/dendrite_bridge.py#L45), [preprod read adapter](../src/defi_kernel/protocols.py#L43) | Replace function rebinding and deployment subclasses once explicit dependencies cover both read and build paths. Retain observed-input identity/value checks, verified rewards and network isolation. |
| [Dendrite #226](https://github.com/Charli3-Official/charli3-dendrite/issues/226) | [Minimum-input guard](../src/defi_kernel/dendrite_bridge.py#L147) | Delegate the guard only when upstream rejects both below-minimum directions before builder mutation; preserve valid candidate bytes. |
| [Dendrite #227](https://github.com/Charli3-Official/charli3-dendrite/issues/227) | [Validity intersection](../src/defi_kernel/dendrite_bridge.py#L226) | Delegate epoch clipping only after mainnet/preprod boundary, tighter-deadline and empty-intersection checks pass. Keep final validity authorization. |
| [Dendrite #12](https://github.com/Charli3-Official/charli3-dendrite/issues/12) | [Index finalization](../src/defi_kernel/transactions.py#L122), [pool output placement](../src/defi_kernel/dendrite_bridge.py#L251) | Replace the private callback/layout plumbing with a supported helper after repeated and composed builds resolve the same indices and outputs before evaluation. |
| [PyCardano #504](https://github.com/Python-Cardano/pycardano/issues/504) | [Reference ordering](../src/defi_kernel/transactions.py#L139) | Replace the private override with qualified opt-in ordering. Compare our existing candidate bytes, hash-seed permutations and Dano indices; never sort imported CBOR. |
| [Dendrite #225](https://github.com/Charli3-Official/charli3-dendrite/issues/225) | [SwapsV1Datum](../src/defi_kernel/protocols.py#L79), [v1 redeemer and builders](../src/defi_kernel/transactions.py#L57) | Replace v1 ABI/building code only with the explicitly selected published-v1 adapter after create/fill/close parity. Keep deployment hashes and independent owner/asset checks. |
| [PyCardano #495](https://github.com/Python-Cardano/pycardano/pull/495) | [Wallet file creation](../src/defi_kernel/wallet.py#L40) | Consider delegating only key-file writes after exclusive creation, permissions, symlink/error handling and durability match. Keep private wallet directories, manifest handling, fsync guarantees and [key-file read validation](../src/defi_kernel/wallet.py#L82). |

Existing regression entry points: [transaction conversion/cost models](../tests/test_transactions.py), [Dano composition](../tests/test_composition.py), [protocol decoding](../tests/test_protocols.py), [exact evaluated candidates and signing](../tests/test_execution.py), [confirmed transaction fixtures](../tests/test_live_evidence.py), and [key/file security](../tests/test_security.py). These are starting points for a future replacement, not a claim that they cover every proposed upstream behavior.

### Related tracking without removable compatibility code

- [PyCardano #496](https://github.com/Python-Cardano/pycardano/pull/496): retain the signer's [per-candidate CBOR check](../src/defi_kernel/signing.py#L328). A startup vector cannot prove every candidate round-trips unchanged.
- [PyCardano #494](https://github.com/Python-Cardano/pycardano/pull/494): [signing-key objects stay out of diagnostics](../src/defi_kernel/signing.py#L358). Redacted repr/str does not make explicit exports or parser inputs safe to log; retain sanitized key-read errors.
- [PyCardano #492](https://github.com/Python-Cardano/pycardano/pull/492): no local serialization optimization fork exists to remove. Benchmark and qualify an upstream release when needed.
- [Dendrite #221](https://github.com/Charli3-Official/charli3-dendrite/pull/221): no local Blockfrost workaround exists. The [Koios provider](../src/defi_kernel/providers.py#L24) is the selected integration, not temporary code to delete when Blockfrost is fixed.

Multi-hop replacement checks now include [arbitrage structural/security tests](../tests/test_arbitrage.py) and [hosted resource probes](../evidence/preprod-arbitrage-benchmark.json), including multiple Dano pools and native-token pairs. Retain final authorization and fresh dependency checks when removing compatibility hooks.

### Long redeemer bytes

The September 7 execution run exposed a separate pinned-SDK round-trip defect, despite using pure-Python cbor2: PyCardano decodes Dano's packed multi-pool redeemers into plain bytes and loses the `ByteString` wrapper that preserves Plutus's 64-byte chunks. Two pools already require a 70-byte payload; the observed three-pool payload was 104 bytes. Four saved unsigned candidates changed witness encoding on ordinary SDK round-trip; their transaction bodies were unchanged. These four attempts had no signatures and no submission attempts.

[`decode_candidate`](../src/defi_kernel/signing.py) restores redeemer data through the SDK's `RawPlutusData` dictionary conversion only when ordinary round-trip parity fails, then demands equality with the **entire original byte sequence**. Shadow/execution validation, signing and the journal submission claim share this gate. Evaluated or stored bytes are never normalized, and unsupported encoding changes still fail closed before signing. Provider submission continues to send the original claimed bytes.

This is distinct from the existing [decoder startup self-test PR #496](https://github.com/Python-Cardano/pycardano/pull/496); no upstream fix for this specific defect is assumed. Remove the restoration only after a qualified SDK release preserves long redeemers byte-for-byte. Retain the equality guard. [The regression](../tests/test_execution.py) covers a multi-pool candidate through final inspection, synthetic evaluation, real signing with disposable keys, and durable submission claim; it also rejects trailing bytes before reservation. This is offline qualification, not evidence of a new on-chain trade.

## Bounded collateral

The September 7 run `a34b28af` restored the 10-tADA operating reserve, but subsequent candidates failed in PyCardano 0.18.0’s `_set_collateral_return` before evaluation: its estimate exceeded the explicit 5-tADA input. The SDK sizes collateral using maximum transaction bytes and execution budgets plus reference-script fees, rather than the candidate’s authorized fee ceiling. [CIP-40](https://cips.cardano.org/cip/CIP-0040) describes collateral returns and their balance/minimum-ADA requirements.

[`CompositionBuilder`](../src/defi_kernel/transactions.py) now sets total collateral to `ceil(max_fee * collateral_percent / 100)` for builds through [`build_transaction`](../src/defi_kernel/execution.py). It uses only explicitly selected collateral, returns the remainder to the owner and rejects insufficient amounts or dust returns. The execution-budget copy retains the subclass and linked Dano outputs; the SDK’s base-class copy would lose the policy. Actual fees above the ceiling and collateral above the authorized loss cap fail construction. Final independent inspection, exact-byte evaluation and signing checks remain mandatory. This changes only newly built candidates; saved transactions are never rewritten. No protocol parameters, provider behavior or global SDK functions are patched.

The [regression](../tests/test_arbitrage.py) reproduces the original SDK failure with a synthetic higher network resource ceiling, builds the same mixed route with 5 tADA under the bounded policy, and checks both provisional and final collateral/continuation outputs. Additional checks cover rounding, dust, insufficient reserves and fee/loss-limit rejection. These are offline construction tests with synthetic budgets, not live Plutus or ledger qualification. Replace these private hooks only when a pinned SDK supports explicit bounded collateral through budget-estimation copies and passes these checks. No upstream issue or PR was opened for this follow-up.

## Reference script fee accounting

PyCardano 0.18 appends a reference script to `_reference_scripts` for each use, even when the corresponding input is already present. Its fee estimator then charges repeated bytes. The ledger instead counts scripts on the union of regular and reference inputs, once per distinct input, including scripts not executed by this transaction. Identical script bytes carried by separate inputs must still count separately. See the [Conway ledger implementation](https://cardano-ledger.cardano.intersectmbo.org/cardano-ledger-conway/src/Cardano.Ledger.Conway.UTxO.html#txNonDistinctRefScriptsSize).

The local [`CompositionBuilder._ref_script_size`](../src/defi_kernel/transactions.py) override derives sizes from the explicitly resolved inputs; final resource inspection independently counts those same input occurrences, including native-script serialization. No installed dependency or global SDK function is modified. Mixed synthetic Swaps/Dano fixtures previously charged over 0.3 tADA of excess reference fees. These offline differences are not a claim about any specific live trade.

Remove the override after a qualified SDK release counts per-input reference bytes correctly. [Regressions](../tests/test_arbitrage_risk.py) cover shared use, the same input in both sets, and identical scripts on separate inputs. Historical hosted-evaluation tests explicitly retain their original SDK accounting to reproduce recorded bytes; newly built candidates require fresh final evaluation. No upstream issue or PR was opened for this follow-up.

Arbitrage now derives its fee ceiling from profitability, collateral-return requirements and loss headroom. The bounded-collateral hook above still uses that ceiling, rounded up; it never sizes collateral from an unbounded gross profit.

### Direct venue adapters (September 8)

[venues.py](../src/defi_kernel/venues.py) reuses datum codecs but constructs direct fills from verified raw values and explicit Preprod deployments. This extends the boundaries tracked by [Dendrite #222](https://github.com/Charli3-Official/charli3-dendrite/issues/222) (lossless UTxO conversion), [#224](https://github.com/Charli3-Official/charli3-dendrite/issues/224) (explicit context, originally Dano), [#225](https://github.com/Charli3-Official/charli3-dendrite/issues/225) (published Swaps v1 ABI), and [#12](https://github.com/Charli3-Official/charli3-dendrite/issues/12) (builder composition). Those issues do not promise fixes for every new venue.

Replacement adapters must preserve integer rounding, original hash-datum witnesses, exact script language/identity, final input/output indices and fee attribution for multiple orders. Re-run the [17-sample hosted qualification](venues.md#sources-and-reproducibility) before retiring local construction. Saturn's unavailable reference-script fallback and conservative Genius shared-fee allowances are separate deployment/optimization choices, detailed there; no new upstream issue was opened for them.

## Dolos adapter compatibility

The deployment targets **Dolos 2.0.0-alpha.0**, source commit
`a08c9d130e6b6d4cdc43fe54228ff6dd4e0f88e4`. Its official amd64 image is pinned by
digest; fresh-storage startup and restart were tested with that image's verified
binary. NAS synchronization and ledger/submission qualification remain pending.
These are local compatibility notes, not newly filed upstream issues. See
[deployment and qualification](dolos-preprod.md) and the
[Dolos adapter](../src/defi_kernel/dolos.py).

| Boundary | Current handling | Condition for removing the workaround |
| --- | --- | --- |
| Unsigned evaluation | Route evaluation to Koios. Dolos gRPC `eval_tx` calls full validation, whose phase-1 checks require payment witnesses. Never sign early to satisfy an evaluator. | An unsigned evaluation API passes the same budget, failure and no-signing tests as Koios. [Dolos evaluation](https://github.com/txpipe/dolos/blob/v2.0.0-alpha.0/src/serve/grpc/v1beta/submit.rs), [validation](https://github.com/txpipe/dolos/blob/v2.0.0-alpha.0/crates/cardano/src/validate.rs), [Pallas witness checks](https://github.com/txpipe/pallas/blob/v1.1.1/pallas-validate/src/phase1/conway.rs). |
| Protocol parameters | Preserve `cost_models_raw` arrays. MiniBF rounds rational values and omits reference-script limits/schedule constants; compare all PyCardano-consumed parameters against the evaluator before building. Disagreement pauses builds. | A qualified API supplies exact rational parameters and all current ledger fee rules. [MiniBF mapping](https://github.com/txpipe/dolos/blob/v2.0.0-alpha.0/crates/minibf/src/routes/epochs/mapping.rs). |
| Era horizon | MiniBF's last era ends at the tip; extend by the smaller of its reported safe zone and one hour. Preserve continuity and reject requests outside that horizon. | The API returns an explicit, qualified forecast horizon. [Era mapping](https://github.com/txpipe/dolos/blob/v2.0.0-alpha.0/crates/minibf/src/routes/network.rs). |
| Historical spend evidence | MiniKupo matches only unspent outputs. Read creation/spender transactions and canonical blocks through MiniBF; null `consumed_by_tx` needs positive live UTxO evidence. | A replacement index API supplies equally strong, qualified spent-output and rollback evidence. [MiniKupo limits](https://docs.txpipe.io/dolos/apis/minikupo), [transaction output mapping](https://github.com/txpipe/dolos/blob/v2.0.0-alpha.0/crates/minibf/src/mapping.rs). |
| Reference discovery | Configure public outref hints from qualified evidence, then re-read and hash-check every referenced script before use. | A bounded script-hash-to-live-reference query passes missing/spent/mismatched reference tests. |

`backends.ProviderBundle` keeps these choices outside strategy code. The current
example uses Koios for ledger state, unsigned evaluation and submission; Dolos
supplies chain/UTxO queries, discovery and recovery. See the governance diagnosis
below. Account reward checks in the Dolos adapter use `registered`, not `active`
(which indicates delegation).

### Live qualification: September 12, 2026

Matching the chain tip did not establish matching ledger state. At Preprod epoch
312, MiniBF and Koios's Ogmios endpoint disagreed on three consumed parameters:

| Parameter | Dolos v1.6.0 | Koios evaluator |
| --- | ---: | ---: |
| Minimum pool cost (lovelace) | 170,000,000 | 75,000,000 |
| Transaction execution memory limit | 16,500,000 | 17,500,000 |
| Block execution memory limit | 72,000,000 | 77,500,000 |

The initial two shadow cycles correctly paused before building or evaluating.
These are integer differences, not rational rounding.

### Dolos governance

Preprod proposal
`e641ec802bb109e150e920c6c0387e85f2efd30944a46d08d08212bde540f69c#0`
was submitted in epoch 303, ratified in 304 and enacted in **305**. Koios's
`epoch_params` changes all three values between epochs 304 and 305. Its
[proposal record](https://preprod.koios.rest/api/v1/proposal_list?proposal_tx_hash=eq.e641ec802bb109e150e920c6c0387e85f2efd30944a46d08d08212bde540f69c)
specifies those exact values. Dolos's archive contains the proposal too: hashing
the original serialized transaction body reproduces that transaction ID.

This is different from the equivalent bundled **mainnet** proposal
`ab474223d40e2e3540555364be27e161a809c33651408f43d84acff10c0ba306#0`,
which [expired in epoch 653 without enactment](https://api.koios.rest/api/v1/proposal_list?proposal_tx_hash=eq.ab474223d40e2e3540555364be27e161a809c33651408f43d84acff10c0ba306).
Both proposals also specify unchanged CPU limits; only the three values above differ.

The cause is Dolos v1.6.0's incomplete governance implementation:

- Its [Preprod outcome table](https://github.com/txpipe/dolos/blob/v1.6.0/crates/cardano/src/hacks.rs)
  lacks the epoch-305 proposal and returns `Unknown` for unlisted Conway proposals.
- [Proposal ingestion](https://github.com/txpipe/dolos/blob/v1.6.0/crates/cardano/src/model/proposals.rs)
  stamps ratification from that table; `Unknown` leaves the ratification epoch unset,
  so enactment never applies the update.
- [MiniBF](https://github.com/txpipe/dolos/blob/v1.6.0/crates/minibf/src/routes/epochs/mod.rs)
  reads the stored live parameters. Tip synchronization cannot repair this state.

**Engine mitigation:** the explicit `ledger` binding in
[preprod-dolos.toml](../examples/preprod-dolos.toml) routes era history, parameters
and rewards to Koios. The existing five-role configuration still works: omitted
`ledger` means `chain`. When ledger and evaluation use distinct providers, exact
normalized parameter equality remains mandatory. Their network identity, freshness
and common canonical block must also match the data provider before building.
No stale Dolos value is accepted or patched into the engine.

Submission also stays on Koios: Dolos's
[`receive_tx`](https://github.com/txpipe/dolos/blob/v1.6.0/crates/core/src/submit.rs)
validates against its local ledger, so otherwise a correctly evaluated transaction
could be rejected under the old limits. Submission identity/anchor checks happen
before transmitting, independently of reconciliation. Hosted cooldowns block new
work while local recovery remains available. There is no automatic provider failover.

**Removal condition:** qualify a governance-correct Dolos build **and rebuilt
ledger state**, then restore its ledger/submission bindings and repeat parameter,
historical, evaluated-shadow and bounded submission checks. Upstream implemented
[computed ratification in #1215](https://github.com/txpipe/dolos/pull/1215) and
[state regeneration in #1222](https://github.com/txpipe/dolos/pull/1222).
The selected [v2.0.0-alpha.0](https://github.com/txpipe/dolos/releases/tag/v2.0.0-alpha.0)
includes breaking storage-v4 changes. The approved replacement discards the old
Dolos database and replays Preprod from genesis in the same directory; no backup
installation or table fork is retained. The version-4 tar snapshot URL returned
404 and the Stelae registry could not be verified, so the deployment uses the
relay directly. See the [one-time replacement steps](dolos-preprod.md#replace-the-existing-installation).
The engine's wallet and journal are retained independently of this node rebuild.

A separate discovery probe found that a malformed Dano pool can raise Dendrite's
`NotAPoolError`, which is not a `ValueError`. `decode_dano` now translates explicit
SDK pool-validation exceptions into the engine's row-rejection error, so all callers
retain their existing safety boundary without aborting the entire market scan.
