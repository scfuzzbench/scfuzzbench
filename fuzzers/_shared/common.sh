#!/usr/bin/env bash
set -euo pipefail

SCFUZZBENCH_ROOT=${SCFUZZBENCH_ROOT:-/opt/scfuzzbench}
SCFUZZBENCH_WORKDIR=${SCFUZZBENCH_WORKDIR:-${SCFUZZBENCH_ROOT}/work}
SCFUZZBENCH_LOG_DIR=${SCFUZZBENCH_LOG_DIR:-${SCFUZZBENCH_ROOT}/logs}
SCFUZZBENCH_CORPUS_DIR=${SCFUZZBENCH_CORPUS_DIR:-}
SCFUZZBENCH_BENCHMARK_TYPE=${SCFUZZBENCH_BENCHMARK_TYPE:-property}
SCFUZZBENCH_BENCHMARK_UUID=${SCFUZZBENCH_BENCHMARK_UUID:-}
SCFUZZBENCH_BENCHMARK_MANIFEST_B64=${SCFUZZBENCH_BENCHMARK_MANIFEST_B64:-}
SCFUZZBENCH_PROPERTIES_PATH=${SCFUZZBENCH_PROPERTIES_PATH:-}
SCFUZZBENCH_RUNNER_METRICS=${SCFUZZBENCH_RUNNER_METRICS:-1}
SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS=${SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS:-5}

SCFUZZBENCH_AWS_CREDS_ENV_FILE=${SCFUZZBENCH_AWS_CREDS_ENV_FILE:-${SCFUZZBENCH_ROOT}/aws_creds.env}
SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS=${SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS:-300}

log() {
  # Use stderr so command substitutions can safely capture stdout.
  echo "[$(date -Is)] $*" >&2
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

install_shutdown_script() {
  local shutdown_path="${SCFUZZBENCH_ROOT}/shutdown.sh"
  if [[ -f "${shutdown_path}" ]]; then
    return 0
  fi
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
  chmod +x "${shutdown_path}"
}

shutdown_instance() {
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

finalize_run() {
  local exit_code=$?
  set +e
  stop_runner_metrics || true
  if [[ -z "${SCFUZZBENCH_UPLOAD_DONE:-}" ]]; then
    if [[ -n "${SCFUZZBENCH_S3_BUCKET:-}" && -n "${SCFUZZBENCH_RUN_ID:-}" && -n "${SCFUZZBENCH_FUZZER_LABEL:-}" ]]; then
      upload_results || true
    else
      log "Skipping upload in finalize; missing S3 bucket, run id, or fuzzer label."
    fi
  fi
  shutdown_instance
  return ${exit_code}
}

register_shutdown_trap() {
  install_shutdown_script
  cache_instance_id || true
  if [[ -z "${SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE:-}" ]]; then
    cache_aws_creds_from_imds || true
    start_aws_creds_refresher || true
  fi
  start_runner_metrics
  trap finalize_run EXIT
}

install_base_packages() {
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
}

install_foundry() {
  if [[ -n "${FOUNDRY_GIT_REPO:-}" ]]; then
    log "Installing Foundry from ${FOUNDRY_GIT_REPO}"
    export HOME=/root
    if ! command -v cargo >/dev/null 2>&1; then
      log "Installing Rust toolchain"
      curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
      # shellcheck source=/dev/null
      source /root/.cargo/env
    fi
    local tmp_dir
    tmp_dir=$(mktemp -d)
    git clone --depth 1 "${FOUNDRY_GIT_REPO}" "${tmp_dir}/foundry"
    if [[ -n "${FOUNDRY_GIT_REF:-}" ]]; then
      git -C "${tmp_dir}/foundry" fetch --depth 1 origin "${FOUNDRY_GIT_REF}"
      git -C "${tmp_dir}/foundry" checkout "${FOUNDRY_GIT_REF}"
    fi
    local commit
    commit=$(git -C "${tmp_dir}/foundry" rev-parse --short HEAD)
    log "Building Foundry at ${commit}"
    # shellcheck source=/dev/null
    source /root/.cargo/env
    cargo build --profile maxperf --manifest-path "${tmp_dir}/foundry/Cargo.toml"
    install -m 0755 "${tmp_dir}/foundry/target/maxperf/forge" /usr/local/bin/forge
    install -m 0755 "${tmp_dir}/foundry/target/maxperf/cast" /usr/local/bin/cast
    install -m 0755 "${tmp_dir}/foundry/target/maxperf/anvil" /usr/local/bin/anvil
    install -m 0755 "${tmp_dir}/foundry/target/maxperf/chisel" /usr/local/bin/chisel || true
    echo "${commit}" > /opt/scfuzzbench/foundry_commit
    echo "${FOUNDRY_GIT_REPO}" > /opt/scfuzzbench/foundry_repo
    rm -rf "${tmp_dir}"
    forge --version
  else
    require_env FOUNDRY_VERSION
    log "Installing Foundry ${FOUNDRY_VERSION}"
    export HOME=/root
    curl -L https://foundry.paradigm.xyz | bash
    export PATH="/root/.foundry/bin:${PATH}"
    /root/.foundry/bin/foundryup -i "${FOUNDRY_VERSION}"
    forge --version
  fi
}

install_crytic_compile() {
  log "Installing crytic-compile"
  python3 -m pip install --no-cache-dir --break-system-packages crytic-compile
  command -v crytic-compile
}

install_slither_analyzer() {
  log "Installing slither-analyzer"
  python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed slither-analyzer
  command -v slither
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
}

apply_benchmark_type() {
  local repo_dir="${SCFUZZBENCH_WORKDIR}/target"
  local mode="${SCFUZZBENCH_BENCHMARK_TYPE}"
  local properties_path="${SCFUZZBENCH_PROPERTIES_PATH}"

  if [[ -z "${properties_path}" ]]; then
    log "SCFUZZBENCH_PROPERTIES_PATH not set; skipping benchmark mode switch."
    if [[ "${mode}" == "optimization" ]]; then
      log "Optimization mode requested, but SCFUZZBENCH_PROPERTIES_PATH is empty."
      return 1
    fi
    return 0
  fi

  local properties_file="${repo_dir}/${properties_path}"

  if [[ ! -f "${properties_file}" ]]; then
    log "Properties.sol not found at ${properties_file}; skipping benchmark mode switch."
    if [[ "${mode}" == "optimization" ]]; then
      log "Optimization mode requested, but Properties.sol is missing."
      return 1
    fi
    return 0
  fi

  if ! grep -q "OPTIMIZATION_MODE" "${properties_file}"; then
    log "OPTIMIZATION_MODE flag not found in Properties.sol; skipping benchmark mode switch."
    if [[ "${mode}" == "optimization" ]]; then
      log "Optimization mode requested, but Properties.sol does not support it."
      return 1
    fi
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
}

build_target() {
  local repo_dir="${SCFUZZBENCH_WORKDIR}/target"
  log "Building target with forge"
  pushd "${repo_dir}" >/dev/null
  if [[ ! -d "lib/forge-std" ]]; then
    log "Installing Foundry dependencies (forge install --no-commit)"
    forge install --no-commit || true
  fi
  forge build
  popd >/dev/null
}

run_with_timeout() {
  require_env SCFUZZBENCH_TIMEOUT_SECONDS
  local log_file=$1
  shift
  local kill_after="${SCFUZZBENCH_TIMEOUT_GRACE_SECONDS:-300}"
  if [[ ! "${kill_after}" =~ ^[0-9]+$ ]]; then
    kill_after=300
  fi
  append_runner_command_log "${SCFUZZBENCH_TIMEOUT_SECONDS}" "${kill_after}" "$@" || true
  log "Running command with timeout ${SCFUZZBENCH_TIMEOUT_SECONDS}s (grace ${kill_after}s)"
  set +e
  timeout --signal=SIGINT --kill-after="${kill_after}s" "${SCFUZZBENCH_TIMEOUT_SECONDS}s" "$@" 2>&1 | tee "${log_file}"
  local exit_code=${PIPESTATUS[0]}
  set -e
  return ${exit_code}
}

upload_results() {
  require_env SCFUZZBENCH_S3_BUCKET SCFUZZBENCH_RUN_ID SCFUZZBENCH_FUZZER_LABEL
  stop_runner_metrics || true
  cache_instance_id || true
  local instance_id="${SCFUZZBENCH_INSTANCE_ID:-unknown}"
  local base_name="${instance_id}-${SCFUZZBENCH_FUZZER_LABEL}"
  local upload_dir="${SCFUZZBENCH_ROOT}/upload"
  mkdir -p "${upload_dir}"
  local log_zip="${upload_dir}/logs-${base_name}.zip"
  local prefix="${SCFUZZBENCH_RUN_ID}"
  if [[ -n "${SCFUZZBENCH_BENCHMARK_UUID}" ]]; then
    # New layout: logs/<run_id>/<benchmark_uuid>/...
    prefix="${SCFUZZBENCH_RUN_ID}/${SCFUZZBENCH_BENCHMARK_UUID}"
  fi

  if [[ -n "${SCFUZZBENCH_BENCHMARK_MANIFEST_B64}" ]]; then
    local manifest_path="${upload_dir}/benchmark_manifest.json"
    echo "${SCFUZZBENCH_BENCHMARK_MANIFEST_B64}" | base64 -d > "${manifest_path}"
    retry_cmd 5 60 aws_cli s3 cp "${manifest_path}" "s3://${SCFUZZBENCH_S3_BUCKET}/logs/${prefix}/manifest.json" --no-progress

    # Timestamp-first discovery index for the docs site:
    # runs/<run_id>/<benchmark_uuid>/manifest.json
    if [[ -n "${SCFUZZBENCH_BENCHMARK_UUID}" && "${SCFUZZBENCH_RUN_ID}" =~ ^[0-9]+$ ]]; then
      local index_dest="s3://${SCFUZZBENCH_S3_BUCKET}/runs/${SCFUZZBENCH_RUN_ID}/${SCFUZZBENCH_BENCHMARK_UUID}/manifest.json"
      retry_cmd 5 60 aws_cli s3 cp "${manifest_path}" "${index_dest}" --no-progress
    else
      log "Skipping docs index upload; missing benchmark UUID or non-numeric run id."
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
}
