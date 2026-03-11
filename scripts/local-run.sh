#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_ECHIDNA_VERSION="2.3.1"
DEFAULT_MEDUSA_VERSION="1.4.1"
DEFAULT_FOUNDRY_VERSION="v1.6.0-rc1"
DEFAULT_BITWUZLA_VERSION="0.8.2"
DEFAULT_BENCHMARK_TYPE="property"
DEFAULT_TIMEOUT="86400"   # 24 h – same as cloud default
DEFAULT_WORKERS=""         # empty = let common.sh pick nproc-based default

# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Run a fuzzer locally against a target repository.

Required:
  -f, --fuzzer FUZZER           Fuzzer to run: echidna | medusa | foundry | echidna-symexec
  -r, --repo   URL              Target git repository URL
  -b, --branch BRANCH           Branch or commit to check out

Optional – general:
  -t, --timeout SECONDS         Campaign timeout (default: ${DEFAULT_TIMEOUT})
  -w, --workers N               Number of fuzzer workers/threads
  -T, --type    TYPE            Benchmark type: property | optimization (default: ${DEFAULT_BENCHMARK_TYPE})
      --install                 Run the fuzzer's install.sh first (idempotent)

Optional – echidna / echidna-symexec:
      --echidna-config  PATH    Echidna YAML config (relative to target repo)
      --echidna-target  PATH    Solidity target file    (e.g. test/recon/CryticTester.sol)
      --echidna-contract NAME   Target contract name    (e.g. CryticTester)
      --echidna-extra-args ARGS Extra args for echidna-test

Optional – medusa:
      --medusa-config   PATH    Medusa TOML/JSON config (relative to target repo)
      --medusa-target   PATH    Compilation target path
      --medusa-contracts LIST   Comma-separated target contracts
      --medusa-extra-args ARGS  Extra args for medusa

Optional – foundry:
      --foundry-test-args ARGS  Extra args for forge test
      --foundry-failure-dir DIR Failure persistence dir (default: cache/invariant)

Optional – versions (override defaults):
      --echidna-version  VER    (default: ${DEFAULT_ECHIDNA_VERSION})
      --medusa-version   VER    (default: ${DEFAULT_MEDUSA_VERSION})
      --foundry-version  VER    (default: ${DEFAULT_FOUNDRY_VERSION})
      --bitwuzla-version VER    (default: ${DEFAULT_BITWUZLA_VERSION})

Environment variables:
  Any SCFUZZBENCH_*, ECHIDNA_*, MEDUSA_*, FOUNDRY_* env vars set before
  invocation are passed through and take precedence over CLI flags.

Examples:
  # Echidna – 10-minute run, 4 workers
  $(basename "$0") -f echidna -r https://github.com/Recon-Fuzz/example-scfuzzbench \\
    -b dev-recon -t 600 -w 4 \\
    --echidna-config echidna.yaml \\
    --echidna-target test/recon/CryticTester.sol \\
    --echidna-contract CryticTester

  # Medusa – default timeout, auto workers
  $(basename "$0") -f medusa -r https://github.com/Recon-Fuzz/example-scfuzzbench \\
    -b dev-recon --medusa-config medusa.json

  # Install + run in one shot
  $(basename "$0") --install -f echidna -r https://github.com/... -b main ...
EOF
  exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
FUZZER=""
REPO_URL=""
BRANCH=""
TIMEOUT="${DEFAULT_TIMEOUT}"
WORKERS="${DEFAULT_WORKERS}"
BENCHMARK_TYPE="${DEFAULT_BENCHMARK_TYPE}"
DO_INSTALL=0

ECHIDNA_VERSION_ARG=""
MEDUSA_VERSION_ARG=""
FOUNDRY_VERSION_ARG=""
BITWUZLA_VERSION_ARG=""

ECHIDNA_CONFIG_ARG=""
ECHIDNA_TARGET_ARG=""
ECHIDNA_CONTRACT_ARG=""
ECHIDNA_EXTRA_ARGS_ARG=""

MEDUSA_CONFIG_ARG=""
MEDUSA_COMPILATION_TARGET_ARG=""
MEDUSA_TARGET_CONTRACTS_ARG=""
MEDUSA_EXTRA_ARGS_ARG=""

FOUNDRY_TEST_ARGS_ARG=""
FOUNDRY_FAILURE_PERSIST_DIR_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--fuzzer)            FUZZER="$2"; shift 2 ;;
    -r|--repo)              REPO_URL="$2"; shift 2 ;;
    -b|--branch)            BRANCH="$2"; shift 2 ;;
    -t|--timeout)           TIMEOUT="$2"; shift 2 ;;
    -w|--workers)           WORKERS="$2"; shift 2 ;;
    -T|--type)              BENCHMARK_TYPE="$2"; shift 2 ;;
    --install)              DO_INSTALL=1; shift ;;
    # echidna
    --echidna-config)       ECHIDNA_CONFIG_ARG="$2"; shift 2 ;;
    --echidna-target)       ECHIDNA_TARGET_ARG="$2"; shift 2 ;;
    --echidna-contract)     ECHIDNA_CONTRACT_ARG="$2"; shift 2 ;;
    --echidna-extra-args)   ECHIDNA_EXTRA_ARGS_ARG="$2"; shift 2 ;;
    # medusa
    --medusa-config)        MEDUSA_CONFIG_ARG="$2"; shift 2 ;;
    --medusa-target)        MEDUSA_COMPILATION_TARGET_ARG="$2"; shift 2 ;;
    --medusa-contracts)     MEDUSA_TARGET_CONTRACTS_ARG="$2"; shift 2 ;;
    --medusa-extra-args)    MEDUSA_EXTRA_ARGS_ARG="$2"; shift 2 ;;
    # foundry
    --foundry-test-args)    FOUNDRY_TEST_ARGS_ARG="$2"; shift 2 ;;
    --foundry-failure-dir)  FOUNDRY_FAILURE_PERSIST_DIR_ARG="$2"; shift 2 ;;
    # versions
    --echidna-version)      ECHIDNA_VERSION_ARG="$2"; shift 2 ;;
    --medusa-version)       MEDUSA_VERSION_ARG="$2"; shift 2 ;;
    --foundry-version)      FOUNDRY_VERSION_ARG="$2"; shift 2 ;;
    --bitwuzla-version)     BITWUZLA_VERSION_ARG="$2"; shift 2 ;;
    -h|--help)              usage 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate required args
# ---------------------------------------------------------------------------
if [[ -z "${FUZZER}" ]]; then
  echo "Error: --fuzzer is required" >&2; usage 1
fi
if [[ -z "${REPO_URL}" ]]; then
  echo "Error: --repo is required" >&2; usage 1
fi
if [[ -z "${BRANCH}" ]]; then
  echo "Error: --branch is required" >&2; usage 1
fi

case "${FUZZER}" in
  echidna|medusa|foundry|echidna-symexec) ;;
  *) echo "Error: unknown fuzzer '${FUZZER}'. Choose: echidna, medusa, foundry, echidna-symexec" >&2; exit 1 ;;
esac

# ---------------------------------------------------------------------------
# Build the environment
# ---------------------------------------------------------------------------
# Ensure locally-built shared libraries (e.g. libsecp256k1) are found
if [[ -d "${HOME}/.local/lib" ]]; then
  export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi

export SCFUZZBENCH_LOCAL_MODE=1
export SCFUZZBENCH_COMMON_SH="${REPO_ROOT}/fuzzers/_shared/common.sh"
export SCFUZZBENCH_REPO_URL="${REPO_URL}"
export SCFUZZBENCH_COMMIT="${BRANCH}"
export SCFUZZBENCH_BENCHMARK_TYPE="${BENCHMARK_TYPE}"
export SCFUZZBENCH_TIMEOUT_SECONDS="${TIMEOUT}"

# Versions – CLI flag → existing env → default
export ECHIDNA_VERSION="${ECHIDNA_VERSION_ARG:-${ECHIDNA_VERSION:-${DEFAULT_ECHIDNA_VERSION}}}"
export MEDUSA_VERSION="${MEDUSA_VERSION_ARG:-${MEDUSA_VERSION:-${DEFAULT_MEDUSA_VERSION}}}"
export FOUNDRY_VERSION="${FOUNDRY_VERSION_ARG:-${FOUNDRY_VERSION:-${DEFAULT_FOUNDRY_VERSION}}}"
export BITWUZLA_VERSION="${BITWUZLA_VERSION_ARG:-${BITWUZLA_VERSION:-${DEFAULT_BITWUZLA_VERSION}}}"

# Workers
if [[ -n "${WORKERS}" ]]; then
  case "${FUZZER}" in
    echidna|echidna-symexec) export ECHIDNA_WORKERS="${ECHIDNA_WORKERS:-${WORKERS}}" ;;
    medusa)                  export MEDUSA_WORKERS="${MEDUSA_WORKERS:-${WORKERS}}" ;;
    foundry)                 export FOUNDRY_THREADS="${FOUNDRY_THREADS:-${WORKERS}}" ;;
  esac
fi

# Fuzzer-specific env – CLI flag → existing env (passthrough)
set_if_nonempty() {
  local var_name="$1" flag_value="$2"
  if [[ -n "${flag_value}" ]]; then
    export "${var_name}=${flag_value}"
  fi
}

# Echidna / Echidna-symexec
set_if_nonempty ECHIDNA_CONFIG     "${ECHIDNA_CONFIG_ARG}"
set_if_nonempty ECHIDNA_TARGET     "${ECHIDNA_TARGET_ARG}"
set_if_nonempty ECHIDNA_CONTRACT   "${ECHIDNA_CONTRACT_ARG}"
set_if_nonempty ECHIDNA_EXTRA_ARGS "${ECHIDNA_EXTRA_ARGS_ARG}"

# Medusa
set_if_nonempty MEDUSA_CONFIG             "${MEDUSA_CONFIG_ARG}"
set_if_nonempty MEDUSA_COMPILATION_TARGET "${MEDUSA_COMPILATION_TARGET_ARG}"
set_if_nonempty MEDUSA_TARGET_CONTRACTS   "${MEDUSA_TARGET_CONTRACTS_ARG}"
set_if_nonempty MEDUSA_EXTRA_ARGS         "${MEDUSA_EXTRA_ARGS_ARG}"

# Foundry
set_if_nonempty FOUNDRY_TEST_ARGS         "${FOUNDRY_TEST_ARGS_ARG}"
set_if_nonempty FOUNDRY_FAILURE_PERSIST_DIR "${FOUNDRY_FAILURE_PERSIST_DIR_ARG}"

# ---------------------------------------------------------------------------
# Optional: run installer first
# ---------------------------------------------------------------------------
if [[ "${DO_INSTALL}" -eq 1 ]]; then
  echo "==> Installing ${FUZZER}..."
  bash "${REPO_ROOT}/fuzzers/${FUZZER}/install.sh"
  echo "==> Installation complete."
  echo ""
fi

# ---------------------------------------------------------------------------
# Print summary
# ---------------------------------------------------------------------------
echo "============================================"
echo "  scfuzzbench local run"
echo "============================================"
echo "  Fuzzer:    ${FUZZER}"
echo "  Repo:      ${REPO_URL}"
echo "  Branch:    ${BRANCH}"
echo "  Type:      ${BENCHMARK_TYPE}"
echo "  Timeout:   ${TIMEOUT}s"
[[ -n "${WORKERS}" ]] && echo "  Workers:   ${WORKERS}"
echo "  Workspace: ${SCFUZZBENCH_ROOT:-${HOME}/.scfuzzbench}"
echo "  Output:    ${SCFUZZBENCH_LOCAL_OUTPUT_DIR:-${SCFUZZBENCH_ROOT:-${HOME}/.scfuzzbench}/output}"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
exec bash "${REPO_ROOT}/fuzzers/${FUZZER}/run.sh"
