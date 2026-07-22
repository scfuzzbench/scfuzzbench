#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_ECHIDNA_VERSION="2.3.2"
DEFAULT_MEDUSA_VERSION="1.5.1"
DEFAULT_MEDUSA_GO_VERSION="1.24.0"
DEFAULT_MEDUSA_GO_SHA256="dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858"
DEFAULT_FOUNDRY_VERSION="v1.7.1"
# Cloud runs build upstream foundry-rs/foundry at the commit pinned in
# infrastructure/variables.tf (foundry_git_ref). Export FOUNDRY_GIT_REPO and
# FOUNDRY_GIT_REF before invoking to match that build locally; the plain
# FOUNDRY_VERSION path installs a released binary via foundryup instead.
DEFAULT_RECON_VERSION="0.4.18"
DEFAULT_BENCHMARK_TYPE="property"
DEFAULT_TIMEOUT="86400"   # 24 h – same as cloud default
DEFAULT_WORKERS=""         # empty = let common.sh pick nproc-based default

# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Run a fuzzer locally against a target repository.

Required:
  -f, --fuzzer FUZZER           Fuzzer to run: echidna | medusa | foundry | recon-fuzzer
  -r, --repo   URL              Target git repository URL
  -b, --branch BRANCH           Branch or commit to check out

Optional – general:
  -t, --timeout SECONDS         Campaign timeout (default: ${DEFAULT_TIMEOUT})
  -w, --workers N               Number of fuzzer workers/threads
  -T, --type    TYPE            Benchmark type: property | optimization (default: ${DEFAULT_BENCHMARK_TYPE})
      --seed-corpus SOURCE      Shared seed directory or s3://bucket/prefix (default: empty)
      --install                 Run the fuzzer's install.sh first (idempotent)

Optional – echidna / recon-fuzzer:
      --echidna-config  PATH    Echidna YAML config (relative to target repo)
      --echidna-target  PATH    Solidity target file    (e.g. test/recon/CryticTester.sol)
      --echidna-contract NAME   Target contract name    (e.g. CryticTester)
      --echidna-extra-args ARGS Extra args for echidna
      --echidna-ci-repo URL     GitHub repo for CI artifact mode
      --echidna-ci-run-id ID    Successful Actions run ID
      --echidna-ci-artifact NAME Exact Linux artifact name
      --echidna-ci-sha256 SHA   Expected Actions artifact ZIP SHA-256
      --echidna-ci-commit SHA   Full head commit expected for the run

Optional – medusa:
      --medusa-config   PATH    Medusa JSON config (relative to target repo)
      --medusa-target   PATH    Compilation target path
      --medusa-contracts LIST   Comma-separated target contracts
      --medusa-extra-args ARGS  Extra args for medusa
      --medusa-git-repo URL     GitHub repo for source mode
      --medusa-git-ref REF      Branch/tag/ref to verify
      --medusa-git-commit SHA   Full immutable source commit
      --medusa-go-version VER   Pinned Go version (default: ${DEFAULT_MEDUSA_GO_VERSION})
      --medusa-go-sha256 SHA    Official Go linux-amd64 archive SHA-256
      --medusa-prune-frequency MINUTES
                                Corpus-pruner interval (default: 0, disabled)

Optional – foundry:
      --foundry-test-args ARGS  Extra args for forge test
      --foundry-failure-dir DIR Failure persistence dir (default: cache/invariant)

Optional – versions (override defaults):
      --echidna-version  VER    (default: ${DEFAULT_ECHIDNA_VERSION})
      --medusa-version   VER    (default: ${DEFAULT_MEDUSA_VERSION})
      --foundry-version  VER    (default: ${DEFAULT_FOUNDRY_VERSION})
      --recon-version    VER    (default: ${DEFAULT_RECON_VERSION})

Environment variables:
  Any SCFUZZBENCH_*, ECHIDNA_*, MEDUSA_*, FOUNDRY_*, RECON_* env vars set before
  invocation are passed through and take precedence over CLI flags.
  Echidna CI mode also requires ECHIDNA_CI_TOKEN in the environment. There is
  intentionally no CLI token flag, so the token does not enter shell history.

Examples:
  # Echidna – 10-minute run, 4 workers
  $(basename "$0") -f echidna -r https://github.com/scfuzzbench/example-scfuzzbench \\
    -b main -t 600 -w 4 \\
    --echidna-config echidna.yaml \\
    --echidna-target test/recon/CryticTester.sol \\
    --echidna-contract CryticTester

  # Medusa – default timeout, auto workers
  $(basename "$0") -f medusa -r https://github.com/scfuzzbench/example-scfuzzbench \\
    -b main --medusa-config medusa.json

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
SEED_CORPUS_ARG=""
DO_INSTALL=0

ECHIDNA_VERSION_ARG=""
MEDUSA_VERSION_ARG=""
FOUNDRY_VERSION_ARG=""
RECON_VERSION_ARG=""

ECHIDNA_CONFIG_ARG=""
ECHIDNA_TARGET_ARG=""
ECHIDNA_CONTRACT_ARG=""
ECHIDNA_EXTRA_ARGS_ARG=""
ECHIDNA_CI_REPO_ARG=""
ECHIDNA_CI_RUN_ID_ARG=""
ECHIDNA_CI_ARTIFACT_NAME_ARG=""
ECHIDNA_CI_ARTIFACT_SHA256_ARG=""
ECHIDNA_CI_COMMIT_ARG=""

MEDUSA_CONFIG_ARG=""
MEDUSA_COMPILATION_TARGET_ARG=""
MEDUSA_TARGET_CONTRACTS_ARG=""
MEDUSA_EXTRA_ARGS_ARG=""
MEDUSA_GIT_REPO_ARG=""
MEDUSA_GIT_REF_ARG=""
MEDUSA_GIT_COMMIT_ARG=""
MEDUSA_GO_VERSION_ARG=""
MEDUSA_GO_SHA256_ARG=""
MEDUSA_PRUNE_FREQUENCY_ARG=""

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
    --seed-corpus)          SEED_CORPUS_ARG="$2"; shift 2 ;;
    --install)              DO_INSTALL=1; shift ;;
    # echidna
    --echidna-config)       ECHIDNA_CONFIG_ARG="$2"; shift 2 ;;
    --echidna-target)       ECHIDNA_TARGET_ARG="$2"; shift 2 ;;
    --echidna-contract)     ECHIDNA_CONTRACT_ARG="$2"; shift 2 ;;
    --echidna-extra-args)   ECHIDNA_EXTRA_ARGS_ARG="$2"; shift 2 ;;
    --echidna-ci-repo)      ECHIDNA_CI_REPO_ARG="$2"; shift 2 ;;
    --echidna-ci-run-id)    ECHIDNA_CI_RUN_ID_ARG="$2"; shift 2 ;;
    --echidna-ci-artifact)  ECHIDNA_CI_ARTIFACT_NAME_ARG="$2"; shift 2 ;;
    --echidna-ci-sha256)    ECHIDNA_CI_ARTIFACT_SHA256_ARG="$2"; shift 2 ;;
    --echidna-ci-commit)    ECHIDNA_CI_COMMIT_ARG="$2"; shift 2 ;;
    # medusa
    --medusa-config)        MEDUSA_CONFIG_ARG="$2"; shift 2 ;;
    --medusa-target)        MEDUSA_COMPILATION_TARGET_ARG="$2"; shift 2 ;;
    --medusa-contracts)     MEDUSA_TARGET_CONTRACTS_ARG="$2"; shift 2 ;;
    --medusa-extra-args)    MEDUSA_EXTRA_ARGS_ARG="$2"; shift 2 ;;
    --medusa-git-repo)      MEDUSA_GIT_REPO_ARG="$2"; shift 2 ;;
    --medusa-git-ref)       MEDUSA_GIT_REF_ARG="$2"; shift 2 ;;
    --medusa-git-commit)    MEDUSA_GIT_COMMIT_ARG="$2"; shift 2 ;;
    --medusa-go-version)    MEDUSA_GO_VERSION_ARG="$2"; shift 2 ;;
    --medusa-go-sha256)     MEDUSA_GO_SHA256_ARG="$2"; shift 2 ;;
    --medusa-prune-frequency) MEDUSA_PRUNE_FREQUENCY_ARG="$2"; shift 2 ;;
    # foundry
    --foundry-test-args)    FOUNDRY_TEST_ARGS_ARG="$2"; shift 2 ;;
    --foundry-failure-dir)  FOUNDRY_FAILURE_PERSIST_DIR_ARG="$2"; shift 2 ;;
    # versions
    --echidna-version)      ECHIDNA_VERSION_ARG="$2"; shift 2 ;;
    --medusa-version)       MEDUSA_VERSION_ARG="$2"; shift 2 ;;
    --foundry-version)      FOUNDRY_VERSION_ARG="$2"; shift 2 ;;
    --recon-version)        RECON_VERSION_ARG="$2"; shift 2 ;;
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
  echidna|medusa|foundry|recon-fuzzer) ;;
  *) echo "Error: unknown fuzzer '${FUZZER}'. Choose: echidna, medusa, foundry, recon-fuzzer" >&2; exit 1 ;;
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
export SCFUZZBENCH_FOUNDRY_SOURCE_PATCH="${SCFUZZBENCH_FOUNDRY_SOURCE_PATCH:-${REPO_ROOT}/fuzzers/foundry/throughput-progress.patch}"
export SCFUZZBENCH_REPO_URL="${REPO_URL}"
export SCFUZZBENCH_COMMIT="${BRANCH}"
export SCFUZZBENCH_BENCHMARK_TYPE="${BENCHMARK_TYPE}"
export SCFUZZBENCH_TIMEOUT_SECONDS="${TIMEOUT}"
if [[ -n "${SEED_CORPUS_ARG}" ]]; then
  if [[ "${SEED_CORPUS_ARG}" == s3://* ]]; then
    export SCFUZZBENCH_SEED_CORPUS_SOURCE="${SEED_CORPUS_ARG}"
  else
    if [[ ! -d "${SEED_CORPUS_ARG}" ]]; then
      echo "Error: shared seed corpus directory not found: ${SEED_CORPUS_ARG}" >&2
      exit 1
    fi
    seed_corpus_path="$(cd "${SEED_CORPUS_ARG}" && pwd -P)"
    seed_corpus_path_sha256="$(printf '%s' "${seed_corpus_path}" | sha256sum | cut -d' ' -f1)"
    export SCFUZZBENCH_SEED_CORPUS_SOURCE="${seed_corpus_path}"
    export SCFUZZBENCH_SEED_CORPUS_PROVENANCE_SOURCE="local-sha256://${seed_corpus_path_sha256}"
  fi
fi

# Versions – CLI flag → existing env → default
export ECHIDNA_VERSION="${ECHIDNA_VERSION_ARG:-${ECHIDNA_VERSION:-${DEFAULT_ECHIDNA_VERSION}}}"
export MEDUSA_VERSION="${MEDUSA_VERSION_ARG:-${MEDUSA_VERSION:-${DEFAULT_MEDUSA_VERSION}}}"
export FOUNDRY_VERSION="${FOUNDRY_VERSION_ARG:-${FOUNDRY_VERSION:-${DEFAULT_FOUNDRY_VERSION}}}"
export RECON_VERSION="${RECON_VERSION_ARG:-${RECON_VERSION:-${DEFAULT_RECON_VERSION}}}"

# Workers
if [[ -n "${WORKERS}" ]]; then
  case "${FUZZER}" in
    echidna)       export ECHIDNA_WORKERS="${ECHIDNA_WORKERS:-${WORKERS}}" ;;
    medusa)        export MEDUSA_WORKERS="${MEDUSA_WORKERS:-${WORKERS}}" ;;
    foundry)       export FOUNDRY_THREADS="${FOUNDRY_THREADS:-${WORKERS}}" ;;
    recon-fuzzer)  export RECON_WORKERS="${RECON_WORKERS:-${WORKERS}}" ;;
  esac
fi

# Fuzzer-specific env – CLI flag → existing env (passthrough)
set_if_nonempty() {
  local var_name="$1" flag_value="$2"
  if [[ -n "${flag_value}" ]]; then
    export "${var_name}=${flag_value}"
  fi
}

# Echidna / Recon Fuzzer
set_if_nonempty ECHIDNA_CONFIG     "${ECHIDNA_CONFIG_ARG}"
set_if_nonempty ECHIDNA_TARGET     "${ECHIDNA_TARGET_ARG}"
set_if_nonempty ECHIDNA_CONTRACT   "${ECHIDNA_CONTRACT_ARG}"
set_if_nonempty ECHIDNA_EXTRA_ARGS "${ECHIDNA_EXTRA_ARGS_ARG}"
set_if_nonempty ECHIDNA_CI_REPO "${ECHIDNA_CI_REPO_ARG}"
set_if_nonempty ECHIDNA_CI_RUN_ID "${ECHIDNA_CI_RUN_ID_ARG}"
set_if_nonempty ECHIDNA_CI_ARTIFACT_NAME "${ECHIDNA_CI_ARTIFACT_NAME_ARG}"
set_if_nonempty ECHIDNA_CI_ARTIFACT_SHA256 "${ECHIDNA_CI_ARTIFACT_SHA256_ARG}"
set_if_nonempty ECHIDNA_CI_COMMIT "${ECHIDNA_CI_COMMIT_ARG}"

# Medusa
set_if_nonempty MEDUSA_CONFIG             "${MEDUSA_CONFIG_ARG}"
set_if_nonempty MEDUSA_COMPILATION_TARGET "${MEDUSA_COMPILATION_TARGET_ARG}"
set_if_nonempty MEDUSA_TARGET_CONTRACTS   "${MEDUSA_TARGET_CONTRACTS_ARG}"
set_if_nonempty MEDUSA_EXTRA_ARGS         "${MEDUSA_EXTRA_ARGS_ARG}"
set_if_nonempty MEDUSA_GIT_REPO           "${MEDUSA_GIT_REPO_ARG}"
set_if_nonempty MEDUSA_GIT_REF            "${MEDUSA_GIT_REF_ARG}"
set_if_nonempty MEDUSA_GIT_COMMIT         "${MEDUSA_GIT_COMMIT_ARG}"
export MEDUSA_GO_VERSION="${MEDUSA_GO_VERSION_ARG:-${MEDUSA_GO_VERSION:-${DEFAULT_MEDUSA_GO_VERSION}}}"
export MEDUSA_GO_SHA256="${MEDUSA_GO_SHA256_ARG:-${MEDUSA_GO_SHA256:-${DEFAULT_MEDUSA_GO_SHA256}}}"
set_if_nonempty MEDUSA_PRUNE_FREQUENCY    "${MEDUSA_PRUNE_FREQUENCY_ARG}"

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
[[ -n "${SCFUZZBENCH_SEED_CORPUS_SOURCE:-}" ]] && echo "  Seeds:     ${SCFUZZBENCH_SEED_CORPUS_PROVENANCE_SOURCE:-${SCFUZZBENCH_SEED_CORPUS_SOURCE}}"
echo "  Workspace: ${SCFUZZBENCH_ROOT:-${HOME}/.scfuzzbench}"
echo "  Output:    ${SCFUZZBENCH_LOCAL_OUTPUT_DIR:-${SCFUZZBENCH_ROOT:-${HOME}/.scfuzzbench}/output}"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
exec bash "${REPO_ROOT}/fuzzers/${FUZZER}/run.sh"
