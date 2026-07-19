import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class BleedingEdgeInstallerContractTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("jq") is None:
            self.skipTest("jq is required for installer integration tests")
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.mock_bin = self.root / "mock-bin"
        self.install_bin = self.root / "installed"
        self.logs = self.root / "logs"
        self.state = self.root / "state"
        for directory in (self.mock_bin, self.install_bin, self.logs, self.state):
            directory.mkdir()
        self.common = self.root / "common.sh"
        self.common.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                : "${SCFUZZBENCH_ROOT:?}"
                : "${SCFUZZBENCH_BIN_DIR:?}"
                : "${SCFUZZBENCH_LOG_DIR:?}"
                prepare_workspace() { mkdir -p "${SCFUZZBENCH_ROOT}" "${SCFUZZBENCH_BIN_DIR}" "${SCFUZZBENCH_LOG_DIR}"; }
                install_base_packages() { :; }
                install_foundry() { :; }
                install_crytic_compile() { :; }
                install_slither_analyzer() { :; }
                is_local_mode() { [[ -n "${SCFUZZBENCH_LOCAL_MODE:-}" ]]; }
                log() { printf '[test] %s\n' "$*" >&2; }
                require_env() {
                  local name
                  for name in "$@"; do
                    [[ -n "${!name:-}" ]] || { log "missing ${name}"; return 1; }
                  done
                }
                retry_cmd() { shift 2; "$@"; }
                aws_cli() {
                  printf '%s\n' "${MOCK_GITHUB_TOKEN:-test_token}"
                }
                """
            ),
            encoding="utf-8",
        )
        self.base_env = {
            **os.environ,
            "PATH": f"{self.mock_bin}:{self.install_bin}:{os.environ['PATH']}",
            "SCFUZZBENCH_COMMON_SH": str(self.common),
            "SCFUZZBENCH_ROOT": str(self.state),
            "SCFUZZBENCH_BIN_DIR": str(self.install_bin),
            "SCFUZZBENCH_LOG_DIR": str(self.logs),
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def write_executable(self, name, content):
        path = self.mock_bin / name
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_installer(self, relative_path, env):
        return subprocess.run(
            ["bash", str(REPO_ROOT / relative_path)],
            env={**self.base_env, **env},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def install_stable_mocks(self):
        curl_log = self.root / "curl.log"
        self.write_executable(
            "file",
            """\
            #!/usr/bin/env bash
            echo "ELF 64-bit LSB pie executable, x86-64"
            """,
        )
        self.write_executable(
            "curl",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "${MOCK_CURL_LOG}"
            output=""
            while (($#)); do
              case "$1" in
                -o|--output) output="$2"; shift 2 ;;
                *) shift ;;
              esac
            done
            : > "${output}"
            """,
        )
        self.write_executable(
            "tar",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            archive=""
            destination=""
            while (($#)); do
              case "$1" in
                -xzf) archive="$2"; shift 2 ;;
                -C) destination="$2"; shift 2 ;;
                *) shift ;;
              esac
            done
            mkdir -p "${destination}"
            if [[ "${archive}" == *echidna* ]]; then
              cp /bin/echo "${destination}/echidna"
            else
              cp /bin/echo "${destination}/medusa"
            fi
            """,
        )
        return curl_log

    def test_blank_opt_in_inputs_keep_stable_release_installers(self):
        curl_log = self.install_stable_mocks()
        stable_env = {"MOCK_CURL_LOG": str(curl_log)}

        echidna = self.run_installer(
            "fuzzers/echidna/install.sh",
            {**stable_env, "ECHIDNA_VERSION": "2.3.1"},
        )
        medusa = self.run_installer(
            "fuzzers/medusa/install.sh",
            {**stable_env, "MEDUSA_VERSION": "1.4.1"},
        )

        self.assertEqual(0, echidna.returncode, echidna.stderr)
        self.assertEqual(0, medusa.returncode, medusa.stderr)
        requests = curl_log.read_text(encoding="utf-8")
        self.assertIn("echidna/releases/download/v2.3.1", requests)
        self.assertIn("medusa/releases/download/v1.4.1", requests)
        self.assertNotIn("api.github.com", requests)
        self.assertNotIn("go.dev", requests)
        self.assertFalse((self.logs / "tool_provenance.json").exists())

    def test_echidna_ci_artifact_path_records_verified_provenance(self):
        artifact_tar = self.root / "echidna.tar.gz"
        with tarfile.open(artifact_tar, "w:gz") as archive:
            info = tarfile.TarInfo("echidna")
            binary = Path("/bin/echo").read_bytes()
            info.size = len(binary)
            info.mode = 0o755
            import io

            archive.addfile(info, io.BytesIO(binary))
        artifact_zip = self.root / "artifact.zip"
        with zipfile.ZipFile(artifact_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(artifact_tar, "echidna.tar.gz")
        artifact_digest = hashlib.sha256(artifact_zip.read_bytes()).hexdigest()
        commit = "9680bb1ef8bf0e11c00fff3e29c5f244d6eb1c85"
        run_json = self.root / "run.json"
        run_json.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": commit,
                    "repository": {"full_name": "crytic/echidna"},
                }
            ),
            encoding="utf-8",
        )
        artifacts_json = self.root / "artifacts.json"
        artifacts_json.write_text(
            json.dumps(
                {
                    "total_count": 1,
                    "artifacts": [
                        {
                            "id": 42,
                            "name": "echidna-Linux",
                            "digest": f"sha256:{artifact_digest}",
                            "expired": False,
                            "expires_at": "2999-01-01T00:00:00Z",
                            "size_in_bytes": artifact_zip.stat().st_size,
                            "workflow_run": {"head_sha": commit},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        curl_log = self.root / "curl.log"
        self.write_executable(
            "file",
            """\
            #!/usr/bin/env bash
            echo "ELF 64-bit LSB pie executable, x86-64"
            """,
        )
        self.write_executable(
            "curl",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "${MOCK_CURL_LOG}"
            output=""
            url=""
            while (($#)); do
              case "$1" in
                -o|--output) output="$2"; shift 2 ;;
                http*) url="$1"; shift ;;
                *) shift ;;
              esac
            done
            case "${url}" in
              */actions/runs/123) cp "${MOCK_RUN_JSON}" "${output}" ;;
              */actions/runs/123/artifacts*) cp "${MOCK_ARTIFACTS_JSON}" "${output}" ;;
              */actions/artifacts/42/zip) cp "${MOCK_ARTIFACT_ZIP}" "${output}" ;;
              *) echo "unexpected URL: ${url}" >&2; exit 1 ;;
            esac
            """,
        )
        result = self.run_installer(
            "fuzzers/echidna/install.sh",
            {
                "ECHIDNA_CI_REPO": "https://github.com/crytic/echidna",
                "ECHIDNA_CI_RUN_ID": "123",
                "ECHIDNA_CI_ARTIFACT_NAME": "echidna-Linux",
                "ECHIDNA_CI_ARTIFACT_SHA256": artifact_digest,
                "ECHIDNA_CI_COMMIT": commit,
                "ECHIDNA_CI_TOKEN_SSM_PARAMETER": "/scfuzzbench/echidna/token",
                "ECHIDNA_CI_EXTRACTOR": str(
                    REPO_ROOT / "fuzzers" / "echidna" / "extract_ci_artifact.py"
                ),
                "MOCK_CURL_LOG": str(curl_log),
                "MOCK_RUN_JSON": str(run_json),
                "MOCK_ARTIFACTS_JSON": str(artifacts_json),
                "MOCK_ARTIFACT_ZIP": str(artifact_zip),
                "MOCK_GITHUB_TOKEN": "github_pat_test_token",
            },
        )

        self.assertEqual(0, result.returncode, result.stderr)
        provenance = json.loads((self.logs / "tool_provenance.json").read_text(encoding="utf-8"))
        self.assertEqual("github-actions-artifact", provenance["install_mode"])
        self.assertEqual(commit, provenance["commit"])
        self.assertEqual(artifact_digest, provenance["artifact"]["sha256"])
        self.assertEqual(hashlib.sha256(Path("/bin/echo").read_bytes()).hexdigest(), provenance["binary"]["sha256"])
        self.assertTrue((self.install_bin / "echidna").is_file())
        self.assertTrue((self.install_bin / "echidna-test").is_symlink())
        self.assertNotIn("github_pat_test_token", curl_log.read_text(encoding="utf-8"))

    def test_medusa_source_path_records_commit_toolchain_and_binary(self):
        go_script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "version" ]]; then
              echo "go version go1.24.0 linux/amd64"
              exit 0
            fi
            if [[ "${1:-}" == "mod" ]]; then
              exit 0
            fi
            if [[ "${1:-}" == "build" ]]; then
              output=""
              while (($#)); do
                if [[ "$1" == "-o" ]]; then output="$2"; shift 2; else shift; fi
              done
              cp /bin/echo "${output}"
              chmod +x "${output}"
              exit 0
            fi
            exit 1
            """
        ).encode()
        go_archive = self.root / "go.tar.gz"
        with tarfile.open(go_archive, "w:gz") as archive:
            info = tarfile.TarInfo("go/bin/go")
            info.size = len(go_script)
            info.mode = 0o755
            import io

            archive.addfile(info, io.BytesIO(go_script))
        go_digest = hashlib.sha256(go_archive.read_bytes()).hexdigest()
        commit = "3857153837ab90ed73adc484414b4b43703a54fb"
        curl_log = self.root / "curl.log"
        self.write_executable(
            "curl",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "${MOCK_CURL_LOG}"
            output=""
            while (($#)); do
              case "$1" in
                -o|--output) output="$2"; shift 2 ;;
                *) shift ;;
              esac
            done
            cp "${MOCK_GO_ARCHIVE}" "${output}"
            """,
        )
        self.write_executable(
            "git",
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "$1" == "init" ]]; then
              destination="${@: -1}"
              mkdir -p "${destination}"
              printf 'module github.com/crytic/medusa\n\ngo 1.24.0\n' > "${destination}/go.mod"
              printf 'example.invalid/module v1.0.0 h1:test\n' > "${destination}/go.sum"
              exit 0
            fi
            if [[ "$*" == *"rev-parse"* ]]; then
              printf '%s\n' "${MOCK_MEDUSA_COMMIT}"
              exit 0
            fi
            exit 0
            """,
        )
        result = self.run_installer(
            "fuzzers/medusa/install.sh",
            {
                "MEDUSA_GIT_REPO": "https://github.com/crytic/medusa",
                "MEDUSA_GIT_REF": "v1.4.1",
                "MEDUSA_GIT_COMMIT": commit,
                "MEDUSA_GO_VERSION": "1.24.0",
                "MEDUSA_GO_SHA256": go_digest,
                "MOCK_CURL_LOG": str(curl_log),
                "MOCK_GO_ARCHIVE": str(go_archive),
                "MOCK_MEDUSA_COMMIT": commit,
            },
        )

        self.assertEqual(0, result.returncode, result.stderr)
        provenance = json.loads((self.logs / "tool_provenance.json").read_text(encoding="utf-8"))
        self.assertEqual("source", provenance["install_mode"])
        self.assertEqual(commit, provenance["commit"])
        self.assertEqual("1.24.0", provenance["toolchain"]["version"])
        self.assertEqual(go_digest, provenance["toolchain"]["distribution_sha256"])
        self.assertEqual(
            hashlib.sha256(b"example.invalid/module v1.0.0 h1:test\n").hexdigest(),
            provenance["toolchain"]["go_sum_sha256"],
        )
        self.assertEqual(hashlib.sha256(Path("/bin/echo").read_bytes()).hexdigest(), provenance["binary"]["sha256"])


if __name__ == "__main__":
    unittest.main()
