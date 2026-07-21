#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

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
shrink_limit_overridden=0
if [[ -n "${recon_extra_args}" ]]; then
  extra_args=()
  mapfile -d '' -t extra_args < <(
    python3 - "${recon_extra_args}" <<'PY'
import shlex
import sys

try:
    args = shlex.split(sys.argv[1], comments=False, posix=True)
except ValueError as exc:
    print(f"Invalid RECON_EXTRA_ARGS: {exc}", file=sys.stderr)
    raise SystemExit(2)

for arg in args:
    sys.stdout.buffer.write(arg.encode() + b"\0")
PY
  )
  parser_pid=$!
  if ! wait "${parser_pid}"; then
    log "RECON_EXTRA_ARGS must use valid shell-style quoting."
    exit 1
  fi

  for ((arg_index = 0; arg_index < ${#extra_args[@]}; arg_index++)); do
    arg="${extra_args[arg_index]}"
    case "${arg}" in
      --shrink-limit)
        if ((shrink_limit_overridden == 1)); then
          log "RECON_EXTRA_ARGS must contain at most one --shrink-limit option."
          exit 1
        fi
        shrink_limit_overridden=1
        shrink_limit_value="${extra_args[arg_index + 1]:-}"
        ;;
      --shrink-limit=*)
        if ((shrink_limit_overridden == 1)); then
          log "RECON_EXTRA_ARGS must contain at most one --shrink-limit option."
          exit 1
        fi
        shrink_limit_overridden=1
        shrink_limit_value="${arg#*=}"
        ;;
    esac
    if [[ "${arg}" == "--shrink-limit" || "${arg}" == --shrink-limit=* ]]; then
      if [[ ! "${shrink_limit_value}" =~ ^[0-9]+$ ]]; then
        log "RECON_EXTRA_ARGS --shrink-limit must be a non-negative integer."
        exit 1
      fi
    fi
  done
  cmd+=("${extra_args[@]}")
fi

# Recon's CLI value overrides shrinkLimit from the shared Echidna-format target
# config. Match the other benchmark legs' worker-local shrinking budget unless
# the operator explicitly requested a different non-comparative experiment.
if ((shrink_limit_overridden == 0)); then
  cmd+=(--shrink-limit 1)
fi

set +e
pushd "${repo_dir}" >/dev/null
run_with_timeout "${log_file}" "${cmd[@]}"
exit_code=$?
popd >/dev/null
set -e

upload_results
exit ${exit_code}
