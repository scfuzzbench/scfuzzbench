import csv
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from analysis import analyze


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "fuzzers" / "foundry" / "run.sh"
FOUNDRY_OUTPUT = (
    ROOT
    / "analysis"
    / "tests"
    / "fixtures"
    / "foundry-throughput-progress.log"
)


class FoundryThroughputEndToEndTests(unittest.TestCase):
    def test_runner_output_populates_summary_report_and_charts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            work_dir = tmp_dir / "work"
            log_dir = tmp_dir / "logs"
            common_sh = tmp_dir / "common.sh"
            common_sh.write_text(
                textwrap.dedent(
                    """
                    prepare_workspace() {
                      mkdir -p "${SCFUZZBENCH_WORKDIR}/target" "${SCFUZZBENCH_LOG_DIR}"
                      SCFUZZBENCH_LOG_ROOT_ANCHOR="${SCFUZZBENCH_LOG_DIR}"
                      SCFUZZBENCH_LOG_ROOT_IDENTITY="test-log-anchor"
                      cat > "${SCFUZZBENCH_WORKDIR}/target/foundry.toml" <<'TOML'
                    [invariant]
                    corpus_dir = "corpus/foundry"
                    TOML
                    }
                    register_shutdown_trap() { prepare_workspace; }
                    resolve_target_corpus_dir() {
                      printf '%s/%s\\n' "${SCFUZZBENCH_WORKDIR}/target" "${1:-$2}"
                    }
                    prepare_shared_seed_corpus() { :; }
                    remove_strict_descendant_tree() { rm -rf -- "$1"; }
                    mkdir_strict_descendant() { mkdir -p -- "$1"; }
                    clone_target() { :; }
                    capture_target_workspace_anchor() {
                      SCFUZZBENCH_TARGET_ROOT_ANCHOR="${SCFUZZBENCH_WORKDIR}/target"
                      SCFUZZBENCH_TARGET_ROOT_IDENTITY="test-anchor"
                    }
                    apply_benchmark_type() { :; }
                    build_target() { :; }
                    set_default_worker_env() { :; }
                    upload_results() { :; }
                    log() { :; }
                    require_env() {
                      for name in "$@"; do
                        if [[ -z "${!name:-}" ]]; then
                          return 1
                        fi
                      done
                    }
                    run_with_timeout() {
                      local log_file=$1
                      shift
                      printf '%s\\n' "$@" > "${SCFUZZBENCH_LOG_DIR}/foundry-command.txt"
                      cp "${SCFUZZBENCH_TEST_FOUNDRY_OUTPUT}" "${log_file}"
                    }
                    """
                ).lstrip(),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "SCFUZZBENCH_COMMON_SH": str(common_sh),
                    "SCFUZZBENCH_WORKDIR": str(work_dir),
                    "SCFUZZBENCH_LOG_DIR": str(log_dir),
                    "SCFUZZBENCH_TIMEOUT_SECONDS": "120",
                    "SCFUZZBENCH_TEST_FOUNDRY_OUTPUT": str(FOUNDRY_OUTPUT),
                    "FOUNDRY_LABEL": "foundry-git-test",
                }
            )
            subprocess.check_call(["bash", str(RUNNER)], env=env)

            command = (log_dir / "foundry-command.txt").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertIn("--show-progress", command)
            workers_index = command.index("--invariant-workers")
            self.assertEqual(command[workers_index + 1], "auto")

            # The bounded corpus and throughput telemetry remain active
            # together in the default benchmark configuration.
            foundry_toml = (work_dir / "target" / "foundry.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn("corpus_dir", foundry_toml)

            foundry_log = log_dir / "foundry.log"
            raw_log = foundry_log.read_text(encoding="utf-8")
            self.assertIn("Ran 1 test for", raw_log)
            self.assertIn("Suite result: ok.", raw_log)

            samples = analyze.parse_throughput_log(
                foundry_log,
                "run-1",
                "i-foundry",
                "foundry-git-test",
            )
            self.assertEqual(len(samples), 2)
            self.assertEqual(samples[-1].source, "json-rate")
            self.assertAlmostEqual(samples[-1].tx_per_second, 40.0)
            self.assertAlmostEqual(samples[-1].gas_per_second, 900000.0)

            samples_csv = tmp_dir / "throughput_samples.csv"
            summary_csv = tmp_dir / "throughput_summary.csv"
            analyze.write_throughput_samples_csv(samples, samples_csv)
            analyze.write_throughput_summary_csv(samples, summary_csv)

            with summary_csv.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["fuzzer"], "foundry")
            self.assertEqual(rows[0]["txps_runs"], "1")
            self.assertEqual(rows[0]["gasps_runs"], "1")
            self.assertAlmostEqual(float(rows[0]["txps_p50"]), 40.0)
            self.assertAlmostEqual(float(rows[0]["gasps_p50"]), 900000.0)

            cumulative_csv = tmp_dir / "cumulative.csv"
            cumulative_csv.write_text(
                "\n".join(
                    [
                        "fuzzer,run_id,time_hours,bugs_found",
                        "foundry,run-1,0,0",
                        "foundry,run-1,1,1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report_dir = tmp_dir / "report"
            subprocess.check_call(
                [
                    sys.executable,
                    str(ROOT / "analysis" / "benchmark_report.py"),
                    "--csv",
                    str(cumulative_csv),
                    "--outdir",
                    str(report_dir),
                    "--budget",
                    "1",
                    "--checkpoints",
                    "1",
                    "--ks",
                    "1",
                    "--throughput-samples-csv",
                    str(samples_csv),
                    "--throughput-summary-csv",
                    str(summary_csv),
                ]
            )

            report = (report_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("## Throughput metrics", report)
            self.assertIn("| foundry | 1 | 1 | 40.00", report)
            self.assertTrue((report_dir / "tx_per_second_over_time.png").exists())
            self.assertTrue((report_dir / "gas_per_second_over_time.png").exists())


if __name__ == "__main__":
    unittest.main()
