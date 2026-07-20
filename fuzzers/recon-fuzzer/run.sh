#!/usr/bin/env bash
set -euo pipefail

source /opt/scfuzzbench/common.sh

register_shutdown_trap
export PATH="/root/.foundry/bin:${PATH}"

require_env RECON_VERSION
recon_version="${RECON_VERSION#v}"
SCFUZZBENCH_FUZZER_LABEL="recon-v${recon_version}"
export SCFUZZBENCH_FUZZER_LABEL

clone_target
capture_target_workspace_anchor
apply_benchmark_type

if [[ "${SCFUZZBENCH_BENCHMARK_TYPE}" == "property" && -n "${ECHIDNA_CONFIG:-}" ]]; then
  config_path="${ECHIDNA_CONFIG}"
  if [[ "${config_path}" != /* ]]; then
    config_path="${SCFUZZBENCH_WORKDIR}/target/${config_path}"
  fi
  if [[ -f "${config_path}" ]]; then
    log "Adjusting property prefix in ${config_path}"
    sed -i 's/prefix:[[:space:]]*\"invariant_\"/prefix: \"echidna_\"/g' "${config_path}"
  else
    log "Config not found at ${config_path}; skipping prefix rewrite."
  fi
fi

build_target

repo_dir="${SCFUZZBENCH_WORKDIR}/target"
log_file="${SCFUZZBENCH_LOG_DIR}/recon-fuzzer.log"
corpus_dir=$(resolve_target_corpus_dir \
  "${RECON_CORPUS_DIR:-${ECHIDNA_CORPUS_DIR:-}}" \
  "corpus/recon-fuzzer")
export SCFUZZBENCH_CORPUS_DIR="${corpus_dir}"
prepare_shared_seed_corpus

if [[ -z "${RECON_WORKERS:-}" && -n "${ECHIDNA_WORKERS:-}" ]]; then
  RECON_WORKERS="${ECHIDNA_WORKERS}"
fi
set_default_worker_env RECON_WORKERS

if [[ -z "${ECHIDNA_CONFIG:-}" && -z "${ECHIDNA_TARGET:-}" ]]; then
  log "Set ECHIDNA_CONFIG or ECHIDNA_TARGET (and ECHIDNA_CONTRACT if needed)."
  exit 1
fi

cmd=(recon fuzz . --format text)
if [[ -n "${ECHIDNA_CONFIG:-}" ]]; then
  cmd+=(--config "${ECHIDNA_CONFIG}")
fi
if [[ -n "${ECHIDNA_CONTRACT:-}" ]]; then
  cmd+=(--contract "${ECHIDNA_CONTRACT}")
fi

recon_test_mode="${RECON_TEST_MODE:-${ECHIDNA_TEST_MODE:-}}"
if [[ -z "${recon_test_mode}" && "${SCFUZZBENCH_BENCHMARK_TYPE}" == "optimization" ]]; then
  recon_test_mode="optimization"
fi
if [[ -n "${recon_test_mode}" ]]; then
  cmd+=(--test-mode "${recon_test_mode}")
fi

if [[ -n "${RECON_WORKERS:-}" ]]; then
  cmd+=(--workers "${RECON_WORKERS}")
fi
cmd+=(--corpus-dir "${SCFUZZBENCH_CORPUS_DIR}")

recon_extra_args="${RECON_EXTRA_ARGS:-${ECHIDNA_EXTRA_ARGS:-}}"
if [[ -n "${recon_extra_args}" ]]; then
  read -r -a extra_args <<< "${recon_extra_args}"
  cmd+=("${extra_args[@]}")
fi

set +e
pushd "${repo_dir}" >/dev/null
run_with_timeout "${log_file}" "${cmd[@]}"
exit_code=$?
popd >/dev/null
set -e

upload_results
exit ${exit_code}
