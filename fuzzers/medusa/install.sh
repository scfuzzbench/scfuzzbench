#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

prepare_workspace
install_base_packages
install_foundry
install_crytic_compile
install_slither_analyzer

require_env MEDUSA_VERSION
log "Installing Medusa ${MEDUSA_VERSION}"

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
