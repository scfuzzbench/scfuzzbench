#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

register_shutdown_trap

prepare_workspace
if [[ -z "${HOME:-}" ]]; then
  export HOME=/root
fi
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

# Start cold even if the target repo accidentally committed fuzzing artifacts:
# a persisted corpus warm-starts coverage and persisted failures replay at t~0,
# which would skew time-to-bug comparisons against the other fuzzers.
rm -rf "${repo_dir}/corpus/foundry" "${repo_dir}/cache/invariant" "${repo_dir}/cache/test-failures"

# Expose the corpus dir so upload_results ships the foundry corpus like the other
# legs (and so post-run showmap replays have something to replay downstream).
default_corpus_dir="${repo_dir}/corpus/foundry"
corpus_dir="${FOUNDRY_CORPUS_DIR:-${default_corpus_dir}}"
if [[ "${corpus_dir}" != /* ]]; then
  corpus_dir="${repo_dir}/${corpus_dir}"
fi
export SCFUZZBENCH_CORPUS_DIR="${corpus_dir}"
mkdir -p "${SCFUZZBENCH_CORPUS_DIR}"

extra_args=()
if [[ -n "${FOUNDRY_TEST_ARGS:-}" ]]; then
  read -r -a extra_args <<< "${FOUNDRY_TEST_ARGS}"
fi

# At the pin every newly found failure is shrunk inline (single-threaded, default
# 5000 sequence replays) before the campaign continues, and SIGINT is not observed
# mid-shrink: on the superform leg of run 1783612049 three of four instances got
# stuck shrinking, ignored SIGINT through the grace period, and were SIGKILLed
# without printing the end-of-run summary (losing every finding). The benchmark
# measures time-to-discovery, not reproducer minimality — skip shrinking unless
# explicitly overridden.
export FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT=${FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT:-0}

# --show-progress installs forge's SIGINT handler (bars stay hidden on non-TTY), so the
# benchmark timeout's SIGINT triggers a graceful exit that prints the end-of-run summary —
# the only place handler assertion bugs appear with their names.
has_progress_arg=0
for arg in "${extra_args[@]}"; do
  if [[ "${arg}" == "--show-progress" ]]; then
    has_progress_arg=1
    break
  fi
done
if [[ "${has_progress_arg}" -eq 0 ]]; then
  extra_args+=(--show-progress)
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
run_with_timeout "${log_file}" forge test --mc CryticToFoundry "${extra_args[@]}"
exit_code=$?
popd >/dev/null
set -e

# Optional post-campaign showmap replay (differential coverage; see issue #183 and
# PR #182): replays the persisted corpus and emits AFL-`afl-showmap`-style coverage
# files the analysis turns into differential-coverage campaigns. Off by default —
# it adds a bounded replay phase after the campaign and its artifacts only matter
# for coverage-comparison (e.g. foundry A/B) runs.
showmap_enabled="${SCFUZZBENCH_FOUNDRY_SHOWMAP:-0}"
showmap_enabled_lc=$(printf '%s' "${showmap_enabled}" | tr '[:upper:]' '[:lower:]')
if [[ "${showmap_enabled}" == "1" || "${showmap_enabled_lc}" == "true" || "${showmap_enabled_lc}" == "yes" ]]; then
  showmap_dir="${SCFUZZBENCH_LOG_DIR}/showmap"
  showmap_log_file="${SCFUZZBENCH_LOG_DIR}/foundry_showmap.log"
  showmap_trial="${SCFUZZBENCH_RUN_ID:-${SCFUZZBENCH_INSTANCE_ID:-$(hostname)}}"
  # Do NOT default --showmap-corpus-dir to SCFUZZBENCH_CORPUS_DIR. For invariant
  # tests forge persists the corpus under a per-contract subdir of the configured
  # `[invariant] corpus_dir` (e.g. `corpus/foundry/<Contract>`), and when
  # --showmap-corpus-dir is omitted the showmap replay resolves that same
  # per-test path from config. Passing the un-nested base dir here makes the
  # replay read an empty directory ("replay: 0 entries, 0 files"), which yields
  # empty showmap coverage and an empty differential-coverage report. Only honor
  # an explicit FOUNDRY_SHOWMAP_CORPUS_DIR override.
  showmap_corpus_dir="${FOUNDRY_SHOWMAP_CORPUS_DIR:-}"
  showmap_args=(
    --showmap-out "${showmap_dir}"
    --showmap-approach "${SCFUZZBENCH_FUZZER_LABEL}"
    --showmap-trial "${showmap_trial}"
  )
  if [[ -n "${showmap_corpus_dir}" ]]; then
    showmap_args+=(--showmap-corpus-dir "${showmap_corpus_dir}")
  fi
  if [[ -n "${FOUNDRY_SHOWMAP_DOMAIN:-}" ]]; then
    showmap_args=(--showmap-domain "${FOUNDRY_SHOWMAP_DOMAIN}" "${showmap_args[@]}")
  fi
  mkdir -p "${showmap_dir}"
  original_timeout="${SCFUZZBENCH_TIMEOUT_SECONDS:-}"
  showmap_timeout="${SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS:-}"
  if [[ -z "${showmap_timeout}" ]]; then
    showmap_timeout=1800
    if [[ "${original_timeout}" =~ ^[0-9]+$ ]] && [[ "${original_timeout}" -gt 0 ]] && [[ "${original_timeout}" -lt "${showmap_timeout}" ]]; then
      showmap_timeout="${original_timeout}"
    fi
  fi
  SCFUZZBENCH_TIMEOUT_SECONDS="${showmap_timeout}"
  # Reuse the campaign's extra args for the replay, minus any showmap flags the
  # operator may have injected via FOUNDRY_TEST_ARGS (we set our own).
  replay_extra_args=()
  skip_showmap_arg_value=0
  for arg in "${extra_args[@]}"; do
    if [[ "${skip_showmap_arg_value}" -eq 1 ]]; then
      skip_showmap_arg_value=0
      continue
    fi
    case "${arg}" in
      --showmap-out|--showmap-approach|--showmap-trial|--showmap-corpus-dir|--showmap-domain)
        skip_showmap_arg_value=1
        ;;
      --showmap-out=*|--showmap-approach=*|--showmap-trial=*|--showmap-corpus-dir=*|--showmap-domain=*)
        ;;
      *)
        replay_extra_args+=("${arg}")
        ;;
    esac
  done
  showmap_cmd=(forge test --mc CryticToFoundry)
  if ((${#replay_extra_args[@]})); then
    showmap_cmd+=("${replay_extra_args[@]}")
  fi
  showmap_cmd+=("${showmap_args[@]}")
  set +e
  pushd "${repo_dir}" >/dev/null
  run_with_timeout "${showmap_log_file}" "${showmap_cmd[@]}" || \
    log "Foundry showmap replay failed; continuing with original forge test exit code ${exit_code}."
  popd >/dev/null
  set -e
  if [[ -n "${original_timeout}" ]]; then
    SCFUZZBENCH_TIMEOUT_SECONDS="${original_timeout}"
  fi
fi

upload_results
exit ${exit_code}
