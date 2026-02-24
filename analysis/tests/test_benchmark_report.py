import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analysis import benchmark_report


class BenchmarkReportTests(unittest.TestCase):
    def test_write_report_mentions_invariant_artifacts(self):
        metrics = [
            benchmark_report.FuzzerMetrics(
                fuzzer="foundry",
                runs=1,
                bugs_p50_t={1.0: 1},
                bugs_p25_t={1.0: 1},
                bugs_p75_t={1.0: 1},
                auc_norm=0.5,
                plateau_time=0.5,
                late_share=0.1,
                time_to_k_p50={1: 0.5},
                success_rate_k={1: 1.0},
                final_p50=1,
                final_iqr=0.0,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            outpath = Path(tmp) / "REPORT.md"
            benchmark_report.write_report(
                metrics=metrics,
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
            )
            report = outpath.read_text(encoding="utf-8")

            self.assertIn("count-based", report)
            self.assertIn("broken_invariants.md", report)
            self.assertIn("broken_invariants.csv", report)

    def test_write_report_includes_throughput_section(self):
        metrics = [
            benchmark_report.FuzzerMetrics(
                fuzzer="foundry",
                runs=1,
                bugs_p50_t={1.0: 1},
                bugs_p25_t={1.0: 1},
                bugs_p75_t={1.0: 1},
                auc_norm=0.5,
                plateau_time=0.5,
                late_share=0.1,
                time_to_k_p50={1: 0.5},
                success_rate_k={1: 1.0},
                final_p50=1,
                final_iqr=0.0,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            throughput_csv = tmp_dir / "throughput_summary.csv"
            throughput_csv.write_text(
                "\n".join(
                    [
                        "fuzzer,runs,txps_runs,gasps_runs,txps_p50,txps_p25,txps_p75,gasps_p50,gasps_p25,gasps_p75",
                        "foundry,1,1,1,12.0,11.0,13.0,950.0,900.0,1000.0",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            throughput = benchmark_report.load_throughput_summary(throughput_csv)

            outpath = tmp_dir / "REPORT.md"
            benchmark_report.write_report(
                metrics=metrics,
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
                throughput_by_fuzzer=throughput,
            )
            report = outpath.read_text(encoding="utf-8")

            self.assertIn("Throughput metrics", report)
            self.assertIn("| foundry | 1 | 1 | 12.00 [11.00,13.00] | 1 | 950.00 [900.00,1000.00] |", report)

    def test_cli_clamps_checkpoints_to_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            csv_path = tmp_dir / "cumulative.csv"
            csv_path.write_text(
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

            out_dir = tmp_dir / "out"
            script = Path(__file__).resolve().parents[1] / "benchmark_report.py"
            subprocess.check_call(
                [
                    sys.executable,
                    str(script),
                    "--csv",
                    str(csv_path),
                    "--outdir",
                    str(out_dir),
                    "--budget",
                    "1",
                    "--checkpoints",
                    "1,4,8,24",
                    "--ks",
                    "1",
                ]
            )

            report = (out_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("| Fuzzer | Runs | 1h |", report)
            self.assertNotIn("| 4h |", report)
            self.assertNotIn("| 8h |", report)
            self.assertNotIn("| 24h |", report)

    def test_cli_no_data_report_includes_throughput_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            csv_path = tmp_dir / "cumulative.csv"
            csv_path.write_text("fuzzer,run_id,time_hours,bugs_found\n", encoding="utf-8")
            throughput_csv = tmp_dir / "throughput_summary.csv"
            throughput_csv.write_text(
                "\n".join(
                    [
                        "fuzzer,runs,txps_runs,gasps_runs,txps_p50,txps_p25,txps_p75,gasps_p50,gasps_p25,gasps_p75",
                        "foundry,2,2,2,10,9,11,700,600,800",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            out_dir = tmp_dir / "out"
            script = Path(__file__).resolve().parents[1] / "benchmark_report.py"
            subprocess.check_call(
                [
                    sys.executable,
                    str(script),
                    "--csv",
                    str(csv_path),
                    "--outdir",
                    str(out_dir),
                    "--throughput-summary-csv",
                    str(throughput_csv),
                ]
            )

            report = (out_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("## No data", report)
            self.assertIn("## Throughput metrics (if supported by log format)", report)


if __name__ == "__main__":
    unittest.main()
