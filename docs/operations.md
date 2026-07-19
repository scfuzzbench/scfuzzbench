# Operations Guide

This page contains the practical setup and execution details for running `scfuzzbench`.

## Benchmark Inputs

Set inputs via `-var`/`tfvars` (`TF_VAR_*` also works):

- `target_repo_url`, `target_commit`
- `benchmark_type` (`property` or `optimization`)
- `instance_type`, `instances_per_fuzzer`, `timeout_hours`
- `fuzzers` (allowlist; empty means all available)
- fuzzer versions (`foundry_git_repo`/`foundry_git_ref`, `echidna_version`, `medusa_version`, `recon_version`)
- `git_token_ssm_parameter_name` (for private repos)
- `shared_seed_corpus_source` (optional directory or `s3://bucket/prefix`; empty by default)
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
# Optional: a directory committed in the target repository.
export TF_VAR_shared_seed_corpus_source="benchmark-seeds/v1"
```

## Optional Shared Seed Corpus

The default remains a clean, empty corpus for every fuzzer. To warm-start a
run, set `shared_seed_corpus_source` to either:

- a directory relative to the cloned target repository, such as
  `benchmark-seeds/v1`; or
- an immutable S3 prefix, such as `s3://benchmark-inputs/seeds/v1`.

Absolute directories are supported when they exist on the runner, which is
mainly useful with `scripts/local-run.sh`. Cloud runners cannot see a path on
the Terraform operator's machine. For S3, Terraform grants the instance role
read/list access only to the configured prefix; cross-account buckets must also
allow that role in their bucket policy.

The directory contents are copied into each selected fuzzer's configured
corpus directory immediately before the campaign. The copy is recursive and
byte-for-byte, preserves relative paths, and rejects symlinks, hardlinks, device
nodes, sockets, and FIFOs. Archives are opaque seed files and are never
extracted. A fixed safety ceiling rejects more than 10,000 files or more than
1 GiB of source bytes before the live corpus is replaced. scfuzzbench does not
translate between fuzzer corpus formats; use only a seed layout every selected
fuzzer can consume. In particular, Foundry invariant seeds must already use
Foundry's contract/test directory layout. An opted-in Foundry seed corpus also
enables Foundry corpus persistence, with its existing memory tradeoff.

Each runner records `source`, file count, total bytes, and a deterministic
SHA-256 tree digest plus per-file paths, sizes, and hashes in
`logs/seed_corpus.json`; the same data is added to the run manifest and rendered
on the run report page. Target-relative inputs are reported as
`target://<path>`. Absolute host paths are represented by a SHA-256 path token
so distinct paths remain distinct without publishing any host path component.

For S3, each object download is bound to the listed ETag or exact version ID,
and the complete prefix listing must remain unchanged before and after the
download. Partial downloads and unsafe or colliding object keys fail closed.
The live corpus is replaced only after a second copy produces the same
byte/path digest. Canonical manifests use create-once writes: later instances
must verify byte-identical content instead of overwriting a different
manifest. The tree digest visits files in UTF-8 relative-path byte order and
hashes, for each file, the big-endian 64-bit path length, relative-path bytes,
big-endian 64-bit file size, and file bytes.

Foundry builds from upstream [foundry-rs/foundry](https://github.com/foundry-rs/foundry) at the commit
pinned in `infrastructure/variables.tf` (`foundry_git_ref`). The pin must include invariant
assertion-failure reporting ([foundry-rs/foundry#14275](https://github.com/foundry-rs/foundry/pull/14275))
and continuous invariant campaigns with handler-bug dedup
([foundry-rs/foundry#14482](https://github.com/foundry-rs/foundry/pull/14482)); stable releases up to
v1.7.1 predate #14482, so keep the commit pin until a stable release ships both. The analysis pipeline
consumes upstream's `event: failure` JSON pulse events. Override `TF_VAR_foundry_git_repo` /
`TF_VAR_foundry_git_ref` only for experiments.

Cloud benchmark requests do not expose `foundry_version`: setting only that release tag would be ignored while the
non-empty git repository selects the source build. For a local release-binary run, use
`scripts/local-run.sh --foundry-version <tag>` without `FOUNDRY_GIT_REPO`. Low-level Terraform callers can select the
same fallback only by explicitly setting `foundry_git_repo` to an empty string.

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

- The fuzzer binary must already be installed (e.g. `echidna` in `$PATH`)
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

Add `--seed-corpus ./path/to/seeds` (resolved from the invocation directory) or
`--seed-corpus s3://bucket/prefix` to opt in locally.

Required flags:
- `-f, --fuzzer`: `echidna`, `medusa`, `foundry`, or `recon-fuzzer`
- `-r, --repo`: target git repository URL
- `-b, --branch`: branch or commit to check out

Optional flags:
- `-t, --timeout`: campaign timeout in seconds (default: 86400)
- `-w, --workers`: number of fuzzer workers
- `-T, --type`: `property` or `optimization` (default: `property`)
- `--install`: run the fuzzer's `install.sh` first
- `--seed-corpus`: shared local directory or S3 prefix (default: empty)
- `--echidna-extra-args`: extra arguments passed to echidna (e.g. `"--server 3000 --shrink-limit 25"`)
- `--medusa-prune-frequency`: positive pruning interval in minutes for an explicit Medusa experiment (default: `0`, disabled)

All fuzzer-specific flags (`--echidna-*`, `--medusa-*`, `--foundry-*`) mirror the environment variables documented in `fuzzers/README.md`.

Echidna runs default to `--shrink-limit 1`, passed as a CLI option so it overrides any `shrinkLimit` in the target config. Supply one non-negative value through `--echidna-extra-args` when an experiment intentionally needs a different amount of shrinking. Extra arguments support shell-style quoting without shell evaluation; malformed quoting and duplicate shrink-limit options fail before Echidna starts.

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

The pipeline also generates:

- evidence-backed ground-truth artifacts (`known_bug_report.md`,
  `known_bug_summary.csv`, and `known_bug_findings.csv`);
- runner resource artifacts (`cpu_usage_over_time.png`,
  `memory_usage_over_time.png`, `runner_resource_usage.md`, and runner resource
  CSVs).

If the run target or commit does not exactly match the curated catalog,
`known_bug_report.md` explains why mapping was withheld and the ground-truth
CSVs remain empty.

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
