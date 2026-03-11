#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

register_shutdown_trap

prepare_workspace
export PATH="${HOME}/.foundry/bin:${PATH}"

if [[ -n "${FOUNDRY_LABEL:-}" ]]; then
  SCFUZZBENCH_FUZZER_LABEL="${FOUNDRY_LABEL}"
elif [[ -f "${SCFUZZBENCH_ROOT:-/opt/scfuzzbench}/foundry_commit" ]]; then
  foundry_commit=$(cat "${SCFUZZBENCH_ROOT:-/opt/scfuzzbench}/foundry_commit")
  SCFUZZBENCH_FUZZER_LABEL="foundry-git-${foundry_commit}"
else
  require_env FOUNDRY_VERSION
  SCFUZZBENCH_FUZZER_LABEL="foundry-${FOUNDRY_VERSION}"
fi
export SCFUZZBENCH_FUZZER_LABEL

clone_target
apply_benchmark_type
build_target

repo_dir="${SCFUZZBENCH_WORKDIR}/target"
log_file="${SCFUZZBENCH_LOG_DIR}/foundry.log"
watcher_stop_file="${SCFUZZBENCH_LOG_DIR}/.foundry_failure_watcher.stop"

start_failure_watcher() {
  local root_dir=$1
  local out_log=$2
  local stop_file=$3
  local failure_dir_rel=$4

  (
    declare -A seen_failure_files

    seed_seen_failures() {
      local failure_root="${root_dir}/${failure_dir_rel}"
      if [[ ! -d "${failure_root}" ]]; then
        return
      fi
      while IFS= read -r -d '' failure_file; do
        seen_failure_files["${failure_file}"]=1
      done < <(find "${failure_root}" -type f -print0 2>/dev/null)
    }

    emit_new_failures() {
      local failure_root="${root_dir}/${failure_dir_rel}"
      if [[ ! -d "${failure_root}" ]]; then
        return
      fi

      while IFS= read -r -d '' failure_file; do
        if [[ -n "${seen_failure_files[${failure_file}]:-}" ]]; then
          continue
        fi
        seen_failure_files["${failure_file}"]=1
        invariant_name=$(basename "${failure_file}")
        if [[ -z "${invariant_name}" ]]; then
          continue
        fi
        ts=$(date +%s)
        printf '{"timestamp":%s,"invariant":"%s","failed":1}\n' "${ts}" "${invariant_name}" >> "${out_log}"
      done < <(find "${failure_root}" -type f -print0 2>/dev/null)
    }

    # Ignore failures that existed before the campaign starts.
    seed_seen_failures

    while [[ ! -f "${stop_file}" ]]; do
      emit_new_failures
      sleep 5
    done

    # Final sweep after stop signal in case a failure landed just before exit.
    emit_new_failures
  ) &
}

extra_args=()
if [[ -n "${FOUNDRY_TEST_ARGS:-}" ]]; then
  read -r -a extra_args <<< "${FOUNDRY_TEST_ARGS}"
fi

set_default_worker_env FOUNDRY_THREADS
if [[ -n "${FOUNDRY_THREADS:-}" ]]; then
  has_threads_arg=0
  for arg in "${extra_args[@]}"; do
    case "${arg}" in
      --threads|--jobs|-j|--threads=*|--jobs=*|-j*)
        has_threads_arg=1
        break
        ;;
    esac
  done
  if [[ "${has_threads_arg}" -eq 0 ]]; then
    extra_args=(--threads "${FOUNDRY_THREADS}" "${extra_args[@]}")
  fi
fi

set +e
pushd "${repo_dir}" >/dev/null
rm -f "${watcher_stop_file}"
failure_persist_dir="${FOUNDRY_FAILURE_PERSIST_DIR:-cache/invariant}"
start_failure_watcher "${repo_dir}" "${log_file}" "${watcher_stop_file}" "${failure_persist_dir}"
failure_watcher_pid=$!
run_with_timeout "${log_file}" forge test --mc CryticToFoundry "${extra_args[@]}"
exit_code=$?
touch "${watcher_stop_file}"
if [[ -n "${failure_watcher_pid:-}" ]]; then
  wait "${failure_watcher_pid}" || true
fi
rm -f "${watcher_stop_file}"
popd >/dev/null
set -e

upload_results
exit ${exit_code}
