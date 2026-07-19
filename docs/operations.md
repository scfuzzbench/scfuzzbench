# Operations Guide

This page contains the practical setup and execution details for running `scfuzzbench`.

## Benchmark Inputs

Set inputs via `-var`/`tfvars` (`TF_VAR_*` also works):

- `target_repo_url`, `target_commit`
- `benchmark_type` (`property` or `optimization`)
- `instance_type`, `instances_per_fuzzer`, `timeout_hours`
- `fuzzers` (allowlist; empty means all available)
- fuzzer versions (`foundry_version`, `echidna_version`, `medusa_version`, `recon_version`)
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

## Isolated Runs And Admission

Each approved [benchmark request](start.md) remains one target and one short
dispatch workflow. Before Terraform initializes, the workflow creates an
immutable ID such as `gh-123456789-1` and forces the backend key to
`runs/<run_id>/terraform.tfstate`. `terraform init` uses `-reconfigure`; it
never migrates or copies the legacy `scfuzzbench/terraform.tfstate`.

The artifact bucket and state backend are pre-existing shared infrastructure.
Cloud dispatch hard-requires `SCFUZZBENCH_BUCKET` and `TF_BACKEND_CONFIG`.
Because `existing_bucket_name` is always set, a run plan owns no S3 bucket,
bucket policy, or public-access configuration. A plan guard rejects shared S3
resources before apply or cleanup.

Admission is intentionally conservative:

- Repository variable `SCFUZZBENCH_MAX_CONCURRENT_RUNS` sets the limit; unset
  means `1`, and supported values are `1` through `20`.
- A short job-level concurrency group serializes only the capacity decision.
  Inside it, the workflow counts active S3 reservations and active
  `Project=scfuzzbench` EC2 instances, then writes one reservation object.
- The admission job releases its Actions lock immediately. It does not wait for
  provisioning or fuzzing.
- Active legacy instances without `RunId` are each counted conservatively.

Keep the default at `1` until AWS vCPU quota and the full per-run EC2 cost have
been reviewed. Increasing the variable is the explicit parallel-run opt-in.
Run-owned AWS resources use run-scoped names and carry `Project`, `RunId`,
`BenchmarkUuid`, `TargetRepo`, and `TargetCommit` tags where AWS supports tags.

### Asynchronous cleanup and recovery

`Benchmark Run Cleanup` runs hourly and can be dispatched manually. It cleans a
run when all expected final log archives exist and its instances are terminal.
Runners also overwrite a small, run-scoped heartbeat object every five minutes,
starting before expensive tool builds. After timeout plus a three-hour recovery
margin, the workflow treats an unfinished reservation as an orphan only when
its heartbeat is stale for at least 30 minutes **and** no active EC2 instance
with that `RunId` remains. A failed heartbeat upload never authorizes destroying
an active instance.

Cleanup initializes only the backend key recorded for that run. Before destroy
it requires exact `run_id`, backend-key, and benchmark outputs, rejects
create/update or shared-S3 changes, and checks every taggable resource in the
plan has the same `RunId`.
Only the saved, verified destroy plan is applied. Capacity is released only
after no same-run EC2 instance remains.

Recovery metadata is retained at:

```text
run-state/runs/<run_id>/metadata.json
run-state/runs/<run_id>/cleanup.auto.tfvars.json
```

The empty post-destroy state remains at its versioned backend key. Failed
provisioning keeps its active reservation and recovery inputs, so a maintainer
can use `Benchmark Run Cleanup` (force orphan cleanup when justified) or
`Terraform Run Recovery`. Neither workflow targets another run's key.
Recovery accepts no arbitrary Terraform arguments or bucket override. Its
read-only plan must be a no-op; destroy must be delete-only. Potentially
sensitive `fuzzer_env` values are never written to recovery objects, because a
destroy plan does not need user-data inputs.

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

Runners are one-shot. In CI, approve a new benchmark-request issue (or retry as
a new Actions attempt); it receives a new immutable run ID and state key.
For local-only Terraform use, set a distinct `TF_VAR_run_id` and
`TF_VAR_run_started_at_epoch` before initializing its dedicated backend key.

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

3. Initialize a dedicated run key:

```bash
RUN_ID="local-$(date +%s)"
make terraform-init-backend BACKEND_KEY="runs/${RUN_ID}/terraform.tfstate"
```

The default init flags are `-reconfigure -input=false`. State migration is
never implicit. If the legacy key exists, leave it and its shared resources
untouched; do not pass `-migrate-state` for a new run key.

## Bucket Reuse

Cloud runs always reuse a long-lived logs bucket. For local runs, set
`EXISTING_BUCKET=<bucket-name>` so the run state cannot own that bucket.

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
