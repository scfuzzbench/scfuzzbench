# Run Error Catalog

Catalog of every distinct error observed in the logs of the 21 benchmark runs executed
between 2026-02-22 (`1771794816`) and 2026-04-26 (`1777163969`), with mitigation status.
All 173 uploaded instance log sets were scanned. Intended-benchmark output (assertion
failures, broken invariants, canaries) is not an error and is excluded.

Use this page as the checklist when auditing a new run: any error not listed here is new
and should be added together with its mitigation.

## Fatal errors (instance produced zero fuzzing)

### E-1 — Echidna: `No tests found in ABI`

- **Seen:** run `1771979026` (liquity, 2026-02-25) — both Echidna instances exited after ~30 s.
- **Cause:** `echidna.yaml` ran property mode while the harness only exposes assertion-based
  tests; after the runner's `invariant_` → `echidna_` prefix rewrite there is nothing to test.
- **Status: mitigated.** The liquity harness switched to `testMode: "assertion"` with
  `prefix: "echidna_"` (later liquity runs ran Echidna for the full budget). The same latent
  bug existed in the nerite harness (property mode, never yet benchmarked) and was fixed in
  the nerite harness normalization. The onboarding skill's acceptance gate (both canaries in
  a 2-minute Echidna trial) prevents reintroduction.

### E-2 — Foundry: `failed to set up invariant testing environment`

- **Seen:** runs `1776020304` and `1776361474` (aave, 2026-04-12/16) — all Foundry instances
  died ~2 min in, with an empty error message. From run `1776376084` onward Foundry was
  dropped from `fuzzer_keys` entirely instead of being fixed.
- **Cause:** regression in the custom `aviggiano/foundry` `fail_on_assert` build
  (`git-cc6767a`); earlier fork builds ran the same harness fine.
- **Status: mitigated.** The setup now builds upstream `foundry-rs/foundry` at a pinned
  commit and the custom fork is gone. Verified with the pinned build
  (`nightly-907ba081`): the aave `CryticToFoundry` invariant setup succeeds and handlers
  are fuzzed. Foundry should be re-enabled in `fuzzer_keys` for the next full run.

### E-3 — Instances that never uploaded logs (silent loss)

- **Seen:** run `1772410040` (superform) lost 1 of 2 Echidna instances; run `1776717487`
  (liquity) lost 1 of 4 Echidna instances. Nothing from those instances exists in S3.
- **Cause:** unknown — launch failure, crash before upload, or upload failure. The
  install/clone phase produced no artifacts, so there was nothing to diagnose with.
- **Status: partially mitigated.** Runners now upload the bootstrap (user-data) log to
  `logs/<run_id>/<benchmark_uuid>/<instance>-<fuzzer>-bootstrap.log` on exit, covering
  install/clone/build failures. A true EC2 launch failure still leaves no artifact; if a
  fuzzer column comes up short in a future run, check the bootstrap logs first.

## Degraded errors (fuzzing ran with reduced capability)

### E-4 — Recon: `Failed to run recon-generate: No such file or directory`

- **Seen:** every recon instance in all 7 runs that included recon (2026-04-16 → 2026-04-26),
  on v0.4.6 and v0.4.8.
- **Cause:** recon shells out to `npx -y recon-generate@latest` for slither-equivalent target
  info, and Node.js was not installed on the instances. Value mining degraded to bytecode
  constants only, plus a wasted full `--build-info` recompile per instance.
- **Status: mitigated.** `fuzzers/recon-fuzzer/install.sh` now installs Node.js and
  prefetches `recon-generate`.

### E-5 — Medusa: slither run fails on the superform target (`exit status 1`)

- **Seen:** all Medusa instances in all 10 superform runs (2026-02-22 → 2026-04-26). Never
  fixed. Medusa's own invocation
  (`slither test/recon/CryticTester.sol --ignore-compile --print echidna --json -`) fails,
  while Echidna's slither pass on the same target succeeds; Medusa loses slither-derived
  constants on this target. Works on aave and liquity.
- **Status: open.** Tracked as a follow-up issue; needs a repro against the superform
  compilation unit to determine whether the fix belongs in `medusa.json` (e.g. a slither
  cache like the aave target uses) or in medusa/slither.

### E-6 — Medusa: `invariant_*` properties skipped due to wrong signature

- **Seen:** all Medusa instances in the 5 earliest runs (2026-02-22 → 2026-02-25,
  superform + liquity). Dozens of `invariant_*` methods were not registered as property
  tests because they were declared `returns()` instead of `nonpayable returns (bool)` —
  skewing early cross-fuzzer comparisons.
- **Status: mitigated.** Harnesses were fixed (gone from 2026-03-02 onward) and the
  onboarding skill mandates `nonpayable returns (bool)` for every `invariant_*` function.
  The nerite harness normalization fixed the last violation (`invariant_canary` was `pure`).

## Cosmetic (fuzzing unaffected)

### E-7 / E-8 — Foundry: unknown config key warnings

- `lint_on_build` (superform, older Foundry builds) and `rpc_endpoints` nested under
  `[profile.default]` (liquity, nerite).
- **Status: mitigated.** The harness normalization moved `rpc_endpoints` to its top-level
  table; fork-only keys (`fail_on_assert`, `continuous_run`) were removed from all target
  `foundry.toml` files. Remaining unknown-key warnings are harmless but should be treated
  as harness bugs and fixed in the target repo.

### E-9 — solc natspec warnings in target code (`@audit`, `@dev:`)

- Upstream target-code quality; benign. **Status: accepted** (pre-target code is kept
  vulnerable/as-is by design).

### E-10 — Corrupted/interleaved log lines (NUL bytes)

- A handful of recon and medusa logs contain NUL bytes from concurrent multi-worker writes
  to the shared tee'd log. The analysis parsers open logs with `errors="ignore"`.
- **Status: accepted / watch.** If parsing ever misses events on affected files, sanitize
  in `scripts/prepare_analysis_logs.py`.

## Blind spots

- Public S3 access is GetObject-only (no listing), so missing instances can only be
  detected by comparing expected vs. present instance counts.
- `runner_commands.log` exists only for runs from 2026-03 onward (feature added then).
