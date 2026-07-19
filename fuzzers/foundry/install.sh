#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

if [[ -z "${SCFUZZBENCH_FOUNDRY_SOURCE_PATCH:-}" ]]; then
  SCFUZZBENCH_FOUNDRY_SOURCE_PATCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/throughput-progress.patch"
  export SCFUZZBENCH_FOUNDRY_SOURCE_PATCH
fi

prepare_workspace
install_base_packages
install_foundry
install_slither_analyzer
