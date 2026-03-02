---
name: target-onboarding
description: Create and execute onboarding for a new scfuzzbench benchmark target end-to-end, including target repo setup, validation, and PR with /start payload.
metadata:
  short-description: Onboard a benchmark target end-to-end
---

# Target Onboarding Skill

Use this skill when onboarding a new benchmark target for `Recon-Fuzz/scfuzzbench`.

This skill covers:
- creating/maintaining `dev` and `dev-recon` branches in the target repo
- porting recon harness/config files
- running local validation
- opening `dev-recon -> dev` PR with exact `/start` request JSON

## Inputs

Required:
- `upstream_target_repo_url`: upstream project URL
- `vulnerable_baseline_commit_sha_for_dev`: baseline commit for `dev`
- `recon_harness_source_repo_url`: source repo containing recon harness
- `recon_harness_source_ref_for_test_recon`: source branch/commit to copy harness from
- `destination_repo_url`: `https://github.com/Recon-Fuzz/<repo>-scfuzzbench`
- `base_branch_name`: usually `dev`
- `recon_branch_name`: usually `dev-recon`
- `benchmark_type`: `property` or `optimization`

Optional:
- requester notes and constraints

## Non-negotiable constraints

1. Keep target code at the vulnerable point in time.
2. Port the full harness (e.g. from `test/recon/`), not partial files.
3. Validate locally before opening PR.
4. Keep global defaults in `scfuzzbench` generic; use per-target overrides only when needed.
5. Do not leak secrets in issues/PRs.
6. Every benchmark target must include canary checks:
   - one canary assertion failure
   - one canary global invariant failure prefixed with `invariant_`
7. Naming rule:
   - `invariant_*` functions must not have parameters
   - if a global check has parameters, it must be prefixed `global_*`
   - apply this naming rule across the full inheritance tree, not only `Properties.sol` (for example files under `test/recon/properties/**` and inherited bases)
8. Assertion reason centralization rule:
   - every assertion failure reason must be declared in `Properties.sol` as a `string constant`
   - no inline assertion reason literals in harness files (for example `CryticToFoundry.sol`)
   - every assertion failure reason value must start with `!!!` (required by `_isAssertion` routing in `CryticToFoundry`)
9. Assertion naming normalization rule:
   - assertion handler functions must use `targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>(...)`
   - `ASSERTION_CONSTANT_SUFFIX` must exactly match the referenced `ASSERTION_*` constant suffix
     - example: `ASSERTION_WITHDRAW_DOS` -> `iSpoke_withdraw_ASSERTION_WITHDRAW_DOS(...)`
   - each assertion handler function must reference exactly one `ASSERTION_*` constant
   - if a property needs multiple checks (`gte/lte/eq/t/...`) in the same handler, all checks must use that same single `ASSERTION_*` constant
   - Foundry wrappers must use `invariant_assertion_failure_targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>()`
     - example: `iSpoke_withdraw_ASSERTION_WITHDRAW_DOS` -> `invariant_assertion_failure_iSpoke_withdraw_ASSERTION_WITHDRAW_DOS()`
   - wrapper suffix after `invariant_assertion_failure_` must exactly match the handler identifier (`targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>`)
   - canonical cross-fuzzer identifier for assertions is always `targetFunctionName` (strip `_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>` and strip Foundry wrapper prefix)
     - example: `iSpoke_withdraw_ASSERTION_WITHDRAW_DOS` -> `iSpoke_withdraw`
10. Scope control rule:
   - prefer minimal, rename-first edits when applying naming normalization
   - preserve existing harness behavior and side effects
   - if helper methods, handler splits, or action/assertion refactors are potentially ambiguous/confusing, ask the user first and apply their case-by-case preference
11. Invariant signature compatibility rule:
   - every `invariant_*` function across `test/recon/**` and inherited bases must be declared `returns (bool)`
   - invariant functions must be nonpayable (not `view`/`pure`) for Medusa property compatibility
   - include an explicit boolean return at function end (for example `return true;`)

## Workflow

### 1) Create target repo baseline branch

In destination repo:
1. Checkout vulnerable baseline commit.
2. Create `base_branch_name` (default `dev`) at that commit.
3. Push and set as baseline/default as needed.

### 2) Create recon branch and port harness

1. Create `recon_branch_name` from base branch (default `dev-recon` from `dev`).
2. Port full recon setup from source ref.

Minimum files/directories to port:
1. `test/recon/` (full tree)
2. `foundry.toml`
3. `echidna.yaml`
4. `medusa.json`
5. Required helpers/remappings/scripts used by recon tests

### 3) Ensure benchmark-compatible config

`foundry.toml` invariant section must include benchmark-compatible values:

```toml
[invariant]
runs = 500000000
depth = 100
include_storage = true
show_solidity = true
show_metrics = true
fail_on_revert = false
continuous_run = true
corpus_dir = "corpus/foundry"
```

### 4) Foundry assertion compatibility shim (`foundry-rs/foundry#13322`)

Because assertion failures can be hidden in invariant output, enforce:
1. keep all assertion reason strings in `Properties.sol` as `string constant ASSERTION_* = "!!! ...";`
2. assertion reason strings must be prefixed with `!!!` (including canaries), because `_isAssertion` uses this marker to classify failures
3. maintain `mapping(string => bool) assertionFailures`
4. override assert helpers (`gt/gte/lt/lte/eq/t`) to route assertion reasons through `_recordAssertion`
5. include `_isAssertion(string memory reason)` and use it to distinguish assertion failures from global invariant checks
6. name assertion handlers as `targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>(...)`
   - `ASSERTION_CONSTANT_SUFFIX` must exactly match the referenced `ASSERTION_*` constant suffix
   - examples: `iHub_mintFeeShares_ASSERTION_MINT_FEE_SHARES_PPS_CHANGE`, `iSpoke_withdraw_ASSERTION_WITHDRAW_DOS`, `assert_canary_ASSERTION_CANARY`
   - each assertion handler must reference exactly one `ASSERTION_*` constant
   - if multiple checks are required in one handler, reuse the same single `ASSERTION_*` constant across those checks
7. add one wrapper `invariant_assertion_failure_targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>()` per assertion handler identifier
8. wrappers must only check `assertionFailures[ASSERTION_*]` and must not trigger actions directly
9. when a wrapper is tied to a handler identifier, wrapper suffix after `invariant_assertion_failure_` must exactly match that assertion handler identifier
   - do not keep legacy multi-assert handlers; split/rename so every handler and wrapper pair maps to a single `ASSERTION_*`
10. `setUp()` must include handler routing (`targetContract`, multiple `targetSender` values)
11. do not pre-seed assertion failures in `setUp()` (no hardcoded `_recordAssertion(false, ...)`)
12. local review must confirm canonical identifier compatibility:
    - Echidna/Medusa report handler name (`targetFunctionName(...)`)
    - Foundry reports wrapper name (`invariant_assertion_failure_targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>`)
    - canonical dedup key is `targetFunctionName`

Reference pattern: https://github.com/foundry-rs/foundry/issues/13322

### 5) Canary requirement for every target

Add these canaries to each target harness:
1. Assertion canary:
   - helper/action function: `assert_canary_ASSERTION_CANARY(uint256 entropy)`
   - assertion reason string: `!!! canary assertion`
   - Foundry wrapper invariant: `invariant_assertion_failure_assert_canary_ASSERTION_CANARY` (do not call `assert_canary_ASSERTION_CANARY` from inside this wrapper)
   - canonical assertion identifier: `assert_canary`
2. Global invariant canary:
   - invariant function name must start with `invariant_`
   - canary invariant must take no parameters
   - canary invariant must use signature `function invariant_canary() public returns (bool)`
   - use `invariant_canary` and make it fail immediately (`Canary invariant`)

Reference implementation:

```solidity
// Properties.sol
string constant ASSERTION_CANARY = "!!! canary assertion";
string constant INVARIANT_CANARY_GLOBAL_INVARIANT_FAILURE = "Canary invariant";

function assert_canary_ASSERTION_CANARY(uint256 entropy) public {
    t(entropy > 0, ASSERTION_CANARY);
}

function invariant_canary() public returns (bool) {
    t(false, INVARIANT_CANARY_GLOBAL_INVARIANT_FAILURE);
    return true;
}
```

```solidity
// CryticToFoundry.sol
mapping(string => bool) internal assertionFailures;

function _isAssertion(string memory reason) internal pure returns (bool) {
    bytes memory b = bytes(reason);
    return b.length >= 3 && b[0] == '!' && b[1] == '!' && b[2] == '!';
}

function t(bool ok, string memory reason) internal virtual override(FoundryAsserts, Asserts) {
    if (_isAssertion(reason)) {
        _recordAssertion(ok, reason);
        return;
    }
    super.t(ok, reason);
}

function _recordAssertion(bool ok, string memory reason) internal {
    if (!ok) {
        assertionFailures[reason] = true;
    }
}

function invariant_assertion_failure_assert_canary_ASSERTION_CANARY() public returns (bool) {
    assertTrue(
        !assertionFailures[ASSERTION_CANARY],
        ASSERTION_CANARY
    );
    return true;
}
```

Both canaries are intentional failures used to verify:
1. all fuzzers emit failures on the target
2. the analysis/parser pipeline is capturing failures correctly
3. normalized assertion id consistency across fuzzers (`assert_canary`)

### 6) Fuzzer-specific path rules

Echidna:
1. usually use `test/recon/CryticTester.sol`
2. use `tests/...` only for target-specific exceptions
3. use the `echidna` binary in commands and docs (do not use `echidna-test`)
4. enforce naming + config split:
   - apply naming rules to `Properties.sol` and all inherited recon contracts
   - global checks in harness code must never use `property_` or `crytic_`
   - use `invariant_` only for no-arg globals
   - if a global check has parameters, prefix it `global_`
   - `echidna.yaml` should use `testMode: "assertion"`
   - `echidna.yaml` should use `prefix: "echidna_"`
   - rationale: in assertion mode, Echidna should catch assertion failures plus global properties in one run, so cannot use prefix "invariant"

Medusa:
1. use concrete compilation target file (not `"."`)
2. usually `test/recon/CryticTester.sol`
3. `medusa.json` property prefix should stay `invariant_`
4. rationale: Medusa can run property and assertion testing at the same time
5. if gas-floor errors occur, raise gas limits

Example:

```json
"compilation": {
  "platform": "crytic-compile",
  "platformConfig": {
    "target": "test/recon/CryticTester.sol"
  }
}
```

### 7) Local validation before PR

Run all:
1. `forge test --match-contract CryticToFoundry --list`
2. Echidna smoke run
3. Medusa smoke run
4. Foundry invariant smoke run
5. 2-minute canary trial for each fuzzer
6. Ensure `CryticToFoundry.sol` has no `test_*` repro/unit tests
7. Canary smoke checks must fail within the smoke trial window:
   - `FOUNDRY_INVARIANT_CONTINUOUS_RUN=false forge test --match-contract CryticToFoundry --match-test invariant_canary -vv`
   - `FOUNDRY_INVARIANT_CONTINUOUS_RUN=false forge test --match-contract CryticToFoundry --match-test 'invariant_assertion_failure_assert_canary_ASSERTION_CANARY' -vv`
8. Acceptance gate: each fuzzer must report at least 2 bugs within 2 minutes:
   - one bug for `invariant_canary` (`Canary invariant`)
   - one bug for the assertion canary (`!!! canary assertion` via `assert_canary_ASSERTION_CANARY` / `invariant_assertion_failure_assert_canary_ASSERTION_CANARY`), with canonical id `assert_canary`

Suggested 2-minute commands:

```bash
# Echidna
timeout 120 echidna test/recon/CryticTester.sol --contract CryticTester --config echidna.yaml --format text --disable-slither

# Medusa (Note: you may need to use SOLC_VERSION=0.8.30)
timeout 120 medusa fuzz --config medusa.json --timeout 120

# Foundry
timeout 120 forge test --match-contract CryticToFoundry --match-test 'invariant_' -vv
```

Completion is tied to the 2-minute 2-canary acceptance gate above.

Debug-only fallback for Foundry output inspection:

```bash
FOUNDRY_INVARIANT_CONTINUOUS_RUN=false forge test --match-contract CryticToFoundry --match-test 'invariant_' -vv
```

### 8) Open PR from recon branch to base branch

Create PR `dev-recon -> dev` (or configured branch names).

PR description must include:
1. vulnerable baseline ref used for base branch
2. recon harness source ref
3. files copied/changed
4. local smoke test summary
5. 2-minute canary trial summary per fuzzer (must show both canaries found)
6. canary validation summary (assertion canary + global invariant canary)
7. exact `/start` request JSON for `scfuzzbench`
8. any target-specific overrides and why

### 9) Final `/start` request JSON guidance

Typical fields:
1. `target_repo_url`: destination repo URL
2. `target_commit`: usually `dev-recon`
3. `benchmark_type`: `property` or `optimization`
4. `instance_type`
5. `instances_per_fuzzer`
6. `timeout_hours`
7. `fuzzers`: `["echidna","medusa","foundry"]`
8. optional `fuzzer_env_json` only when target-specific override is necessary
9. optional Foundry source fields:
   - `foundry_git_repo`: `https://github.com/aviggiano/foundry`
   - `foundry_git_ref`: `master`

## Common failures and fixes

1. Echidna: `tests/recon/CryticTester.sol does not exist`
   - fix target path to `test/recon/CryticTester.sol` unless repo is a known exception
2. Medusa: target `"."` treated as directory
   - use explicit Solidity file target
3. Medusa: `insufficient gas for floor data gas cost`
   - raise `transactionGasLimit` and `blockGasLimit`
4. Foundry failures not surfaced
   - verify assertion reasons are constants in `Properties.sol` + `!!!` prefix + per-assertion invariants + overridden assert helpers
5. Foundry unrealistically fast/all bugs immediate
   - remove any `test_*` functions in `CryticToFoundry`
6. Echidna returns 0 issues unexpectedly
   - enforce `testMode: "assertion"` with `prefix: "echidna_"` in `echidna.yaml`
   - enforce naming rule across inherited recon properties too: `invariant_*` must be no-arg, parameterized globals must be `global_*`
   - keep global checks out of `property_` and `crytic_`
7. Broken-invariant overlap shows assertion bugs as Foundry-only (`invariant_assertion_failure_*`)
   - ensure assertion handler is named `targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>`
   - ensure `ASSERTION_CONSTANT_SUFFIX` exactly matches the referenced `ASSERTION_*` constant suffix
   - ensure Foundry wrapper is named `invariant_assertion_failure_targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>`
   - ensure wrapper suffix exactly matches handler identifier so canonical id remains `targetFunctionName`
   - ensure each handler references exactly one `ASSERTION_*` constant; split legacy multi-assert handlers when needed

## Completion checklist

Done means all are true:
1. destination repo is created/updated in `Recon-Fuzz`
2. base and recon branches are pushed
3. recon PR is open with required validation details
4. canary assertion + canary `invariant_` global failure are present and intentionally failing
5. no parameterized function is prefixed `invariant_` (use `global_*` for parameterized globals)
6. naming rules are satisfied across inherited recon property contracts, not only `Properties.sol`
7. each fuzzer reports at least 2 canary bugs (assertion + global invariant) within 2 minutes
8. exact `/start` JSON is provided
9. PR URL is recorded in final report; include tracking issue URL only if one was explicitly requested
10. all assertion failure reasons are constants in `Properties.sol` and all begin with `!!!`
11. every assertion handler `targetFunctionName_ASSERTION_<ASSERTION_CONSTANT_SUFFIX>` has exactly one referenced `ASSERTION_*` constant and a matching wrapper in `CryticToFoundry.sol`
12. assertion failures normalize to `targetFunctionName` across Echidna, Medusa, and Foundry
