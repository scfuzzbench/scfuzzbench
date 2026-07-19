#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

prepare_workspace
install_base_packages
install_foundry
install_crytic_compile
install_slither_analyzer

install_medusa_from_source() {
  require_env \
    MEDUSA_GIT_REPO \
    MEDUSA_GIT_REF \
    MEDUSA_GIT_COMMIT \
    MEDUSA_GO_VERSION \
    MEDUSA_GO_SHA256

  if [[ ! "${MEDUSA_GIT_REPO}" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(\.git)?/?$ ]]; then
    log "MEDUSA_GIT_REPO must be https://github.com/<org>/<repo>"
    return 1
  fi
  if [[ ! "${MEDUSA_GIT_REF}" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    log "MEDUSA_GIT_REF contains unsupported characters"
    return 1
  fi
  if [[ ! "${MEDUSA_GIT_COMMIT}" =~ ^[A-Fa-f0-9]{40}$ ]]; then
    log "MEDUSA_GIT_COMMIT must be a full 40-character commit SHA"
    return 1
  fi
  if [[ ! "${MEDUSA_GO_VERSION}" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
    log "MEDUSA_GO_VERSION must look like 1.24.0"
    return 1
  fi
  if [[ ! "${MEDUSA_GO_SHA256}" =~ ^[A-Fa-f0-9]{64}$ ]]; then
    log "MEDUSA_GO_SHA256 must be a 64-character SHA-256 digest"
    return 1
  fi
  command -v jq >/dev/null || {
    log "jq is required to record Medusa source provenance"
    return 1
  }

  local tmp_dir
  tmp_dir=$(mktemp -d)
  MEDUSA_SOURCE_TMP_DIR_CLEANUP="${tmp_dir}"
  cleanup_medusa_source() {
    [[ -z "${MEDUSA_SOURCE_TMP_DIR_CLEANUP:-}" ]] || rm -rf "${MEDUSA_SOURCE_TMP_DIR_CLEANUP}"
  }
  trap cleanup_medusa_source EXIT

  local go_archive="${tmp_dir}/go.tar.gz"
  local go_max_bytes=209715200
  local go_url="https://go.dev/dl/go${MEDUSA_GO_VERSION}.linux-amd64.tar.gz"
  log "Installing verified Go ${MEDUSA_GO_VERSION} for Medusa source build"
  retry_cmd 3 5 curl --fail --silent --show-error --location \
    --max-filesize "${go_max_bytes}" \
    "${go_url}" \
    --output "${go_archive}"
  local go_archive_size
  go_archive_size=$(stat -c '%s' "${go_archive}")
  if (( go_archive_size <= 0 || go_archive_size > go_max_bytes )); then
    log "Go distribution size ${go_archive_size} is outside the allowed range"
    return 1
  fi
  local go_archive_digest
  go_archive_digest=$(sha256sum "${go_archive}" | awk '{print tolower($1)}')
  if [[ "${go_archive_digest}" != "${MEDUSA_GO_SHA256,,}" ]]; then
    log "Go distribution SHA-256 mismatch"
    return 1
  fi

  # The checksum authenticates the official distribution. Still reject paths
  # outside its single go/ root and links/special files before extraction.
  local tar_entry
  while IFS= read -r tar_entry; do
    case "${tar_entry}" in
      go|go/*) ;;
      *)
        log "Unsafe path in Go distribution: ${tar_entry}"
        return 1
        ;;
    esac
    if [[ "${tar_entry}" == *"/../"* || "${tar_entry}" == "../"* ]]; then
      log "Unsafe traversal path in Go distribution: ${tar_entry}"
      return 1
    fi
  done < <(tar -tzf "${go_archive}")
  if tar -tvzf "${go_archive}" | awk '
    substr($1, 1, 1) != "d" && substr($1, 1, 1) != "-" { bad = 1 }
    END { exit bad ? 0 : 1 }
  '; then
    log "Go distribution contains a link or special file"
    return 1
  fi

  local toolchain_root="${tmp_dir}/toolchain"
  mkdir -p "${toolchain_root}"
  tar -xzf "${go_archive}" -C "${toolchain_root}"
  local go_bin="${toolchain_root}/go/bin/go"
  if [[ ! -x "${go_bin}" ]]; then
    log "Verified Go distribution did not contain go/bin/go"
    return 1
  fi
  local go_version_output
  go_version_output=$("${go_bin}" version)
  if [[ "${go_version_output}" != "go version go${MEDUSA_GO_VERSION} linux/amd64" ]]; then
    log "Unexpected Go toolchain identity: ${go_version_output}"
    return 1
  fi

  local source_dir="${tmp_dir}/medusa"
  git init -q "${source_dir}"
  git -C "${source_dir}" remote add origin "${MEDUSA_GIT_REPO}"
  local expected_commit="${MEDUSA_GIT_COMMIT,,}"

  log "Fetching immutable Medusa commit ${expected_commit}"
  GIT_TERMINAL_PROMPT=0 git -C "${source_dir}" fetch --no-tags --depth 1 origin "${expected_commit}"
  local fetched_commit
  fetched_commit=$(git -C "${source_dir}" rev-parse 'FETCH_HEAD^{commit}')
  if [[ "${fetched_commit,,}" != "${expected_commit}" ]]; then
    log "Fetched Medusa commit mismatch: expected ${expected_commit}, got ${fetched_commit}"
    return 1
  fi

  # Re-resolve the human-readable ref on every instance. If it moved after the
  # request was approved, fail instead of benchmarking a silently different build.
  GIT_TERMINAL_PROMPT=0 git -C "${source_dir}" fetch --no-tags --depth 1 origin "${MEDUSA_GIT_REF}"
  local ref_commit
  ref_commit=$(git -C "${source_dir}" rev-parse 'FETCH_HEAD^{commit}')
  if [[ "${ref_commit,,}" != "${expected_commit}" ]]; then
    log "Medusa ref drift: ${MEDUSA_GIT_REF} resolves to ${ref_commit}, expected ${expected_commit}"
    return 1
  fi
  git -C "${source_dir}" checkout -q --detach "${expected_commit}"

  if [[ ! -f "${source_dir}/go.mod" || ! -f "${source_dir}/go.sum" ]]; then
    log "Medusa source commit must contain go.mod and go.sum"
    return 1
  fi
  local go_sum_digest
  go_sum_digest=$(sha256sum "${source_dir}/go.sum" | awk '{print tolower($1)}')

  export GOTOOLCHAIN=local
  export GOPATH="${tmp_dir}/gopath"
  export GOMODCACHE="${GOPATH}/pkg/mod"
  export GOCACHE="${tmp_dir}/gocache"
  export CGO_ENABLED=1
  mkdir -p "${GOPATH}" "${GOMODCACHE}" "${GOCACHE}"

  log "Verifying locked Medusa modules at ${expected_commit}"
  (
    cd "${source_dir}"
    "${go_bin}" mod download
    "${go_bin}" mod verify
    "${go_bin}" build \
      -mod=readonly \
      -trimpath \
      -buildvcs=true \
      -ldflags=-buildid= \
      -o "${tmp_dir}/medusa-bin" \
      .
  )

  install -m 0755 "${tmp_dir}/medusa-bin" "${SCFUZZBENCH_BIN_DIR}/medusa"
  command -v medusa >/dev/null
  local binary_digest binary_version
  binary_digest=$(sha256sum "${SCFUZZBENCH_BIN_DIR}/medusa" | awk '{print tolower($1)}')
  binary_version=$(medusa --version 2>&1 | head -n 1 || true)
  if [[ -z "${binary_version}" ]]; then
    log "Built Medusa binary did not report a version"
    return 1
  fi

  printf '%s\n' "${expected_commit}" >"${SCFUZZBENCH_ROOT}/medusa_git_commit"
  jq -n \
    --arg tool "medusa" \
    --arg install_mode "source" \
    --arg repo "${MEDUSA_GIT_REPO}" \
    --arg ref "${MEDUSA_GIT_REF}" \
    --arg commit "${expected_commit}" \
    --arg go_version "${MEDUSA_GO_VERSION}" \
    --arg go_distribution_sha256 "${go_archive_digest}" \
    --arg go_sum_sha256 "${go_sum_digest}" \
    --arg binary_sha256 "${binary_digest}" \
    --arg binary_version "${binary_version}" \
    '{
      schema_version: 1,
      tool: $tool,
      install_mode: $install_mode,
      repository: $repo,
      requested_ref: $ref,
      commit: $commit,
      toolchain: {
        name: "go",
        version: $go_version,
        os: "linux",
        arch: "amd64",
        distribution_sha256: $go_distribution_sha256,
        module_mode: "readonly",
        go_sum_sha256: $go_sum_sha256
      },
      binary: {name: "medusa", sha256: $binary_sha256, version: $binary_version}
    }' >"${SCFUZZBENCH_LOG_DIR}/tool_provenance.json"

  log "Built Medusa ${expected_commit} with Go ${MEDUSA_GO_VERSION}; binary SHA-256 ${binary_digest}"
  cleanup_medusa_source
  trap - EXIT
  unset MEDUSA_SOURCE_TMP_DIR_CLEANUP
}

if [[ -n "${MEDUSA_GIT_REPO:-}" ]]; then
  install_medusa_from_source
  exit 0
fi

require_env MEDUSA_VERSION
log "Installing stable Medusa ${MEDUSA_VERSION}"

tmp_dir=$(mktemp -d)
archive="medusa-linux-x64.tar.gz"
url="https://github.com/crytic/medusa/releases/download/v${MEDUSA_VERSION}/${archive}"

curl -L "${url}" -o "${tmp_dir}/${archive}"
mkdir -p "${tmp_dir}/medusa"
tar -xzf "${tmp_dir}/${archive}" -C "${tmp_dir}/medusa"

bin_path=$(find "${tmp_dir}/medusa" -type f -name "medusa" | head -n 1)
if [[ -z "${bin_path}" ]]; then
  log "medusa binary not found in archive"
  exit 1
fi
install -m 0755 "${bin_path}" "${SCFUZZBENCH_BIN_DIR}/medusa"

rm -rf "${tmp_dir}"

command -v medusa
