#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

register_shutdown_trap
if [[ -z "${HOME:-}" ]]; then
  export HOME=/root
fi
export PATH="${HOME}/.foundry/bin:${PATH}"

if [[ -n "${MEDUSA_GIT_REPO:-}" ]]; then
  commit_file="${SCFUZZBENCH_ROOT}/medusa_git_commit"
  if [[ ! -f "${commit_file}" ]]; then
    log "Missing resolved Medusa source commit provenance: ${commit_file}"
    exit 1
  fi
  medusa_git_commit=$(<"${commit_file}")
  if [[ ! "${medusa_git_commit}" =~ ^[A-Fa-f0-9]{40}$ ]]; then
    log "Invalid resolved Medusa source commit provenance"
    exit 1
  fi
  SCFUZZBENCH_FUZZER_LABEL="medusa-git-${medusa_git_commit:0:12}"
else
  require_env MEDUSA_VERSION
  SCFUZZBENCH_FUZZER_LABEL="medusa-v${MEDUSA_VERSION}"
fi
export SCFUZZBENCH_FUZZER_LABEL

clone_target
capture_target_workspace_anchor
apply_benchmark_type
build_target

repo_dir="${SCFUZZBENCH_WORKDIR}/target"
log_file="${SCFUZZBENCH_LOG_DIR}/medusa.log"
corpus_dir=$(resolve_target_corpus_dir "${MEDUSA_CORPUS_DIR:-}" "corpus/medusa")
export SCFUZZBENCH_CORPUS_DIR="${corpus_dir}"
prepare_shared_seed_corpus

set_default_worker_env MEDUSA_WORKERS

run_medusa_with_effective_config() (
  set -euo pipefail

  # Medusa 1.4.1 has no CLI flag for corpus pruning. Its
  # `fuzzing.pruneFrequency` config value controls a separate corpus-pruner
  # goroutine, and 0 disables it. Use a working copy so benchmark fairness does
  # not depend on each target's checked-in default while leaving the target
  # config untouched. Keep the copy beside the source config because Medusa
  # resolves relative compilation paths from the config directory.
  local medusa_prune_frequency="${MEDUSA_PRUNE_FREQUENCY:-0}"
  if ! [[ "${medusa_prune_frequency}" =~ ^[0-9]+$ ]]; then
    log "Invalid MEDUSA_PRUNE_FREQUENCY='${medusa_prune_frequency}'; expected a non-negative integer number of minutes."
    exit 1
  fi

  # Shrinking is performed inline by the worker that found a failure. Keep its
  # exploration-budget cost equal to the other benchmark legs, independently
  # of the separate background corpus-pruning control above.
  local medusa_shrink_limit="${MEDUSA_SHRINK_LIMIT-1}"
  if ! [[ "${medusa_shrink_limit}" =~ ^[0-9]+$ ]]; then
    log "Invalid MEDUSA_SHRINK_LIMIT='${medusa_shrink_limit}'; expected a non-negative integer."
    exit 1
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    log "python3 is required to create the effective Medusa config."
    exit 1
  fi

  local medusa_source_config=""
  if [[ -n "${MEDUSA_CONFIG:-}" ]]; then
    medusa_source_config="${MEDUSA_CONFIG}"
    if [[ "${medusa_source_config}" != /* ]]; then
      medusa_source_config="${repo_dir}/${medusa_source_config}"
    fi
    if [[ ! -f "${medusa_source_config}" ]]; then
      log "Medusa config not found at ${medusa_source_config}."
      exit 1
    fi
  elif [[ -f "${repo_dir}/medusa.json" ]]; then
    # This matches Medusa's own no-flag behavior: it loads medusa.json from the
    # current working directory when that file exists.
    medusa_source_config="${repo_dir}/medusa.json"
  fi

  local medusa_config_dir="${repo_dir}"
  if [[ -n "${medusa_source_config}" ]]; then
    medusa_config_dir=$(dirname "${medusa_source_config}")
  fi

  local medusa_effective_config=""
  cleanup_medusa_effective_config() {
    if [[ -n "${medusa_effective_config:-}" ]]; then
      remove_strict_descendant_file \
        "${medusa_effective_config}" "${repo_dir}" \
        "effective Medusa config" \
        "${SCFUZZBENCH_TARGET_ROOT_ANCHOR}" \
        "${SCFUZZBENCH_TARGET_ROOT_IDENTITY}" || true
    fi
  }
  trap cleanup_medusa_effective_config EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  medusa_effective_config="${medusa_config_dir}/.scfuzzbench-medusa-${BASHPID}.json"
  {
    if [[ -n "${medusa_source_config}" ]]; then
      read_strict_descendant_file \
        "${medusa_source_config}" "${repo_dir}" \
        "${SCFUZZBENCH_TARGET_ROOT_ANCHOR}" \
        "${SCFUZZBENCH_TARGET_ROOT_IDENTITY}"
    else
      printf '{}\n'
    fi
  } |
    python3 -c "
import json
import sys

raw_frequency = sys.argv[1]
raw_shrink_limit = sys.argv[2]
config = json.load(sys.stdin)

if not isinstance(config, dict):
    raise SystemExit('Medusa config root must be a JSON object')

fuzzing = config.setdefault('fuzzing', {})
if not isinstance(fuzzing, dict):
    raise SystemExit('Medusa config fuzzing value must be a JSON object')

frequency = int(raw_frequency)
if frequency > 2**64 - 1:
    raise SystemExit('MEDUSA_PRUNE_FREQUENCY exceeds Medusa uint64 range')
shrink_limit = int(raw_shrink_limit)
if shrink_limit > 2**64 - 1:
    raise SystemExit('MEDUSA_SHRINK_LIMIT exceeds Medusa uint64 range')
fuzzing['pruneFrequency'] = frequency
fuzzing['shrinkLimit'] = shrink_limit

json.dump(config, sys.stdout, indent=2)
sys.stdout.write('\\n')
" "${medusa_prune_frequency}" "${medusa_shrink_limit}" |
    write_strict_descendant_file \
      "${medusa_effective_config}" "${repo_dir}" \
      "${SCFUZZBENCH_TARGET_ROOT_ANCHOR}" \
      "${SCFUZZBENCH_TARGET_ROOT_IDENTITY}"
  log "Medusa corpus pruning frequency: ${medusa_prune_frequency} minute(s) (0 disables the background pruner)."
  log "Medusa inline shrink limit: ${medusa_shrink_limit}."

  local -a cmd=(medusa fuzz --no-color)
  cmd+=(--config "${medusa_effective_config}")
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
    local -a extra_args=()
    read -r -a extra_args <<< "${MEDUSA_EXTRA_ARGS}"
    cmd+=("${extra_args[@]}")
  fi
  cmd+=(--corpus-dir "${SCFUZZBENCH_CORPUS_DIR}")

  local exit_code
  set +e
  pushd "${repo_dir}" >/dev/null
  run_with_timeout "${log_file}" "${cmd[@]}"
  exit_code=$?
  popd >/dev/null
  set -e
  exit "${exit_code}"
)

set +e
run_medusa_with_effective_config
exit_code=$?
set -e
upload_results
exit ${exit_code}
