import shlex
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON = REPO_ROOT / "fuzzers" / "_shared" / "common.sh"


class RunnerTimeoutTests(unittest.TestCase):
    def run_bash(
        self, body: str, *, timeout: float = 10
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", body],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    @staticmethod
    def process_is_running(pid: int) -> bool:
        stat_path = Path(f"/proc/{pid}/stat")
        if not stat_path.exists():
            return False
        try:
            state = stat_path.read_text(encoding="utf-8").split()[2]
        except (FileNotFoundError, IndexError):
            return False
        return state not in {"X", "Z"}

    def test_build_target_rejects_invalid_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            (target / "lib" / "forge-std").mkdir(parents=True)
            result = self.run_bash(
                textwrap.dedent(
                    f"""
                    source {COMMON}
                    log() {{ :; }}
                    export SCFUZZBENCH_WORKDIR={root}
                    export SCFUZZBENCH_BUILD_TIMEOUT_SECONDS=invalid
                    build_target
                    """
                )
            )

        self.assertEqual(2, result.returncode, result.stderr)

    def test_build_target_enforces_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            bin_dir = root / "bin"
            pid_file = root / "forge.pids"
            pwd_file = root / "after-build.pwd"
            (target / "lib" / "forge-std").mkdir(parents=True)
            bin_dir.mkdir()
            forge = bin_dir / "forge"
            forge.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ \"$1\" == build ]]; then\n"
                "  printf '%s\\n' \"$$\" > \"$SCFUZZBENCH_TEST_PID_FILE\"\n"
                "  trap '' TERM\n"
                "  sleep 30 &\n"
                "  printf '%s\\n' \"$!\" >> \"$SCFUZZBENCH_TEST_PID_FILE\"\n"
                "  wait\n"
                "fi\n",
                encoding="utf-8",
            )
            forge.chmod(0o755)
            started = time.monotonic()
            result = self.run_bash(
                textwrap.dedent(
                    f"""
                    source {shlex.quote(str(COMMON))}
                    export PATH={shlex.quote(str(bin_dir))}:$PATH
                    export SCFUZZBENCH_WORKDIR={shlex.quote(str(root))}
                    export SCFUZZBENCH_BUILD_TIMEOUT_SECONDS=1
                    export SCFUZZBENCH_BUILD_TIMEOUT_GRACE_SECONDS=1
                    export SCFUZZBENCH_TEST_PID_FILE={shlex.quote(str(pid_file))}
                    build_status=0
                    build_target || build_status=$?
                    pwd > {shlex.quote(str(pwd_file))}
                    exit "$build_status"
                    """
                ),
                timeout=6,
            )
            elapsed = time.monotonic() - started

            self.assertIn(result.returncode, {124, 137}, result.stderr)
            self.assertLess(elapsed, 4)
            self.assertIn("1s remaining", result.stderr)
            self.assertEqual(
                str(REPO_ROOT),
                pwd_file.read_text(encoding="utf-8").strip(),
            )
            pids = [
                int(value)
                for value in pid_file.read_text(encoding="utf-8").split()
            ]
            deadline = time.monotonic() + 2
            while (
                any(self.process_is_running(pid) for pid in pids)
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue(
                all(not self.process_is_running(pid) for pid in pids)
            )

    def test_run_with_timeout_finishes_when_descendant_keeps_stdout_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "descendant.pid"
            marker = root / "post-run.marker"
            leader_script = textwrap.dedent(
                """
                trap 'exit 0' INT
                (
                  trap '' INT TERM
                  while :; do
                    printf 'holding stdout\n'
                    sleep 0.01
                  done
                ) &
                printf '%s\n' "$!" > "$1"
                while :; do sleep 0.1; done
                """
            )
            started = time.monotonic()
            result = self.run_bash(
                textwrap.dedent(
                    f"""
                    unset SCFUZZBENCH_LOCAL_MODE
                    export SCFUZZBENCH_ROOT={shlex.quote(str(root))}
                    export SCFUZZBENCH_WORKDIR={shlex.quote(str(root / "work"))}
                    export SCFUZZBENCH_LOG_DIR={shlex.quote(str(root / "logs"))}
                    source {shlex.quote(str(COMMON))}
                    prepare_workspace
                    start_preliminary_snapshots() {{ :; }}
                    stop_preliminary_snapshots() {{ :; }}
                    export SCFUZZBENCH_TIMEOUT_SECONDS=1
                    export SCFUZZBENCH_TIMEOUT_GRACE_SECONDS=1
                    run_with_timeout "$SCFUZZBENCH_LOG_DIR/fuzzer.log" \
                      bash -c {shlex.quote(leader_script)} _ \
                      {shlex.quote(str(pid_file))}
                    printf complete > {shlex.quote(str(marker))}
                    """
                ),
                timeout=10,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertLess(elapsed, 8)
            self.assertIn(
                "reached configured benchmark timeout",
                result.stderr,
            )
            self.assertEqual("complete", marker.read_text(encoding="utf-8"))
            self.assertIn(
                "holding stdout",
                (root / "logs" / "fuzzer.log").read_text(encoding="utf-8"),
            )
            descendant_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while (
                self.process_is_running(descendant_pid)
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertFalse(self.process_is_running(descendant_pid))


if __name__ == "__main__":
    unittest.main()
