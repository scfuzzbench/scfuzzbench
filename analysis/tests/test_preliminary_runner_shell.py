import shlex
import subprocess
from pathlib import Path
import tempfile
import time
import unittest


COMMON_SH = Path(__file__).resolve().parents[2] / "fuzzers" / "_shared" / "common.sh"
SNAPSHOT_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "preliminary_snapshot.py"


def run_bash(script: str, *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-lc", script],
        text=True,
        capture_output=True,
        check=check,
        timeout=10,
    )


class PreliminaryRunnerShellTests(unittest.TestCase):
    def test_interval_defaults_hourly_and_supports_explicit_disable(self):
        common = shlex.quote(str(COMMON_SH))
        result = run_bash(
            f"""
            source {common}
            unset SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS
            first=$(preliminary_snapshot_interval_seconds)
            SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS=off
            second=$(preliminary_snapshot_interval_seconds)
            printf '%s %s\n' "$first" "$second"
            """
        )
        self.assertEqual("3600 0", result.stdout.strip())

    def test_opaque_run_id_schedules_from_explicit_start_epoch_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = int(time.time())
            result = run_bash(
                f"""
                export SCFUZZBENCH_ROOT={shlex.quote(tmp)}
                export SCFUZZBENCH_RUN_ID=gh-24680-1
                export SCFUZZBENCH_RUN_STARTED_AT_EPOCH={now}
                export SCFUZZBENCH_BENCHMARK_UUID={'a' * 32}
                export SCFUZZBENCH_FUZZER_KEY=foundry
                export SCFUZZBENCH_RUN_INDEX=0
                export SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS=3600
                export SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT={shlex.quote(str(SNAPSHOT_HELPER))}
                source {shlex.quote(str(COMMON_SH))}
                start_preliminary_snapshots
                pid="$SCFUZZBENCH_PRELIMINARY_PID"
                kill -0 "$pid"
                stop_preliminary_snapshots
                if kill -0 "$pid" 2>/dev/null; then
                  echo still-running
                  exit 1
                fi
                echo stopped
                """
            )
        self.assertEqual("stopped", result.stdout.strip())
        self.assertNotIn("numeric run ID", result.stderr)

    def test_existing_matching_object_is_idempotent_but_divergent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "snapshot.zip"
            source.write_bytes(b"stable snapshot bytes")
            digest = subprocess.check_output(
                ["sha256sum", str(source)], text=True
            ).split()[0]
            common = shlex.quote(str(COMMON_SH))
            source_q = shlex.quote(str(source))
            matching = run_bash(
                f"""
                export SCFUZZBENCH_S3_BUCKET=bucket
                source {common}
                aws_cli() {{
                  if [[ "$2" == "put-object" ]]; then return 1; fi
                  printf '%s\n' {shlex.quote(digest)}
                }}
                put_preliminary_immutable {source_q} preliminary/gh-1/{'a' * 32}/snapshot.zip
                """
            )
            self.assertEqual(0, matching.returncode)

            divergent = run_bash(
                f"""
                export SCFUZZBENCH_S3_BUCKET=bucket
                source {common}
                aws_cli() {{
                  if [[ "$2" == "put-object" ]]; then return 1; fi
                  printf '%064d\n' 0
                }}
                put_preliminary_immutable {source_q} preliminary/gh-1/{'a' * 32}/snapshot.zip
                """,
                check=False,
            )
            self.assertNotEqual(0, divergent.returncode)
            self.assertIn("Refusing to overwrite divergent", divergent.stderr)


if __name__ == "__main__":
    unittest.main()
