TF_DIR := infrastructure
TF_ARGS ?=
BACKEND_CONFIG ?= backend.hcl
BACKEND_INIT_FLAGS ?= -migrate-state -force-copy -input=false
LOGS_DIR ?= logs
OUT_DIR ?= analysis_out
ANALYSIS_VENV ?= .venv-analysis
ANALYSIS_PY ?= $(ANALYSIS_VENV)/bin/python
ANALYSIS_PIP ?= $(ANALYSIS_VENV)/bin/pip
ANALYSIS_REQ ?= analysis/requirements.txt
RUN_ID ?=
AWS_PROFILE ?=
BUCKET ?=
BENCHMARK_UUID ?=
EXISTING_BUCKET ?=
DEST ?= /tmp/scfuzzbench-results-$(RUN_ID)
ARTIFACT_CATEGORY ?= logs
UNZIPPED_DIR ?= $(DEST)/logs/unzipped
ANALYSIS_LOGS_DIR ?= $(DEST)/analysis
ANALYSIS_OUT_DIR ?= $(DEST)/data
IMAGES_OUT_DIR ?= $(DEST)/images
EXCLUDE_FUZZERS ?=
DURATION_HOURS ?=
SHOW_MEAN ?=
EVENTS_CSV ?= $(ANALYSIS_OUT_DIR)/events.csv
CUMULATIVE_CSV ?= $(ANALYSIS_OUT_DIR)/cumulative.csv
REPORT_CSV ?= $(CUMULATIVE_CSV)
REPORT_OUT_DIR ?= $(ANALYSIS_OUT_DIR)
REPORT_BUDGET ?= $(DURATION_HOURS)
REPORT_GRID_STEP_MIN ?= 6
REPORT_CHECKPOINTS ?= 1,4,8,24
REPORT_KS ?= 1,3,5
REPORT_ANONYMIZE ?=
BROKEN_INVARIANTS_CSV ?= $(ANALYSIS_OUT_DIR)/broken_invariants.csv
BROKEN_INVARIANTS_MD ?= $(ANALYSIS_OUT_DIR)/broken_invariants.md
INVARIANT_OVERLAP_PNG ?= $(IMAGES_OUT_DIR)/invariant_overlap_upset.png
INVARIANT_TOP_K ?= 20
THROUGHPUT_SAMPLES_CSV ?= $(ANALYSIS_OUT_DIR)/throughput_samples.csv
THROUGHPUT_SUMMARY_CSV ?= $(ANALYSIS_OUT_DIR)/throughput_summary.csv
PROGRESS_METRICS_SAMPLES_CSV ?= $(ANALYSIS_OUT_DIR)/progress_metrics_samples.csv
PROGRESS_METRICS_SUMMARY_CSV ?= $(ANALYSIS_OUT_DIR)/progress_metrics_summary.csv
RUNNER_RESOURCE_SUMMARY_CSV ?= $(ANALYSIS_OUT_DIR)/runner_resource_summary.csv
RUNNER_RESOURCE_TIMESERIES_CSV ?= $(ANALYSIS_OUT_DIR)/runner_resource_timeseries.csv
RUNNER_RESOURCE_MD ?= $(ANALYSIS_OUT_DIR)/runner_resource_usage.md
CPU_USAGE_PNG ?= $(IMAGES_OUT_DIR)/cpu_usage_over_time.png
MEMORY_USAGE_PNG ?= $(IMAGES_OUT_DIR)/memory_usage_over_time.png
RUNNER_METRICS_BIN_SECONDS ?= 60
WIDE_CSV ?=
LONG_CSV ?= results_long.csv
RUN_ID_ARG :=
ifneq ($(strip $(RUN_ID)),)
RUN_ID_ARG := --run-id $(RUN_ID)
endif
REPORT_BUDGET_ARG :=
ifneq ($(strip $(REPORT_BUDGET)),)
REPORT_BUDGET_ARG := --budget $(REPORT_BUDGET)
endif
INVARIANT_BUDGET_ARG :=
ifneq ($(strip $(REPORT_BUDGET)),)
INVARIANT_BUDGET_ARG := --budget-hours $(REPORT_BUDGET)
endif
RUNNER_BUDGET_ARG :=
ifneq ($(strip $(REPORT_BUDGET)),)
RUNNER_BUDGET_ARG := --budget-hours $(REPORT_BUDGET)
endif
BENCHMARK_UUID_ARG :=
ifneq ($(strip $(BENCHMARK_UUID)),)
BENCHMARK_UUID_ARG := --benchmark-uuid $(BENCHMARK_UUID)
endif
SCFUZZBENCH_COMMIT ?= $(shell git rev-parse HEAD 2>/dev/null)
SCFUZZBENCH_COMMIT_ARG :=
ifneq ($(strip $(SCFUZZBENCH_COMMIT)),)
SCFUZZBENCH_COMMIT_ARG := -var 'scfuzzbench_commit=$(SCFUZZBENCH_COMMIT)'
endif
EXISTING_BUCKET_ARG :=
ifneq ($(strip $(EXISTING_BUCKET)),)
EXISTING_BUCKET_ARG := -var 'existing_bucket_name=$(EXISTING_BUCKET)'
endif
PROFILE_ARG :=
ifneq ($(strip $(AWS_PROFILE)),)
PROFILE_ARG := --profile $(AWS_PROFILE)
endif
NO_UNZIP ?=
NO_UNZIP_ARG :=
ifneq ($(strip $(NO_UNZIP)),)
NO_UNZIP_ARG := --no-unzip
endif
EXCLUDE_ARG :=
ifneq ($(strip $(EXCLUDE_FUZZERS)),)
EXCLUDE_ARG := --exclude-fuzzers $(EXCLUDE_FUZZERS)
endif
RAW_LABELS ?=
RAW_LABELS_ARG :=
ifneq ($(strip $(RAW_LABELS)),)
RAW_LABELS_ARG := --raw-labels
endif
DURATION_ARG :=

.PHONY: terraform-init terraform-init-backend terraform-fmt terraform-validate terraform-plan terraform-deploy terraform-destroy terraform-destroy-infra analysis-venv results-analyze results-download results-prepare results-analyze-filtered results-analyze-all results-inspect s3-purge-versions report-benchmark report-wide-to-long report-events-to-cumulative report-invariant-overlap report-runner-metrics

terraform-init:
	terraform -chdir=$(TF_DIR) init

terraform-init-backend:
	terraform -chdir=$(TF_DIR) init -backend-config=$(BACKEND_CONFIG) $(BACKEND_INIT_FLAGS)

terraform-fmt:
	terraform -chdir=$(TF_DIR) fmt -recursive

terraform-validate:
	terraform -chdir=$(TF_DIR) validate

terraform-plan:
	terraform -chdir=$(TF_DIR) plan $(TF_ARGS) $(SCFUZZBENCH_COMMIT_ARG) $(EXISTING_BUCKET_ARG)

terraform-deploy:
	terraform -chdir=$(TF_DIR) apply $(TF_ARGS) $(SCFUZZBENCH_COMMIT_ARG) $(EXISTING_BUCKET_ARG)

terraform-destroy:
	terraform -chdir=$(TF_DIR) destroy $(TF_ARGS) $(SCFUZZBENCH_COMMIT_ARG) $(EXISTING_BUCKET_ARG)

terraform-destroy-infra:
	terraform -chdir=$(TF_DIR) destroy $(TF_ARGS) -target=aws_instance.fuzzer -target=aws_iam_instance_profile.fuzzer -target=aws_iam_role_policy.s3_access -target=aws_iam_role.fuzzer -target=aws_key_pair.ssh -target=local_sensitive_file.ssh_private_key -target=tls_private_key.ssh -target=aws_security_group.ssh -target=aws_route_table_association.public -target=aws_route_table.public -target=aws_subnet.public -target=aws_internet_gateway.main -target=aws_vpc.main $(SCFUZZBENCH_COMMIT_ARG) $(EXISTING_BUCKET_ARG)

analysis-venv:
	python3 -m venv $(ANALYSIS_VENV)
	$(ANALYSIS_PIP) install -r $(ANALYSIS_REQ)

results-analyze: analysis-venv
	$(ANALYSIS_PY) analysis/analyze.py run --logs-dir $(LOGS_DIR) --out-dir $(OUT_DIR) $(RUN_ID_ARG) $(RAW_LABELS_ARG)

results-download:
	python3 scripts/download_run_artifacts.py --bucket $(BUCKET) --run-id $(RUN_ID) $(BENCHMARK_UUID_ARG) --dest $(DEST) --category $(ARTIFACT_CATEGORY) $(PROFILE_ARG) $(NO_UNZIP_ARG)

results-prepare:
	python3 scripts/prepare_analysis_logs.py --unzipped-dir $(UNZIPPED_DIR) --out-dir $(ANALYSIS_LOGS_DIR)

results-analyze-filtered: analysis-venv
	$(ANALYSIS_PY) scripts/run_analysis_filtered.py --logs-dir $(ANALYSIS_LOGS_DIR) --out-dir $(ANALYSIS_OUT_DIR) $(RUN_ID_ARG) $(EXCLUDE_ARG) $(RAW_LABELS_ARG)

results-analyze-all: analysis-venv results-download results-prepare results-analyze-filtered report-events-to-cumulative report-benchmark report-invariant-overlap report-runner-metrics

results-inspect:
	python3 scripts/inspect_logs.py --logs-dir $(ANALYSIS_LOGS_DIR)

s3-purge-versions:
	python3 scripts/purge_s3_versions.py --bucket $(BUCKET) $(PROFILE_ARG)

report-benchmark: analysis-venv
	$(ANALYSIS_PY) analysis/benchmark_report.py --csv $(REPORT_CSV) --report-outdir $(REPORT_OUT_DIR) --images-outdir $(IMAGES_OUT_DIR) $(REPORT_BUDGET_ARG) --grid_step_min $(REPORT_GRID_STEP_MIN) --checkpoints $(REPORT_CHECKPOINTS) --ks $(REPORT_KS) --throughput-summary-csv $(THROUGHPUT_SUMMARY_CSV) --throughput-samples-csv $(THROUGHPUT_SAMPLES_CSV) --progress-metrics-summary-csv $(PROGRESS_METRICS_SUMMARY_CSV) --progress-metrics-samples-csv $(PROGRESS_METRICS_SAMPLES_CSV) $(if $(REPORT_ANONYMIZE),--anonymize,)

report-wide-to-long: analysis-venv
	$(ANALYSIS_PY) analysis/wide_to_long.py --wide_csv $(WIDE_CSV) --out_csv $(LONG_CSV)

report-events-to-cumulative: analysis-venv
	$(ANALYSIS_PY) analysis/events_to_cumulative.py --events-csv $(EVENTS_CSV) --out-csv $(CUMULATIVE_CSV) --logs-dir $(ANALYSIS_LOGS_DIR) $(RUN_ID_ARG) $(EXCLUDE_ARG) $(RAW_LABELS_ARG)

report-invariant-overlap: analysis-venv
	$(ANALYSIS_PY) analysis/invariant_overlap_report.py --events-csv $(EVENTS_CSV) --out-md $(BROKEN_INVARIANTS_MD) --out-csv $(BROKEN_INVARIANTS_CSV) --out-png $(INVARIANT_OVERLAP_PNG) $(INVARIANT_BUDGET_ARG) --top-k $(INVARIANT_TOP_K)

report-runner-metrics: analysis-venv
	$(ANALYSIS_PY) analysis/runner_metrics_report.py --logs-dir $(ANALYSIS_LOGS_DIR) --out-summary-csv $(RUNNER_RESOURCE_SUMMARY_CSV) --out-timeseries-csv $(RUNNER_RESOURCE_TIMESERIES_CSV) --out-md $(RUNNER_RESOURCE_MD) --out-cpu-png $(CPU_USAGE_PNG) --out-memory-png $(MEMORY_USAGE_PNG) --bin-seconds $(RUNNER_METRICS_BIN_SECONDS) $(RUN_ID_ARG) $(RUNNER_BUDGET_ARG) $(RAW_LABELS_ARG)
