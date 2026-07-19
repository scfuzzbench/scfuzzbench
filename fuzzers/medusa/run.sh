#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

register_shutdown_trap

prepare_workspace
if [[ -z "${HOME:-}" ]]; then
  export HOME=/root
fi
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

# Medusa 1.4.1 has no CLI flag for corpus pruning. Its
# `fuzzing.pruneFrequency` config value controls a separate corpus-pruner
# goroutine, and 0 disables it. Use a working copy so benchmark fairness does
# not depend on each target's checked-in default while leaving the target
# config untouched. Keep the copy beside the source config because Medusa
# resolves relative compilation paths from the config directory.
medusa_prune_frequency="${MEDUSA_PRUNE_FREQUENCY:-0}"
if ! [[ "${medusa_prune_frequency}" =~ ^[0-9]+$ ]]; then
  log "Invalid MEDUSA_PRUNE_FREQUENCY='${medusa_prune_frequency}'; expected a non-negative integer number of minutes."
  exit 1
fi

medusa_source_config=""
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
  medusa_source_config="${repo_dir}/medusa.json"
fi

if [[ -n "${medusa_source_config}" ]]; then
  medusa_config_dir=$(dirname "${medusa_source_config}")
else
  medusa_config_dir="${repo_dir}"
fi
medusa_effective_config=$(mktemp "${medusa_config_dir}/.scfuzzbench-medusa.XXXXXX.json")

python3 - "${medusa_source_config}" "${medusa_effective_config}" "${medusa_prune_frequency}" <<'PY'
import json
import os
import sys

source_path, output_path, raw_frequency = sys.argv[1:]
if source_path:
    with open(source_path, encoding="utf-8") as source:
        config = json.load(source)
else:
    config = {}

if not isinstance(config, dict):
    raise SystemExit("Medusa config root must be a JSON object")

fuzzing = config.setdefault("fuzzing", {})
if not isinstance(fuzzing, dict):
    raise SystemExit("Medusa config 'fuzzing' value must be a JSON object")

frequency = int(raw_frequency)
if frequency > 2**64 - 1:
    raise SystemExit("MEDUSA_PRUNE_FREQUENCY exceeds Medusa's uint64 range")
fuzzing["pruneFrequency"] = frequency

temporary_path = f"{output_path}.tmp"
with open(temporary_path, "w", encoding="utf-8") as output:
    json.dump(config, output, indent=2)
    output.write("\n")
os.replace(temporary_path, output_path)
PY
log "Medusa corpus pruning frequency: ${medusa_prune_frequency} minute(s) (0 disables the background pruner)."

cmd=(medusa fuzz --no-color)
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

rm -f "${medusa_effective_config}"
upload_results
exit ${exit_code}
