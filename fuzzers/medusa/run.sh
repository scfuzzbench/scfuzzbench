#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

register_shutdown_trap

prepare_workspace
export PATH="${HOME}/.foundry/bin:${PATH}"

require_env MEDUSA_VERSION
SCFUZZBENCH_FUZZER_LABEL="medusa-v${MEDUSA_VERSION}"
export SCFUZZBENCH_FUZZER_LABEL

clone_target
apply_benchmark_type
build_target

repo_dir="${SCFUZZBENCH_WORKDIR}/target"
log_file="${SCFUZZBENCH_LOG_DIR}/medusa.log"
default_corpus_dir="${repo_dir}/corpus/medusa"
corpus_dir="${MEDUSA_CORPUS_DIR:-${default_corpus_dir}}"
if [[ "${corpus_dir}" != /* ]]; then
  corpus_dir="${repo_dir}/${corpus_dir}"
fi
export SCFUZZBENCH_CORPUS_DIR="${corpus_dir}"
mkdir -p "${SCFUZZBENCH_CORPUS_DIR}"
log "Cleaning corpus directory ${SCFUZZBENCH_CORPUS_DIR}"
rm -rf "${SCFUZZBENCH_CORPUS_DIR:?}"/*

set_default_worker_env MEDUSA_WORKERS

cmd=(medusa fuzz --no-color)
if [[ -n "${MEDUSA_CONFIG:-}" ]]; then
  cmd+=(--config "${MEDUSA_CONFIG}")
fi
if [[ -n "${MEDUSA_COMPILATION_TARGET:-}" ]]; then
  cmd+=(--compilation-target "${MEDUSA_COMPILATION_TARGET}")
fi
if [[ -n "${MEDUSA_TARGET_CONTRACTS:-}" ]]; then
  cmd+=(--target-contracts "${MEDUSA_TARGET_CONTRACTS}")
fi
if [[ -n "${MEDUSA_WORKERS:-}" ]]; then
  cmd+=(--workers "${MEDUSA_WORKERS}")
fi
if [[ -n "${MEDUSA_EXTRA_ARGS:-}" ]]; then
  read -r -a extra_args <<< "${MEDUSA_EXTRA_ARGS}"
  cmd+=("${extra_args[@]}")
fi
cmd+=(--corpus-dir "${SCFUZZBENCH_CORPUS_DIR}")

set +e
pushd "${repo_dir}" >/dev/null
run_with_timeout "${log_file}" "${cmd[@]}"
exit_code=$?
popd >/dev/null
set -e

upload_results
exit ${exit_code}
