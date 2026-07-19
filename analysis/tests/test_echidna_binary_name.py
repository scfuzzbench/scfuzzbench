import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "fuzzers" / "echidna" / "install.sh"
RUN_SCRIPT = REPO_ROOT / "fuzzers" / "echidna" / "run.sh"


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class EchidnaBinaryNameTests(unittest.TestCase):
    def test_runner_invokes_echidna(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            work_dir = tmp_dir / "work"
            log_dir = tmp_dir / "logs"
            common_sh = tmp_dir / "common.sh"
            common_sh.write_text(
                """
register_shutdown_trap() { :; }
prepare_workspace() {
  mkdir -p "${SCFUZZBENCH_WORKDIR}/target" "${SCFUZZBENCH_LOG_DIR}"
}
require_env() {
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || return 1
  done
}
clone_target() { :; }
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
                    bin_dir = tmp_dir / "bin"
                    fake_commands = tmp_dir / "fake-commands"
                    bin_dir.mkdir()
                    fake_commands.mkdir()

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
log() { :; }
""",
                        encoding="utf-8",
                    )

                    write_executable(fake_commands / "curl", "#!/usr/bin/env bash\nexit 0\n")
                    write_executable(
                        fake_commands / "tar",
                        """#!/usr/bin/env bash
set -euo pipefail
destination=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-C" ]]; then
    destination="$2"
    shift 2
  else
    shift
  fi
done
mkdir -p "${destination}/release"
printf '#!/usr/bin/env bash\\nexit 0\\n' > \
  "${destination}/release/${FAKE_ECHIDNA_ARCHIVE_BINARY}"
chmod +x "${destination}/release/${FAKE_ECHIDNA_ARCHIVE_BINARY}"
""",
                    )

                    env = os.environ.copy()
                    env.update(
                        {
                            "SCFUZZBENCH_COMMON_SH": str(common_sh),
                            "SCFUZZBENCH_BIN_DIR": str(bin_dir),
                            "ECHIDNA_VERSION": "test",
                            "FAKE_ECHIDNA_ARCHIVE_BINARY": archive_binary,
                            "PATH": f"{fake_commands}:{bin_dir}:{env['PATH']}",
                        }
                    )

                    subprocess.check_call(["bash", str(INSTALL_SCRIPT)], env=env)

                    installed_binary = bin_dir / "echidna"
                    self.assertTrue(installed_binary.is_file())
                    self.assertTrue(os.access(installed_binary, os.X_OK))
                    self.assertFalse((bin_dir / "echidna-test").exists())


if __name__ == "__main__":
    unittest.main()
