#!/usr/bin/env bash
set -euo pipefail

SCFUZZBENCH_LOCAL_MODE=${SCFUZZBENCH_LOCAL_MODE:-}

is_local_mode() {
  [[ -n "${SCFUZZBENCH_LOCAL_MODE:-}" ]]
}

if is_local_mode; then
  SCFUZZBENCH_ROOT=${SCFUZZBENCH_ROOT:-${HOME}/.scfuzzbench}
  SCFUZZBENCH_BIN_DIR=${SCFUZZBENCH_BIN_DIR:-${HOME}/.local/bin}
  SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE=1
  export SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE
  mkdir -p "${SCFUZZBENCH_BIN_DIR}"
  case ":${PATH}:" in
    *":${SCFUZZBENCH_BIN_DIR}:"*) ;;
    *) export PATH="${SCFUZZBENCH_BIN_DIR}:${PATH}" ;;
  esac
else
  SCFUZZBENCH_ROOT=${SCFUZZBENCH_ROOT:-/opt/scfuzzbench}
  SCFUZZBENCH_BIN_DIR=${SCFUZZBENCH_BIN_DIR:-/usr/local/bin}
fi

SCFUZZBENCH_WORKDIR=${SCFUZZBENCH_WORKDIR:-${SCFUZZBENCH_ROOT}/work}
SCFUZZBENCH_LOG_DIR=${SCFUZZBENCH_LOG_DIR:-${SCFUZZBENCH_ROOT}/logs}
SCFUZZBENCH_CORPUS_DIR=${SCFUZZBENCH_CORPUS_DIR:-}
SCFUZZBENCH_BENCHMARK_TYPE=${SCFUZZBENCH_BENCHMARK_TYPE:-property}
SCFUZZBENCH_BENCHMARK_UUID=${SCFUZZBENCH_BENCHMARK_UUID:-}
SCFUZZBENCH_BENCHMARK_MANIFEST_B64=${SCFUZZBENCH_BENCHMARK_MANIFEST_B64:-}
SCFUZZBENCH_PROPERTIES_PATH=${SCFUZZBENCH_PROPERTIES_PATH:-}
SCFUZZBENCH_SEED_CORPUS_SOURCE=${SCFUZZBENCH_SEED_CORPUS_SOURCE:-}
SCFUZZBENCH_SEED_CORPUS_PROVENANCE_SOURCE=${SCFUZZBENCH_SEED_CORPUS_PROVENANCE_SOURCE:-}
SCFUZZBENCH_SEED_CORPUS_HELPER=${SCFUZZBENCH_SEED_CORPUS_HELPER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prepare_seed_corpus.py}
SCFUZZBENCH_RUNNER_METRICS=${SCFUZZBENCH_RUNNER_METRICS:-1}
SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS=${SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS:-5}
SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS=${SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS:-3600}
SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT=${SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT:-${SCFUZZBENCH_ROOT}/preliminary_snapshot.py}
SCFUZZBENCH_PRELIMINARY_HELPER_TIMEOUT_SECONDS=${SCFUZZBENCH_PRELIMINARY_HELPER_TIMEOUT_SECONDS:-300}
SCFUZZBENCH_PRELIMINARY_MAX_LATENESS_SECONDS=${SCFUZZBENCH_PRELIMINARY_MAX_LATENESS_SECONDS:-300}
SCFUZZBENCH_PRELIMINARY_UPLOAD_ATTEMPTS=${SCFUZZBENCH_PRELIMINARY_UPLOAD_ATTEMPTS:-2}
SCFUZZBENCH_PRELIMINARY_UPLOAD_RETRY_SECONDS=${SCFUZZBENCH_PRELIMINARY_UPLOAD_RETRY_SECONDS:-5}

SCFUZZBENCH_AWS_CREDS_ENV_FILE=${SCFUZZBENCH_AWS_CREDS_ENV_FILE:-${SCFUZZBENCH_ROOT}/aws_creds.env}
SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS=${SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS:-300}
SCFUZZBENCH_SEED_CORPUS_MAX_FILES=10000
SCFUZZBENCH_SEED_CORPUS_MAX_BYTES=1073741824

# The pinned Foundry build defaults `dynamic_test_linking` to true, which injects
# virtual `foundry-pp/DeployHelper*.sol` sources into build-info output.
# crytic-compile (the echidna/medusa compile path) does not recognize those files
# and aborts with `Unknown file: foundry-pp/DeployHelper114.sol` (observed on
# every echidna/medusa instance of run 1783547385). Force the classic static
# linking pipeline so every leg compiles the target identically.
export FOUNDRY_DYNAMIC_TEST_LINKING=${FOUNDRY_DYNAMIC_TEST_LINKING:-false}

log() {
  # Use stderr so command substitutions can safely capture stdout.
  echo "[$(date -Is)] $*" >&2
}

now_epoch_seconds() {
  date +%s
}

log_duration() {
  local label=$1
  local start=$2
  local end
  end=$(now_epoch_seconds)
  log "timing: ${label} completed in $((end - start))s"
}

retry_cmd() {
  local max_retries=${1:-5}
  local delay=${2:-60}
  shift 2
  local attempt=1
  while true; do
    if "$@"; then
      return 0
    fi
    if (( attempt >= max_retries )); then
      log "Command failed after ${attempt} attempts: $*"
      return 1
    fi
    log "Command failed (attempt ${attempt}/${max_retries}); retrying in ${delay}s: $*"
    sleep "${delay}" || true
    attempt=$((attempt + 1))
  done
}

require_env() {
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      log "Missing required env var: ${name}"
      exit 1
    fi
  done
}

is_positive_int() {
  local value=$1
  [[ "${value}" =~ ^[0-9]+$ ]] && [[ "${value}" -gt 0 ]]
}

get_vcpu_count() {
  local count=""
  if command -v nproc >/dev/null 2>&1; then
    count=$(nproc --all 2>/dev/null || nproc 2>/dev/null || true)
  fi
  if ! is_positive_int "${count}"; then
    count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)
  fi
  if ! is_positive_int "${count}"; then
    count=$(grep -c ^processor /proc/cpuinfo 2>/dev/null || true)
  fi
  if ! is_positive_int "${count}"; then
    count=1
  fi
  echo "${count}"
}

resolve_worker_count() {
  if [[ -n "${SCFUZZBENCH_WORKERS_RESOLVED:-}" ]]; then
    echo "${SCFUZZBENCH_WORKERS_RESOLVED}"
    return 0
  fi

  local override="${SCFUZZBENCH_WORKERS:-}"
  local source="vcpus"
  local value=""
  if is_positive_int "${override}"; then
    value="${override}"
    source="override"
  else
    if [[ -n "${override}" ]]; then
      log "Invalid SCFUZZBENCH_WORKERS='${override}', falling back to vCPU count."
    fi
    value=$(get_vcpu_count)
  fi

  SCFUZZBENCH_WORKERS="${value}"
  SCFUZZBENCH_WORKERS_RESOLVED="${value}"
  export SCFUZZBENCH_WORKERS
  export SCFUZZBENCH_WORKERS_RESOLVED
  log "Resolved worker count: ${value} (source: ${source})"
  echo "${value}"
}

set_default_worker_env() {
  local var_name=$1
  local current="${!var_name:-}"
  if is_positive_int "${current}"; then
    return 0
  fi
  if [[ -n "${current}" ]]; then
    log "Invalid ${var_name}='${current}', falling back to worker default."
  fi
  local value
  value=$(resolve_worker_count)
  printf -v "${var_name}" '%s' "${value}"
  export "${var_name}"
}

is_sensitive_arg_name() {
  local name="${1:-}"
  name="${name,,}"
  case "${name}" in
    *token*|*secret*|*password*|*passwd*|*api-key*|*apikey*|*private-key*|*access-key*|*secret-key*|*auth*|*authorization*|*cookie*|*session*)
      return 0
      ;;
  esac
  return 1
}

is_url_like_value() {
  local value="${1:-}"
  [[ "${value}" =~ ^[A-Za-z][A-Za-z0-9+.-]*:// ]]
}

sanitize_command_for_log() {
  local -a sanitized=()
  local redact_next=0
  local arg key value normalized rendered

  for arg in "$@"; do
    if [[ "${redact_next}" -eq 1 ]]; then
      sanitized+=("***")
      redact_next=0
      continue
    fi

    if [[ "${arg}" == --*=* ]]; then
      key="${arg%%=*}"
      value="${arg#*=}"
      normalized="${key#--}"
      if is_sensitive_arg_name "${normalized}" || is_url_like_value "${value}"; then
        sanitized+=("${key}=***")
      else
        sanitized+=("${key}=${value}")
      fi
      continue
    fi

    if [[ "${arg}" == *=* && "${arg}" != -* ]]; then
      key="${arg%%=*}"
      value="${arg#*=}"
      if is_sensitive_arg_name "${key}" || is_url_like_value "${value}"; then
        sanitized+=("${key}=***")
      else
        sanitized+=("${key}=${value}")
      fi
      continue
    fi

    if [[ "${arg}" == --* ]]; then
      normalized="${arg#--}"
      if is_sensitive_arg_name "${normalized}"; then
        redact_next=1
      fi
      sanitized+=("${arg}")
      continue
    fi

    if [[ "${arg}" == -* ]]; then
      normalized="${arg#-}"
      if is_sensitive_arg_name "${normalized}"; then
        redact_next=1
      fi
      sanitized+=("${arg}")
      continue
    fi

    if is_url_like_value "${arg}"; then
      sanitized+=("***")
      continue
    fi

    sanitized+=("${arg}")
  done

  rendered=""
  for arg in "${sanitized[@]}"; do
    if [[ -n "${rendered}" ]]; then
      rendered+=" "
    fi
    rendered+="${arg}"
  done
  echo "${rendered}"
}

append_runner_command_log() {
  local timeout_seconds="${1:-unknown}"
  local grace_seconds="${2:-unknown}"
  shift 2 || true

  if [[ -z "${SCFUZZBENCH_LOG_DIR:-}" ]]; then
    return 0
  fi
  if ! mkdir -p "${SCFUZZBENCH_LOG_DIR}" >/dev/null 2>&1; then
    return 0
  fi

  local cmd_log_path="${SCFUZZBENCH_LOG_DIR}/runner_commands.log"
  local rendered_cmd
  rendered_cmd=$(sanitize_command_for_log "$@")
  if [[ -z "${rendered_cmd}" ]]; then
    rendered_cmd="(empty command)"
  fi
  printf '[%s] timeout=%ss grace=%ss cmd=%s\n' \
    "$(date -Is)" \
    "${timeout_seconds}" \
    "${grace_seconds}" \
    "${rendered_cmd}" \
    >> "${cmd_log_path}" 2>/dev/null || true
}

prepare_workspace() {
  mkdir -p "${SCFUZZBENCH_ROOT}" "${SCFUZZBENCH_WORKDIR}" "${SCFUZZBENCH_LOG_DIR}"
}

resolve_target_corpus_dir() {
  local configured=${1:-}
  local default_relative=$2
  local target_root
  local candidate
  if [[ -z "${configured}" ]]; then
    configured="${default_relative}"
  fi
  if [[ "${configured}" == /* ]] \
    || [[ ! "${configured}" =~ ^[A-Za-z0-9_.+/-]+$ ]] \
    || [[ "/${configured}/" == *"/./"* ]] \
    || [[ "/${configured}/" == *"/../"* ]]; then
    log "Refusing unsafe corpus directory override: ${configured}"
    return 1
  fi
  target_root=$(realpath -e -- "${SCFUZZBENCH_WORKDIR}/target") || {
    log "Target workspace is missing while resolving corpus directory."
    return 1
  }
  candidate=$(realpath -m -- "${target_root}/${configured}") || return 1
  case "${candidate}" in
    "${target_root}"/*)
      printf '%s\n' "${candidate}"
      ;;
    *)
      log "Refusing corpus directory outside target workspace: ${candidate}"
      return 1
      ;;
  esac
}

prepare_shared_seed_corpus() {
  local corpus_dir="${SCFUZZBENCH_CORPUS_DIR:-}"
  local target_root
  if [[ -z "${corpus_dir}" || "${corpus_dir}" != /* || "${corpus_dir}" == "/" ]]; then
    log "Refusing to reset unsafe corpus directory: ${corpus_dir:-<empty>}"
    return 1
  fi
  target_root=$(realpath -e -- "${SCFUZZBENCH_WORKDIR}/target") || {
    log "Refusing corpus reset because target workspace is missing."
    return 1
  }
  corpus_dir=$(realpath -m -- "${corpus_dir}")
  case "${corpus_dir}" in
    "${target_root}"/*) ;;
    *)
      log "Refusing corpus reset outside target workspace: ${corpus_dir}"
      return 1
      ;;
  esac
  export SCFUZZBENCH_CORPUS_DIR="${corpus_dir}"

  local source="${SCFUZZBENCH_SEED_CORPUS_SOURCE:-}"
  local staging_dir="${SCFUZZBENCH_ROOT}/shared-seed-corpus"
  local metadata_path="${SCFUZZBENCH_LOG_DIR}/seed_corpus.json"
  local metadata_tmp="${metadata_path}.tmp-${BASHPID}"
  local installed_metadata="${SCFUZZBENCH_ROOT}/seed-corpus-installed-${BASHPID}.json"
  local corpus_tmp="${corpus_dir}.scfuzzbench-tmp-${BASHPID}"
  local corpus_backup="${corpus_dir}.scfuzzbench-old-${BASHPID}"
  local protected
  for protected in \
    "${SCFUZZBENCH_ROOT}" \
    "${SCFUZZBENCH_WORKDIR}" \
    "${SCFUZZBENCH_WORKDIR}/target" \
    "${SCFUZZBENCH_LOG_DIR}" \
    "${staging_dir}"; do
    if [[ "${corpus_dir}" == "$(realpath -m -- "${protected}")" ]]; then
      log "Refusing to reset protected corpus directory: ${corpus_dir}"
      return 1
    fi
  done
  if [[ ! -f "${SCFUZZBENCH_SEED_CORPUS_HELPER}" ]]; then
    log "Shared seed corpus helper not found: ${SCFUZZBENCH_SEED_CORPUS_HELPER}"
    return 1
  fi
  rm -f -- "${metadata_path}" "${metadata_tmp}" "${installed_metadata}"
  rm -rf -- "${staging_dir}" "${corpus_tmp}" "${corpus_backup}"
  unset SCFUZZBENCH_SEED_CORPUS_METADATA_PATH

  if [[ -z "${source}" ]]; then
    mkdir -p "$(dirname "${corpus_tmp}")"
    mkdir -m 0700 "${corpus_tmp}"
    if [[ -e "${corpus_dir}" || -L "${corpus_dir}" ]]; then
      mv -- "${corpus_dir}" "${corpus_backup}"
    fi
    if ! mv -- "${corpus_tmp}" "${corpus_dir}"; then
      [[ ! -e "${corpus_backup}" ]] || mv -- "${corpus_backup}" "${corpus_dir}"
      return 1
    fi
    rm -rf -- "${corpus_backup}"
    log "Starting with an empty corpus"
    return 0
  fi

  local source_type
  local provenance_source="${SCFUZZBENCH_SEED_CORPUS_PROVENANCE_SOURCE:-}"
  local helper_source
  case "${source}" in
    s3://*)
      if [[ ! "${source}" =~ ^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/.+$ ]] || \
        [[ "${source}" == *"?"* || "${source}" == *"#"* ]]; then
        log "Invalid shared seed corpus S3 prefix: ${source}"
        return 1
      fi
      source_type="s3"
      provenance_source="${provenance_source:-${source%/}}"
      helper_source="${source#s3://}"
      if ! command -v aws >/dev/null 2>&1; then
        log "AWS CLI is required for an S3 shared seed corpus"
        return 1
      fi
      log "Downloading shared seed corpus from ${provenance_source}"
      ;;
    *)
      source_type="local"
      local source_path="${source}"
      local source_was_relative=0
      if [[ "${source_path}" != /* ]]; then
        source_was_relative=1
        source_path="${SCFUZZBENCH_WORKDIR}/target/${source_path#./}"
      fi
      if [[ ! -d "${source_path}" ]]; then
        log "Shared seed corpus directory not found: ${source_path}"
        return 1
      fi
      local normalized_source_path
      normalized_source_path=$(realpath -m -- "${source_path}")
      if [[ "${staging_dir}" == "${normalized_source_path}" || "${staging_dir}" == "${normalized_source_path}/"* ]]; then
        log "Shared seed corpus source must not contain the staging directory"
        return 1
      fi
      if [[ "${corpus_dir}" == "${normalized_source_path}/"* ]]; then
        log "Shared seed corpus source must not contain the destination corpus"
        return 1
      fi
      if [[ -z "${provenance_source}" ]]; then
        if (( source_was_relative )); then
          provenance_source="target://${source#./}"
        else
          local source_path_sha256
          source_path_sha256=$(printf '%s' "${normalized_source_path}" | sha256sum | cut -d' ' -f1)
          provenance_source="local-sha256://${source_path_sha256}"
        fi
      fi
      helper_source="${source_path}"
      log "Copying shared seed corpus from ${provenance_source}"
      ;;
  esac

  if ! python3 "${SCFUZZBENCH_SEED_CORPUS_HELPER}" \
    --mode "${source_type}" \
    --source "${helper_source}" \
    --source-label "${provenance_source}" \
    --destination "${staging_dir}" \
    --metadata "${metadata_tmp}" \
    --max-files "${SCFUZZBENCH_SEED_CORPUS_MAX_FILES}" \
    --max-bytes "${SCFUZZBENCH_SEED_CORPUS_MAX_BYTES}"; then
    rm -rf -- "${staging_dir}" "${corpus_tmp}"
    rm -f -- "${metadata_tmp}" "${installed_metadata}"
    return 1
  fi

  # Copy the trusted snapshot into a sibling temporary directory and verify its
  # byte/path digest before replacing the live corpus.
  if ! python3 "${SCFUZZBENCH_SEED_CORPUS_HELPER}" \
    --mode local \
    --source "${staging_dir}" \
    --source-label "${provenance_source}" \
    --destination "${corpus_tmp}" \
    --metadata "${installed_metadata}" \
    --max-files "${SCFUZZBENCH_SEED_CORPUS_MAX_FILES}" \
    --max-bytes "${SCFUZZBENCH_SEED_CORPUS_MAX_BYTES}"; then
    rm -rf -- "${staging_dir}" "${corpus_tmp}"
    rm -f -- "${metadata_tmp}" "${installed_metadata}"
    return 1
  fi
  if ! python3 - "${metadata_tmp}" "${installed_metadata}" <<'PY'
import json
import sys
from pathlib import Path

source = json.loads(Path(sys.argv[1]).read_text())
installed = json.loads(Path(sys.argv[2]).read_text())
fields = ("file_count", "size_bytes", "sha256", "files")
if any(source.get(field) != installed.get(field) for field in fields):
    raise SystemExit("installed seed corpus does not match the staged snapshot")
PY
  then
    rm -rf -- "${staging_dir}" "${corpus_tmp}"
    rm -f -- "${metadata_tmp}" "${installed_metadata}"
    return 1
  fi

  if [[ -e "${corpus_dir}" || -L "${corpus_dir}" ]]; then
    mv -- "${corpus_dir}" "${corpus_backup}"
  fi
  if ! mv -- "${corpus_tmp}" "${corpus_dir}"; then
    [[ ! -e "${corpus_backup}" ]] || mv -- "${corpus_backup}" "${corpus_dir}"
    rm -rf -- "${staging_dir}" "${corpus_tmp}"
    rm -f -- "${metadata_tmp}" "${installed_metadata}"
    return 1
  fi
  rm -rf -- "${corpus_backup}" "${staging_dir}"
  rm -f -- "${installed_metadata}"
  mv -- "${metadata_tmp}" "${metadata_path}"
  export SCFUZZBENCH_SEED_CORPUS_METADATA_PATH="${metadata_path}"
  local file_count
  file_count=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["file_count"])' "${metadata_path}")
  log "Loaded ${file_count} shared seed corpus files into ${corpus_dir}"
}

install_shutdown_script() {
  local shutdown_path="${SCFUZZBENCH_ROOT}/shutdown.sh"
  if [[ -f "${shutdown_path}" ]]; then
    return 0
  fi
  if is_local_mode; then
    cat <<'SHUTDOWN' >"${shutdown_path}"
#!/usr/bin/env bash
echo "[$(date -Is)] Shutdown suppressed (local mode)"
SHUTDOWN
  else
    cat <<'SHUTDOWN' >"${shutdown_path}"
#!/usr/bin/env bash
set +e

log() {
  echo "[$(date -Is)] $*"
}

log "Shutting down instance"
sync || true
shutdown -h now || systemctl poweroff || halt -p || true
SHUTDOWN
  fi
  chmod +x "${shutdown_path}"
}

shutdown_instance() {
  if is_local_mode; then
    log "Skipping instance shutdown (local mode)"
    return 0
  fi
  install_shutdown_script
  local delay="${SCFUZZBENCH_SHUTDOWN_GRACE_SECONDS:-0}"
  if [[ "${delay}" =~ ^[0-9]+$ ]] && [[ "${delay}" -gt 0 ]]; then
    log "Delaying shutdown for ${delay}s"
    sleep "${delay}" || true
  fi
  "${SCFUZZBENCH_ROOT}/shutdown.sh" || true
}

runner_metrics_enabled() {
  local flag="${SCFUZZBENCH_RUNNER_METRICS:-1}"
  case "${flag}" in
    0|false|FALSE|no|NO|off|OFF)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

start_runner_metrics() {
  if ! runner_metrics_enabled; then
    log "Runner metrics disabled (SCFUZZBENCH_RUNNER_METRICS=${SCFUZZBENCH_RUNNER_METRICS})"
    return 0
  fi
  if [[ -n "${SCFUZZBENCH_RUNNER_METRICS_PID:-}" ]] && kill -0 "${SCFUZZBENCH_RUNNER_METRICS_PID}" 2>/dev/null; then
    return 0
  fi
  if [[ -z "${SCFUZZBENCH_LOG_DIR:-}" ]]; then
    log "Runner metrics skipped; SCFUZZBENCH_LOG_DIR is empty."
    return 0
  fi
  mkdir -p "${SCFUZZBENCH_LOG_DIR}"
  local metrics_file="${SCFUZZBENCH_LOG_DIR}/runner_metrics.csv"
  local interval="${SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS:-5}"
  if [[ ! "${interval}" =~ ^[0-9]+$ ]] || [[ "${interval}" -le 0 ]]; then
    interval=5
  fi
  printf "%s\n" \
    "timestamp,uptime_seconds,load1,load5,load15,cpu_user_pct,cpu_system_pct,cpu_idle_pct,cpu_iowait_pct,mem_total_kb,mem_available_kb,mem_used_kb,swap_total_kb,swap_free_kb,swap_used_kb" \
    >"${metrics_file}"

  (
    set +e
    set +u
    set +o pipefail

    read_cpu() {
      local cpu user nice system idle iowait irq softirq steal
      if read -r cpu user nice system idle iowait irq softirq steal _ < /proc/stat; then
        local total=$((user + nice + system + idle + iowait + irq + softirq + steal))
        local idle_all=$((idle + iowait))
        echo "${total} ${user} ${system} ${idle_all} ${iowait}"
      else
        echo "0 0 0 0 0"
      fi
    }

    local prev_total prev_user prev_system prev_idle prev_iowait
    read -r prev_total prev_user prev_system prev_idle prev_iowait <<< "$(read_cpu)"

    while true; do
      local ts uptime_seconds load1 load5 load15
      ts=$(date -Is)
      uptime_seconds=$(awk '{print int($1)}' /proc/uptime 2>/dev/null)
      if [[ -z "${uptime_seconds}" ]]; then
        uptime_seconds=0
      fi
      if read -r load1 load5 load15 _ < /proc/loadavg; then
        :
      else
        load1=0
        load5=0
        load15=0
      fi

      local mem_total mem_avail swap_total swap_free
      read -r mem_total mem_avail swap_total swap_free < <(
        awk '/MemTotal/ {mt=$2} /MemAvailable/ {ma=$2} /SwapTotal/ {st=$2} /SwapFree/ {sf=$2} END {print mt+0, ma+0, st+0, sf+0}' /proc/meminfo 2>/dev/null
      )
      mem_total=${mem_total:-0}
      mem_avail=${mem_avail:-0}
      swap_total=${swap_total:-0}
      swap_free=${swap_free:-0}
      local mem_used=$((mem_total - mem_avail))
      local swap_used=$((swap_total - swap_free))

      local cur_total cur_user cur_system cur_idle cur_iowait
      read -r cur_total cur_user cur_system cur_idle cur_iowait <<< "$(read_cpu)"
      local delta_total=$((cur_total - prev_total))
      local delta_user=$((cur_user - prev_user))
      local delta_system=$((cur_system - prev_system))
      local delta_idle=$((cur_idle - prev_idle))
      local delta_iowait=$((cur_iowait - prev_iowait))

      local cpu_user_pct cpu_system_pct cpu_idle_pct cpu_iowait_pct
      if [[ "${delta_total}" -gt 0 ]]; then
        cpu_user_pct=$(awk -v v="${delta_user}" -v t="${delta_total}" 'BEGIN { printf "%.2f", (v / t) * 100 }')
        cpu_system_pct=$(awk -v v="${delta_system}" -v t="${delta_total}" 'BEGIN { printf "%.2f", (v / t) * 100 }')
        cpu_idle_pct=$(awk -v v="${delta_idle}" -v t="${delta_total}" 'BEGIN { printf "%.2f", (v / t) * 100 }')
        cpu_iowait_pct=$(awk -v v="${delta_iowait}" -v t="${delta_total}" 'BEGIN { printf "%.2f", (v / t) * 100 }')
      else
        cpu_user_pct="0.00"
        cpu_system_pct="0.00"
        cpu_idle_pct="0.00"
        cpu_iowait_pct="0.00"
      fi

      printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n" \
        "${ts}" \
        "${uptime_seconds}" \
        "${load1}" \
        "${load5}" \
        "${load15}" \
        "${cpu_user_pct}" \
        "${cpu_system_pct}" \
        "${cpu_idle_pct}" \
        "${cpu_iowait_pct}" \
        "${mem_total}" \
        "${mem_avail}" \
        "${mem_used}" \
        "${swap_total}" \
        "${swap_free}" \
        "${swap_used}" \
        >>"${metrics_file}"

      prev_total=${cur_total}
      prev_user=${cur_user}
      prev_system=${cur_system}
      prev_idle=${cur_idle}
      prev_iowait=${cur_iowait}

      sleep "${interval}" || break
    done
  ) &

  export SCFUZZBENCH_RUNNER_METRICS_PID=$!
}

stop_runner_metrics() {
  local pid="${SCFUZZBENCH_RUNNER_METRICS_PID:-}"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  fi
}

preliminary_snapshot_interval_seconds() {
  local raw="${SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS:-3600}"
  local normalized
  normalized=$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]')
  case "${normalized}" in
    0|false|no|off|disabled)
      echo 0
      return 0
      ;;
  esac
  if [[ "${raw}" =~ ^[0-9]+$ ]] && (( raw >= 60 && raw <= 86400 )); then
    echo "${raw}"
    return 0
  fi
  log "Invalid preliminary snapshot interval '${raw}' (expected 0 or 60-86400); using 3600 seconds."
  echo 3600
}

preliminary_snapshots_enabled() {
  if is_local_mode; then
    return 1
  fi
  local interval
  interval=$(preliminary_snapshot_interval_seconds)
  [[ "${interval}" -gt 0 ]]
}

preliminary_snapshot_prefix() {
  require_env SCFUZZBENCH_RUN_ID SCFUZZBENCH_BENCHMARK_UUID
  printf 'preliminary/%s/%s' \
    "${SCFUZZBENCH_RUN_ID}" \
    "${SCFUZZBENCH_BENCHMARK_UUID}"
}

# PutObject is atomic. If a retry finds the key already present, accept it only
# when its recorded SHA-256 matches the exact local bytes. A divergent retry is
# a hard collision and is never allowed to overwrite the first checkpoint.
put_preliminary_immutable() {
  local source=$1
  local key=$2
  require_env SCFUZZBENCH_S3_BUCKET
  local sha256
  sha256=$(sha256sum "${source}" | awk '{print $1}')
  local attempt=1
  local max_attempts="${SCFUZZBENCH_PRELIMINARY_UPLOAD_ATTEMPTS:-2}"
  local retry_seconds="${SCFUZZBENCH_PRELIMINARY_UPLOAD_RETRY_SECONDS:-5}"
  if [[ ! "${max_attempts}" =~ ^[0-9]+$ ]] || (( max_attempts < 1 || max_attempts > 5 )); then
    max_attempts=2
  fi
  if [[ ! "${retry_seconds}" =~ ^[0-9]+$ ]] || (( retry_seconds > 60 )); then
    retry_seconds=5
  fi
  while (( attempt <= max_attempts )); do
    if AWS_MAX_ATTEMPTS=2 AWS_RETRY_MODE=standard aws_cli s3api put-object \
      --bucket "${SCFUZZBENCH_S3_BUCKET}" \
      --key "${key}" \
      --body "${source}" \
      --if-none-match '*' \
      --metadata "sha256=${sha256}" \
      --cli-connect-timeout 10 \
      --cli-read-timeout 60 \
      >/dev/null; then
      return 0
    fi

    local remote_sha=""
    if remote_sha=$(AWS_MAX_ATTEMPTS=2 AWS_RETRY_MODE=standard aws_cli s3api head-object \
      --bucket "${SCFUZZBENCH_S3_BUCKET}" \
      --key "${key}" \
      --query 'Metadata.sha256' \
      --output text \
      --cli-connect-timeout 10 \
      --cli-read-timeout 60 2>/dev/null); then
      if [[ "${remote_sha}" == "${sha256}" ]]; then
        log "Immutable preliminary object already exists with matching SHA-256: ${key}"
        return 0
      fi
      log "Refusing to overwrite preliminary object ${key}; existing SHA-256 is '${remote_sha:-missing}', local is ${sha256}."
      return 1
    fi
    if (( attempt == max_attempts )); then
      break
    fi
    log "Preliminary upload failed (attempt ${attempt}/${max_attempts}); retrying in ${retry_seconds}s: ${key}"
    sleep "${retry_seconds}" || true
    attempt=$((attempt + 1))
  done
  log "Preliminary upload failed after ${max_attempts} attempts: ${key}"
  return 1
}

capture_preliminary_snapshot() (
  set -euo pipefail
  local checkpoint=$1
  local scheduled_at=$2
  require_env \
    SCFUZZBENCH_RUN_ID \
    SCFUZZBENCH_RUN_STARTED_AT_EPOCH \
    SCFUZZBENCH_BENCHMARK_UUID \
    SCFUZZBENCH_TIMEOUT_SECONDS \
    SCFUZZBENCH_FUZZER_LABEL \
    SCFUZZBENCH_FUZZER_KEY \
    SCFUZZBENCH_RUN_INDEX
  cache_instance_id || true
  local instance_id="${SCFUZZBENCH_INSTANCE_ID:-unknown}"
  local checkpoint_padded
  checkpoint_padded=$(printf '%06d' "${checkpoint}")
  local capture_root="${SCFUZZBENCH_ROOT}/preliminary-checkpoints"
  mkdir -p "${capture_root}"
  exec 9>"${capture_root}/capture.lock"
  if ! flock -n 9; then
    log "Skipping preliminary checkpoint ${checkpoint_padded}; another capture is active."
    return 75
  fi
  local capture_dir="${capture_root}/${checkpoint_padded}"
  local archive="${capture_dir}/snapshot.zip"
  local captured_at
  captured_at=$(date +%s)
  rm -rf "${capture_dir}"
  mkdir -p "${capture_dir}"
  trap 'rm -rf "${capture_dir}"' EXIT

  local helper_timeout="${SCFUZZBENCH_PRELIMINARY_HELPER_TIMEOUT_SECONDS:-300}"
  if [[ ! "${helper_timeout}" =~ ^[0-9]+$ ]] || (( helper_timeout < 1 || helper_timeout > 900 )); then
    helper_timeout=300
  fi

  timeout --signal=TERM --kill-after=10s "${helper_timeout}s" \
    python3 "${SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT}" \
    --log-dir "${SCFUZZBENCH_LOG_DIR}" \
    --archive "${archive}" \
    --run-id "${SCFUZZBENCH_RUN_ID}" \
    --run-started-at-epoch "${SCFUZZBENCH_RUN_STARTED_AT_EPOCH}" \
    --benchmark-uuid "${SCFUZZBENCH_BENCHMARK_UUID}" \
    --checkpoint "${checkpoint}" \
    --interval-seconds "$(preliminary_snapshot_interval_seconds)" \
    --scheduled-at-epoch "${scheduled_at}" \
    --captured-at-epoch "${captured_at}" \
    --instance-id "${instance_id}" \
    --fuzzer-key "${SCFUZZBENCH_FUZZER_KEY}" \
    --run-index "${SCFUZZBENCH_RUN_INDEX}" \
    --fuzzer-label "${SCFUZZBENCH_FUZZER_LABEL}" \
    --timeout-seconds "${SCFUZZBENCH_TIMEOUT_SECONDS}" \
    >"${capture_dir}/capture-result.json"

  local identity="${SCFUZZBENCH_FUZZER_KEY}-${SCFUZZBENCH_RUN_INDEX}-${instance_id}"
  local key
  key="$(preliminary_snapshot_prefix)/snapshots/${checkpoint_padded}/${identity}/snapshot.zip"
  put_preliminary_immutable "${archive}" "${key}"
  log "Uploaded preliminary checkpoint ${checkpoint_padded}: ${key}"
)

preliminary_process_identity() {
  local pid=$1
  if [[ ! "${pid}" =~ ^[0-9]+$ ]] || [[ ! -r "/proc/${pid}/stat" ]]; then
    return 1
  fi
  local process_stat remainder
  IFS= read -r process_stat <"/proc/${pid}/stat" || return 1
  # The command name in field 2 can contain whitespace and parentheses. Strip
  # through the final ") " and parse only the fixed-position remainder.
  remainder="${process_stat##*) }"
  if [[ "${remainder}" == "${process_stat}" ]]; then
    return 1
  fi
  local -a fields=()
  read -r -a fields <<<"${remainder}"
  if (( ${#fields[@]} <= 19 )) \
    || [[ ! "${fields[2]}" =~ ^[0-9]+$ ]] \
    || [[ ! "${fields[19]}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  # starttime (field 22), process group (field 5), and state (field 3).
  printf '%s %s %s\n' "${fields[19]}" "${fields[2]}" "${fields[0]}"
}

preliminary_process_start_ticks() {
  local identity
  identity=$(preliminary_process_identity "$1") || return 1
  printf '%s\n' "${identity%% *}"
}

preliminary_wait_for_process_start_ticks() {
  local pid=$1
  local attempts="${2:-50}"
  local token=""
  local attempt
  for ((attempt = 0; attempt < attempts; attempt++)); do
    if token=$(preliminary_process_start_ticks "${pid}"); then
      printf '%s\n' "${token}"
      return 0
    fi
    sleep 0.01
  done
  return 1
}

preliminary_process_owned() {
  local pid=$1
  local expected_start=$2
  local identity actual_start
  [[ "${expected_start}" =~ ^[0-9]+$ ]] || return 1
  identity=$(preliminary_process_identity "${pid}") || return 1
  actual_start="${identity%% *}"
  [[ "${actual_start}" == "${expected_start}" ]]
}

preliminary_process_running_owned() {
  local pid=$1
  local expected_start=$2
  local identity actual_start process_state
  [[ "${expected_start}" =~ ^[0-9]+$ ]] || return 1
  identity=$(preliminary_process_identity "${pid}") || return 1
  read -r actual_start _ process_state <<<"${identity}"
  [[ "${actual_start}" == "${expected_start}" && "${process_state}" != "Z" && "${process_state}" != "X" ]]
}

preliminary_process_group_owned() {
  local pid=$1
  local expected_start=$2
  local identity actual_start process_group
  [[ "${expected_start}" =~ ^[0-9]+$ ]] || return 1
  identity=$(preliminary_process_identity "${pid}") || return 1
  read -r actual_start process_group _ <<<"${identity}"
  [[ "${actual_start}" == "${expected_start}" && "${process_group}" == "${pid}" ]]
}

preliminary_signal_pid_if_owned() {
  local signal=$1
  local pid=$2
  local expected_start=$3
  preliminary_process_owned "${pid}" "${expected_start}" || return 1
  kill "-${signal}" -- "${pid}"
}

preliminary_signal_group_if_owned() {
  local signal=$1
  local leader_pid=$2
  local expected_start=$3
  preliminary_process_group_owned "${leader_pid}" "${expected_start}" || return 1
  kill "-${signal}" -- "-${leader_pid}"
}

preliminary_write_active_owner() (
  local owner_file=$1
  local loop_pid=$2
  local loop_start=$3
  local capture_pid=$4
  local capture_start=$5
  local lock_file="${owner_file}.lock"
  local tmp_file="${owner_file}.${loop_pid}.${capture_pid}.tmp"
  local lock_fd
  mkdir -p "$(dirname "${owner_file}")"
  exec {lock_fd}>"${lock_file}" || return 1
  flock "${lock_fd}" || {
    exec {lock_fd}>&-
    return 1
  }
  umask 077
  if ! printf '%s %s %s %s\n' \
    "${loop_pid}" "${loop_start}" "${capture_pid}" "${capture_start}" \
    >"${tmp_file}" \
    || ! mv -f -- "${tmp_file}" "${owner_file}"; then
    rm -f -- "${tmp_file}"
    flock -u "${lock_fd}" || true
    exec {lock_fd}>&-
    return 1
  fi
  flock -u "${lock_fd}" || true
  exec {lock_fd}>&-
)

preliminary_read_active_owner() {
  local owner_file=$1
  local lock_file="${owner_file}.lock"
  local lock_fd
  local loop_pid loop_start capture_pid capture_start extra
  exec {lock_fd}>"${lock_file}" || return 1
  flock "${lock_fd}" || {
    exec {lock_fd}>&-
    return 1
  }
  if [[ ! -f "${owner_file}" ]] \
    || ! read -r loop_pid loop_start capture_pid capture_start extra <"${owner_file}" \
    || [[ -n "${extra:-}" ]] \
    || [[ ! "${loop_pid}" =~ ^[0-9]+$ ]] \
    || [[ ! "${loop_start}" =~ ^[0-9]+$ ]] \
    || [[ ! "${capture_pid}" =~ ^[0-9]+$ ]] \
    || [[ ! "${capture_start}" =~ ^[0-9]+$ ]]; then
    flock -u "${lock_fd}" || true
    exec {lock_fd}>&-
    return 1
  fi
  flock -u "${lock_fd}" || true
  exec {lock_fd}>&-
  printf '%s %s %s %s\n' \
    "${loop_pid}" "${loop_start}" "${capture_pid}" "${capture_start}"
}

preliminary_remove_active_owner_if_matches() {
  local owner_file=$1
  local expected_loop_pid=$2
  local expected_loop_start=$3
  local lock_file="${owner_file}.lock"
  local lock_fd
  local loop_pid loop_start _
  exec {lock_fd}>"${lock_file}" || return 1
  flock "${lock_fd}" || {
    exec {lock_fd}>&-
    return 1
  }
  if [[ -f "${owner_file}" ]] \
    && read -r loop_pid loop_start _ <"${owner_file}" \
    && [[ "${loop_pid}" == "${expected_loop_pid}" ]] \
    && [[ "${loop_start}" == "${expected_loop_start}" ]]; then
    rm -f -- "${owner_file}"
  fi
  flock -u "${lock_fd}" || true
  exec {lock_fd}>&-
}

preliminary_capture_supervisor() {
  local checkpoint=$1
  local scheduled_at=$2
  local supervisor_pid="${BASHPID}"
  local supervisor_start=""
  local worker_pid=""
  local worker_start=""
  local watchdog_pid=""
  local termination_requested=0
  local grace_seconds="${SCFUZZBENCH_PRELIMINARY_TERM_GRACE_SECONDS:-2}"
  if [[ ! "${grace_seconds}" =~ ^[0-9]+$ ]] \
    || (( grace_seconds < 1 || grace_seconds > 10 )); then
    grace_seconds=2
  fi
  supervisor_start=$(preliminary_process_start_ticks "${supervisor_pid}") || return 70

  preliminary_capture_supervisor_stop() {
    # Do not exit from the signal trap. Remaining alive as the identifiable
    # process-group leader lets the loop revalidate ownership immediately
    # before a grace-period SIGKILL of TERM-resistant descendants.
    termination_requested=1
    log "Preliminary capture supervisor received a stop signal; arming group cleanup."
    if [[ ! "${watchdog_pid}" =~ ^[0-9]+$ ]]; then
      # Fork the watchdog while TERM/INT are ignored so it inherits that
      # disposition atomically. The caller's immediately following group TERM
      # must stop the worker, not cancel the only delayed group-kill guard.
      trap '' TERM INT
      (
        trap '' TERM INT
        sleep "${grace_seconds}"
        log "Preliminary capture grace expired; stopping the owned process group."
        preliminary_signal_group_if_owned \
          KILL "${supervisor_pid}" "${supervisor_start}" 2>/dev/null || true
      ) &
      watchdog_pid=$!
      trap preliminary_capture_supervisor_stop TERM INT
    fi
  }
  trap preliminary_capture_supervisor_stop TERM INT
  capture_preliminary_snapshot "${checkpoint}" "${scheduled_at}" &
  worker_pid=$!
  if ! worker_start=$(preliminary_wait_for_process_start_ticks "${worker_pid}"); then
    local early_status=0
    wait "${worker_pid}" || early_status=$?
    trap - TERM INT
    if (( termination_requested )) && [[ "${watchdog_pid}" =~ ^[0-9]+$ ]]; then
      wait "${watchdog_pid}" 2>/dev/null || true
    fi
    return "${early_status}"
  fi
  if (( termination_requested )); then
    preliminary_signal_pid_if_owned \
      TERM "${worker_pid}" "${worker_start}" 2>/dev/null || true
  fi
  local status=0
  while preliminary_process_owned "${worker_pid}" "${worker_start}"; do
    local wait_status=0
    wait "${worker_pid}" || wait_status=$?
    if ! preliminary_process_owned "${worker_pid}" "${worker_start}"; then
      status="${wait_status}"
      break
    fi
  done
  if (( ! termination_requested )); then
    # A worker can observe the group TERM and exit just before Bash dispatches
    # the supervisor's own pending trap. Keep the leader/trap alive across that
    # narrow ordering window instead of dropping the only safe group identity.
    sleep 0.1 || true
  fi
  trap - TERM INT
  if (( termination_requested )) && [[ "${watchdog_pid}" =~ ^[0-9]+$ ]]; then
    # A worker may exit while leaving a TERM-resistant grandchild in this
    # process group. Keep the identifiable leader alive until the watchdog
    # revalidates its PID/start-time/PGID tuple and kills the whole group.
    wait "${watchdog_pid}" 2>/dev/null || true
  fi
  if (( termination_requested )) && (( status == 0 )); then
    status=143
  fi
  return "${status}"
}

start_preliminary_snapshots() {
  if ! preliminary_snapshots_enabled; then
    log "Preliminary snapshots disabled (SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS=${SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS})."
    return 0
  fi
  if [[ -n "${SCFUZZBENCH_PRELIMINARY_PID:-}" ]] \
    && [[ -n "${SCFUZZBENCH_PRELIMINARY_PID_START_TICKS:-}" ]] \
    && preliminary_process_owned \
      "${SCFUZZBENCH_PRELIMINARY_PID}" \
      "${SCFUZZBENCH_PRELIMINARY_PID_START_TICKS}"; then
    return 0
  fi
  unset SCFUZZBENCH_PRELIMINARY_PID SCFUZZBENCH_PRELIMINARY_PID_START_TICKS
  require_env \
    SCFUZZBENCH_RUN_ID \
    SCFUZZBENCH_BENCHMARK_UUID \
    SCFUZZBENCH_FUZZER_KEY \
    SCFUZZBENCH_RUN_INDEX
  local run_started_at="${SCFUZZBENCH_RUN_STARTED_AT_EPOCH:-}"
  if [[ -z "${run_started_at}" && "${SCFUZZBENCH_RUN_ID}" =~ ^[0-9]+$ ]]; then
    run_started_at="${SCFUZZBENCH_RUN_ID}"
    export SCFUZZBENCH_RUN_STARTED_AT_EPOCH="${run_started_at}"
  fi
  if [[ ! "${run_started_at}" =~ ^[0-9]+$ ]] || (( run_started_at <= 0 )); then
    log "Preliminary snapshots require SCFUZZBENCH_RUN_STARTED_AT_EPOCH; skipping."
    return 0
  fi
  if [[ ! -f "${SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT}" ]]; then
    log "Preliminary snapshot helper missing: ${SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT}"
    return 1
  fi

  local interval
  interval=$(preliminary_snapshot_interval_seconds)
  local timeout_seconds="${SCFUZZBENCH_TIMEOUT_SECONDS:-}"
  if [[ ! "${timeout_seconds}" =~ ^[0-9]+$ ]] || (( timeout_seconds <= 0 )); then
    log "Preliminary snapshots require a positive SCFUZZBENCH_TIMEOUT_SECONDS; skipping."
    return 0
  fi
  local deadline=$((run_started_at + timeout_seconds))
  local max_lateness="${SCFUZZBENCH_PRELIMINARY_MAX_LATENESS_SECONDS:-300}"
  if [[ ! "${max_lateness}" =~ ^[0-9]+$ ]] || (( max_lateness < 0 || max_lateness > 900 )); then
    max_lateness=300
  fi
  local now elapsed checkpoint
  now=$(date +%s)
  elapsed=$(( now - run_started_at ))
  if (( elapsed < 0 )); then
    elapsed=0
  fi
  checkpoint=$(( elapsed / interval + 1 ))

  (
    set +e
    local capture_pid=""
    local capture_start=""
    local sleep_pid=""
    local sleep_start=""
    local capture_pid_file="${SCFUZZBENCH_ROOT}/preliminary-checkpoints/active.pid"
    local loop_pid="${BASHPID}"
    local loop_start=""
    loop_start=$(preliminary_process_start_ticks "${loop_pid}") || exit 70
    preliminary_loop_exit() {
      local status=$?
      trap - EXIT TERM INT
      if [[ "${sleep_pid}" =~ ^[0-9]+$ && "${sleep_start}" =~ ^[0-9]+$ ]]; then
        preliminary_signal_pid_if_owned \
          TERM "${sleep_pid}" "${sleep_start}" 2>/dev/null || true
        wait "${sleep_pid}" 2>/dev/null || true
      fi
      if [[ "${capture_pid}" =~ ^[0-9]+$ && "${capture_start}" =~ ^[0-9]+$ ]]; then
        preliminary_signal_pid_if_owned \
          TERM "${capture_pid}" "${capture_start}" 2>/dev/null || true
        preliminary_signal_group_if_owned \
          TERM "${capture_pid}" "${capture_start}" 2>/dev/null || true
        local capture_wait=0
        while preliminary_process_running_owned "${capture_pid}" "${capture_start}" \
          && (( capture_wait < 100 )); do
          sleep 0.02
          capture_wait=$((capture_wait + 1))
        done
        preliminary_signal_group_if_owned \
          KILL "${capture_pid}" "${capture_start}" 2>/dev/null || true
        preliminary_signal_pid_if_owned \
          KILL "${capture_pid}" "${capture_start}" 2>/dev/null || true
        wait "${capture_pid}" 2>/dev/null || true
      fi
      preliminary_remove_active_owner_if_matches \
        "${capture_pid_file}" "${loop_pid}" "${loop_start}" || true
      return "${status}"
    }
    trap preliminary_loop_exit EXIT
    trap 'exit 0' TERM INT
    while true; do
      local scheduled_at=$(( run_started_at + checkpoint * interval ))
      if (( scheduled_at >= deadline )); then
        log "Preliminary checkpoint loop reached the terminal benchmark deadline."
        exit 0
      fi
      local sleep_for=$(( scheduled_at - $(date +%s) ))
      if (( sleep_for > 0 )); then
        sleep "${sleep_for}" &
        sleep_pid=$!
        sleep_start=$(preliminary_wait_for_process_start_ticks "${sleep_pid}") || exit 70
        wait "${sleep_pid}" || exit 0
        sleep_pid=""
        sleep_start=""
      fi
      now=$(date +%s)
      if (( now - scheduled_at > max_lateness )); then
        local next_checkpoint=$(( (now - run_started_at) / interval + 1 ))
        log "Skipping missed preliminary checkpoint ${checkpoint}; capture is $((now - scheduled_at))s late (next ${next_checkpoint})."
        checkpoint="${next_checkpoint}"
        continue
      fi
      local common_path="${SCFUZZBENCH_COMMON_SH:-${SCFUZZBENCH_ROOT}/common.sh}"
      setsid bash -c \
        'set -euo pipefail; source "$1"; preliminary_capture_supervisor "$2" "$3"' \
        preliminary-capture "${common_path}" "${checkpoint}" "${scheduled_at}" &
      capture_pid=$!
      capture_start=$(preliminary_wait_for_process_start_ticks "${capture_pid}") || {
        wait "${capture_pid}" 2>/dev/null || true
        capture_pid=""
        log "Preliminary checkpoint ${checkpoint} exited before ownership could be recorded."
        checkpoint=$((checkpoint + 1))
        continue
      }
      if ! preliminary_write_active_owner \
        "${capture_pid_file}" \
        "${loop_pid}" \
        "${loop_start}" \
        "${capture_pid}" \
        "${capture_start}"; then
        preliminary_signal_group_if_owned \
          KILL "${capture_pid}" "${capture_start}" 2>/dev/null || true
        preliminary_signal_pid_if_owned \
          KILL "${capture_pid}" "${capture_start}" 2>/dev/null || true
        wait "${capture_pid}" 2>/dev/null || true
        exit 70
      fi
      wait "${capture_pid}" || \
        log "Preliminary checkpoint ${checkpoint} failed; the live campaign continues."
      preliminary_remove_active_owner_if_matches \
        "${capture_pid_file}" "${loop_pid}" "${loop_start}" || true
      capture_pid=""
      capture_start=""
      now=$(date +%s)
      local next_checkpoint=$(( (now - run_started_at) / interval + 1 ))
      if (( next_checkpoint > checkpoint + 1 )); then
        log "Skipping preliminary checkpoints $((checkpoint + 1))-$((next_checkpoint - 1)); the previous capture crossed their boundaries."
      fi
      if (( next_checkpoint <= checkpoint )); then
        next_checkpoint=$((checkpoint + 1))
      fi
      checkpoint="${next_checkpoint}"
    done
  ) &
  local loop_pid=$!
  local loop_start=""
  if ! loop_start=$(preliminary_wait_for_process_start_ticks "${loop_pid}"); then
    wait "${loop_pid}" 2>/dev/null || true
    log "Preliminary checkpoint loop exited before ownership could be recorded."
    return 0
  fi
  export SCFUZZBENCH_PRELIMINARY_PID="${loop_pid}"
  export SCFUZZBENCH_PRELIMINARY_PID_START_TICKS="${loop_start}"
  log "Scheduled preliminary snapshots every ${interval}s from run epoch ${run_started_at} (next checkpoint ${checkpoint})."
}

stop_preliminary_snapshots() {
  local pid="${SCFUZZBENCH_PRELIMINARY_PID:-}"
  local pid_start="${SCFUZZBENCH_PRELIMINARY_PID_START_TICKS:-}"
  local capture_pid_file="${SCFUZZBENCH_ROOT}/preliminary-checkpoints/active.pid"
  local loop_owner=""
  local loop_owner_start=""
  local capture_pid=""
  local capture_start=""
  local owner_record=""
  if owner_record=$(preliminary_read_active_owner "${capture_pid_file}"); then
    read -r loop_owner loop_owner_start capture_pid capture_start <<<"${owner_record}"
  fi
  local loop_was_owned=0
  if [[ "${pid}" =~ ^[0-9]+$ && "${pid_start}" =~ ^[0-9]+$ ]] \
    && preliminary_process_owned "${pid}" "${pid_start}"; then
    loop_was_owned=1
  fi
  local active_owned=0
  if (( loop_was_owned )) \
    && [[ "${loop_owner}" == "${pid}" ]] \
    && [[ "${loop_owner_start}" == "${pid_start}" ]] \
    && [[ "${capture_pid}" =~ ^[0-9]+$ ]] \
    && [[ "${capture_start}" =~ ^[0-9]+$ ]]; then
    active_owned=1
    # The direct signal covers the brief setsid startup window before the child
    # has established its same-numbered process group. Once its supervisor trap
    # is live, this also arms the bounded group-kill watchdog before descendants
    # receive TERM and can make the direct worker exit.
    preliminary_signal_pid_if_owned \
      TERM "${capture_pid}" "${capture_start}" 2>/dev/null || true
    preliminary_signal_group_if_owned \
      TERM "${capture_pid}" "${capture_start}" 2>/dev/null || true
  fi
  if (( loop_was_owned )); then
    preliminary_signal_pid_if_owned TERM "${pid}" "${pid_start}" 2>/dev/null || true
  fi
  local loop_wait=0
  while (( loop_was_owned )) \
    && preliminary_process_running_owned "${pid}" "${pid_start}" \
    && (( loop_wait < 150 )); do
    sleep 0.02
    loop_wait=$((loop_wait + 1))
  done
  if (( active_owned )); then
    local capture_wait=0
    while preliminary_process_running_owned "${capture_pid}" "${capture_start}" \
      && (( capture_wait < 100 )); do
      sleep 0.02
      capture_wait=$((capture_wait + 1))
    done
    preliminary_signal_group_if_owned \
      KILL "${capture_pid}" "${capture_start}" 2>/dev/null || true
    preliminary_signal_pid_if_owned \
      KILL "${capture_pid}" "${capture_start}" 2>/dev/null || true
  fi
  if (( loop_was_owned )); then
    preliminary_signal_pid_if_owned KILL "${pid}" "${pid_start}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  fi
  if (( loop_was_owned )) \
    && [[ "${loop_owner}" == "${pid}" ]] \
    && [[ "${loop_owner_start}" == "${pid_start}" ]]; then
    preliminary_remove_active_owner_if_matches \
      "${capture_pid_file}" "${pid}" "${pid_start}" || true
  fi
  unset SCFUZZBENCH_PRELIMINARY_PID SCFUZZBENCH_PRELIMINARY_PID_START_TICKS
}

finalize_run() {
  local exit_code=$?
  set +e
  stop_preliminary_snapshots || true
  stop_runner_metrics || true
  if [[ -z "${SCFUZZBENCH_UPLOAD_DONE:-}" ]]; then
    if is_local_mode; then
      save_results_local || true
    elif [[ -n "${SCFUZZBENCH_S3_BUCKET:-}" && -n "${SCFUZZBENCH_RUN_ID:-}" && -n "${SCFUZZBENCH_FUZZER_LABEL:-}" ]]; then
      upload_results || true
    else
      log "Skipping upload in finalize; missing S3 bucket, run id, or fuzzer label."
    fi
  fi
  shutdown_instance
  return ${exit_code}
}

# Dead-man switch: if the runner script never reaches finalize (unkillable
# fuzzer process, OOM-killed shell, kernel hang), the instance would otherwise
# run — and bill — forever, since self-termination happens in the EXIT trap.
# Observed on run 1783621460: three foundry instances died mid-run without
# finalize and kept running until the next terraform apply replaced them.
# `shutdown -h` is honored by the kernel independently of the wedged script,
# and instance_initiated_shutdown_behavior=terminate turns it into termination.
schedule_hard_deadline() {
  if is_local_mode; then
    return 0
  fi
  local timeout_seconds="${SCFUZZBENCH_TIMEOUT_SECONDS:-86400}"
  if ! [[ "${timeout_seconds}" =~ ^[0-9]+$ ]]; then
    timeout_seconds=86400
  fi
  # Fuzzing budget + SIGKILL grace + generous margin for build/upload phases.
  local deadline_minutes=$(( timeout_seconds / 60 + 90 ))
  if command -v shutdown >/dev/null 2>&1; then
    log "Scheduling hard shutdown deadline in ${deadline_minutes} minutes"
    shutdown -h "+${deadline_minutes}" "scfuzzbench hard deadline" || true
  fi
}

register_shutdown_trap() {
  install_shutdown_script
  cache_instance_id || true
  if [[ -z "${SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE:-}" ]]; then
    cache_aws_creds_from_imds || true
    start_aws_creds_refresher || true
  fi
  schedule_hard_deadline || true
  start_runner_metrics
  trap finalize_run EXIT
}

install_base_packages() {
  local install_start
  install_start=$(now_epoch_seconds)
  if is_local_mode; then
    log "Skipping system package installation (local mode)"
    log_duration "install_base_packages" "${install_start}"
    return 0
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y \
    ca-certificates \
    curl \
    git \
    jq \
    tar \
    zip \
    unzip \
    build-essential \
    pkg-config \
    libssl-dev \
    python3 \
    python3-pip \
    python3-venv

  if ! command -v aws >/dev/null 2>&1; then
    log "Installing AWS CLI v2"
    local tmp_dir
    tmp_dir=$(mktemp -d)
    curl -sSfL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "${tmp_dir}/awscliv2.zip"
    unzip -q "${tmp_dir}/awscliv2.zip" -d "${tmp_dir}"
    "${tmp_dir}/aws/install" --update
    rm -rf "${tmp_dir}"
    aws --version
  fi
  # Begin lifecycle evidence as soon as the AWS client exists, before expensive
  # compiler/fuzzer builds. The PID-file guard prevents later sourced runners
  # from starting a duplicate loop.
  start_run_heartbeat || true
  log_duration "install_base_packages" "${install_start}"
}

install_foundry() {
  local install_start
  install_start=$(now_epoch_seconds)
  if [[ -n "${FOUNDRY_GIT_REPO:-}" ]]; then
    log "Installing Foundry from ${FOUNDRY_GIT_REPO}"
    if ! is_local_mode; then
      export HOME=/root
    fi
    local foundry_build_profile="${FOUNDRY_BUILD_PROFILE:-dist}"
    # Must satisfy the pinned commit's LOCKED transitive dependencies, not its
    # workspace MSRV (Cargo.toml says rust-version = "1.89", but the locked
    # foundry-compilers 0.21 / reth 2.3 / alloy-op crates require rustc >= 1.95).
    # A `--locked` build fails on an older toolchain. Bump this in lockstep with
    # foundry_git_ref if a future pin raises the floor again.
    local foundry_rust_toolchain="${FOUNDRY_RUST_TOOLCHAIN:-1.95.0}"
    if ! command -v rustup >/dev/null 2>&1; then
      log "Installing Rust toolchain manager"
      curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
    fi
    # shellcheck source=/dev/null
    source "${HOME}/.cargo/env"
    log "Installing Rust ${foundry_rust_toolchain} for custom Foundry build"
    rustup toolchain install "${foundry_rust_toolchain}" --profile minimal
    local tmp_dir
    tmp_dir=$(mktemp -d)
    git clone --depth 1 "${FOUNDRY_GIT_REPO}" "${tmp_dir}/foundry"
    if [[ -n "${FOUNDRY_GIT_REF:-}" ]]; then
      git -C "${tmp_dir}/foundry" fetch --depth 1 origin "${FOUNDRY_GIT_REF}"
      # `git fetch origin <ref>` updates FETCH_HEAD but does not always create a local branch.
      git -C "${tmp_dir}/foundry" checkout --detach FETCH_HEAD
    fi
    local commit commit_full
    commit=$(git -C "${tmp_dir}/foundry" rev-parse --short HEAD)
    commit_full=$(git -C "${tmp_dir}/foundry" rev-parse HEAD)

    # Upstream PR #14266 added invariant tx/gas counters, but the pinned source
    # only emits them when the progress UI is disabled and edge coverage is
    # enabled. The benchmark deliberately keeps --show-progress for graceful
    # SIGINT summaries and disables corpus persistence to avoid unbounded memory
    # growth, so apply the narrow scfuzzbench pulse patch at the exact known pin.
    # Explicit source-ref experiments remain unpatched and are called out in the
    # benchmark manifest.
    local throughput_patch="${SCFUZZBENCH_FOUNDRY_SOURCE_PATCH:-}"
    local throughput_patch_ref="02c05d970d2801da0aef8b82486ce84b01ede36d"
    local throughput_patch_sha256="2ee9e69b77c8007c78c816eb9ca791684aa5ecede0651b63f86cdd2e055eb17e"
    if [[ "${commit_full}" == "${throughput_patch_ref}" ]]; then
      if [[ -z "${throughput_patch}" || ! -f "${throughput_patch}" ]]; then
        log "Missing Foundry throughput source patch for pinned commit ${throughput_patch_ref}."
        return 1
      fi
      local actual_patch_sha256
      actual_patch_sha256=$(sha256sum -- "${throughput_patch}" | awk '{print $1}')
      if [[ "${actual_patch_sha256}" != "${throughput_patch_sha256}" ]]; then
        log "Foundry throughput patch digest mismatch: expected ${throughput_patch_sha256}, got ${actual_patch_sha256}."
        return 1
      fi
      if git -C "${tmp_dir}/foundry" apply --check -- "${throughput_patch}"; then
        git -C "${tmp_dir}/foundry" apply -- "${throughput_patch}"
      elif git -C "${tmp_dir}/foundry" apply --reverse --check -- "${throughput_patch}"; then
        log "Foundry throughput patch is already applied."
      else
        log "Foundry throughput patch does not apply cleanly to ${commit_full}; refusing a drifted build."
        return 1
      fi
      printf '%s\n' "scfuzzbench-throughput-progress-v1@sha256:${throughput_patch_sha256}" \
        > "${SCFUZZBENCH_ROOT}/foundry_source_patch"
      log "Applied Foundry throughput patch ${throughput_patch_sha256} to ${commit_full}"
    elif [[ "${FOUNDRY_GIT_REF:-}" == "${throughput_patch_ref}" ]]; then
      log "Foundry ref ${FOUNDRY_GIT_REF} resolved to unexpected commit ${commit_full}; refusing a drifted build."
      return 1
    else
      log "Foundry source override ${commit_full} is outside the throughput patch pin; leaving it unpatched."
    fi

    log "Building Foundry at ${commit} with profile ${foundry_build_profile} on Rust ${foundry_rust_toolchain}"
    # The benchmark path only invokes forge; do not build extra Foundry binaries
    # on every CI comparison side.
    cargo +"${foundry_rust_toolchain}" build \
      --locked \
      --profile "${foundry_build_profile}" \
      --bin forge \
      --manifest-path "${tmp_dir}/foundry/Cargo.toml"
    install -m 0755 "${tmp_dir}/foundry/target/${foundry_build_profile}/forge" "${SCFUZZBENCH_BIN_DIR}/forge"
    echo "${commit}" > "${SCFUZZBENCH_ROOT}/foundry_commit"
    echo "${FOUNDRY_GIT_REPO}" > "${SCFUZZBENCH_ROOT}/foundry_repo"
    rm -rf "${tmp_dir}"
    forge --version
  else
    require_env FOUNDRY_VERSION
    log "Installing Foundry ${FOUNDRY_VERSION}"
    if ! is_local_mode; then
      export HOME=/root
    fi
    curl -L https://foundry.paradigm.xyz | bash
    export PATH="${HOME}/.foundry/bin:${PATH}"
    "${HOME}/.foundry/bin/foundryup" -i "${FOUNDRY_VERSION}"
    forge --version
  fi
  log_duration "install_foundry" "${install_start}"
}

install_crytic_compile() {
  local install_start
  install_start=$(now_epoch_seconds)
  log "Installing crytic-compile"
  python3 -m pip install --no-cache-dir --break-system-packages crytic-compile
  command -v crytic-compile
  log_duration "install_crytic_compile" "${install_start}"
}

install_slither_analyzer() {
  local install_start
  install_start=$(now_epoch_seconds)
  log "Installing slither-analyzer"
  python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed slither-analyzer
  command -v slither
  log_duration "install_slither_analyzer" "${install_start}"
}

imds_token() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null || true
}

imds_get() {
  local path=$1
  local token
  token=$(imds_token)
  if [[ -z "${token}" ]]; then
    return 1
  fi
  curl -fsS --connect-timeout 1 --max-time 2 \
    -H "X-aws-ec2-metadata-token: ${token}" \
    "http://169.254.169.254/latest/${path}" 2>/dev/null
}

get_instance_id() {
  imds_get "meta-data/instance-id" || true
}

cache_instance_id() {
  if [[ -n "${SCFUZZBENCH_INSTANCE_ID:-}" ]]; then
    return 0
  fi
  local instance_id
  instance_id=$(get_instance_id 2>/dev/null | head -n 1 | tr -d '\r' || true)
  if [[ -n "${instance_id}" ]]; then
    export SCFUZZBENCH_INSTANCE_ID="${instance_id}"
    return 0
  fi
  instance_id=$(hostname 2>/dev/null || true)
  if [[ -z "${instance_id}" ]]; then
    instance_id="unknown"
  fi
  export SCFUZZBENCH_INSTANCE_ID="${instance_id}"
  return 0
}

cache_aws_creds_from_imds() {
  if [[ -n "${SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE:-}" ]]; then
    return 0
  fi
  if ! command -v jq >/dev/null 2>&1; then
    log "jq not found; skipping IMDS credential cache."
    return 1
  fi

  local role_name
  role_name=$(imds_get "meta-data/iam/security-credentials/" 2>/dev/null | head -n 1 | tr -d '\r' || true)
  if [[ -z "${role_name}" ]]; then
    log "Could not fetch IAM role name from IMDS; skipping credential cache."
    return 1
  fi

  local creds_json
  creds_json=$(imds_get "meta-data/iam/security-credentials/${role_name}" 2>/dev/null || true)
  if [[ -z "${creds_json}" ]]; then
    log "Could not fetch IAM role credentials from IMDS; skipping credential cache."
    return 1
  fi

  local access_key_id_sh
  local secret_access_key_sh
  local session_token_sh
  local expiration_raw
  local expiration_sh
  access_key_id_sh=$(jq -r '.AccessKeyId // empty | @sh' <<<"${creds_json}")
  secret_access_key_sh=$(jq -r '.SecretAccessKey // empty | @sh' <<<"${creds_json}")
  session_token_sh=$(jq -r '.Token // empty | @sh' <<<"${creds_json}")
  expiration_raw=$(jq -r '.Expiration // empty' <<<"${creds_json}")
  expiration_sh=$(jq -r '.Expiration // empty | @sh' <<<"${creds_json}")
  if [[ -z "${access_key_id_sh}" || -z "${secret_access_key_sh}" || -z "${session_token_sh}" ]]; then
    log "IMDS returned incomplete IAM role credentials; skipping credential cache."
    return 1
  fi

  local expiration_epoch=""
  if [[ -n "${expiration_raw}" ]]; then
    expiration_epoch=$(date -u -d "${expiration_raw}" +%s 2>/dev/null || true)
  fi

  local creds_file="${SCFUZZBENCH_AWS_CREDS_ENV_FILE:-${SCFUZZBENCH_ROOT}/aws_creds.env}"
  mkdir -p "$(dirname "${creds_file}")"
  umask 077
  local tmp_file
  tmp_file=$(mktemp "${creds_file}.tmp.XXXXXX")
  chmod 0600 "${tmp_file}"
  {
    echo "# Cached from IMDS. Used to keep S3/SSM uploads working during shutdown."
    echo "AWS_ACCESS_KEY_ID=${access_key_id_sh}"
    echo "AWS_SECRET_ACCESS_KEY=${secret_access_key_sh}"
    echo "AWS_SESSION_TOKEN=${session_token_sh}"
    if [[ -n "${expiration_sh}" ]]; then
      echo "SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION=${expiration_sh}"
    fi
    if [[ -n "${expiration_epoch}" ]]; then
      echo "SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH=${expiration_epoch}"
    fi
  } >"${tmp_file}"
  mv -f "${tmp_file}" "${creds_file}"
  return 0
}

load_cached_aws_creds() {
  if [[ -n "${SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE:-}" ]]; then
    return 1
  fi
  local creds_file="${SCFUZZBENCH_AWS_CREDS_ENV_FILE:-${SCFUZZBENCH_ROOT}/aws_creds.env}"
  if [[ ! -f "${creds_file}" ]]; then
    return 1
  fi

  local old_ak_set=0
  local old_sk_set=0
  local old_st_set=0
  local old_exp_set=0
  local old_exp_epoch_set=0
  if [[ "${AWS_ACCESS_KEY_ID+x}" == "x" ]]; then old_ak_set=1; fi
  if [[ "${AWS_SECRET_ACCESS_KEY+x}" == "x" ]]; then old_sk_set=1; fi
  if [[ "${AWS_SESSION_TOKEN+x}" == "x" ]]; then old_st_set=1; fi
  if [[ "${SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION+x}" == "x" ]]; then old_exp_set=1; fi
  if [[ "${SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH+x}" == "x" ]]; then old_exp_epoch_set=1; fi
  local old_ak="${AWS_ACCESS_KEY_ID-}"
  local old_sk="${AWS_SECRET_ACCESS_KEY-}"
  local old_st="${AWS_SESSION_TOKEN-}"
  local old_exp="${SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION-}"
  local old_exp_epoch="${SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH-}"

  local ok=0
  # shellcheck disable=SC1090
  set -a
  if source "${creds_file}"; then
    ok=1
  fi
  set +a

  if (( ok )); then
    if [[ -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" || -z "${AWS_SESSION_TOKEN:-}" ]]; then
      ok=0
    fi
    local exp_epoch="${SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH:-}"
    if [[ -n "${exp_epoch}" && "${exp_epoch}" =~ ^[0-9]+$ ]]; then
      local now
      now=$(date -u +%s)
      if (( exp_epoch <= now )); then
        ok=0
      fi
    fi
  fi

  if (( ok )); then
    return 0
  fi

  if (( old_ak_set )); then export AWS_ACCESS_KEY_ID="${old_ak}"; else unset AWS_ACCESS_KEY_ID; fi
  if (( old_sk_set )); then export AWS_SECRET_ACCESS_KEY="${old_sk}"; else unset AWS_SECRET_ACCESS_KEY; fi
  if (( old_st_set )); then export AWS_SESSION_TOKEN="${old_st}"; else unset AWS_SESSION_TOKEN; fi
  if (( old_exp_set )); then export SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION="${old_exp}"; else unset SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION; fi
  if (( old_exp_epoch_set )); then export SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH="${old_exp_epoch}"; else unset SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH; fi
  return 1
}

aws_cli() {
  local have_cached=0
  if [[ -z "${SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE:-}" ]]; then
    if load_cached_aws_creds; then
      have_cached=1
    else
      cache_aws_creds_from_imds >/dev/null 2>&1 || true
      if load_cached_aws_creds >/dev/null 2>&1; then
        have_cached=1
      fi
    fi
  fi
  if (( have_cached )); then
    AWS_EC2_METADATA_DISABLED=true aws "$@"
  else
    aws "$@"
  fi
}

start_run_heartbeat() {
  if is_local_mode; then
    return 0
  fi
  if [[ -z "${SCFUZZBENCH_S3_BUCKET:-}" ||
        -z "${SCFUZZBENCH_RUN_ID:-}" ||
        -z "${SCFUZZBENCH_BENCHMARK_UUID:-}" ]]; then
    log "Run heartbeat skipped; run identity or bucket is missing."
    return 0
  fi

  local pid_file="${SCFUZZBENCH_ROOT}/run-heartbeat.pid"
  if [[ -s "${pid_file}" ]]; then
    local existing_pid
    existing_pid=$(cat "${pid_file}" 2>/dev/null || true)
    if [[ "${existing_pid}" =~ ^[0-9]+$ ]] && kill -0 "${existing_pid}" 2>/dev/null; then
      return 0
    fi
  fi

  cache_instance_id || true
  local instance_token
  instance_token=$(printf '%s' "${SCFUZZBENCH_INSTANCE_ID:-unknown}" |
    tr -cd 'A-Za-z0-9._-')
  instance_token="${instance_token:-unknown}"
  local interval="${SCFUZZBENCH_RUN_HEARTBEAT_SECONDS:-300}"
  if [[ ! "${interval}" =~ ^[0-9]+$ ]] || (( interval < 60 )); then
    interval=300
  fi
  local heartbeat_file="${SCFUZZBENCH_ROOT}/run-heartbeat.json"
  local heartbeat_dest="s3://${SCFUZZBENCH_S3_BUCKET}/run-state/heartbeats/${SCFUZZBENCH_RUN_ID}/${SCFUZZBENCH_BENCHMARK_UUID}/${instance_token}.json"

  (
    set +e
    while true; do
      printf '{"run_id":"%s","benchmark_uuid":"%s","instance_id":"%s","observed_at_epoch":%s}\n' \
        "${SCFUZZBENCH_RUN_ID}" \
        "${SCFUZZBENCH_BENCHMARK_UUID}" \
        "${instance_token}" \
        "$(date +%s)" >"${heartbeat_file}"
      aws_cli s3 cp "${heartbeat_file}" "${heartbeat_dest}" \
        --only-show-errors --content-type application/json || true
      sleep "${interval}" || break
    done
  ) &
  printf '%s\n' "$!" >"${pid_file}"
}

start_aws_creds_refresher() {
  if [[ -n "${SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE:-}" ]]; then
    return 0
  fi
  if [[ -n "${SCFUZZBENCH_AWS_CREDS_REFRESH_PID:-}" ]] && kill -0 "${SCFUZZBENCH_AWS_CREDS_REFRESH_PID}" 2>/dev/null; then
    return 0
  fi

  local interval="${SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS:-300}"
  if [[ ! "${interval}" =~ ^[0-9]+$ ]] || (( interval < 60 )); then
    interval=300
  fi

  (
    set +e
    while true; do
      cache_aws_creds_from_imds >/dev/null 2>&1 || true
      sleep "${interval}" || true
    done
  ) &
  export SCFUZZBENCH_AWS_CREDS_REFRESH_PID=$!
}

get_github_token() {
  if [[ -n "${SCFUZZBENCH_GIT_TOKEN:-}" ]]; then
    echo "${SCFUZZBENCH_GIT_TOKEN}"
    return 0
  fi
  if [[ -n "${SCFUZZBENCH_GIT_TOKEN_SSM_PARAMETER:-}" ]]; then
    retry_cmd 5 10 aws_cli ssm get-parameter --with-decryption --name "${SCFUZZBENCH_GIT_TOKEN_SSM_PARAMETER}" \
      --query 'Parameter.Value' --output text
    return 0
  fi
  return 1
}

clone_target() {
  local clone_start
  clone_start=$(now_epoch_seconds)
  require_env SCFUZZBENCH_REPO_URL SCFUZZBENCH_COMMIT
  local repo_dir="${SCFUZZBENCH_WORKDIR}/target"
  local git_token=""
  local token_loaded=0

  get_git_token_cached() {
    if (( token_loaded )); then
      printf '%s' "${git_token}"
      return 0
    fi
    token_loaded=1
    git_token=$(get_github_token 2>/dev/null || true)
    printf '%s' "${git_token}"
  }

  token_clone_url() {
    local token
    token=$(get_git_token_cached)
    if [[ -z "${token}" ]]; then
      return 1
    fi
    if [[ "${SCFUZZBENCH_REPO_URL}" != https://* ]]; then
      return 1
    fi
    printf '%s' "https://x-access-token:${token}@${SCFUZZBENCH_REPO_URL#https://}"
    return 0
  }

  if [[ ! -d "${repo_dir}/.git" ]]; then
    rm -rf "${repo_dir}" || true
    log "Cloning ${SCFUZZBENCH_REPO_URL}"
    if ! GIT_TERMINAL_PROMPT=0 git clone "${SCFUZZBENCH_REPO_URL}" "${repo_dir}"; then
      local clone_url
      if clone_url=$(token_clone_url); then
        log "Unauthenticated clone failed; retrying with GitHub token."
        rm -rf "${repo_dir}" || true
        GIT_TERMINAL_PROMPT=0 git clone "${clone_url}" "${repo_dir}"
        git -C "${repo_dir}" remote set-url origin "${clone_url}"
      else
        log "Clone failed and no GitHub token is available."
        return 1
      fi
    fi
  fi

  pushd "${repo_dir}" >/dev/null

  if ! GIT_TERMINAL_PROMPT=0 git fetch --depth 1 origin "${SCFUZZBENCH_COMMIT}"; then
    # If origin is currently using a bad/expired token, public repos should still work without it.
    log "Fetch failed; retrying with public origin URL."
    git remote set-url origin "${SCFUZZBENCH_REPO_URL}" || true
    if ! GIT_TERMINAL_PROMPT=0 git fetch --depth 1 origin "${SCFUZZBENCH_COMMIT}"; then
      local clone_url
      if clone_url=$(token_clone_url); then
        log "Fetch failed; retrying with GitHub token."
        git remote set-url origin "${clone_url}"
        GIT_TERMINAL_PROMPT=0 git fetch --depth 1 origin "${SCFUZZBENCH_COMMIT}"
      else
        log "Fetch failed and no GitHub token is available."
        return 1
      fi
    fi
  fi

  git checkout "${SCFUZZBENCH_COMMIT}"

  if [[ -f .gitmodules ]]; then
    log "Initializing git submodules"

    # Normalize SSH/git URLs to https so public submodules don't require SSH keys.
    sed -i \
      -e 's#git@github.com:#https://github.com/#g' \
      -e 's#ssh://git@github.com/#https://github.com/#g' \
      -e 's#git://github.com/#https://github.com/#g' \
      .gitmodules || true
    git submodule sync --recursive || true

    if ! GIT_TERMINAL_PROMPT=0 git submodule update --init --recursive; then
      local token
      token=$(get_git_token_cached)
      if [[ -z "${token}" ]]; then
        log "Submodule init failed and no GitHub token is available."
        return 1
      fi
      log "Submodule init failed; retrying with GitHub token."
      git config --local --add url."https://x-access-token:${token}@github.com/".insteadOf "https://github.com/"
      git config --local --add url."https://x-access-token:${token}@github.com/".insteadOf "git@github.com:"
      git config --local --add url."https://x-access-token:${token}@github.com/".insteadOf "ssh://git@github.com/"
      git config --local --add url."https://x-access-token:${token}@github.com/".insteadOf "git://github.com/"
      git submodule sync --recursive
      GIT_TERMINAL_PROMPT=0 git -c url."https://x-access-token:${token}@github.com/".insteadOf="https://github.com/" \
        submodule update --init --recursive
    fi
  fi

  popd >/dev/null
  log_duration "clone_target" "${clone_start}"
}

apply_benchmark_type() {
  local mode_start
  mode_start=$(now_epoch_seconds)
  local repo_dir="${SCFUZZBENCH_WORKDIR}/target"
  local mode="${SCFUZZBENCH_BENCHMARK_TYPE}"
  local properties_path="${SCFUZZBENCH_PROPERTIES_PATH}"

  if [[ -z "${properties_path}" ]]; then
    log "SCFUZZBENCH_PROPERTIES_PATH not set; skipping benchmark mode switch."
    if [[ "${mode}" == "optimization" ]]; then
      log "Optimization mode requested, but SCFUZZBENCH_PROPERTIES_PATH is empty."
      return 1
    fi
    log_duration "apply_benchmark_type" "${mode_start}"
    return 0
  fi

  local properties_file="${repo_dir}/${properties_path}"

  if [[ ! -f "${properties_file}" ]]; then
    log "Properties.sol not found at ${properties_file}; skipping benchmark mode switch."
    if [[ "${mode}" == "optimization" ]]; then
      log "Optimization mode requested, but Properties.sol is missing."
      return 1
    fi
    log_duration "apply_benchmark_type" "${mode_start}"
    return 0
  fi

  if ! grep -q "OPTIMIZATION_MODE" "${properties_file}"; then
    log "OPTIMIZATION_MODE flag not found in Properties.sol; skipping benchmark mode switch."
    if [[ "${mode}" == "optimization" ]]; then
      log "Optimization mode requested, but Properties.sol does not support it."
      return 1
    fi
    log_duration "apply_benchmark_type" "${mode_start}"
    return 0
  fi

  case "${mode}" in
    property)
      if grep -q "OPTIMIZATION_MODE = true" "${properties_file}" || grep -q "public returns (int256 maxViolation)" "${properties_file}"; then
        log "Switching benchmark to property mode"
        sed -i \
          -e 's/OPTIMIZATION_MODE = true/OPTIMIZATION_MODE = false/' \
          -e 's/public returns (int256 maxViolation)/public returns (bool)/g' \
          -e 's/return maxViolation;/return maxViolation <= 0;/g' \
          -e 's/optimize_/invariant_/g' \
          "${properties_file}"
      else
        log "Benchmark already in property mode"
      fi
      ;;
    optimization)
      if grep -q "OPTIMIZATION_MODE = false" "${properties_file}" || grep -q "public returns (bool)" "${properties_file}"; then
        log "Switching benchmark to optimization mode"
        sed -i \
          -e 's/OPTIMIZATION_MODE = false/OPTIMIZATION_MODE = true/' \
          -e 's/public returns (bool)/public returns (int256 maxViolation)/g' \
          -e 's/return maxViolation <= 0;/return maxViolation;/g' \
          -e 's/invariant_/optimize_/g' \
          "${properties_file}"
      else
        log "Benchmark already in optimization mode"
      fi
      ;;
    *)
      log "Unknown SCFUZZBENCH_BENCHMARK_TYPE: ${mode} (expected property or optimization)"
      return 1
      ;;
  esac
  log_duration "apply_benchmark_type" "${mode_start}"
}

build_target() {
  local build_start
  build_start=$(now_epoch_seconds)
  local repo_dir="${SCFUZZBENCH_WORKDIR}/target"
  log "Building target with forge"
  pushd "${repo_dir}" >/dev/null
  if [[ ! -d "lib/forge-std" ]]; then
    log "Installing Foundry dependencies (forge install --no-commit)"
    forge install --no-commit || true
  fi
  forge build
  popd >/dev/null
  log_duration "build_target" "${build_start}"
}

run_with_timeout() {
  require_env SCFUZZBENCH_TIMEOUT_SECONDS
  local log_file=$1
  shift
  local run_start
  run_start=$(now_epoch_seconds)
  local kill_after="${SCFUZZBENCH_TIMEOUT_GRACE_SECONDS:-300}"
  if [[ ! "${kill_after}" =~ ^[0-9]+$ ]]; then
    kill_after=300
  fi
  append_runner_command_log "${SCFUZZBENCH_TIMEOUT_SECONDS}" "${kill_after}" "$@" || true
  start_preliminary_snapshots
  log "Running command with timeout ${SCFUZZBENCH_TIMEOUT_SECONDS}s (grace ${kill_after}s)"
  set +e
  timeout --signal=SIGINT --kill-after="${kill_after}s" "${SCFUZZBENCH_TIMEOUT_SECONDS}s" "$@" 2>&1 | tee "${log_file}"
  local exit_code=${PIPESTATUS[0]}
  set -e
  stop_preliminary_snapshots || true
  log_duration "run_with_timeout $(basename "${log_file}")" "${run_start}"
  if [[ "${exit_code}" -eq 124 ]]; then
    log "Command reached configured benchmark timeout; treating as completed run"
    return 0
  fi
  return ${exit_code}
}

# Git-pinned Foundry builds bake an empty `foundry_version` into the manifest,
# so the docs site had no version to show for the foundry leg. Patch the
# decoded manifest with the version forge itself reports (e.g. "1.3.6-dev")
# before upload; `foundry_git_ref` stays in the manifest as the exact pin.
# Every instance of a run resolves the same pin, so uploads stay identical.
resolve_manifest_foundry_version() {
  local manifest_path=$1
  command -v jq >/dev/null 2>&1 || return 0
  command -v forge >/dev/null 2>&1 || return 0
  [[ -f "${manifest_path}" ]] || return 0
  local manifest_version
  manifest_version=$(jq -r '.foundry_version // ""' "${manifest_path}" 2>/dev/null) || return 0
  if [[ -n "${manifest_version}" ]]; then
    return 0
  fi
  # `forge --version` first line is either "forge Version: 1.3.6-dev" (new)
  # or "forge 0.2.0 (abc1234 2024-01-01)" (old); keep just the version token.
  local resolved
  resolved=$(forge --version 2>/dev/null | head -n1 | sed -E 's/^forge[[:space:]]+(Version:[[:space:]]*)?//; s/[[:space:]]*\(.*$//')
  [[ -n "${resolved}" ]] || return 0
  log "Recording resolved foundry version in manifest: ${resolved}"
  jq --arg v "${resolved}" '.foundry_version = $v' "${manifest_path}" > "${manifest_path}.tmp" \
    && mv "${manifest_path}.tmp" "${manifest_path}"
}

record_seed_corpus_in_manifest() {
  local manifest_path=$1
  local metadata_path="${SCFUZZBENCH_SEED_CORPUS_METADATA_PATH:-${SCFUZZBENCH_LOG_DIR}/seed_corpus.json}"
  [[ -f "${manifest_path}" ]] || return 0
  if [[ ! -f "${metadata_path}" ]]; then
    if [[ -n "${SCFUZZBENCH_SEED_CORPUS_SOURCE:-}" ]]; then
      log "Configured seed corpus metadata is missing: ${metadata_path}"
      return 1
    fi
    return 0
  fi
  python3 - "${manifest_path}" "${metadata_path}" <<'PY'
import json
import os
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
metadata_path = Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text())
manifest["seed_corpus"] = json.loads(metadata_path.read_text())
temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
os.replace(temporary, manifest_path)
PY
}

upload_manifest_once_or_verify() {
  local manifest_path=$1
  local bucket=$2
  local key=$3
  local digest
  digest=$(sha256sum "${manifest_path}" | cut -d' ' -f1)
  local existing
  existing=$(mktemp "${SCFUZZBENCH_ROOT}/manifest-existing.XXXXXX")
  local attempt
  for attempt in 1 2 3 4 5; do
    if aws_cli s3api put-object \
      --bucket "${bucket}" \
      --key "${key}" \
      --body "${manifest_path}" \
      --content-type application/json \
      --metadata "sha256=${digest}" \
      --if-none-match '*' >/dev/null 2>&1; then
      rm -f -- "${existing}"
      return 0
    fi
    rm -f -- "${existing}"
    if aws_cli s3api get-object \
      --bucket "${bucket}" \
      --key "${key}" \
      "${existing}" >/dev/null 2>&1; then
      if cmp -s -- "${manifest_path}" "${existing}"; then
        rm -f -- "${existing}"
        return 0
      fi
      rm -f -- "${existing}"
      log "Refusing to overwrite a different benchmark manifest at s3://${bucket}/${key}"
      return 1
    fi
    sleep $((attempt * 2))
  done
  rm -f -- "${existing}"
  log "Could not create or verify benchmark manifest at s3://${bucket}/${key}"
  return 1
}

upload_results() {
  local upload_start
  upload_start=$(now_epoch_seconds)
  if is_local_mode; then
    save_results_local
    log_duration "upload_results_local" "${upload_start}"
    return $?
  fi
  require_env SCFUZZBENCH_S3_BUCKET SCFUZZBENCH_RUN_ID SCFUZZBENCH_FUZZER_LABEL
  stop_runner_metrics || true
  cache_instance_id || true
  local instance_id="${SCFUZZBENCH_INSTANCE_ID:-unknown}"
  local base_name="${instance_id}-${SCFUZZBENCH_FUZZER_LABEL}"
  local upload_dir="${SCFUZZBENCH_ROOT}/upload"
  mkdir -p "${upload_dir}"
  local log_zip="${upload_dir}/logs-${base_name}.zip"
  local prefix="${SCFUZZBENCH_RUN_ID}"
  local manifest_upload_ok=1
  if [[ -n "${SCFUZZBENCH_BENCHMARK_UUID}" ]]; then
    # New layout: logs/<run_id>/<benchmark_uuid>/...
    prefix="${SCFUZZBENCH_RUN_ID}/${SCFUZZBENCH_BENCHMARK_UUID}"
  fi

  if [[ -n "${SCFUZZBENCH_BENCHMARK_MANIFEST_B64}" ]]; then
    local manifest_path="${upload_dir}/benchmark_manifest.json"
    echo "${SCFUZZBENCH_BENCHMARK_MANIFEST_B64}" | base64 -d > "${manifest_path}"
    resolve_manifest_foundry_version "${manifest_path}" || true
    if ! record_seed_corpus_in_manifest "${manifest_path}"; then
      manifest_upload_ok=0
    elif ! upload_manifest_once_or_verify \
      "${manifest_path}" \
      "${SCFUZZBENCH_S3_BUCKET}" \
      "logs/${prefix}/manifest.json"; then
      manifest_upload_ok=0
    fi

    # Run-identity-first discovery index for the docs site:
    # runs/<run_id>/<benchmark_uuid>/manifest.json
    if [[ -n "${SCFUZZBENCH_BENCHMARK_UUID}" && "${SCFUZZBENCH_RUN_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
      if (( manifest_upload_ok )) && ! upload_manifest_once_or_verify \
        "${manifest_path}" \
        "${SCFUZZBENCH_S3_BUCKET}" \
        "runs/${SCFUZZBENCH_RUN_ID}/${SCFUZZBENCH_BENCHMARK_UUID}/manifest.json"; then
        manifest_upload_ok=0
      fi
    else
      log "Skipping docs index upload; missing benchmark UUID or unsafe run id."
    fi
  fi

  local log_dest="s3://${SCFUZZBENCH_S3_BUCKET}/logs/${prefix}/${base_name}.zip"
  if [[ -d "${SCFUZZBENCH_LOG_DIR}" ]]; then
    log "Zipping logs to ${log_zip}"
    local log_parent
    local log_base
    log_parent=$(dirname "${SCFUZZBENCH_LOG_DIR}")
    log_base=$(basename "${SCFUZZBENCH_LOG_DIR}")
    (cd "${log_parent}" && zip -r -q "${log_zip}" "${log_base}")
    log "Uploading logs to ${log_dest}"
    retry_cmd 5 60 aws_cli s3 cp "${log_zip}" "${log_dest}" --no-progress
  else
    log "No logs directory found; skipping log upload."
  fi

  if [[ -n "${SCFUZZBENCH_CORPUS_DIR}" && -d "${SCFUZZBENCH_CORPUS_DIR}" ]]; then
    local corpus_zip="${upload_dir}/corpus-${base_name}.zip"
    local corpus_dest="s3://${SCFUZZBENCH_S3_BUCKET}/corpus/${prefix}/${base_name}.zip"
    log "Zipping corpus to ${corpus_zip}"
    local corpus_parent
    local corpus_base
    corpus_parent=$(dirname "${SCFUZZBENCH_CORPUS_DIR}")
    corpus_base=$(basename "${SCFUZZBENCH_CORPUS_DIR}")
    (cd "${corpus_parent}" && zip -r -q "${corpus_zip}" "${corpus_base}")
    log "Uploading corpus to ${corpus_dest}"
    retry_cmd 5 60 aws_cli s3 cp "${corpus_zip}" "${corpus_dest}" --no-progress
  else
    log "No corpus directory configured or found; skipping corpus upload."
  fi

  export SCFUZZBENCH_UPLOAD_DONE=1
  log_duration "upload_results" "${upload_start}"
  if (( ! manifest_upload_ok )); then
    log "Artifacts uploaded, but benchmark manifest creation or verification failed"
    return 1
  fi
}

save_results_local() {
  local save_start
  save_start=$(now_epoch_seconds)
  stop_runner_metrics || true
  cache_instance_id || true
  local fuzzer_label="${SCFUZZBENCH_FUZZER_LABEL:-unknown}"
  local repo_name
  repo_name=$(basename "${SCFUZZBENCH_REPO_URL:-unknown}" .git)
  local timestamp
  timestamp=$(date +%Y-%m-%dT%H-%M-%S)
  local run_dir="${repo_name}/${fuzzer_label}/${timestamp}"
  local output_dir="${SCFUZZBENCH_LOCAL_OUTPUT_DIR:-${SCFUZZBENCH_ROOT}/output}/${run_dir}"
  mkdir -p "${output_dir}"

  if [[ -n "${SCFUZZBENCH_BENCHMARK_MANIFEST_B64:-}" ]]; then
    local manifest_path="${output_dir}/benchmark_manifest.json"
    echo "${SCFUZZBENCH_BENCHMARK_MANIFEST_B64}" | base64 -d > "${manifest_path}"
    resolve_manifest_foundry_version "${manifest_path}" || true
    record_seed_corpus_in_manifest "${manifest_path}"
  fi

  if [[ -d "${SCFUZZBENCH_LOG_DIR}" ]]; then
    local log_zip="${output_dir}/logs.zip"
    local log_parent log_base
    log_parent=$(dirname "${SCFUZZBENCH_LOG_DIR}")
    log_base=$(basename "${SCFUZZBENCH_LOG_DIR}")
    (cd "${log_parent}" && zip -r -q "${log_zip}" "${log_base}")
    log "Logs saved to ${log_zip}"
  fi

  if [[ -n "${SCFUZZBENCH_CORPUS_DIR:-}" && -d "${SCFUZZBENCH_CORPUS_DIR}" ]]; then
    local corpus_zip="${output_dir}/corpus.zip"
    local corpus_parent corpus_base
    corpus_parent=$(dirname "${SCFUZZBENCH_CORPUS_DIR}")
    corpus_base=$(basename "${SCFUZZBENCH_CORPUS_DIR}")
    (cd "${corpus_parent}" && zip -r -q "${corpus_zip}" "${corpus_base}")
    log "Corpus saved to ${corpus_zip}"
  fi

  log "Results saved to ${output_dir}"
  export SCFUZZBENCH_UPLOAD_DONE=1
  log_duration "save_results_local" "${save_start}"
}
