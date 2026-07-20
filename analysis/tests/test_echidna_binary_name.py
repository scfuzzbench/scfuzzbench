import io
import os
import platform
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "fuzzers" / "echidna" / "install.sh"
RUN_SCRIPT = REPO_ROOT / "fuzzers" / "echidna" / "run.sh"


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def add_bytes(archive: tarfile.TarFile, name: str, contents: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.mode = 0o755
    member.size = len(contents)
    archive.addfile(member, io.BytesIO(contents))


def write_release_archive(
    path: Path,
    binary_names: tuple[str, ...] = ("echidna",),
    *,
    unsafe_path: bool = False,
    linked_binary: bool = False,
    binary_contents: bytes | None = None,
) -> None:
    executable = Path(sys.executable).resolve()
    with tarfile.open(path, "w:gz") as archive:
        if unsafe_path:
            add_bytes(archive, "../outside", b"must not be extracted")
        for binary_name in binary_names:
            member_name = f"release/{binary_name}"
            if linked_binary:
                member = tarfile.TarInfo(member_name)
                member.type = tarfile.SYMTYPE
                member.linkname = "../real-echidna"
                archive.addfile(member)
            elif binary_contents is not None:
                add_bytes(archive, member_name, binary_contents)
            else:
                archive.add(executable, arcname=member_name, recursive=False)


class EchidnaBinaryNameTests(unittest.TestCase):
    def run_installer(
        self,
        tmp_dir: Path,
        archive: Path,
        *,
        version: str | None = None,
        check: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        bin_dir = tmp_dir / "bin"
        fake_commands = tmp_dir / "fake-commands"
        bin_dir.mkdir(parents=True, exist_ok=True)
        fake_commands.mkdir(parents=True, exist_ok=True)

        common_sh = tmp_dir / "common.sh"
        common_sh.write_text(
            """
prepare_workspace() { mkdir -p "${SCFUZZBENCH_BIN_DIR}"; }
install_base_packages() { :; }
install_foundry() { :; }
install_crytic_compile() { :; }
install_slither_analyzer() { :; }
require_env() {
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || return 1
  done
}
log() { printf '%s\\n' "$*" >&2; }
""",
            encoding="utf-8",
        )
        write_executable(
            fake_commands / "curl",
            """#!/usr/bin/env bash
set -euo pipefail
output=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
cp "${FAKE_ECHIDNA_ARCHIVE}" "${output}"
""",
        )

        env = os.environ.copy()
        env.update(
            {
                "SCFUZZBENCH_COMMON_SH": str(common_sh),
                "SCFUZZBENCH_BIN_DIR": str(bin_dir),
                "ECHIDNA_VERSION": version or platform.python_version(),
                "FAKE_ECHIDNA_ARCHIVE": str(archive),
                "PATH": f"{fake_commands}:{bin_dir}:{env['PATH']}",
            }
        )
        result = subprocess.run(
            ["bash", str(INSTALL_SCRIPT)],
            env=env,
            check=check,
            capture_output=True,
            text=True,
        )
        return result, bin_dir

    def test_runner_invokes_echidna(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            work_dir = tmp_dir / "work"
            log_dir = tmp_dir / "logs"
            common_sh = tmp_dir / "common.sh"
            common_sh.write_text(
                """
prepare_workspace() {
  mkdir -p "${SCFUZZBENCH_WORKDIR}/target" "${SCFUZZBENCH_LOG_DIR}"
}
register_shutdown_trap() { prepare_workspace; }
resolve_target_corpus_dir() {
  printf '%s/%s\\n' "${SCFUZZBENCH_WORKDIR}/target" "${1:-$2}"
}
prepare_shared_seed_corpus() { :; }
require_env() {
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || return 1
  done
}
clone_target() { :; }
capture_target_workspace_anchor() { :; }
apply_benchmark_type() { :; }
build_target() { :; }
set_default_worker_env() { :; }
log() { :; }
run_with_timeout() {
  shift
  printf '%s\\n' "$@" > "${SCFUZZBENCH_LOG_DIR}/command"
}
upload_results() { :; }
""",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "SCFUZZBENCH_COMMON_SH": str(common_sh),
                    "SCFUZZBENCH_WORKDIR": str(work_dir),
                    "SCFUZZBENCH_LOG_DIR": str(log_dir),
                    "SCFUZZBENCH_BENCHMARK_TYPE": "property",
                    "ECHIDNA_VERSION": "test",
                    "ECHIDNA_TARGET": "/tmp/CryticTester.sol",
                    "ECHIDNA_RTS_ARGS": "",
                }
            )

            subprocess.check_call(["bash", str(RUN_SCRIPT)], env=env)

            command = (log_dir / "command").read_text(encoding="utf-8").splitlines()
            self.assertEqual(command[0], "echidna")
            self.assertNotIn("echidna-test", command)

    def test_installer_normalizes_release_binary_name(self):
        for archive_binary in ("echidna", "echidna-test"):
            with self.subTest(archive_binary=archive_binary):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_dir = Path(tmp)
                    archive = tmp_dir / "release.tar.gz"
                    write_release_archive(archive, (archive_binary,))
                    install_root = tmp_dir / "install"
                    bin_dir = install_root / "bin"
                    bin_dir.mkdir(parents=True)
                    write_executable(
                        bin_dir / "echidna-test",
                        "#!/usr/bin/env bash\nexit 0\n",
                    )

                    _, bin_dir = self.run_installer(install_root, archive)
                    installed_binary = bin_dir / "echidna"
                    self.assertTrue(installed_binary.is_file())
                    self.assertTrue(os.access(installed_binary, os.X_OK))
                    self.assertEqual(
                        stat.S_IMODE(installed_binary.stat().st_mode), 0o755
                    )
                    self.assertFalse((bin_dir / "echidna-test").exists())

    def test_installer_rejects_ambiguous_or_unsafe_binary_entries(self):
        cases = (
            ("ambiguous", {"binary_names": ("echidna", "echidna-test")}),
            ("linked", {"linked_binary": True}),
            ("path-traversal", {"unsafe_path": True}),
            ("non-elf", {"binary_contents": b"#!/usr/bin/env bash\nexit 0\n"}),
        )
        for case_name, archive_options in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                archive = tmp_dir / "release.tar.gz"
                write_release_archive(archive, **archive_options)

                result, bin_dir = self.run_installer(
                    tmp_dir / "install", archive, check=False
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid Echidna release archive", result.stderr)
                self.assertFalse((bin_dir / "echidna").exists())

    def test_installer_rejects_version_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            archive = tmp_dir / "release.tar.gz"
            write_release_archive(archive)

            result, bin_dir = self.run_installer(
                tmp_dir / "install", archive, version="../2.3.1", check=False
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid ECHIDNA_VERSION", result.stderr)
            self.assertFalse((bin_dir / "echidna").exists())


if __name__ == "__main__":
    unittest.main()
