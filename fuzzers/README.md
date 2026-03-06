# Fuzzers

Each fuzzer lives in `fuzzers/<name>/` with an `install.sh` and `run.sh`. Common behavior (clone, build, upload, timeout, shutdown) is in `fuzzers/_shared/common.sh`. Per-fuzzer configuration is provided via `fuzzer_env` in your local `tfvars`.

## Shared settings

- `SCFUZZBENCH_PROPERTIES_PATH`: repo-relative path to the properties file that gets patched for `benchmark_type` switching.
- `SCFUZZBENCH_SHUTDOWN_GRACE_SECONDS`, `SCFUZZBENCH_TIMEOUT_GRACE_SECONDS`: graceful shutdown/timeouts.
- `SCFUZZBENCH_GIT_TOKEN_SSM_PARAMETER`: SSM name for a token used to clone private target repos.
- `SCFUZZBENCH_WORKERS`: override default worker count (defaults to vCPU count on the instance).
- `SCFUZZBENCH_RUNNER_METRICS`: set to `0` to disable runner metrics collection (default `1`).
- `SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS`: sampling interval in seconds for runner metrics (default `5`).

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

## Echidna (symexec)

Environment variables:
- `ECHIDNA_VERSION`, `BITWUZLA_VERSION` (required)
- Same `ECHIDNA_*` knobs as above
- `ECHIDNA_SYMEXEC_CORPUS_DIR` (optional; defaults to `corpus/echidna-symexec`)

Notes:
- Runs with `echidna-test --sym-exec true`.

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
