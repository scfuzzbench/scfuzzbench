#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

prepare_workspace
install_base_packages
install_foundry
install_crytic_compile
install_slither_analyzer

require_env ECHIDNA_VERSION
log "Installing Echidna ${ECHIDNA_VERSION}"

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

require_env BITWUZLA_VERSION
bitwuzla_version="${BITWUZLA_VERSION#v}"
log "Installing Bitwuzla v${bitwuzla_version}"
tmp_dir=$(mktemp -d)
archive="Bitwuzla-Linux-x86_64-static.zip"
url="https://github.com/bitwuzla/bitwuzla/releases/download/${bitwuzla_version}/${archive}"
curl -L "${url}" -o "${tmp_dir}/${archive}"
unzip -q "${tmp_dir}/${archive}" -d "${tmp_dir}/bitwuzla"
bin_path=$(find "${tmp_dir}/bitwuzla" -type f -name "bitwuzla" | head -n 1)
if [[ -z "${bin_path}" ]]; then
  log "bitwuzla binary not found in archive"
  exit 1
fi
install -m 0755 "${bin_path}" "${SCFUZZBENCH_BIN_DIR}/bitwuzla"
rm -rf "${tmp_dir}"

command -v bitwuzla
command -v echidna-test
