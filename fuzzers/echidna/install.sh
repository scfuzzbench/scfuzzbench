#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

# Keep a local-mode token in shell memory only; do not let unrelated bootstrap
# subprocesses inherit it from the caller's environment.
ECHIDNA_CI_LOCAL_TOKEN=""
if [[ "${ECHIDNA_CI_TOKEN+x}" == "x" ]]; then
  set +x
  ECHIDNA_CI_LOCAL_TOKEN="${ECHIDNA_CI_TOKEN}"
  unset ECHIDNA_CI_TOKEN
fi

prepare_workspace
install_base_packages
install_foundry
install_crytic_compile
install_slither_analyzer

install_echidna_ci_artifact() {
  require_env \
    ECHIDNA_CI_REPO \
    ECHIDNA_CI_RUN_ID \
    ECHIDNA_CI_ARTIFACT_NAME \
    ECHIDNA_CI_ARTIFACT_SHA256 \
    ECHIDNA_CI_COMMIT

  if [[ ! "${ECHIDNA_CI_REPO}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?/?$ ]]; then
    log "ECHIDNA_CI_REPO must be https://github.com/<org>/<repo>"
    return 1
  fi
  if [[ ! "${ECHIDNA_CI_RUN_ID}" =~ ^[1-9][0-9]*$ ]]; then
    log "ECHIDNA_CI_RUN_ID must be a positive GitHub Actions run ID"
    return 1
  fi
  if [[ ! "${ECHIDNA_CI_ARTIFACT_NAME}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    log "ECHIDNA_CI_ARTIFACT_NAME contains unsupported characters"
    return 1
  fi
  if [[ ! "${ECHIDNA_CI_ARTIFACT_NAME,,}" =~ linux ]]; then
    log "ECHIDNA_CI_ARTIFACT_NAME must identify a Linux artifact"
    return 1
  fi
  if [[ ! "${ECHIDNA_CI_ARTIFACT_SHA256}" =~ ^[A-Fa-f0-9]{64}$ ]]; then
    log "ECHIDNA_CI_ARTIFACT_SHA256 must be a 64-character SHA-256 digest"
    return 1
  fi
  if [[ ! "${ECHIDNA_CI_COMMIT}" =~ ^[A-Fa-f0-9]{40}$ ]]; then
    log "ECHIDNA_CI_COMMIT must be a full 40-character commit SHA"
    return 1
  fi

  command -v jq >/dev/null || {
    log "jq is required for Echidna CI artifact metadata validation"
    return 1
  }
  local max_bytes="${ECHIDNA_CI_MAX_BYTES:-536870912}"
  if [[ ! "${max_bytes}" =~ ^[1-9][0-9]*$ ]] || (( max_bytes > 1073741824 )); then
    log "ECHIDNA_CI_MAX_BYTES must be a positive integer no larger than 1073741824"
    return 1
  fi

  local tmp_dir
  tmp_dir=$(mktemp -d)
  local auth_config="${tmp_dir}/curl-auth.conf"
  ECHIDNA_CI_TMP_DIR_CLEANUP="${tmp_dir}"
  ECHIDNA_CI_AUTH_CONFIG_CLEANUP="${auth_config}"
  cleanup_echidna_ci() {
    [[ -z "${ECHIDNA_CI_AUTH_CONFIG_CLEANUP:-}" ]] || rm -f "${ECHIDNA_CI_AUTH_CONFIG_CLEANUP}"
    [[ -z "${ECHIDNA_CI_TMP_DIR_CLEANUP:-}" ]] || rm -rf "${ECHIDNA_CI_TMP_DIR_CLEANUP}"
  }
  trap cleanup_echidna_ci EXIT

  # Never allow an inherited xtrace setting to expose the token. Cloud runs
  # fetch only the named SecureString at boot; the secret is not in Terraform
  # variables, state, user-data, or command-line arguments.
  set +x
  local github_token=""
  if [[ -n "${ECHIDNA_CI_LOCAL_TOKEN:-}" ]]; then
    github_token="${ECHIDNA_CI_LOCAL_TOKEN}"
  elif [[ -n "${ECHIDNA_CI_TOKEN_SSM_PARAMETER:-}" ]]; then
    local parameter_json=""
    parameter_json=$(aws_cli ssm get-parameter \
      --with-decryption \
      --name "${ECHIDNA_CI_TOKEN_SSM_PARAMETER}" \
      --output json)
    if [[ "$(jq -r '.Parameter.Type // ""' <<<"${parameter_json}")" != "SecureString" ]] \
      || [[ "$(jq -r '.Parameter.Name // ""' <<<"${parameter_json}")" != "${ECHIDNA_CI_TOKEN_SSM_PARAMETER}" ]]; then
      unset parameter_json
      log "Echidna CI token parameter must be the exact requested SecureString"
      return 1
    fi
    github_token=$(jq -r '.Parameter.Value // ""' <<<"${parameter_json}")
    unset parameter_json
  else
    log "Echidna CI artifact mode requires ECHIDNA_CI_TOKEN locally or ECHIDNA_CI_TOKEN_SSM_PARAMETER in cloud runs"
    return 1
  fi
  if [[ -z "${github_token}" || ! "${github_token}" =~ ^[A-Za-z0-9_]+$ ]]; then
    unset github_token ECHIDNA_CI_LOCAL_TOKEN
    log "Echidna CI token is empty or has an unexpected format"
    return 1
  fi

  umask 077
  {
    printf 'header = "Accept: application/vnd.github+json"\n'
    printf 'header = "X-GitHub-Api-Version: 2022-11-28"\n'
    printf 'header = "Authorization: Bearer %s"\n' "${github_token}"
  } >"${auth_config}"
  chmod 0600 "${auth_config}"
  unset github_token ECHIDNA_CI_LOCAL_TOKEN

  local repo_path="${ECHIDNA_CI_REPO#https://github.com/}"
  repo_path="${repo_path%.git}"
  repo_path="${repo_path%/}"
  local api_base="https://api.github.com/repos/${repo_path}"
  local run_json="${tmp_dir}/run.json"
  local artifacts_json="${tmp_dir}/artifacts.json"
  local artifact_zip="${tmp_dir}/artifact.zip"

  log "Validating Echidna CI run ${ECHIDNA_CI_RUN_ID} in ${ECHIDNA_CI_REPO}"
  retry_cmd 3 5 curl --config "${auth_config}" --fail --silent --show-error --location \
    "${api_base}/actions/runs/${ECHIDNA_CI_RUN_ID}" \
    --output "${run_json}"
  retry_cmd 3 5 curl --config "${auth_config}" --fail --silent --show-error --location \
    "${api_base}/actions/runs/${ECHIDNA_CI_RUN_ID}/artifacts?name=${ECHIDNA_CI_ARTIFACT_NAME}&per_page=100" \
    --output "${artifacts_json}"

  local expected_commit="${ECHIDNA_CI_COMMIT,,}"
  local resolved_commit
  resolved_commit=$(jq -r '.head_sha // "" | ascii_downcase' "${run_json}")
  local resolved_repo
  resolved_repo=$(jq -r '.repository.full_name // "" | ascii_downcase' "${run_json}")
  if [[ "${resolved_repo}" != "${repo_path,,}" ]]; then
    log "GitHub run repository mismatch: expected ${repo_path}, got ${resolved_repo:-missing}"
    return 1
  fi
  if [[ "${resolved_commit}" != "${expected_commit}" ]]; then
    log "GitHub run commit drift: expected ${expected_commit}, got ${resolved_commit:-missing}"
    return 1
  fi
  if [[ "$(jq -r '.status // ""' "${run_json}")" != "completed" ]] \
    || [[ "$(jq -r '.conclusion // ""' "${run_json}")" != "success" ]]; then
    log "GitHub run ${ECHIDNA_CI_RUN_ID} is not a completed successful run"
    return 1
  fi

  local artifact_count
  artifact_count=$(jq --arg name "${ECHIDNA_CI_ARTIFACT_NAME}" \
    '[.artifacts[]? | select(.name == $name)] | length' "${artifacts_json}")
  if [[ "${artifact_count}" != "1" ]]; then
    log "Expected exactly one artifact named ${ECHIDNA_CI_ARTIFACT_NAME}; found ${artifact_count}"
    return 1
  fi

  local artifact_id artifact_digest artifact_expired artifact_expires_at artifact_size artifact_commit
  artifact_id=$(jq -r --arg name "${ECHIDNA_CI_ARTIFACT_NAME}" \
    '.artifacts[] | select(.name == $name) | .id' "${artifacts_json}")
  artifact_digest=$(jq -r --arg name "${ECHIDNA_CI_ARTIFACT_NAME}" \
    '.artifacts[] | select(.name == $name) | .digest // "" | ascii_downcase' "${artifacts_json}")
  artifact_expired=$(jq -r --arg name "${ECHIDNA_CI_ARTIFACT_NAME}" \
    '.artifacts[] | select(.name == $name) | .expired' "${artifacts_json}")
  artifact_expires_at=$(jq -r --arg name "${ECHIDNA_CI_ARTIFACT_NAME}" \
    '.artifacts[] | select(.name == $name) | .expires_at // ""' "${artifacts_json}")
  artifact_size=$(jq -r --arg name "${ECHIDNA_CI_ARTIFACT_NAME}" \
    '.artifacts[] | select(.name == $name) | .size_in_bytes // 0' "${artifacts_json}")
  artifact_commit=$(jq -r --arg name "${ECHIDNA_CI_ARTIFACT_NAME}" \
    '.artifacts[] | select(.name == $name) | .workflow_run.head_sha // "" | ascii_downcase' "${artifacts_json}")

  if [[ ! "${artifact_id}" =~ ^[1-9][0-9]*$ ]]; then
    log "Echidna CI artifact metadata is missing a positive numeric ID"
    return 1
  fi
  if [[ "${artifact_expired}" != "false" ]]; then
    log "Echidna CI artifact ${ECHIDNA_CI_ARTIFACT_NAME} is expired"
    return 1
  fi
  if [[ -z "${artifact_expires_at}" ]] || ! python3 - "${artifact_expires_at}" <<'PY'
import datetime as dt
import sys

try:
    expiry = dt.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if expiry > dt.datetime.now(dt.timezone.utc) else 1)
PY
  then
    log "Echidna CI artifact ${ECHIDNA_CI_ARTIFACT_NAME} has an invalid or elapsed expiry"
    return 1
  fi
  if [[ "${artifact_commit}" != "${expected_commit}" ]]; then
    log "Artifact commit mismatch: expected ${expected_commit}, got ${artifact_commit:-missing}"
    return 1
  fi
  local expected_digest="sha256:${ECHIDNA_CI_ARTIFACT_SHA256,,}"
  if [[ "${artifact_digest}" != "${expected_digest}" ]]; then
    log "Artifact API digest mismatch: expected ${expected_digest}, got ${artifact_digest:-missing}"
    return 1
  fi
  if [[ ! "${artifact_size}" =~ ^[0-9]+$ ]] || (( artifact_size <= 0 || artifact_size > max_bytes )); then
    log "Artifact metadata size ${artifact_size} is outside the allowed range"
    return 1
  fi

  log "Downloading verified Echidna CI artifact ${ECHIDNA_CI_ARTIFACT_NAME} (${artifact_id})"
  retry_cmd 3 5 curl --config "${auth_config}" --fail --silent --show-error --location \
    --max-filesize "${max_bytes}" \
    "${api_base}/actions/artifacts/${artifact_id}/zip" \
    --output "${artifact_zip}"

  # Delete authentication material before inspecting or executing downloaded
  # content. The EXIT trap remains as a defense for every earlier error path.
  rm -f "${auth_config}"
  local downloaded_size
  downloaded_size=$(stat -c '%s' "${artifact_zip}")
  if (( downloaded_size <= 0 || downloaded_size > max_bytes )); then
    log "Downloaded artifact size ${downloaded_size} is outside the allowed range"
    return 1
  fi
  local downloaded_digest
  downloaded_digest=$(sha256sum "${artifact_zip}" | awk '{print tolower($1)}')
  if [[ "${downloaded_digest}" != "${ECHIDNA_CI_ARTIFACT_SHA256,,}" ]]; then
    log "Downloaded artifact SHA-256 mismatch"
    return 1
  fi

  local extractor="${ECHIDNA_CI_EXTRACTOR:-}"
  if [[ -z "${extractor}" ]]; then
    if is_local_mode; then
      extractor="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/extract_ci_artifact.py"
    else
      extractor="/opt/scfuzzbench/extract_echidna_ci_artifact.py"
    fi
  fi
  if [[ ! -f "${extractor}" ]]; then
    log "Echidna CI artifact extractor not found at ${extractor}"
    return 1
  fi

  local binary_path
  binary_path=$(python3 "${extractor}" \
    --artifact "${artifact_zip}" \
    --destination "${tmp_dir}/extracted" \
    --max-bytes "${max_bytes}")

  install -m 0755 "${binary_path}" "${SCFUZZBENCH_BIN_DIR}/echidna"
  rm -f -- "${SCFUZZBENCH_BIN_DIR}/echidna-test"
  command -v echidna >/dev/null

  local binary_digest binary_version
  binary_digest=$(sha256sum "${SCFUZZBENCH_BIN_DIR}/echidna" | awk '{print tolower($1)}')
  binary_version=$(echidna --version 2>&1 | head -n 1 || true)
  if [[ -z "${binary_version}" ]]; then
    log "Installed Echidna CI binary did not report a version"
    return 1
  fi

  printf '%s\n' "${expected_commit}" >"${SCFUZZBENCH_ROOT}/echidna_ci_commit"
  jq -n \
    --arg tool "echidna" \
    --arg install_mode "github-actions-artifact" \
    --arg repo "${ECHIDNA_CI_REPO}" \
    --arg run_id "${ECHIDNA_CI_RUN_ID}" \
    --arg commit "${expected_commit}" \
    --arg artifact_id "${artifact_id}" \
    --arg artifact_name "${ECHIDNA_CI_ARTIFACT_NAME}" \
    --arg artifact_sha256 "${downloaded_digest}" \
    --arg artifact_api_digest "${artifact_digest}" \
    --arg artifact_expires_at "${artifact_expires_at}" \
    --arg os "linux" \
    --arg arch "x86_64" \
    --arg binary_sha256 "${binary_digest}" \
    --arg binary_version "${binary_version}" \
    '{
      schema_version: 1,
      tool: $tool,
      install_mode: $install_mode,
      repository: $repo,
      workflow_run_id: $run_id,
      commit: $commit,
      artifact: {
        id: $artifact_id,
        name: $artifact_name,
        sha256: $artifact_sha256,
        api_digest: $artifact_api_digest,
        expires_at: $artifact_expires_at
      },
      platform: {os: $os, arch: $arch},
      binary: {name: "echidna", sha256: $binary_sha256, version: $binary_version}
    }' >"${SCFUZZBENCH_LOG_DIR}/tool_provenance.json"

  log "Installed Echidna CI commit ${expected_commit} with binary SHA-256 ${binary_digest}"
  cleanup_echidna_ci
  trap - EXIT
  unset ECHIDNA_CI_TMP_DIR_CLEANUP ECHIDNA_CI_AUTH_CONFIG_CLEANUP
}

if [[ -n "${ECHIDNA_CI_REPO:-}" ]]; then
  install_echidna_ci_artifact
  exit 0
fi

require_env ECHIDNA_VERSION
log "Installing stable Echidna ${ECHIDNA_VERSION}"

tmp_dir=$(mktemp -d)
archive="echidna-${ECHIDNA_VERSION}-x86_64-linux.tar.gz"
url="https://github.com/crytic/echidna/releases/download/v${ECHIDNA_VERSION}/${archive}"

curl -L "${url}" -o "${tmp_dir}/${archive}"
mkdir -p "${tmp_dir}/echidna"
tar -xzf "${tmp_dir}/${archive}" -C "${tmp_dir}/echidna"

bin_path=$(find "${tmp_dir}/echidna" -type f \( -name "echidna-test" -o -name "echidna" \) | head -n 1)
if [[ -z "${bin_path}" ]]; then
  log "echidna binary not found in archive"
  exit 1
fi
install -m 0755 "${bin_path}" "${SCFUZZBENCH_BIN_DIR}/echidna-test"

rm -rf "${tmp_dir}"

command -v echidna-test
