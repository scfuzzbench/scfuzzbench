# Fuzzers

Each fuzzer lives in `fuzzers/<name>/` with an `install.sh` and `run.sh`. Common behavior (clone, build, upload, timeout, shutdown) is in `fuzzers/_shared/common.sh`. Per-fuzzer configuration is provided via `fuzzer_env` in your local `tfvars`.

## Shared settings

- `SCFUZZBENCH_PROPERTIES_PATH`: repo-relative path to the properties file that gets patched for `benchmark_type` switching.
- `SCFUZZBENCH_SHUTDOWN_GRACE_SECONDS`, `SCFUZZBENCH_TIMEOUT_GRACE_SECONDS`: graceful shutdown/timeouts.
- `SCFUZZBENCH_GIT_TOKEN_SSM_PARAMETER`: SSM name for a token used to clone private target repos.
- `SCFUZZBENCH_WORKERS`: override default worker count (defaults to vCPU count on the instance).
- `SCFUZZBENCH_RUNNER_METRICS`: set to `0` to disable runner metrics collection (default `1`).
- `SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS`: sampling interval in seconds for runner metrics (default `5`).
- `SCFUZZBENCH_SEED_CORPUS_SOURCE`: optional target-relative/absolute directory or `s3://bucket/prefix`. The same raw file tree is copied into every selected fuzzer corpus; no corpus-format conversion or archive extraction is performed. Inputs are limited to 10,000 regular, non-hardlinked files and 1 GiB.
- `SCFUZZBENCH_LOCAL_MODE`: set to `1` to enable local mode (used by `scripts/local-run.sh`). Changes workspace to `~/.scfuzzbench/`, skips shutdown/upload/apt, saves results locally.
- `SCFUZZBENCH_COMMON_SH`: path to `common.sh` (default: `/opt/scfuzzbench/common.sh`). Set automatically by `local-run.sh`.
- `SCFUZZBENCH_BIN_DIR`: directory for installed binaries (default: `/usr/local/bin`, or `~/.local/bin` in local mode).

## Echidna

Environment variables:
- `ECHIDNA_VERSION` (required)
- `ECHIDNA_CONFIG` or `ECHIDNA_TARGET` (required; add `ECHIDNA_CONTRACT` if needed)
- `ECHIDNA_WORKERS`, `ECHIDNA_TEST_MODE`, `ECHIDNA_EXTRA_ARGS`
- `ECHIDNA_CORPUS_DIR`
- `ECHIDNA_RTS_ARGS` (optional; defaults to `-A1g`; set to empty to disable RTS args)

Notes:
- In `property` mode, the runner rewrites `prefix: "invariant_"` to `prefix: "echidna_"` inside the config file so global properties are treated like assertions.
- The runner passes `--shrink-limit 1` by default, overriding `shrinkLimit` in target configs so shrinking does not consume the campaign budget. Set one non-negative `--shrink-limit` in `ECHIDNA_EXTRA_ARGS` for an explicit operator override. Extra arguments support shell-style quoting, but are parsed without shell evaluation.
- By default, the runner appends `+RTS -A1g -RTS` to reduce GC overhead on multicore instances.
- In pinned Echidna 2.3.1, the fuzzing worker that finds a failure also performs its shrinking inline before returning to fuzzing; there is no separate background minimizer. `shrinkLimit` bounds this worker-local work and is configured independently of Medusa corpus pruning.

## Recon Fuzzer

Environment variables:
- `RECON_VERSION` (required)
- `ECHIDNA_CONFIG` or `ECHIDNA_TARGET` (required; add `ECHIDNA_CONTRACT` if needed)
- `RECON_WORKERS`, `RECON_TEST_MODE`, `RECON_EXTRA_ARGS`, `RECON_CORPUS_DIR`
- Fallback compatibility knobs: `ECHIDNA_WORKERS`, `ECHIDNA_TEST_MODE`, `ECHIDNA_EXTRA_ARGS`, `ECHIDNA_CORPUS_DIR`

Notes:
- Runs with `recon fuzz . --format text`.
- In `property` mode, rewrites `prefix: "invariant_"` to `prefix: "echidna_"` in config for global property compatibility.

## Medusa

Environment variables:
- `MEDUSA_VERSION` (required)
- `MEDUSA_CONFIG` (optional; defaults to `medusa.json` when present)
- `MEDUSA_WORKERS`, `MEDUSA_CORPUS_DIR`, `MEDUSA_EXTRA_ARGS`
- `MEDUSA_PRUNE_FREQUENCY` (non-negative minutes; defaults to `0`, which disables the background corpus-pruner goroutine)

Notes:
- The pinned Medusa 1.4.1 release controls pruning through
  [`fuzzing.pruneFrequency`](https://github.com/crytic/medusa/blob/v1.4.1/docs/src/project_configuration/fuzzing_config.md#prunefrequency);
  it does not expose a pruning CLI flag. The runner creates a temporary working
  config beside the selected config, applies `MEDUSA_PRUNE_FREQUENCY`, and
  leaves the target's config unchanged. Set a positive value explicitly to
  re-enable periodic pruning for a non-comparative experiment.

## Foundry

Environment variables:
- `FOUNDRY_VERSION` or (`FOUNDRY_GIT_REPO` + `FOUNDRY_GIT_REF`)
- `FOUNDRY_THREADS` (defaults to `SCFUZZBENCH_WORKERS`, passes `--threads` to `forge test`)
- `FOUNDRY_TEST_ARGS` (passed to `forge test`; scfuzzbench adds `--invariant-workers auto` unless this includes an explicit `--invariant-workers` value)
- `SCFUZZBENCH_FOUNDRY_SOURCE_PATCH` (path to the digest-verified throughput pulse patch; cloud and `scripts/local-run.sh` set this automatically for the default pinned source)
- `SCFUZZBENCH_FOUNDRY_SHOWMAP` (set to `0` to skip Foundry showmap replay after the main campaign)
- `SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS` (optional timeout override for showmap replay; default is the smaller of the campaign timeout and 1800 seconds)
- `FOUNDRY_SHOWMAP_DOMAIN` (optional `forge test --showmap-domain` value)
- `FOUNDRY_SHOWMAP_CORPUS_DIR` (optional `forge test --showmap-corpus-dir` override; when unset, `forge` resolves corpus directories from project config)

The default pinned source is patched narrowly so its existing tx/gas JSON
pulse is emitted while `--show-progress` remains active and corpus persistence
remains disabled. The installer verifies both the exact Foundry commit and
patch digest. Source experiments that resolve away from the exact pin are not
patched.
