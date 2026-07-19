# Fuzzers

Each fuzzer lives in `fuzzers/<name>/` with an `install.sh` and `run.sh`. Common behavior (clone, build, upload, timeout, shutdown) is in `fuzzers/_shared/common.sh`. Per-fuzzer configuration is provided via `fuzzer_env` in your local `tfvars`.

## Shared settings

- `SCFUZZBENCH_PROPERTIES_PATH`: repo-relative path to the properties file that gets patched for `benchmark_type` switching.
- `SCFUZZBENCH_SHUTDOWN_GRACE_SECONDS`, `SCFUZZBENCH_TIMEOUT_GRACE_SECONDS`: graceful shutdown/timeouts.
- `SCFUZZBENCH_GIT_TOKEN_SSM_PARAMETER`: SSM name for a token used to clone private target repos.
- `SCFUZZBENCH_WORKERS`: override default worker count (defaults to vCPU count on the instance).
- `SCFUZZBENCH_RUNNER_METRICS`: set to `0` to disable runner metrics collection (default `1`).
- `SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS`: sampling interval in seconds for runner metrics (default `5`).
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
- `ECHIDNA_CORPUS_DIR`
- `ECHIDNA_RTS_ARGS` (optional; defaults to `-A1g`; set to empty to disable RTS args)

Notes:
- CI artifact mode verifies run/commit/artifact metadata, expiry, archive and
  binary digests, safe extraction, and Linux x86-64 ELF identity. It installs
  the canonical `echidna` command and records `tool_provenance.json`.
- In `property` mode, the runner rewrites `prefix: "invariant_"` to `prefix: "echidna_"` inside the config file so global properties are treated like assertions.
- By default, the runner appends `+RTS -A1g -RTS` to reduce GC overhead on multicore instances.

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
- `MEDUSA_VERSION` (required for the default stable release path)
- Source mode (all required together): `MEDUSA_GIT_REPO`, `MEDUSA_GIT_REF`,
  `MEDUSA_GIT_COMMIT`, `MEDUSA_GO_VERSION`, `MEDUSA_GO_SHA256`
- `MEDUSA_CONFIG` (required)
- `MEDUSA_WORKERS`, `MEDUSA_CORPUS_DIR`

Source mode verifies that the ref still resolves to the requested full commit,
checks the official Go distribution digest, uses `GOTOOLCHAIN=local`, verifies
the module cache against `go.sum`, builds with readonly module semantics, and
records source/toolchain/binary provenance in `tool_provenance.json`.

## Foundry

Environment variables:
- `FOUNDRY_VERSION` or (`FOUNDRY_GIT_REPO` + `FOUNDRY_GIT_REF`)
- `FOUNDRY_THREADS` (defaults to `SCFUZZBENCH_WORKERS`, passes `--threads` to `forge test`)
- `FOUNDRY_TEST_ARGS` (passed to `forge test`; scfuzzbench adds `--invariant-workers auto` unless this includes an explicit `--invariant-workers` value)
- `SCFUZZBENCH_FOUNDRY_SHOWMAP` (set to `0` to skip Foundry showmap replay after the main campaign)
- `SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS` (optional timeout override for showmap replay; default is the smaller of the campaign timeout and 1800 seconds)
- `FOUNDRY_SHOWMAP_DOMAIN` (optional `forge test --showmap-domain` value)
- `FOUNDRY_SHOWMAP_CORPUS_DIR` (optional `forge test --showmap-corpus-dir` override; when unset, `forge` resolves corpus directories from project config)
