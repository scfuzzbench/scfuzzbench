# Fuzzers

Each fuzzer lives in `fuzzers/<name>/` with an `install.sh` and `run.sh`. Common behavior (clone, build, upload, timeout, shutdown) is in `fuzzers/_shared/common.sh`. Per-fuzzer configuration is provided via `fuzzer_env` in your local `tfvars`.

Cloud `fuzzer_env` accepts ordinary fuzzer settings plus a small
`SCFUZZBENCH_*` safe allowlist: `SCFUZZBENCH_WORKERS`, runner-metrics controls,
and the documented Foundry corpus/showmap controls below. Framework-owned
identity, credential, AWS, timing, helper, and filesystem settings are
rejected; use their dedicated workflow/Terraform inputs instead. Corpus
directory overrides must be repo-relative paths and are resolved strictly
beneath the cloned target before any reset or upload.

## Shared settings

- `properties_path` (dedicated Terraform/workflow input): repo-relative path to the properties file that gets patched for `benchmark_type` switching.
- `git_token_ssm_parameter_name` (dedicated Terraform/workflow input): SSM name for a token used to clone private target repos.
- `SCFUZZBENCH_WORKERS`: override default worker count (defaults to vCPU count on the instance).
- `SCFUZZBENCH_RUNNER_METRICS`: set to `0` to disable runner metrics collection (default `1`).
- `SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS`: sampling interval in seconds for runner metrics (default `5`).
- `preliminary_interval_seconds` (dedicated input): immutable preliminary checkpoint interval. Cloud runners align checkpoints to `SCFUZZBENCH_RUN_STARTED_AT_EPOCH`; local mode never uploads them.
- `shared_seed_corpus_source` (dedicated input): optional target-relative directory or `s3://bucket/prefix`. The same raw file tree is copied into every selected fuzzer corpus; no corpus-format conversion or archive extraction is performed. Inputs are limited to 10,000 regular, non-hardlinked files and 1 GiB.
- `SCFUZZBENCH_LOCAL_MODE`: set to `1` to enable local mode (used by `scripts/local-run.sh`). Changes workspace to `~/.scfuzzbench/`, skips shutdown/upload/apt, saves results locally.
- `SCFUZZBENCH_COMMON_SH`: path to `common.sh` (default: `/opt/scfuzzbench/common.sh`). Set automatically by `local-run.sh`.
- `SCFUZZBENCH_BIN_DIR`: directory for installed binaries (default: `/usr/local/bin`, or `~/.local/bin` in local mode).

## Echidna

Environment variables:
- `ECHIDNA_VERSION` (required for the default stable release path)
- CI artifact mode (all required together): `ECHIDNA_CI_REPO`,
  `ECHIDNA_CI_RUN_ID`, `ECHIDNA_CI_ARTIFACT_NAME`,
  `ECHIDNA_CI_ARTIFACT_SHA256`, `ECHIDNA_CI_COMMIT`
- CI artifact authentication: `ECHIDNA_CI_TOKEN_SSM_PARAMETER` (cloud) or
  `ECHIDNA_CI_TOKEN` (local only; never pass it as a Terraform variable)
- `ECHIDNA_CONFIG` or `ECHIDNA_TARGET` (required; add `ECHIDNA_CONTRACT` if needed)
- `ECHIDNA_WORKERS`, `ECHIDNA_TEST_MODE`, `ECHIDNA_EXTRA_ARGS`
- `ECHIDNA_CORPUS_DIR` (optional repo-relative path beneath the cloned target)
- `ECHIDNA_RTS_ARGS` (optional; defaults to `-A1g`; set to empty to disable RTS args)

Notes:
- CI artifact mode verifies run/commit/artifact metadata, expiry, archive and
  binary digests, safe extraction, and Linux x86-64 ELF identity. It installs
  the canonical `echidna` command without a legacy alias and records
  `tool_provenance.json`.
- In `property` mode, the runner rewrites `prefix: "invariant_"` to `prefix: "echidna_"` inside the config file so global properties are treated like assertions.
- The runner passes `--shrink-limit 1` by default, overriding `shrinkLimit` in target configs so shrinking cannot consume a large part of the campaign budget. Set one integer in `[0, 9223372036854775807]` in `ECHIDNA_EXTRA_ARGS` for an explicit operator override on the pinned amd64 runner. Extra arguments support shell-style quoting, but are parsed without shell evaluation. A bare `--` option terminator is rejected so the runner's appended arguments remain effective.
- By default, the runner appends `+RTS -A1g -RTS` to reduce GC overhead on multicore instances.
- In Echidna, the fuzzing worker that finds a failure also performs its shrinking inline before returning to fuzzing; there is no separate background minimizer. `shrinkLimit` bounds this worker-local work and is configured independently of Medusa corpus pruning.

## Recon Fuzzer

Environment variables:
- `RECON_VERSION` (required)
- `ECHIDNA_CONFIG` or `ECHIDNA_TARGET` (required; add `ECHIDNA_CONTRACT` if needed)
- `RECON_WORKERS`, `RECON_TEST_MODE`, `RECON_EXTRA_ARGS`, `RECON_CORPUS_DIR` (corpus path must be repo-relative)
- Fallback compatibility knobs: `ECHIDNA_WORKERS`, `ECHIDNA_TEST_MODE`, `ECHIDNA_EXTRA_ARGS`, `ECHIDNA_CORPUS_DIR` (corpus path must be repo-relative)

Notes:
- Runs with `recon fuzz . --format text`.
- In `property` mode, rewrites `prefix: "invariant_"` to `prefix: "echidna_"` in config for global property compatibility.
- The runner passes `--shrink-limit 1` by default, overriding both Recon's
  built-in limit and `shrinkLimit` in the target's Echidna-format config. Set
  one integer in `[0, 2147483647]` in `RECON_EXTRA_ARGS` for an explicit
  operator override. This bound avoids overflow in Recon's signed
  shrink-progress counter. The `ECHIDNA_EXTRA_ARGS` compatibility fallback has
  the same validation. Extra arguments support shell-style quoting without
  shell evaluation. A bare `--` option terminator is rejected so the runner's
  appended arguments remain effective.

## Medusa

Environment variables:
- `MEDUSA_VERSION` (required for the default stable release path)
- Source mode (all required together): `MEDUSA_GIT_REPO`, `MEDUSA_GIT_REF`,
  `MEDUSA_GIT_COMMIT`, `MEDUSA_GO_VERSION`, `MEDUSA_GO_SHA256`
- `MEDUSA_CONFIG` (optional; defaults to `medusa.json` when present)
- `MEDUSA_WORKERS`, `MEDUSA_CORPUS_DIR` (repo-relative), `MEDUSA_EXTRA_ARGS`
- `MEDUSA_PRUNE_FREQUENCY` (non-negative minutes; defaults to `0`, which disables the background corpus-pruner goroutine)
- `MEDUSA_SHRINK_LIMIT` (`uint64` iterations; defaults to `1`)

Notes:
- The Medusa release controls pruning through
  [`fuzzing.pruneFrequency`](https://github.com/crytic/medusa/blob/master/docs/src/project_configuration/fuzzing_config.md#prunefrequency);
  it does not expose a pruning CLI flag. The runner creates a temporary working
  config beside the selected config, applies `MEDUSA_PRUNE_FREQUENCY`, and
  leaves the target's config unchanged. Set a positive value explicitly to
  re-enable periodic pruning for a non-comparative experiment.
- The same temporary config sets `fuzzing.shrinkLimit` to
  `MEDUSA_SHRINK_LIMIT`, overriding the target config without modifying it.
  Shrinking and corpus pruning are separate controls. `MEDUSA_EXTRA_ARGS` is
  parsed with shell-style quoting but without shell evaluation, and may not
  contain another `--config` because the generated config is authoritative.
  A bare `--` option terminator is rejected so the runner's appended corpus
  directory remains effective.

Source mode verifies that the ref still resolves to the requested full commit,
checks digest and size against official Go metadata, stream-extracts with
bounded entry/depth/size limits, uses `GOTOOLCHAIN=local`, verifies the module
cache and unchanged module lock files, builds with readonly module semantics,
and records source/toolchain/binary provenance in `tool_provenance.json`.

## Foundry

Environment variables:
- `FOUNDRY_VERSION` or (`FOUNDRY_GIT_REPO` + `FOUNDRY_GIT_REF`)
- `FOUNDRY_THREADS` (defaults to `SCFUZZBENCH_WORKERS`, passes `--threads` to `forge test`)
- `FOUNDRY_TEST_ARGS` (passed to `forge test`; scfuzzbench adds `--invariant-workers auto` unless this includes an explicit `--invariant-workers` value)
- `FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT` (`uint32` invariant shrink attempts; defaults to `1`)
- `FOUNDRY_CORPUS_DIR` (optional repo-relative path beneath the cloned target)
- `SCFUZZBENCH_FOUNDRY_KEEP_CORPUS` (`0` or `1`; preserves the invariant corpus when enabled)
- `SCFUZZBENCH_FOUNDRY_SOURCE_PATCH` (path to the digest-verified throughput pulse patch; cloud and `scripts/local-run.sh` set this automatically for the default pinned source)
- `SCFUZZBENCH_FOUNDRY_SHOWMAP` (set to `1` to opt in to Foundry showmap replay after the main campaign; disabled by default)
- `SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS` (optional timeout override for showmap replay; default is the smaller of the campaign timeout and 1800 seconds)
- `FOUNDRY_SHOWMAP_DOMAIN` (optional `forge test --showmap-domain` value)
- `FOUNDRY_SHOWMAP_CORPUS_DIR` (optional `forge test --showmap-corpus-dir` override; when unset, `forge` resolves corpus directories from project config)

The default pinned source is patched narrowly so its existing tx/gas JSON
pulse is emitted while `--show-progress` remains active and corpus persistence
remains disabled. The installer verifies both the exact Foundry commit and
patch digest. Source experiments that resolve away from the exact pin are not
patched.

## Comparable inline shrinking

Comparative runs set every supported fuzzer's tool-native numeric shrink limit
to `1`. Echidna and Recon receive `--shrink-limit 1`, Medusa receives
`fuzzing.shrinkLimit: 1` in its temporary effective config, and Foundry receives
`FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT=1`. This aligns the configured number, not
the algorithms, candidate replay count, or CPU work. In particular, one Recon
shrink iteration may generate and evaluate a parallel batch of candidates.
Per-tool overrides within the pinned tool's numeric domain are for intentional
non-comparative experiments.
