#!/usr/bin/env bash
set -euo pipefail

source /opt/scfuzzbench/common.sh

prepare_workspace
install_base_packages
install_foundry
install_crytic_compile
install_slither_analyzer

# recon shells out to `npx -y recon-generate@latest` for slither-equivalent
# target info; without Node.js every run degrades to bytecode constants only
# and wastes a full --build-info recompile. recon-generate@latest needs
# Node.js >= 20 (its commander dependency is ESM-only and better-sqlite3 ships
# no Node 18 prebuilds), so Ubuntu's apt nodejs (18) is not enough: install
# a digest-pinned official Node.js archive instead.
if ! command -v npx >/dev/null 2>&1; then
  if is_local_mode; then
    log "npx not found; install Node.js >= 20 so recon-generate is available."
  else
    node_version="22.23.1"
    node_sha256="7a8cb04b4a1df4eaf432125324b81b29a088e73570a23259a8de1c65d07fc129"
    node_archive="node-v${node_version}-linux-x64.tar.gz"
    node_tmp_dir=$(mktemp -d)
    node_install_root="/opt/node-v${node_version}"
    log "Installing digest-pinned Node.js ${node_version} (recon-generate needs Node >= 20)"
    retry_cmd 5 10 curl -fsSL \
      "https://nodejs.org/dist/v${node_version}/${node_archive}" \
      -o "${node_tmp_dir}/${node_archive}"
    actual_node_sha256=$(sha256sum "${node_tmp_dir}/${node_archive}" | cut -d' ' -f1)
    if [[ "${actual_node_sha256}" != "${node_sha256}" ]]; then
      log "Node.js archive digest mismatch"
      exit 1
    fi
    mkdir -p "${node_install_root}"
    tar -xzf "${node_tmp_dir}/${node_archive}" \
      -C "${node_install_root}" --strip-components=1
    ln -sfn "${node_install_root}/bin/node" /usr/local/bin/node
    ln -sfn "${node_install_root}/bin/npm" /usr/local/bin/npm
    ln -sfn "${node_install_root}/bin/npx" /usr/local/bin/npx
    rm -rf "${node_tmp_dir}"
  fi
fi
if command -v npx >/dev/null 2>&1; then
  log "Prefetching recon-generate"
  npx -y recon-generate@latest --version || log "WARNING: recon-generate unavailable (prefetch failed); recon will retry at runtime."
  log "node version: $(node --version 2>/dev/null || echo missing)"
  log "npx version: $(npx --version 2>/dev/null || echo missing)"
else
  log "WARNING: recon-generate unavailable (npx not installed); value mining degrades to bytecode constants."
fi

require_env RECON_VERSION
recon_version="${RECON_VERSION#v}"
log "Installing Recon fuzzer v${recon_version}"

tmp_dir=$(mktemp -d)
archive="recon-linux-x86_64.tar.gz"
url="https://github.com/Recon-Fuzz/recon-fuzzer/releases/download/v${recon_version}/${archive}"

curl -L "${url}" -o "${tmp_dir}/${archive}"
tar -xzf "${tmp_dir}/${archive}" -C "${tmp_dir}"

bin_path=$(find "${tmp_dir}" -type f -name "recon" | head -n 1)
if [[ -z "${bin_path}" ]]; then
  log "recon binary not found in archive"
  exit 1
fi
install -m 0755 "${bin_path}" /usr/local/bin/recon

rm -rf "${tmp_dir}"

command -v recon
recon --version || true
