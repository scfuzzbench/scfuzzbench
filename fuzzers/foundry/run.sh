#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

register_shutdown_trap
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
capture_target_workspace_anchor
apply_benchmark_type
build_target

repo_dir="${SCFUZZBENCH_WORKDIR}/target"
log_file="${SCFUZZBENCH_LOG_DIR}/foundry.log"
if [[ -z "${SCFUZZBENCH_TARGET_ROOT_ANCHOR:-}" ||
      -z "${SCFUZZBENCH_TARGET_ROOT_IDENTITY:-}" ]]; then
  log "Refusing Foundry cache reset without the trusted target anchor."
  exit 1
fi
repo_root="${SCFUZZBENCH_TARGET_ROOT_ANCHOR}"
repo_root_identity="${SCFUZZBENCH_TARGET_ROOT_IDENTITY}"

# Start cold even if the target repo accidentally committed persisted failures.
# The corpus itself is reset centrally below, after any configured local seed
# source has first been staged.
remove_strict_descendant_tree \
  "${repo_dir}/cache/invariant" "${repo_dir}" "Foundry invariant cache" \
  "${repo_root}" "${repo_root_identity}"
remove_strict_descendant_tree \
  "${repo_dir}/cache/test-failures" "${repo_dir}" "Foundry failure cache" \
  "${repo_root}" "${repo_root_identity}"

# Expose the corpus dir so upload_results ships the foundry corpus like the other
# legs (and so post-run showmap replays have something to replay downstream).
corpus_dir=$(resolve_target_corpus_dir "${FOUNDRY_CORPUS_DIR:-}" "corpus/foundry")
export SCFUZZBENCH_CORPUS_DIR="${corpus_dir}"
prepare_shared_seed_corpus

extra_args=()
if [[ -n "${FOUNDRY_TEST_ARGS:-}" ]]; then
  read -r -a extra_args <<< "${FOUNDRY_TEST_ARGS}"
fi
for arg in "${extra_args[@]}"; do
  case "${arg}" in
    --invariant-corpus-dir|--invariant-corpus-dir=*)
      log "Set FOUNDRY_CORPUS_DIR instead of passing --invariant-corpus-dir through FOUNDRY_TEST_ARGS."
      exit 1
      ;;
  esac
done

# Keep Foundry's bounded invariant corpus enabled by default so comparative
# campaigns use the same coverage-guided search mode as the other fuzzers. The
# pinned Foundry stack evicts non-favored entries from worker memory and
# persists interesting entries immediately. It deliberately stops before the
# follow-up observed-call dictionary, whose separate in-memory pool is uncapped.
#
# Operators can still disable guidance explicitly for an A/B run. Showmap and
# seeded-corpus runs always require persistence.
showmap_enabled="${SCFUZZBENCH_FOUNDRY_SHOWMAP:-0}"
showmap_enabled_lc=$(printf '%s' "${showmap_enabled}" | tr '[:upper:]' '[:lower:]')
keep_corpus="${SCFUZZBENCH_FOUNDRY_KEEP_CORPUS:-1}"
if [[ "${keep_corpus}" != "0" && "${keep_corpus}" != "1" ]]; then
  log "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS must be 0 or 1."
  exit 1
fi
if [[ -n "${SCFUZZBENCH_SEED_CORPUS_SOURCE:-}" ]]; then
  keep_corpus=1
fi
if [[ "${showmap_enabled}" == "1" || "${showmap_enabled_lc}" == "true" || "${showmap_enabled_lc}" == "yes" ]]; then
  keep_corpus=1
fi
if [[ "${keep_corpus}" == "1" ]]; then
  export FOUNDRY_INVARIANT_CORPUS_DIR="${SCFUZZBENCH_CORPUS_DIR}"
  log "Foundry coverage guidance enabled; corpus: ${SCFUZZBENCH_CORPUS_DIR}."
else
  sed -i -E '/^[[:space:]]*corpus_dir[[:space:]]*=/d' "${repo_dir}/foundry.toml"
  unset FOUNDRY_INVARIANT_CORPUS_DIR
  log "Foundry coverage guidance disabled by SCFUZZBENCH_FOUNDRY_KEEP_CORPUS=0."
fi

# At the pin every newly found failure is shrunk inline (single-threaded, default
# 5000 sequence replays) before the campaign continues, and SIGINT is not observed
# mid-shrink: on the superform leg of run 1783612049 three of four instances got
# stuck shrinking, ignored SIGINT through the grace period, and were SIGKILLed
# without printing the end-of-run summary (losing every finding). The benchmark
# measures time-to-discovery, not reproducer minimality. Match every other
# benchmark leg's numeric tool-native limit unless explicitly overridden; the
# algorithms and work performed by one tool-native attempt are not equivalent.
foundry_shrink_run_limit="${FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT-1}"
if ! python3 - "${foundry_shrink_run_limit}" <<'PY'
import sys

raw_limit = sys.argv[1]
if not raw_limit.isascii() or not raw_limit.isdecimal():
    raise SystemExit(1)
if int(raw_limit) > 2**32 - 1:
    raise SystemExit(1)
PY
then
  log "FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT must be an integer in [0, 4294967295]."
  exit 1
fi
export FOUNDRY_INVARIANT_SHRINK_RUN_LIMIT="${foundry_shrink_run_limit}"
log "Foundry inline shrink limit: ${foundry_shrink_run_limit}."

# End the campaign from the inside instead of relying on SIGINT: a timed
# campaign ([invariant] timeout) ends naturally on the campaign thread and
# prints the full summary — the authoritative channel naming handler assertion
# bugs and preflight-failing canaries — without depending on signal delivery.
# The pin includes foundry-rs/foundry#15689, so SIGINT can interrupt a
# transaction mid-execution and remains a working backstop (older pins wedged
# on unbounded-gas targets; see #191/#193 history), but natural completion
# stays the primary exit path. Budget minus a small margin so the wind-down
# finishes before run_with_timeout's SIGINT backstop fires.
if [[ -z "${FOUNDRY_INVARIANT_TIMEOUT:-}" && "${SCFUZZBENCH_TIMEOUT_SECONDS:-}" =~ ^[0-9]+$ ]]; then
  if (( SCFUZZBENCH_TIMEOUT_SECONDS > 180 )); then
    export FOUNDRY_INVARIANT_TIMEOUT=$(( SCFUZZBENCH_TIMEOUT_SECONDS - 60 ))
  else
    export FOUNDRY_INVARIANT_TIMEOUT="${SCFUZZBENCH_TIMEOUT_SECONDS}"
  fi
fi

# On targets whose findings only surface in the end-of-run summary (no mid-run
# failure events — e.g. preflight-failing canaries and handler assertion bugs),
# losing the graceful exit loses the whole leg. Forge's post-campaign wind-down
# (counterexample persistence, corpus bookkeeping) also gets a generous grace
# window before SIGKILL; the schedule_hard_deadline watchdog still bounds the
# instance.
export SCFUZZBENCH_TIMEOUT_GRACE_SECONDS=${SCFUZZBENCH_TIMEOUT_GRACE_SECONDS:-1800}

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

# Forge's test thread pool and invariant campaign sharding are separate dials.
# The July 2026 N=2 smoke runs passed --threads 16 but omitted invariant workers,
# so Foundry used one worker and averaged only ~6.3% CPU on 16-vCPU instances.
# Keep the benchmark parallel even when a target or older Foundry pin defaults
# invariant workers to one. An explicit operator override remains authoritative.
has_invariant_workers_arg=0
for arg in "${extra_args[@]}"; do
  case "${arg}" in
    --invariant-workers|--invariant-workers=*)
      has_invariant_workers_arg=1
      break
      ;;
  esac
done
if [[ "${has_invariant_workers_arg}" -eq 0 ]]; then
  extra_args=(--invariant-workers auto "${extra_args[@]}")
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
  mkdir_strict_descendant \
    "${showmap_dir}" "${SCFUZZBENCH_LOG_DIR}" \
    "${SCFUZZBENCH_LOG_ROOT_ANCHOR}" \
    "${SCFUZZBENCH_LOG_ROOT_IDENTITY}" 0700
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
