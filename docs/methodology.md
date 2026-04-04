# Methodology

This page documents the current benchmarking methodology used by `scfuzzbench`.

## Objectives

- Run different fuzzers under equivalent infrastructure and runtime constraints.
- Pin versions and inputs so runs are reproducible.
- Publish enough raw and processed artifacts for independent inspection.
- Use robust, distribution-aware reporting across repeated runs.

## End-to-End Benchmark Flow

### 1) Define and pin benchmark inputs

Core inputs are defined through Terraform vars and/or workflow dispatch:

- Target: `target_repo_url`, `target_commit`
- Mode: `benchmark_type` (`property` or `optimization`)
- Infra: `instance_type`, `instances_per_fuzzer`, `timeout_hours`
- Fuzzer set: `fuzzers` (or default all available)
- Tool versions: `foundry_version`, `echidna_version`, `medusa_version`, plus optional `bitwuzla_version`

In CI (`.github/workflows/benchmark-run.yml`), inputs are validated before apply (value ranges, formats, and conservative character constraints).

### 2) Compute run identity and benchmark identity

Terraform computes two IDs used across the pipeline:

- `run_id`:
  - Explicit `var.run_id` if provided.
  - Otherwise `time_static.run.unix` (state-stable; repeated applies can reuse it).
- `benchmark_uuid`:
  - `md5(jsonencode(benchmark_manifest))` in `infrastructure/main.tf`.

`benchmark_manifest` includes pinned context such as:

- `scfuzzbench_commit`, `target_repo_url`, `target_commit`
- `benchmark_type`, `instance_type`, `instances_per_fuzzer`, `timeout_hours`
- `aws_region`, `ubuntu_ami_id`
- tool versions and selected `fuzzer_keys`

This means changing any of those manifest fields changes `benchmark_uuid`.

### 3) Provision equivalent runners

Terraform provisions one EC2 instance per `(fuzzer, run_index)` pair:

- Same AMI family for all (`ubuntu_ami_ssm_parameter`).
- Same instance type and timeout budget for all fuzzers in a run.
- AZ auto-selected from offering data for the requested instance type (unless `availability_zone` is explicitly set).
- `user_data_replace_on_change = true` so runner behavior changes trigger replacement.

### 4) Execute benchmark on each runner

Runner lifecycle is defined in `infrastructure/user_data.sh.tftpl` and `fuzzers/_shared/common.sh`:

- Install only that runner's fuzzer implementation (`fuzzers/<name>/install.sh`).
- Clone target repository and checkout the pinned commit.
- Build with `forge build`.
- Run fuzzer command under `timeout` (`SCFUZZBENCH_TIMEOUT_SECONDS`).
- Collect host metrics periodically into `runner_metrics.csv` (enabled by default).
- Upload artifacts to S3, then self-shutdown.

Instances are intentionally one-shot:

- A bootstrap sentinel (`/opt/scfuzzbench/.bootstrapped`) avoids accidental reruns after reboot.
- Shutdown occurs even on failures via trap/finalizer handling.

### 5) Benchmark type switching

`benchmark_type` behavior is applied by `apply_benchmark_type` in `fuzzers/_shared/common.sh`:

- Uses `SCFUZZBENCH_PROPERTIES_PATH` from `fuzzer_env` to locate the properties contract.
- Applies deterministic `sed` transforms for `property` vs `optimization` mode.
- If `optimization` is requested but required markers/files are missing, the run fails early.

### 6) Upload and index artifacts

Each instance uploads:

- Logs zip: `s3://<bucket>/logs/<run_id>/<benchmark_uuid>/i-...-<fuzzer>.zip`
- Optional corpus zip: `s3://<bucket>/corpus/<run_id>/<benchmark_uuid>/i-...-<fuzzer>.zip`
- Benchmark manifest:
  - `logs/<run_id>/<benchmark_uuid>/manifest.json`
  - `runs/<run_id>/<benchmark_uuid>/manifest.json` (timestamp-first index used by docs)

## What Counts as a Complete Run

Docs and release automation use the same completion rule:

- `now >= run_id + (timeout_hours * 3600) + 3600`

Notes:

- `run_id` is interpreted as a Unix timestamp.
- `timeout_hours` comes from `manifest.json` (default `24` if missing).
- `3600` is a fixed 1-hour grace window.

This rule is implemented in:

- `scripts/generate_docs_site.py`
- `.github/workflows/benchmark-release.yml`

Only complete runs are listed as benchmark results pages.

## Analysis and Reporting Methodology

### Canonical analysis pipeline

The default full pipeline is:

```bash
make results-analyze-all BUCKET=... RUN_ID=... BENCHMARK_UUID=... DEST=...
```

This expands to:

1. Download logs/corpus bundles (`scripts/download_run_artifacts.py`)
2. Collect `*.log` files into analysis layout (`scripts/prepare_analysis_logs.py`)
3. Parse events + summaries (`scripts/run_analysis_filtered.py` -> `analysis/analyze.py`)
4. Convert event stream to cumulative series (`analysis/events_to_cumulative.py`)
5. Build report + charts (`analysis/benchmark_report.py`)
6. Build broken-invariant overlap artifacts (`analysis/invariant_overlap_report.py`)
7. Build runner CPU/memory artifacts (`analysis/runner_metrics_report.py`)

Optional controls include `EXCLUDE_FUZZERS`, `REPORT_BUDGET`, `REPORT_GRID_STEP_MIN`, `REPORT_CHECKPOINTS`, `REPORT_KS`, `INVARIANT_TOP_K`, and `RUNNER_METRICS_BIN_SECONDS`.

### Event extraction semantics (`analysis/analyze.py`)

- Parser is fuzzer-aware:
  - Foundry: parse JSON lines, count events only from records with `event=failure`, and use the first JSON `timestamp` as the elapsed-time baseline.
  - Medusa: parse elapsed markers and failed assertions/properties from textual logs.
  - Echidna variants: parse falsification markers from textual logs.
  - Unknown fuzzers: fall back to generic pattern parsing.
- Event de-duplication is per run-instance stream (same event name counted once per run).
- Outputs:
  - `events.csv` (raw event stream)
  - `summary.csv` (run-level aggregates)
  - `overlap.csv` (cross-fuzzer Jaccard overlap)
  - `exclusive.csv` (events found by exactly one fuzzer)
  - `throughput_samples.csv` (raw tx/s and gas/s samples recovered from logs when available)
  - `throughput_summary.csv` (per-fuzzer tx/s and gas/s distribution summary)
  - `progress_metrics_samples.csv` (raw fuzzer-native progress metrics such as seq/s, coverage proxy, corpus size, favored items, failure rate when available)
  - `progress_metrics_summary.csv` (per-fuzzer distribution summary of those progress metrics)

### Cumulative conversion (`analysis/events_to_cumulative.py`)

- Produces long-form CSV: `fuzzer, run_id, time_hours, bugs_found`.
- Run keys are stabilized as `run_id:instance_id`.
- When `--logs-dir` is provided, runs with zero detected events still emit a time `0` row (unless `--no-zero`).

### Report generation (`analysis/benchmark_report.py`)

- Validates each run's cumulative sequence:
  - non-decreasing time
  - non-decreasing integer bug counts
  - non-negative counts
- Resamples all runs onto a common forward-filled time grid (`REPORT_GRID_STEP_MIN`, default 6 min).
- Computes distribution-oriented metrics per fuzzer:
  - checkpoint medians + IQR
  - normalized AUC
  - plateau time
  - late discovery share
  - time-to-k median + reach rate
  - final distribution (median + IQR)
- Note: these report scorecards are count-based. They do not score severity or root-cause uniqueness.
- If `throughput_summary.csv` is present, the report also includes tx/s and gas/s summary tables.
- If `throughput_samples.csv` is present, the report also emits throughput trend charts (`tx_per_second_over_time.png`, `gas_per_second_over_time.png`).
- If `progress_metrics_summary.csv` is present, the report also includes per-fuzzer progress proxy tables (seq/s, coverage, corpus, favored, failure rate) and progress-metrics summary charts.
- If `progress_metrics_samples.csv` is present, the report also emits progress trend charts (`seq_per_second_over_time.png`, `coverage_proxy_over_time.png`, `corpus_size_over_time.png`, `favored_items_over_time.png`, `failure_rate_over_time.png`).
- Emits:
  - `REPORT.md`
  - `bugs_over_time.png`
  - `time_to_k.png`
  - `final_distribution.png`
  - `plateau_and_late_share.png`

If input CSV is empty, the report explicitly records the no-data condition and emits placeholder plots.

### Broken-invariant overlap (`analysis/invariant_overlap_report.py`)

- Uses `events.csv` (optionally budget-filtered) to summarize which invariant/event names were observed.
- Emits:
  - `broken_invariants.md`
  - `broken_invariants.csv`
  - `invariant_overlap_upset.png`
- These artifacts provide per-fuzzer totals, exclusives, shared subsets, and normalized invariant labels.
- Important interpretation note: UpSet overlap is approximate, not exact root-cause equivalence.
  - Two assertions inside one target function can represent distinct bugs (for example, one in the `try` success path vs one in the `catch` path, where one indicates an unexpected successful-result condition and the other indicates a DoS/revert behavior).
  - Foundry-side assertion surfacing depends on the current `foundry-rs/foundry#13322` behavior (<https://github.com/foundry-rs/foundry/issues/13322>), so normalized overlap should be read as an approximation.
  - Even in Echidna vs Medusa comparisons, overlap is still approximate: Echidna may falsify `assert(x != y)` while Medusa falsifies `assert(a != b)` in the same target-function body, which are distinct bugs even if function-level normalization groups them together.
- UpSet chart layout follows: Lex A, Gehlenborg N, Strobelt H, Vuillemot R, Pfister H. *UpSet: Visualization of Intersecting Sets*. IEEE TVCG 20(12), 2014 ([doi:10.1109/TVCG.2014.2346248](https://doi.org/10.1109/TVCG.2014.2346248)).

### Runner resource reporting (`analysis/runner_metrics_report.py`)

- Uses `runner_metrics*.csv` files collected on each runner to summarize host resource usage over time.
- Emits:
  - `runner_resource_usage.md`
  - `runner_resource_summary.csv`
  - `runner_resource_timeseries.csv`
  - `cpu_usage_over_time.png`
  - `memory_usage_over_time.png`
- CPU is reported as active percentage (`user + system + iowait`) and memory is reported as used percentage/GiB.

## Publication and Release

`Benchmark Release` workflow:

- Discovers complete runs automatically (or accepts explicit `benchmark_uuid` + `run_id`).
- Runs the same analysis pipeline in CI.
- Publishes analysis artifacts to:
  - `s3://<bucket>/analysis/<benchmark_uuid>/<run_id>/...`
- Creates a GitHub release tag:
  - `scfuzzbench-<benchmark_uuid>-<run_id>`

The docs site also supports legacy analysis under `reports/<benchmark_uuid>/<run_id>/`, but new runs should use `analysis/...`.

## Missing Analysis Triage

If a run is complete but shows missing analysis:

1. Re-run GitHub Actions `Benchmark Release` for that `benchmark_uuid` + `run_id`.
2. Or run analysis locally and upload artifacts to `analysis/<benchmark_uuid>/<run_id>/`.
3. If the run is junk, delete its S3 prefixes (`runs/`, `logs/`, optional `corpus/`, partial `analysis/`).

These runs remain visible in docs to support triage.

## Choosing Target Projects

Issue reference: <https://github.com/Recon-Fuzz/scfuzzbench/issues/8>  
Guideline source: <https://github.com/fuzz-evaluator/guidelines>

Target selection should follow guideline items A.2.2-A.2.5:

- A.2.2: select a representative set from the target domain.
- A.2.3: include targets used by related work for comparability.
- A.2.4: do not cherry-pick targets based on preliminary outcomes.
- A.2.5: avoid overlapping codebases with substantial shared code.

Recommended operational policy for this repository:

1. Freeze the target list before benchmark execution.
2. Pin each target to an immutable commit.
3. Record a rationale for each target (why it improves representativeness).
4. Include related-work targets where feasible, and cite source papers/benchmarks.
5. Track overlap groups (for forks/wrappers/shared-core code) and keep only one representative per overlap group unless explicitly justified.
6. Keep the selection manifest in-repo so additions/removals are reviewable.

Suggested manifest fields per target:

- repository URL
- pinned commit
- properties path (`SCFUZZBENCH_PROPERTIES_PATH`)
- benchmark mode(s) intended
- rationale
- related-work reference(s)
- overlap group / exclusion notes

## Caveats and Reproducibility Notes

- `timeout_hours` applies to fuzzer execution; clone/build/setup occur before timed fuzzing starts.
- Re-running Terraform without changing state can reuse `time_static` `run_id`; set explicit `run_id` for distinct runs.
- Bucket defaults allow public object read (`bucket_public_read=true`) so docs/releases can link directly to S3 artifacts.
- Keep secrets out of Terraform vars and docs; use SSM or environment-based secret handling.
