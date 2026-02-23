import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "runner_metrics_report.py"


class RunnerMetricsReportTests(unittest.TestCase):
    def write_metrics_csv(self, path: Path, rows: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "timestamp,uptime_seconds,load1,load5,load15,cpu_user_pct,cpu_system_pct,"
            "cpu_idle_pct,cpu_iowait_pct,mem_total_kb,mem_available_kb,mem_used_kb,"
            "swap_total_kb,swap_free_kb,swap_used_kb\n"
        )
        path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")

    def test_generates_usage_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            logs_dir = tmp_dir / "logs"
            self.write_metrics_csv(
                logs_dir / "i-aaa-foundry" / "runner_metrics.csv",
                [
                    "2026-02-23T00:00:00+00:00,1000,0.1,0.1,0.1,10,5,80,5,1000000,700000,300000,0,0,0",
                    "2026-02-23T00:00:20+00:00,1020,0.2,0.2,0.2,20,10,65,5,1000000,600000,400000,0,0,0",
                    "2026-02-23T00:00:40+00:00,1040,0.3,0.3,0.3,30,15,50,5,1000000,500000,500000,0,0,0",
                ],
            )
            self.write_metrics_csv(
                logs_dir / "i-bbb-medusa" / "runner_metrics.csv",
                [
                    "2026-02-23T00:00:00+00:00,2000,0.1,0.1,0.1,5,5,85,5,2000000,1500000,500000,0,0,0",
                    "2026-02-23T00:00:20+00:00,2020,0.2,0.2,0.2,10,10,75,5,2000000,1400000,600000,0,0,0",
                    "2026-02-23T00:00:40+00:00,2040,0.3,0.3,0.3,20,10,65,5,2000000,1200000,800000,0,0,0",
                ],
            )

            out_summary = tmp_dir / "runner_resource_summary.csv"
            out_timeseries = tmp_dir / "runner_resource_timeseries.csv"
            out_md = tmp_dir / "runner_resource_usage.md"
            cpu_png = tmp_dir / "cpu_usage_over_time.png"
            memory_png = tmp_dir / "memory_usage_over_time.png"

            subprocess.check_call(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--logs-dir",
                    str(logs_dir),
                    "--out-summary-csv",
                    str(out_summary),
                    "--out-timeseries-csv",
                    str(out_timeseries),
                    "--out-md",
                    str(out_md),
                    "--out-cpu-png",
                    str(cpu_png),
                    "--out-memory-png",
                    str(memory_png),
                    "--run-id",
                    "1772000000",
                    "--budget-hours",
                    "0.01",
                    "--bin-seconds",
                    "20",
                ]
            )

            self.assertTrue(out_summary.exists())
            self.assertTrue(out_timeseries.exists())
            self.assertTrue(out_md.exists())
            self.assertTrue(cpu_png.exists())
            self.assertTrue(memory_png.exists())

            with out_summary.open("r", newline="", encoding="utf-8") as handle:
                summary_rows = list(csv.DictReader(handle))
            self.assertEqual(2, len(summary_rows))
            self.assertEqual({"foundry", "medusa"}, {row["fuzzer"] for row in summary_rows})

            with out_timeseries.open("r", newline="", encoding="utf-8") as handle:
                timeseries_rows = list(csv.DictReader(handle))
            self.assertTrue(timeseries_rows)
            self.assertTrue(all(float(row["elapsed_seconds"]) <= 36.0 for row in timeseries_rows))

            report_text = out_md.read_text(encoding="utf-8")
            self.assertIn("# Runner resource usage", report_text)
            self.assertIn("foundry", report_text)
            self.assertIn("medusa", report_text)

    def test_handles_missing_metrics_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            logs_dir = tmp_dir / "logs"
            (logs_dir / "i-aaa-foundry").mkdir(parents=True, exist_ok=True)
            (logs_dir / "i-aaa-foundry" / "fuzzer.log").write_text("hello\n", encoding="utf-8")

            out_summary = tmp_dir / "runner_resource_summary.csv"
            out_timeseries = tmp_dir / "runner_resource_timeseries.csv"
            out_md = tmp_dir / "runner_resource_usage.md"
            cpu_png = tmp_dir / "cpu_usage_over_time.png"
            memory_png = tmp_dir / "memory_usage_over_time.png"

            subprocess.check_call(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--logs-dir",
                    str(logs_dir),
                    "--out-summary-csv",
                    str(out_summary),
                    "--out-timeseries-csv",
                    str(out_timeseries),
                    "--out-md",
                    str(out_md),
                    "--out-cpu-png",
                    str(cpu_png),
                    "--out-memory-png",
                    str(memory_png),
                ]
            )

            with out_summary.open("r", newline="", encoding="utf-8") as handle:
                summary_rows = list(csv.DictReader(handle))
            with out_timeseries.open("r", newline="", encoding="utf-8") as handle:
                timeseries_rows = list(csv.DictReader(handle))

            self.assertEqual([], summary_rows)
            self.assertEqual([], timeseries_rows)
            self.assertIn("No `runner_metrics*.csv` files were found", out_md.read_text(encoding="utf-8"))
            self.assertTrue(cpu_png.exists())
            self.assertTrue(memory_png.exists())

    def test_accepts_prefixed_runner_metrics_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            logs_dir = tmp_dir / "logs"
            self.write_metrics_csv(
                logs_dir / "i-aaa-foundry" / "logs__runner_metrics.csv",
                [
                    "2026-02-23T00:00:00+00:00,1000,0.1,0.1,0.1,10,5,80,5,1000000,700000,300000,0,0,0",
                    "2026-02-23T00:00:20+00:00,1020,0.2,0.2,0.2,20,10,65,5,1000000,600000,400000,0,0,0",
                ],
            )

            out_summary = tmp_dir / "runner_resource_summary.csv"
            out_timeseries = tmp_dir / "runner_resource_timeseries.csv"
            out_md = tmp_dir / "runner_resource_usage.md"
            cpu_png = tmp_dir / "cpu_usage_over_time.png"
            memory_png = tmp_dir / "memory_usage_over_time.png"

            subprocess.check_call(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--logs-dir",
                    str(logs_dir),
                    "--out-summary-csv",
                    str(out_summary),
                    "--out-timeseries-csv",
                    str(out_timeseries),
                    "--out-md",
                    str(out_md),
                    "--out-cpu-png",
                    str(cpu_png),
                    "--out-memory-png",
                    str(memory_png),
                    "--run-id",
                    "1772000001",
                ]
            )

            with out_summary.open("r", newline="", encoding="utf-8") as handle:
                summary_rows = list(csv.DictReader(handle))
            with out_timeseries.open("r", newline="", encoding="utf-8") as handle:
                timeseries_rows = list(csv.DictReader(handle))

            self.assertEqual(1, len(summary_rows))
            self.assertEqual("foundry", summary_rows[0]["fuzzer"])
            self.assertTrue(timeseries_rows)
            self.assertNotIn("No `runner_metrics*.csv` files were found", out_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
