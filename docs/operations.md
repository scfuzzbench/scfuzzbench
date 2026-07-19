# Operations Guide

This page contains the practical setup and execution details for running `scfuzzbench`.

## Benchmark Inputs

Set inputs via `-var`/`tfvars` (`TF_VAR_*` also works):

- `target_repo_url`, `target_commit`
- `benchmark_type` (`property` or `optimization`)
- `instance_type`, `instances_per_fuzzer`, `timeout_hours`
- `fuzzers` (allowlist; empty means all available)
- stable fuzzer versions (`foundry_version`, `echidna_version`, `medusa_version`, `recon_version`)
- opt-in tool builds (`echidna_ci_*`, `medusa_git_*`, `medusa_go_*`; see below)
- `git_token_ssm_parameter_name` (for private repos)
- `fuzzer_env` values such as `SCFUZZBENCH_PROPERTIES_PATH`

Per-fuzzer environment variables are documented in `fuzzers/README.md`.

## Quick Start

```bash
make terraform-init
make terraform-deploy TF_ARGS="-var 'ssh_cidr=YOUR_IP/32' -var 'target_repo_url=REPO_URL' -var 'target_commit=COMMIT'"
```

## Local `.env` (Recommended)

```bash
# Usage: source .env
export AWS_PROFILE="your-profile"
export EXISTING_BUCKET="scfuzzbench-logs-..."
export TF_VAR_target_repo_url="https://github.com/org/repo"
export TF_VAR_target_commit="..."
export TF_VAR_timeout_hours=1
export TF_VAR_instances_per_fuzzer=4
export TF_VAR_fuzzers='["echidna","medusa","foundry","recon-fuzzer"]'
export TF_VAR_git_token_ssm_parameter_name="/scfuzzbench/recon/github_token"
```

Foundry builds from upstream [foundry-rs/foundry](https://github.com/foundry-rs/foundry) at the commit
pinned in `infrastructure/variables.tf` (`foundry_git_ref`). The pin must include invariant
assertion-failure reporting ([foundry-rs/foundry#14275](https://github.com/foundry-rs/foundry/pull/14275))
and continuous invariant campaigns with handler-bug dedup
([foundry-rs/foundry#14482](https://github.com/foundry-rs/foundry/pull/14482)); stable releases up to
v1.7.1 predate #14482, so keep the commit pin until a stable release ships both. The analysis pipeline
consumes upstream's `event: failure` JSON pulse events. Override `TF_VAR_foundry_git_repo` /
`TF_VAR_foundry_git_ref` only for experiments.

## Opt-in bleeding-edge tool builds

Stable Echidna and Medusa releases remain the default. A bleeding-edge mode is
enabled only when its complete input group is present. The runner never falls
back to a stable binary after an opt-in install fails.

### Echidna GitHub Actions artifact

Provide all of:

- `echidna_ci_repo`: public GitHub repository URL.
- `echidna_ci_run_id`: completed, successful Actions run ID.
- `echidna_ci_artifact_name`: exact Linux artifact name (upstream CI currently
  publishes `echidna-Linux`).
- `echidna_ci_artifact_sha256`: the artifact API `digest` without `sha256:`.
- `echidna_ci_commit`: full 40-character run head commit.
- `echidna_ci_token_ssm_parameter_name`: a SecureString containing a token with
  Actions read access to that repository.

GitHub Actions artifacts expire. Inspect the run before requesting a paid
benchmark:

```bash
repo=crytic/echidna
run_id=<run-id>
artifact=echidna-Linux
gh api "repos/$repo/actions/runs/$run_id" \
  --jq '{status,conclusion,head_sha,repository:.repository.full_name}'
gh api "repos/$repo/actions/runs/$run_id/artifacts?name=$artifact" \
  --jq '.artifacts[] | {id,name,digest,expired,expires_at,size_in_bytes,workflow_run}'
```

Store the token separately; put only its parameter name in a public benchmark
request:

```bash
aws ssm put-parameter \
  --name /scfuzzbench/echidna/actions_token \
  --type SecureString \
  --value "$ECHIDNA_ACTIONS_TOKEN" \
  --overwrite
```

The EC2 role receives `ssm:GetParameter` only for the configured parameter ARN.
At boot the installer re-checks repository, run success, head commit, artifact
identity, expiry, API digest, downloaded ZIP digest, archive safety, and the
binary's Linux x86-64 ELF identity. Authentication is held in a temporary
mode-`0600` curl config that is deleted immediately after download. The
canonical installed command is `echidna`.

### Medusa source build

Provide `medusa_git_repo`, `medusa_git_ref`, and the full
`medusa_git_commit`. The ref is resolved before Terraform apply and again on
every runner; movement away from the expected commit stops the run.

Source builds use an official pinned Go Linux amd64 distribution. The defaults
are Go `1.24.0` and SHA-256
`dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858`.
Override `medusa_go_version` and `medusa_go_sha256` together when the selected
commit requires a different toolchain. The installer verifies the distribution
checksum and toolchain identity, disables automatic toolchain switching, runs
`go mod verify`, and builds with `-mod=readonly -trimpath`.

Each opt-in leg writes `tool_provenance.json` into its log archive. It records
the repository/ref/commit, artifact or Go distribution digest, module
provenance, binary version, and binary SHA-256. The public benchmark manifest
also carries the non-secret requested pins.

Benchmark request issues expose the fields individually. GitHub's manual
`workflow_dispatch` form has a fixed input-count limit, so that form groups the
same fields into `echidna_ci_json` and `medusa_source_json`, for example:

```json
{"medusa_git_repo":"https://github.com/crytic/medusa","medusa_git_ref":"v1.4.1","medusa_git_commit":"3857153837ab90ed73adc484414b4b43703a54fb","medusa_go_version":"1.24.0","medusa_go_sha256":"dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858"}
```

## One Run At A Time

All benchmark dispatches share a single Terraform state and the same
`aws_instance.fuzzer["<fuzzer>-<index>"]` resource addresses with
`user_data_replace_on_change = true`. Applying a new run while a previous run's
instances are still fuzzing plans a destroy/recreate of those instances and
kills the in-flight run. **Dispatch runs strictly sequentially**: wait until a
run's instances have self-terminated (timeout + upload, plus the Foundry source
build before the fuzz window) before approving or dispatching the next one.

Create one [benchmark request](start.md) per target and apply
`benchmark/03-approved` to only one request at a time. The shared-state CI guard
rejects a new apply while benchmark instances remain active.

## Foundry Log Visibility

The foundry runner passes `--show-progress` to `forge test`. This is not
cosmetic: it installs forge's SIGINT handler (progress bars stay hidden on
non-TTY output), so the benchmark timeout's SIGINT produces a graceful exit
that prints the end-of-run summary. That summary is the only place handler
assertion bugs appear **with their names**
(`[FAIL: ...] <artifact>:<Contract>::<function>`); mid-campaign they are visible
only as `broken_assertions` counts in pulse metrics, which the analysis converts
into correctly-timed events and then names from the summary. Broken invariants
emit named `event: failure` JSON records mid-campaign either way.

## Re-run A Benchmark

Runners are one-shot. To execute again with a fresh run prefix:

```bash
export TF_VAR_run_id="$(date +%s)"
make terraform-destroy-infra TF_ARGS="-auto-approve -input=false"
make terraform-deploy TF_ARGS="-auto-approve -input=false"
```

## Remote State Backend

1. Create backend resources:

```bash
aws s3api create-bucket --bucket <state-bucket> --region us-east-1
aws s3api put-bucket-versioning --bucket <state-bucket> --versioning-configuration Status=Enabled
aws dynamodb create-table \
  --table-name <lock-table> \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

2. Create backend config:

```bash
cp infrastructure/backend.hcl.template infrastructure/backend.hcl
```

3. Initialize and migrate:

```bash
make terraform-init-backend
```

## Bucket Reuse

To reuse a long-lived logs bucket, set `EXISTING_BUCKET=<bucket-name>`.

If state still tracks bucket resources from an older deployment, remove them before switching:

```bash
AWS_PROFILE=your-profile terraform -chdir=infrastructure state rm \
  aws_s3_bucket.logs \
  aws_s3_bucket_public_access_block.logs \
  aws_s3_bucket_server_side_encryption_configuration.logs \
  aws_s3_bucket_versioning.logs
```

Destroy infra while preserving data bucket:

```bash
make terraform-destroy-infra
```

## Local Mode

You can run fuzzers locally without AWS infrastructure using `scripts/local-run.sh`. This is useful for development, debugging harnesses, or comparing fuzzer configurations on a single machine.

### Prerequisites

- The fuzzer binary must already be installed (the current stable runner uses
  `echidna-test`; CI artifact mode installs canonical `echidna` plus that
  compatibility alias), or pass `--install`
- Foundry must be installed (`forge`, `cast`)
- `zip` must be available for result packaging

### Usage

```bash
scripts/local-run.sh \
  -f echidna \
  -r https://github.com/org/target-repo \
  -b main \
  -t 3600 \
  -w 4 \
  --echidna-config echidna.yaml \
  --echidna-target test/recon/CryticTester.sol \
  --echidna-contract CryticTester
```

Required flags:
- `-f, --fuzzer`: `echidna`, `medusa`, `foundry`, or `recon-fuzzer`
- `-r, --repo`: target git repository URL
- `-b, --branch`: branch or commit to check out

Optional flags:
- `-t, --timeout`: campaign timeout in seconds (default: 86400)
- `-w, --workers`: number of fuzzer workers
- `-T, --type`: `property` or `optimization` (default: `property`)
- `--install`: run the fuzzer's `install.sh` first
- `--echidna-extra-args`: extra arguments passed to echidna (e.g. `"--server 3000 --shrink-limit 1"`)

All fuzzer-specific flags (`--echidna-*`, `--medusa-*`, `--foundry-*`) mirror the environment variables documented in `fuzzers/README.md`.

For local Echidna artifact mode, export `ECHIDNA_CI_TOKEN` and pass the five
non-secret `--echidna-ci-*` flags. There is intentionally no token flag, which
keeps the credential out of shell history. Medusa source mode uses
`--medusa-git-repo`, `--medusa-git-ref`, `--medusa-git-commit`, and optionally
the two `--medusa-go-*` overrides.

### How it works

Local mode sets `SCFUZZBENCH_LOCAL_MODE=1`, which changes common.sh behavior:

- **Workspace**: `~/.scfuzzbench/` instead of `/opt/scfuzzbench/`
- **Binaries**: `~/.local/bin/` instead of `/usr/local/bin/`
- **No shutdown**: instance shutdown is suppressed
- **No S3 upload**: results are saved locally to `~/.scfuzzbench/output/<repo>/<fuzzer>/<timestamp>/`
- **No apt**: system package installation is skipped

### Comparing configurations

To compare two fuzzer configurations (e.g. different Echidna builds), run them sequentially. Each run produces a timestamped output directory with logs and corpus archives. Use the analysis pipeline with `--raw-labels` (see below) to plot them as separate series.

## Analyze Results

Run the full pipeline in one pass:

```bash
DEST="$(mktemp -d /tmp/scfuzzbench-analysis-1770053924-XXXXXX)"
make results-analyze-all BUCKET=<bucket-name> RUN_ID=1770053924 BENCHMARK_UUID=<benchmark_uuid> DEST="$DEST" ARTIFACT_CATEGORY=both
```

This pipeline now also generates runner resource artifacts (`cpu_usage_over_time.png`, `memory_usage_over_time.png`, `runner_resource_usage.md`, and runner resource CSVs).

Quick readiness checks:

```bash
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "logs/$BENCHMARK_UUID/$RUN_ID/" --max-keys 1000 --query 'KeyCount' --output text
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "corpus/$BENCHMARK_UUID/$RUN_ID/" --max-keys 1000 --query 'KeyCount' --output text
```

Download with explicit benchmark UUID when needed:

```bash
make results-download BUCKET=<bucket-name> RUN_ID=1770053924 BENCHMARK_UUID=<benchmark_uuid> ARTIFACT_CATEGORY=both
```

Troubleshooting:

```bash
make results-inspect DEST="$DEST"
rg -n "error:|Usage:|cannot parse value" "$DEST/analysis" -S
```

```bash
aws ec2 get-console-output --instance-id i-0123456789abcdef0 --latest --output json \
  | jq -r '.Output' | tail -n 200
```

### Raw Labels

By default, the analysis pipeline normalizes fuzzer names: `echidna-baseline`, `echidna-bandit`, and `echidna-v2.3.1` all collapse to `echidna`. This is correct for cross-fuzzer benchmarks but wrong when comparing two configurations of the same fuzzer.

Pass `RAW_LABELS=1` to preserve directory names as fuzzer labels:

```bash
make results-analyze-all RAW_LABELS=1 BUCKET=<bucket> RUN_ID=<id> DEST="$DEST"
```

This threads `--raw-labels` through the full pipeline (`results-analyze-filtered`, `report-events-to-cumulative`, `report-runner-metrics`). Reports and plots will show `echidna-baseline` and `echidna-bandit` as separate series instead of merging them under `echidna`.

The flag works with both cloud-downloaded and local-mode logs. When using local mode, structure your prepared logs directory as:

```
logs/
  echidna-baseline/
    echidna.log
  echidna-bandit/
    echidna.log
```

Each subdirectory name becomes the fuzzer label in all CSVs and plots.

## CSV Report

```bash
make report-benchmark REPORT_CSV=results.csv REPORT_OUT_DIR=report_out REPORT_BUDGET=24
```

## Private Repos

Store a short-lived token in SSM and set `git_token_ssm_parameter_name`:

```bash
aws ssm put-parameter \
  --name "/scfuzzbench/recon/github_token" \
  --type "SecureString" \
  --value "$GITHUB_TOKEN" \
  --overwrite
```

For public repos, leave `git_token_ssm_parameter_name` empty.

## GitHub Actions

Two workflows publish benchmark runs and releases:

- `Benchmark Run` (`.github/workflows/benchmark-run.yml`): dispatch with target/mode/infra inputs.
- `Benchmark Release` (`.github/workflows/benchmark-release.yml`): analyzes completed runs and publishes release artifacts.

A run is treated as complete after `run_id + timeout_hours + 1h`.
