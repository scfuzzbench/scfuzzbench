# Operations Guide

This page contains the practical setup and execution details for running `scfuzzbench`.

## Benchmark Inputs

Set inputs via `-var`/`tfvars` (`TF_VAR_*` also works):

- `target_repo_url`, `target_commit`
- `benchmark_type` (`property` or `optimization`)
- `instance_type`, `instances_per_fuzzer`, `timeout_hours`
- `fuzzers` (allowlist; empty means all available)
- fuzzer versions (`foundry_git_repo`/`foundry_git_ref`, `echidna_version`, `medusa_version`, `recon_version`)
- opt-in tool builds (`echidna_ci_*`, `medusa_git_*`, `medusa_go_*`; see below)
- `git_token_ssm_parameter_name` (for private repos)
- `properties_path` (optional repo-relative properties contract path)
- `shared_seed_corpus_source` (optional directory or `s3://bucket/prefix`; empty by default)
- `fuzzer_env` (optional extra fuzzer settings; framework-owned, AWS, credential, path, and identity keys are rejected)

Per-fuzzer environment variables are documented in `fuzzers/README.md`.
The combined UTF-8 byte length of caller-supplied `fuzzer_env` keys and values
is limited to 4096 bytes so EC2 bootstrap data stays within the API limit.

## Quick Start

```bash
make terraform-init
make terraform-deploy TF_ARGS="-var 'ssh_cidr=YOUR_IP/32' -var 'target_repo_url=REPO_URL' -var 'target_commit=COMMIT'"
```

## Cloud Bootstrap Provenance

Cloud instances download runtime scripts only from the canonical public
`scfuzzbench/scfuzzbench` repository at the full `scfuzzbench_commit`. Terraform
records and passes a SHA-256 manifest for every required regular file; the
instance rejects unsafe archive entries and verifies every file before
installing or executing any repository content. Direct Terraform deployments
therefore require every bundled bootstrap file (including the rendered
user-data template and verifier) to match content already published at that
commit. This check also works in the tarball-based CI checkout, where no
`.git` directory exists.

`custom_fuzzer_definitions` are intentionally rejected by the cloud Terraform
module. Local-only scripts cannot be proven to exist in the immutable public
archive. Use `scripts/local-run.sh` for local customizations; add a new
committed built-in definition when a fuzzer should be deployable in cloud
benchmarks.

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
([foundry-rs/foundry#14482](https://github.com/foundry-rs/foundry/pull/14482)) and the tx/gas
throughput counters from
[foundry-rs/foundry#14266](https://github.com/foundry-rs/foundry/pull/14266). Stable releases up to
v1.7.1 predate #14482, so keep the commit pin until a stable release ships the required behavior.

The pinned upstream source only writes its throughput pulse when terminal progress is disabled and
edge coverage is enabled. Those conditions conflict with two production safeguards below.
`fuzzers/foundry/throughput-progress.patch` therefore makes the existing pulse cadence independent
of those display/coverage modes. The installer applies that patch only to the exact pinned commit,
verifies its SHA-256 digest, and fails on drift. `foundry_source_patch` in each benchmark manifest
records the patch identity. A source override that resolves away from the exact pinned commit is
intentionally left unpatched, so its throughput availability depends on that source tree.

Cloud benchmark requests do not expose `foundry_version`: setting only that release tag would be ignored while the
non-empty git repository selects the source build. For a local release-binary run, use
`scripts/local-run.sh --foundry-version <tag>` without `FOUNDRY_GIT_REPO`. Low-level Terraform callers can select the
same fallback only by explicitly setting `foundry_git_repo` to an empty string.

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
- `echidna_ci_token_kms_key_arn` (optional): the exact customer-managed KMS key
  ARN used by that SecureString. Leave blank for the account's AWS-managed
  `alias/aws/ssm` key.

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

Only Echidna artifact instances use the dedicated CI instance profile. It grants
`ssm:GetParameter` only for the configured parameter ARN; stable Echidna and
other fuzzer instances receive no access to that token. With a customer-managed
KMS key, the profile grants `kms:Decrypt` only for the exact key ARN and only
when the encryption context is the exact parameter ARN via regional SSM.
Aliases and wildcard KMS permissions are rejected.
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
commit requires a different toolchain. The installer binds the configured
digest and size to Go's official download metadata, stream-extracts with
entry/depth/expanded-byte limits, verifies toolchain identity, disables
automatic toolchain switching, runs `go mod verify`, preserves `go.mod` and
`go.sum`, and builds with `-mod=readonly -trimpath`.

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

Cleanup initializes only the backend key recorded for that run. The saved
cleanup variables, active reservation, and cleanup matrix must agree on the
exact `run_id`, backend key, shared bucket, and full lowercase
`scfuzzbench_commit`. That provisioning commit must be current `main` or an
ancestor of current `main`. Terraform init, inspection, planning, and apply use
the repository tree downloaded at that exact provisioning commit, so cleanup
continues to match the configuration that created the state. The workflow
orchestration and every state/plan validator always run from the separately
checked-out current `main` tree.

Normal cleanup requires exact `run_id`, backend-key, and benchmark outputs.
An explicitly forced or recorded provisioning-failure cleanup may recover a
partially applied state whose outputs were never created, but every output that
does exist must still match. All paths reject create/update or shared-S3
changes and require every taggable plan resource to carry the same `RunId`.
Only the saved, verified delete-only plan is applied. Capacity is released only
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
Forced cleanup requires one explicit, valid `run_id`; it never advances the
deadline or selects every reservation. It may terminate active resources for
that one run, so use it only after confirming the run should stop.
Recovery accepts no arbitrary Terraform arguments or bucket override. Its
read-only plan must be a no-op; destroy must be delete-only. Potentially
sensitive `fuzzer_env` values are never written to recovery objects, because a
destroy plan does not need user-data inputs.

All workflows that consume AWS credentials or publish GitHub/Pages state first
authorize `refs/heads/main`. Their concurrency groups are ref-scoped or
run-scoped so an untrusted branch dispatch cannot cancel, replace, or overlap a
privileged main run.

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

The pinned source patch also emits an `event: pulse` JSON record approximately
every five seconds after a completed invariant run while `--show-progress` is
active. It preserves the final summary and does not re-enable `corpus_dir`.
Each pulse carries campaign-wide `total_txs`, `total_gas`, `tps`, and `gps`;
the `worker` field identifies the worker that won the reporting interval.

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

- The fuzzer binary must already be installed, or pass `--install`
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

For local Echidna artifact mode, export `ECHIDNA_CI_TOKEN` and pass the five
non-secret `--echidna-ci-*` flags. There is intentionally no token flag, which
keeps the credential out of shell history. Medusa source mode uses
`--medusa-git-repo`, `--medusa-git-ref`, `--medusa-git-commit`, and optionally
the two `--medusa-go-*` overrides.

Comparative runs default every fuzzer's tool-native numeric shrink limit to
`1`, overriding target configuration: Echidna and Recon use
`--shrink-limit 1`, Medusa's temporary config uses `fuzzing.shrinkLimit: 1`,
and Foundry uses `FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT=1`. This does not imply
equal algorithms, candidate replay counts, or CPU work; one Recon iteration may
evaluate a parallel batch of candidates. Intentional non-comparative
experiments can set one override within the pinned tool's numeric domain
through `ECHIDNA_EXTRA_ARGS`, `RECON_EXTRA_ARGS`, `MEDUSA_SHRINK_LIMIT`, or
`FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT`, respectively. Echidna and Recon extra
arguments support shell-style quoting without shell evaluation; malformed
quoting and duplicate shrink-limit options fail before the fuzzer starts.
Medusa extra arguments use the same safe quoting behavior and reject
`--config` so they cannot replace the generated effective config.

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
  CSVs);
- saved-corpus selector artifacts (`selector_distribution.csv` and
  `selector_summary.json`).

Keep `ARTIFACT_CATEGORY=both`: selector analytics require downloaded corpus archives.

If the run target or commit does not exactly match the curated catalog,
`known_bug_report.md` explains why mapping was withheld and the ground-truth
CSVs remain empty.

The default expected-selector list is an explicitly labelled peer-consensus heuristic. Echidna and Recon count as one related evidence family, so they cannot corroborate each other by themselves; unavailable and empty corpora do not define consensus. To use a reviewed ground-truth catalog instead, pass a JSON array of selectors or objects containing `selector` and optional `signature`/`function_name` fields:

```bash
make results-analyze-all ... ARTIFACT_CATEGORY=both EXPECTED_SELECTORS_JSON=/path/to/expected-selectors.json
```

Foundry is reported as selector data `unavailable` when its corpus was not persisted; failure-only log selectors are intentionally not counted as a corpus distribution.
Peer-heuristic gaps are informational diagnostics rather than ground-truth failures. A selector/signature disagreement in an explicit catalog fails analysis, and unsafe or malformed corpus inputs are bounded and surfaced in `selector_summary.json`.

Quick readiness checks:

```bash
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "logs/$RUN_ID/$BENCHMARK_UUID/" --max-keys 1000 --query 'KeyCount' --output text
aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "corpus/$RUN_ID/$BENCHMARK_UUID/" --max-keys 1000 --query 'KeyCount' --output text
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

### Preliminary results for active runs

Cloud runs publish immutable checkpoints every 60 minutes by default. Set
`preliminary_interval_minutes` in the benchmark request:

- Use a value from 1 to 1440 minutes to change the interval.
- Use `0` to disable preliminary results.

The scheduled **Benchmark Preliminary Results** workflow selects one settled
checkpoint number for the whole run. It verifies every archive and file
checksum before running the existing analysis. Results stay under:

```text
preliminary/<run_id>/<benchmark_uuid>/
  run.json
  snapshots/<checkpoint>/<replicate>/snapshot.zip
  analysis/<checkpoint>/
```

Each chart and Markdown report includes its as-of time, elapsed and planned
budget, and missing-replicate count. These views are incomplete and
non-terminal. Do not rank fuzzers, declare success or failure, or stop a run
from preliminary data. Canonical analysis still appears only after the
**Benchmark Release** workflow completes.

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

### Coverage Over Time

Native coverage-over-time reporting is disabled by default because the fuzzers expose different bytecode/instrumentation counters rather than a common source-based metric. Enable it during analysis with:

```bash
make results-analyze-all \
  COVERAGE_OVER_TIME=1 \
  BUCKET=<bucket> RUN_ID=<id> BENCHMARK_UUID=<benchmark_uuid> DEST="$DEST"
```

This is an analysis-only opt-in; it does not start another benchmark. It adds per-fuzzer signal provenance, availability, observation windows, and limitations to `REPORT.md`. Repeated live signals are shown in `coverage_over_time.png` on independent y-axes; end-only signals are table-only, and lines stop at the last observation instead of being extended to the report budget. Malformed coverage values fail analysis explicitly. See [Coverage over time](/methodology#coverage-over-time) for signal definitions and comparability limits.

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

Three workflows publish benchmark data:

- `Benchmark Run` (`.github/workflows/benchmark-run.yml`): dispatch with target/mode/infra inputs.
- `Benchmark Preliminary Results` (`.github/workflows/benchmark-preliminary.yml`): analyzes immutable checkpoints for active runs without waiting in Actions.
- `Benchmark Release` (`.github/workflows/benchmark-release.yml`): analyzes completed runs and publishes release artifacts.

A run is treated as complete after its explicit start epoch, timeout, and one-hour release grace.
