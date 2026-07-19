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
- Tool versions: `foundry_git_repo`/`foundry_git_ref`, `echidna_version`, `medusa_version`, `recon_version`
- Optional immutable tool sources: an Echidna Actions run/artifact/commit/digest
  or a Medusa repository/ref/commit plus checksummed Go toolchain
- Optional shared input: `shared_seed_corpus_source` (empty by default)

In CI (`.github/workflows/benchmark-run.yml`), inputs are validated before apply (value ranges, formats, and conservative character constraints).
Cloud runs build Foundry from the pinned git source. The release-tag `foundry_version` path is available only to
`scripts/local-run.sh` (or a low-level Terraform invocation with `foundry_git_repo` explicitly empty).

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
- tool versions, `foundry_source_patch`, and selected `fuzzer_keys`
- non-secret opt-in source pins and expected artifact/toolchain digests
- shared seed source/type/copy semantics when a seed corpus is configured

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
- Reset the fuzzer corpus and optionally copy the same shared seed tree into it.
- Run fuzzer command under `timeout` (`SCFUZZBENCH_TIMEOUT_SECONDS`).
- Collect host metrics periodically into `runner_metrics.csv` (enabled by default).
- Upload artifacts to S3, then self-shutdown.

Medusa's pinned 1.4.1 release normally starts a separate corpus-pruner
goroutine every five minutes when coverage is enabled. Comparative runs set
`fuzzing.pruneFrequency` to `0` in a temporary working config so this auxiliary
CPU work does not sit outside the normalized worker count. The target's config
is not modified. Operators can explicitly set `MEDUSA_PRUNE_FREQUENCY` to a
positive minute interval for non-comparative experiments.

Pinned Echidna 2.3.1 does not spawn a separate minimization worker. Its
[`runFuzzWorker`](https://github.com/crytic/echidna/blob/v2.3.1/lib/Echidna/Campaign.hs#L353-L418)
performs shrinking inline on the same fuzzing worker that found the failure
before that worker resumes fuzzing. Its `shrinkLimit` therefore bounds
worker-local work and is configured separately; it is not a background-thread
control and is unchanged by the Medusa pruning policy.

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
- Per-leg `tool_provenance.json` inside the logs archive for opt-in binaries,
  including resolved commit and installed binary SHA-256
- Shared-seed provenance, when configured:
  - `seed_corpus.json` inside each logs zip
  - `seed_corpus` in the benchmark manifest (redacted/collision-safe source,
    fixed limits, per-file hashes, tree SHA-256, and S3 object identity)

Canonical manifests are create-once objects. Concurrent instances may confirm
that an existing object is byte-identical, but cannot overwrite a different
manifest.

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
make results-analyze-all BUCKET=... RUN_ID=... BENCHMARK_UUID=... DEST=... ARTIFACT_CATEGORY=both
```

This expands to:

1. Download logs/corpus bundles (`scripts/download_run_artifacts.py`)
2. Collect `*.log` files, runner metrics, and Foundry showmap artifacts into analysis layout (`scripts/prepare_analysis_logs.py`)
3. Parse events, summaries, saved-corpus selector distributions, and differential coverage artifacts (`scripts/run_analysis_filtered.py`)
4. Convert event stream to cumulative series (`analysis/events_to_cumulative.py`)
5. Map events to revision-locked known-bug IDs (`analysis/known_bug_report.py`)
6. Build report + charts (`analysis/benchmark_report.py`)
7. Build broken-invariant overlap artifacts (`analysis/invariant_overlap_report.py`)
8. Build runner CPU/memory artifacts (`analysis/runner_metrics_report.py`)

Optional controls include `EXCLUDE_FUZZERS`, `REPORT_BUDGET`, `REPORT_GRID_STEP_MIN`, `REPORT_CHECKPOINTS`, `REPORT_KS`, `INVARIANT_TOP_K`, `RUNNER_METRICS_BIN_SECONDS`, and `COVERAGE_OVER_TIME`.

### Event extraction semantics (`analysis/analyze.py`)

- Parser is fuzzer-aware:
  - Foundry: parse JSON lines, count bug events only from records with `event=failure`, recover tx/gas throughput from `event=pulse`, and use the first JSON `timestamp` as the elapsed-time baseline.
  - Medusa: parse elapsed markers and failed assertions/properties from textual logs.
  - Echidna and Recon Fuzzer: parse falsification markers from textual logs.
  - Unknown fuzzers: fall back to generic pattern parsing.
- Event de-duplication is per run-instance stream (same event name counted once
  per replicate).
  These are normalized failure identities, not crash inputs and not necessarily
  confirmed root-cause bugs.
- Outputs:
  - `events.csv` (raw event stream)
  - `summary.csv` (run-level aggregates)
  - `overlap.csv` (cross-fuzzer Jaccard overlap)
  - `exclusive.csv` (events found by exactly one fuzzer)
  - `throughput_samples.csv` (raw tx/s and gas/s samples recovered from logs when available)
  - `throughput_summary.csv` (per-fuzzer tx/s and gas/s distribution summary)
  - `progress_metrics_samples.csv` (raw fuzzer-native progress metrics such as seq/s, corpus size, favored items, and failure rate; native coverage proxies are included only with `COVERAGE_OVER_TIME=1`)
  - `progress_metrics_summary.csv` (per-fuzzer distribution summary of those progress metrics)
  - `selector_distribution.csv` (per-fuzzer selector counts from unique saved corpus sequences)
  - `selector_summary.json` (instance availability, provenance, expected-selector heuristic, and health warnings)
  - `differential_coverage_summary.csv` (human-readable baseline/feature verdicts computed from per-sample relscore statistics and relcov non-inferiority against baseline reliability)
  - `differential_coverage_statistics.json` (machine-readable verdict inputs, per-campaign test results, intervals, sample counts, and aggregate verdict)
  - `differential_coverage_relscores.csv` (relscore values computed from normalized AFL showmap campaigns)
  - `differential_coverage_relcov.csv` (pairwise non-self relcov values computed from normalized AFL showmap campaigns)
  - `showmap_campaign_manifest.json` (raw showmap inputs, skipped inputs, and normalized campaign summaries)
  - `showmap_campaigns/` (canonical `approach/trial.txt` campaign directories used for relscore scoring)

### Function selector sanity checks (`analysis/selector_analytics.py`)

- Selector counts are occurrences in unique saved corpus sequences, not runtime invocation frequencies. Identical serialized sequences are counted once per instance.
- Echidna and Recon calls are reconstructed from typed corpus entries. Medusa uses its saved ABI signature and calldata. Foundry is parsed only when a persisted Foundry corpus is present; current runs that disable Foundry corpus persistence are reported as `unavailable`, and failure-log selectors are never substituted for corpus data.
- `observed_zero` means a supported, readable corpus was present but contained no calls. It is distinct from `unavailable`, which means selector coverage could not be measured, `malformed`, which means no selector artifact could be safely parsed, and `partial`, which means only part of the corpus was usable.
- By default, the expected-selector list is a peer-consensus heuristic, not benchmark ground truth. A selector qualifies only when it appears in every available instance of each supporting engine and support spans at least two independent evidence families. Echidna and Recon count as one related typed-corpus family; an unavailable or empty tool never supplies support. Supply a reviewed catalog with `EXPECTED_SELECTORS_JSON=/path/to/selectors.json` when ground-truth expectations are available.
- Missing selectors from the peer heuristic are informational diagnostics, while gaps against a reviewed explicit catalog are warnings. The report also warns about readable corpora with zero calls, malformed or resource-limited artifacts, and signature/selector mismatches. Tool-specific limitations and expectation provenance remain visible in `REPORT.md` and `selector_summary.json`.
- Corpus discovery skips symlinks and enforces traversal-depth, file-count, per-file decompression, aggregate decompression, and parsed-call limits. Hitting a limit produces an explicit partial/malformed status instead of silently using incomplete data.

### Foundry throughput semantics

- Upstream Foundry PR [#14266](https://github.com/foundry-rs/foundry/pull/14266) supplies cumulative
  accepted-call and gas counters plus average rates. The pinned upstream source gates those pulses
  behind mutually incompatible display/coverage conditions, so scfuzzbench applies the
  digest-verified `fuzzers/foundry/throughput-progress.patch` only at the exact default source pin.
- The patch changes reporting reachability only. It keeps `--show-progress` (and therefore graceful
  SIGINT/final summaries), automatic invariant workers, the natural campaign timeout, and the
  corpus-off memory safeguard unchanged.
- `total_txs` counts completed non-discard calls; `total_gas` sums their reported gas. `tps` and
  `gps` are campaign-to-date averages, not instantaneous rates. With multiple invariant workers the
  totals are campaign-wide; the pulse's `worker.id` only identifies the worker that emitted that
  interval.
- Pulses are cadence-gated at roughly five seconds and occur after a completed invariant run. A
  long in-flight transaction can therefore create a wider gap. Source overrides that resolve away
  from the exact pinned commit do not receive the patch and may legitimately produce no Foundry
  throughput samples.
- The existing release pipeline publishes `throughput_samples.csv`,
  `throughput_summary.csv`, `tx_per_second_over_time.png`, and
  `gas_per_second_over_time.png`; no rate is inferred from host CPU or other proxy telemetry.

### Ground-truth known-bug mapping (`analysis/known_bug_report.py`)

- [`benchmarks/known_bugs.json`](../benchmarks/known_bugs.json) is the
  authoritative, schema-validated mapping from target-specific canonical IDs to
  fuzzer event aliases.
- Mapping is applied only when the run repository and commit exactly match the
  evidence-pinned target catalog. Runs against mutable or different revisions
  receive an explicit "mapping unavailable" report instead of speculative
  classifications.
- Qualified names, Solidity signatures, assertion suffixes, and legacy Foundry
  assertion-wrapper prefixes are normalized before alias lookup.
- Multiple aliases for one canonical bug count at most once per replicate,
  identified by `(run_id, instance_id, fuzzer)`. Crash
  inputs and corpus entries are never used as bug identities.
- Health-check canaries have their own denominator and never contribute to the
  real known-bug hit rate.
- Unknown event identities remain explicit `unmapped` rows. They are triage
  candidates, not claimed bugs.
- Hit rate is computed per fuzzer as canonical known-bug/replicate hits divided
  by `(known bugs discoverable by that fuzzer × configured replicates)`.
  Configured replicates with missing logs remain in the denominator.
- Outputs:
  - `known_bug_report.md` (human-readable hit rates, catalog entries, and unmapped identities)
  - `known_bug_summary.csv` (per-fuzzer known-bug and canary hit rates)
  - `known_bug_findings.csv` (per-run canonical mappings and unmapped identities)

### Differential coverage from Foundry showmap

- Foundry runs emit AFL `showmap`-style coverage files under the uploaded log artifact when the installed `forge` supports `forge test --showmap-out`.
- Raw Foundry replay output may use `approach__suite/trial.txt` for invariant replay and `approach__suite__test/trial.txt` for fuzz-test replay.
- `scripts/prepare_analysis_logs.py` preserves uploaded `showmap/` trees beside each prepared instance log directory.
- Analysis normalizes raw Foundry showmap output into canonical campaign directories before scoring:
  - `showmap_campaigns/combined/<approach>/<trial>.txt` unions all showmap files for each trial.
  - `showmap_campaigns/by_test/<suite-test>/<approach>/<trial>.txt` preserves per-test drill-down campaigns.
- `by_test/...` campaigns are drill-down only: their point relscore/relcov values are emitted in the relscore/relcov CSVs and as inconclusive summary rows, but the full per-campaign bootstrap verdict is skipped by default because it feeds neither the aggregate verdict nor any report consumer and otherwise dominates analysis time. Set `--verdict-by-test` (or `SCFUZZBENCH_DIFFCOV_VERDICT_BY_TEST=1`) to compute them.
- Relscore and relcov are computed through the `differential-coverage` package from normalized AFL showmap campaign directories. Only positive AFL showmap counts are treated as covered edges.
- Relcov gating compares the feature's per-trial retention of `upper(baseline)` against the baseline reliability diagonal, not against an absolute coverage floor. The default non-inferiority margin is 0.05 relcov.
- When a campaign has one baseline approach (`master`, `main`, `stable`, or a `*-master`/`*-main` label) and one feature approach, `differential_coverage_summary.csv` records separate `relscore` and `relcov` metric rows with the same reported verdict:
  - `improvement`: relscore is significantly higher after target-family Holm correction, the effect size is meaningful, and relcov non-inferiority is held against the baseline reliability diagonal.
  - `needs-review`: relscore is significantly higher but relcov non-inferiority is inconclusive.
  - `regression`: relscore is significantly lower, or relcov non-inferiority fails.
  - `inconclusive`: the result is not significant after correction, has too few samples, is missing required seed pairs, or has insufficient samples for the confidence intervals.
- Low-run campaigns still report point relscore, relcov, pairing rate, and sample counts, but the verdict is `inconclusive` with a `verdict_reason` such as `too few runs`.
- Differential coverage has an explicit pairing mode. `unpaired` treats repeated
  rounds as independent samples. `paired` requires matching seed-labeled trials
  across arms and refuses silent fallback to an unpaired test when matches are
  absent.
- Multi-target CI tags each trial with its target before differential coverage
  analysis. `differential_coverage_summary.csv` then includes `by_target/<name>`
  campaign rows before any Slack status rendering. Aggregate status is only an
  improvement when a majority of targets improve and no target regresses; any
  target regression blocks an aggregate improvement.
- `SCFUZZBENCH_FOUNDRY_SHOWMAP=1` enables Foundry showmap collection; it is disabled by default. `FOUNDRY_SHOWMAP_DOMAIN`, `FOUNDRY_SHOWMAP_CORPUS_DIR`, and `SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS` tune replay behavior. When no corpus override is set, showmap replay lets `forge` resolve the corpus directories from the target's Foundry config. Replay timeout defaults to the smaller of the campaign timeout and 1800 seconds so showmap collection stays within the benchmark completion grace window unless explicitly overridden.

### Coverage over time

Coverage-over-time analysis is opt-in:

```bash
make results-analyze-all \
  BUCKET=... RUN_ID=... BENCHMARK_UUID=... \
  COVERAGE_OVER_TIME=1
```

The flag extracts supported native coverage counters and adds a coverage limitations table to `REPORT.md`. Repeated live counters are plotted in `coverage_over_time.png`, with a separate panel and y-axis for each available fuzzer signal. End-of-run-only counters remain in the table and are not stretched into a time series. Native signals are not normalized, pooled, ranked, or treated as a universal coverage metric.

Source-based coverage is preferred, but no current real-time log format exposes source locations. The available signals and limitations are:

| Fuzzer | Native signal | Source-based? | Temporal availability | Limitation |
|---|---|---|---|---|
| Echidna | `cov` coverage points | No | Echidna status lines | Tool/config-specific counter; the progress log has no source locations. |
| Medusa | `branches hit` | No | Medusa status lines | Branch identities depend on compiled bytecode/config; the progress log has no source locations. |
| Foundry | `cumulative_edges_seen` | No | Compatible Foundry JSON pulse builds | Edge identities depend on the build/instrumentation. |
| Recon Fuzzer | `Unique instructions` | No | End-of-run summary only | Tool/version-specific final instruction count; there are no source locations or intermediate observations. Recon's separate Echidna-style `cov` status field is not mixed into this series. |

Foundry showmap is intentionally separate. It is an opt-in, post-campaign edge replay for comparable Foundry A/B campaigns, not real-time or source-based coverage. When a native signal is absent, the report records zero available runs rather than substituting another metric. The table also records parser provenance and observation windows: partial runs are labelled, live lines stop at each run's last observation, and malformed/non-finite/negative coverage values make opt-in report generation fail instead of being reported as unavailable.

### Cumulative conversion (`analysis/events_to_cumulative.py`)

- Produces long-form CSV: `fuzzer, run_id, time_hours, bugs_found`.
- `bugs_found` is a legacy compatibility name for cumulative normalized event
  identities. Use the ground-truth outputs for confirmed known-bug claims.
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
- Compares each fuzzer pair's end-of-budget bug counts with a two-sided
  Mann-Whitney U test, applies a Bonferroni correction, and reports the
  Vargha-Delaney A12 effect size. A12 is expressed as the probability that
  Fuzzer A outperforms Fuzzer B (ties count as half); values above `0.5` favor
  A and values below `0.5` favor B. Effect magnitudes are classified by
  distance from `0.5` as negligible (`<0.06`), small (`<0.14`), medium
  (`<0.21`), or large (`>=0.21`). These magnitude thresholds are descriptive
  rules of thumb; neither A12 nor statistical significance establishes
  practical importance, causation, or performance beyond the observed runs.
- Note: these report scorecards use normalized event-identity counts. They do
  not count crash inputs, but they also do not establish severity or root-cause
  uniqueness. Use `known_bug_report.md` and its CSVs for ground-truth hit rates.
- If `throughput_summary.csv` is present, the report also includes tx/s and gas/s summary tables.
- If `throughput_samples.csv` is present, the report also emits throughput trend charts (`tx_per_second_over_time.png`, `gas_per_second_over_time.png`).
- If `progress_metrics_summary.csv` is present, the report also includes per-fuzzer progress proxy tables (seq/s and corpus).
- If `progress_metrics_samples.csv` is present, the report also emits progress trend charts (`seq_per_second_over_time.png` and `corpus_size_over_time.png`).
- With `COVERAGE_OVER_TIME=1`, the report includes per-fuzzer native coverage availability, provenance, observation windows, and final-value rows. It emits `coverage_over_time.png` with independent scales when at least one live signal has two observations. Without the opt-in, coverage columns remain empty, the coverage report section is omitted, and no coverage chart is generated.
- If `selector_summary.json` is present, the report includes corpus availability, selector distributions, expected-selector provenance, health warnings, and limitations.
- Emits:
  - `REPORT.md`
  - `bugs_over_time.png`
  - `time_to_k.png`
  - `final_distribution.png`
  - `plateau_and_late_share.png`
  - `differential_coverage_statistics.png`, when differential coverage statistics are available

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
  - Foundry-side assertion surfacing uses upstream Foundry's handler-assertion reporting (issue <https://github.com/foundry-rs/foundry/issues/13322>, implemented by [#14275](https://github.com/foundry-rs/foundry/pull/14275) and [#14482](https://github.com/foundry-rs/foundry/pull/14482)), which dedups handler bugs at the function-entry-point level like Echidna/Medusa; normalized overlap should still be read as an approximation.
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

### Preliminary checkpoints

Active cloud runs may expose hourly, point-in-time copies of runner logs and
metrics under the separate `preliminary/` prefix. A checkpoint is a monitoring
aid, not a benchmark result: it is incomplete, may omit late replicas, and
cannot support comparisons, pass/fail decisions, or optional stopping. Every
preliminary report and chart carries that warning plus the fixed checkpoint
time and elapsed budget. Final reports use the unchanged canonical pipeline.

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

The authoritative catalog is
[`benchmarks/targets.json`](https://github.com/scfuzzbench/scfuzzbench/blob/main/benchmarks/targets.json);
its validation and maintenance workflow are documented in
[`benchmarks/README.md`](https://github.com/scfuzzbench/scfuzzbench/blob/main/benchmarks/README.md).
It records:

- repository URL
- pinned commit
- properties path (`SCFUZZBENCH_PROPERTIES_PATH`)
- rationale
- related-work reference(s)
- overlap group / exclusion notes

## Caveats and Reproducibility Notes

- **Recon ignores time-delay caps from echidna-format configs.** Recon-fuzzer (as of v0.4.x)
  has no `maxTimeDelay`/`maxBlockDelay` fields in its echidna.yaml parser and no CLI
  equivalent; it runs with a default max time delay of 604800s and auto-adjusts it upward
  from mined timestamp constants. On targets calibrated to bounded time advances (e.g. Drips:
  echidna `maxTimeDelay: 100`, medusa `blockTimestampDelayMax: 100`, foundry
  `max_time_delay = 100`), recon explores a vastly larger time-delay space than the other
  three legs, so recon-exclusive findings on time-calibrated targets may be time-delay
  artifacts and are not directly comparable. Tracked in scfuzzbench#177; upstream support
  requested in Recon-Fuzz/recon-fuzzer.
- `timeout_hours` applies to fuzzer execution; clone/build/setup occur before timed fuzzing starts.
- Re-running Terraform without changing state can reuse `time_static` `run_id`; set explicit `run_id` for distinct runs.
- Bucket defaults allow public object read (`bucket_public_read=true`) so docs/releases can link directly to S3 artifacts.
- Keep secrets out of Terraform vars and docs; use SSM or environment-based secret handling.
