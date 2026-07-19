#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

register_shutdown_trap

prepare_workspace
if [[ -z "${HOME:-}" ]]; then
  export HOME=/root
fi
export PATH="${HOME}/.foundry/bin:${PATH}"

require_env ECHIDNA_VERSION
SCFUZZBENCH_FUZZER_LABEL="echidna-v${ECHIDNA_VERSION}"
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
log_file="${SCFUZZBENCH_LOG_DIR}/echidna.log"

default_corpus_dir="${repo_dir}/corpus/echidna"
corpus_dir="${ECHIDNA_CORPUS_DIR:-${default_corpus_dir}}"
if [[ "${corpus_dir}" != /* ]]; then
  corpus_dir="${repo_dir}/${corpus_dir}"
fi
export SCFUZZBENCH_CORPUS_DIR="${corpus_dir}"
prepare_shared_seed_corpus

set_default_worker_env ECHIDNA_WORKERS

if [[ -z "${ECHIDNA_CONFIG:-}" && -z "${ECHIDNA_TARGET:-}" ]]; then
  log "Set ECHIDNA_CONFIG or ECHIDNA_TARGET (and ECHIDNA_CONTRACT if needed)."
  exit 1
fi

# Some targets keep the harness under tests/ instead of test/ (upstream layout,
# e.g. Aave). Fall back automatically so no per-target override is needed.
if [[ -n "${ECHIDNA_TARGET:-}" && "${ECHIDNA_TARGET}" != /* && ! -f "${repo_dir}/${ECHIDNA_TARGET}" ]]; then
  alt_target="${ECHIDNA_TARGET/#test\//tests/}"
  if [[ "${alt_target}" == "${ECHIDNA_TARGET}" ]]; then
    alt_target="${ECHIDNA_TARGET/#tests\//test/}"
  fi
  if [[ "${alt_target}" != "${ECHIDNA_TARGET}" && -f "${repo_dir}/${alt_target}" ]]; then
    log "ECHIDNA_TARGET ${ECHIDNA_TARGET} not found; using ${alt_target}"
    ECHIDNA_TARGET="${alt_target}"
  fi
fi

cmd=(echidna)
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

shrink_limit_overridden=0
if [[ -n "${ECHIDNA_EXTRA_ARGS:-}" ]]; then
  extra_args=()
  mapfile -d '' -t extra_args < <(
    python3 - "${ECHIDNA_EXTRA_ARGS}" <<'PY'
import shlex
import sys

try:
    args = shlex.split(sys.argv[1], comments=False, posix=True)
except ValueError as exc:
    print(f"Invalid ECHIDNA_EXTRA_ARGS: {exc}", file=sys.stderr)
    raise SystemExit(2)

for arg in args:
    sys.stdout.buffer.write(arg.encode() + b"\0")
PY
  )
  parser_pid=$!
  if ! wait "${parser_pid}"; then
    log "ECHIDNA_EXTRA_ARGS must use valid shell-style quoting."
    exit 1
  fi

  for ((arg_index = 0; arg_index < ${#extra_args[@]}; arg_index++)); do
    arg="${extra_args[arg_index]}"
    case "${arg}" in
      --shrink-limit)
        if ((shrink_limit_overridden == 1)); then
          log "ECHIDNA_EXTRA_ARGS must contain at most one --shrink-limit option."
          exit 1
        fi
        shrink_limit_overridden=1
        shrink_limit_value="${extra_args[arg_index + 1]:-}"
        ;;
      --shrink-limit=*)
        if ((shrink_limit_overridden == 1)); then
          log "ECHIDNA_EXTRA_ARGS must contain at most one --shrink-limit option."
          exit 1
        fi
        shrink_limit_overridden=1
        shrink_limit_value="${arg#*=}"
        ;;
    esac
    if [[ "${arg}" == "--shrink-limit" || "${arg}" == --shrink-limit=* ]]; then
      if [[ ! "${shrink_limit_value}" =~ ^[0-9]+$ ]]; then
        log "ECHIDNA_EXTRA_ARGS --shrink-limit must be a non-negative integer."
        exit 1
      fi
    fi
  done
  cmd+=("${extra_args[@]}")
fi

# A CLI value overrides shrinkLimit from the target config. Keep shrinking from
# consuming the benchmark exploration budget unless the operator explicitly
# supplied a different limit.
if ((shrink_limit_overridden == 0)); then
  cmd+=(--shrink-limit 1)
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
