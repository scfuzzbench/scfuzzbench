# Operations Guide

This page contains the practical setup and execution details for running `scfuzzbench`.

## Benchmark Inputs

Set inputs via `-var`/`tfvars` (`TF_VAR_*` also works):

- `target_repo_url`, `target_commit`
- `benchmark_type` (`property` or `optimization`)
- `instance_type`, `instances_per_fuzzer`, `timeout_hours`
- `fuzzers` (allowlist; empty means all available)
- fuzzer versions (`foundry_version`, `echidna_version`, `medusa_version`, optional `bitwuzla_version`)
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
export TF_VAR_fuzzers='["echidna","medusa","foundry"]'
export TF_VAR_git_token_ssm_parameter_name="/scfuzzbench/recon/github_token"
export TF_VAR_foundry_git_repo="https://github.com/0xalpharush/foundry"
export TF_VAR_foundry_git_ref="fail_on_assert"
```

For Foundry runs, use [0xalpharush/foundry](https://github.com/0xalpharush/foundry/tree/fail_on_assert).
Current analysis expects the branch's `event: failure` JSON events.

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

- The fuzzer binary must already be installed (e.g. `echidna-test` in `$PATH`)
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
- `-f, --fuzzer`: `echidna`, `medusa`, `foundry`, or `echidna-symexec`
- `-r, --repo`: target git repository URL
- `-b, --branch`: branch or commit to check out

Optional flags:
- `-t, --timeout`: campaign timeout in seconds (default: 86400)
- `-w, --workers`: number of fuzzer workers
- `-T, --type`: `property` or `optimization` (default: `property`)
- `--install`: run the fuzzer's `install.sh` first
- `--echidna-extra-args`: extra arguments passed to echidna (e.g. `"--server 3000 --shrink-limit 1"`)

All fuzzer-specific flags (`--echidna-*`, `--medusa-*`, `--foundry-*`) mirror the environment variables documented in `fuzzers/README.md`.

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
