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
- `ECHIDNA_VERSION` (required)
- `ECHIDNA_CONFIG` or `ECHIDNA_TARGET` (required; add `ECHIDNA_CONTRACT` if needed)
- `ECHIDNA_WORKERS`, `ECHIDNA_TEST_MODE`, `ECHIDNA_EXTRA_ARGS`
- `ECHIDNA_CORPUS_DIR`
- `ECHIDNA_RTS_ARGS` (optional; defaults to `-A1g`; set to empty to disable RTS args)

Notes:
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
- `MEDUSA_VERSION` (required)
- `MEDUSA_CONFIG` (required)
- `MEDUSA_WORKERS`, `MEDUSA_CORPUS_DIR`

## Foundry

Environment variables:
- `FOUNDRY_VERSION` or (`FOUNDRY_GIT_REPO` + `FOUNDRY_GIT_REF`)
- `FOUNDRY_THREADS` (defaults to `SCFUZZBENCH_WORKERS`, passes `--threads` to `forge test`)
- `FOUNDRY_TEST_ARGS` (passed to `forge test`)
- `SCFUZZBENCH_FOUNDRY_SHOWMAP` (set to `0` to skip Foundry showmap replay after the main campaign)
- `SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS` (optional timeout override for showmap replay; default is the smaller of the campaign timeout and 1800 seconds)
- `FOUNDRY_SHOWMAP_DOMAIN` (optional `forge test --showmap-domain` value)
- `FOUNDRY_SHOWMAP_CORPUS_DIR` (optional `forge test --showmap-corpus-dir` override; when unset, `forge` resolves corpus directories from project config)
