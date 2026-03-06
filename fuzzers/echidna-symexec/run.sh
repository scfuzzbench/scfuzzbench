#!/usr/bin/env bash
set -euo pipefail

source /opt/scfuzzbench/common.sh

register_shutdown_trap

prepare_workspace
export PATH="/root/.foundry/bin:${PATH}"

require_env ECHIDNA_VERSION BITWUZLA_VERSION

bitwuzla_version="${BITWUZLA_VERSION#v}"
SCFUZZBENCH_FUZZER_LABEL="echidna-v${ECHIDNA_VERSION}-symexec-bitwuzla-v${bitwuzla_version}"
export SCFUZZBENCH_FUZZER_LABEL

clone_target
apply_benchmark_type

if [[ "${SCFUZZBENCH_BENCHMARK_TYPE}" == "property" && -n "${ECHIDNA_CONFIG:-}" ]]; then
  config_path="${ECHIDNA_CONFIG}"
  if [[ "${config_path}" != /* ]]; then
    config_path="${SCFUZZBENCH_WORKDIR}/target/${config_path}"
  fi
  if [[ -f "${config_path}" ]]; then
    log "Adjusting Echidna property prefix in ${config_path}"
    sed -i 's/prefix:[[:space:]]*\"invariant_\"/prefix: \"echidna_\"/g' "${config_path}"
  else
    log "Echidna config not found at ${config_path}; skipping prefix rewrite."
  fi
fi

build_target

repo_dir="${SCFUZZBENCH_WORKDIR}/target"
log_file="${SCFUZZBENCH_LOG_DIR}/echidna-symexec.log"
default_corpus_dir="${repo_dir}/corpus/echidna-symexec"
corpus_dir="${ECHIDNA_SYMEXEC_CORPUS_DIR:-${ECHIDNA_CORPUS_DIR:-${default_corpus_dir}}}"
if [[ "${corpus_dir}" != /* ]]; then
  corpus_dir="${repo_dir}/${corpus_dir}"
fi
export SCFUZZBENCH_CORPUS_DIR="${corpus_dir}"
mkdir -p "${SCFUZZBENCH_CORPUS_DIR}"

set_default_worker_env ECHIDNA_WORKERS

if [[ -z "${ECHIDNA_CONFIG:-}" && -z "${ECHIDNA_TARGET:-}" ]]; then
  log "Set ECHIDNA_CONFIG or ECHIDNA_TARGET (and ECHIDNA_CONTRACT if needed)."
  exit 1
fi

cmd=(echidna-test --sym-exec true)
if [[ -n "${ECHIDNA_CONFIG:-}" ]]; then
  cmd+=(--config "${ECHIDNA_CONFIG}")
fi
if [[ -n "${ECHIDNA_CONTRACT:-}" ]]; then
  cmd+=(--contract "${ECHIDNA_CONTRACT}")
fi
if [[ -z "${ECHIDNA_TEST_MODE:-}" && "${SCFUZZBENCH_BENCHMARK_TYPE}" == "optimization" ]]; then
  ECHIDNA_TEST_MODE="optimization"
fi
if [[ -n "${ECHIDNA_TEST_MODE:-}" ]]; then
  cmd+=(--test-mode "${ECHIDNA_TEST_MODE}")
fi
if [[ -n "${ECHIDNA_WORKERS:-}" ]]; then
  cmd+=(--workers "${ECHIDNA_WORKERS}")
fi
cmd+=(--corpus-dir "${SCFUZZBENCH_CORPUS_DIR}")
if [[ -n "${ECHIDNA_EXTRA_ARGS:-}" ]]; then
  read -r -a extra_args <<< "${ECHIDNA_EXTRA_ARGS}"
  cmd+=("${extra_args[@]}")
fi
if [[ -n "${ECHIDNA_TARGET:-}" ]]; then
  cmd+=("${ECHIDNA_TARGET}")
fi

echidna_rts_args="${ECHIDNA_RTS_ARGS:--A1g}"
if [[ -n "${echidna_rts_args}" ]]; then
  read -r -a rts_args <<< "${echidna_rts_args}"
  cmd+=(+RTS "${rts_args[@]}" -RTS)
fi

set +e
pushd "${repo_dir}" >/dev/null
run_with_timeout "${log_file}" "${cmd[@]}"
exit_code=$?
popd >/dev/null
set -e

upload_results
exit ${exit_code}
