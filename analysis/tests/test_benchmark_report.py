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

    def test_write_report_includes_progress_metrics_section(self):
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
            progress_csv = tmp_dir / "progress_metrics_summary.csv"
            progress_csv.write_text(
                "\n".join(
                    [
                        "fuzzer,runs,seqps_runs,coverage_runs,corpus_runs,favored_runs,failure_rate_runs,seqps_p50,seqps_p25,seqps_p75,coverage_p50,coverage_p25,coverage_p75,corpus_p50,corpus_p25,corpus_p75,favored_p50,favored_p25,favored_p75,failure_rate_p50,failure_rate_p25,failure_rate_p75",
                        "foundry,1,0,1,1,1,1,,,,280,260,300,85,80,90,66,60,70,0.25,0.20,0.30",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            progress = benchmark_report.load_progress_metrics_summary(progress_csv)

            outpath = tmp_dir / "REPORT.md"
            benchmark_report.write_report(
                metrics=metrics,
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
                progress_metrics_by_fuzzer=progress,
            )
            report = outpath.read_text(encoding="utf-8")

            self.assertIn("Progress metrics from logs", report)
            self.assertIn(
                "| foundry | 1 | 0 | n/a | 1 | 280.00 [260.00,300.00] | 1 | 85.00 [80.00,90.00] | 1 | 66.00 [60.00,70.00] | 1 | 25.0% [20.0%,30.0%] |",
                report,
            )

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
            progress_csv = tmp_dir / "progress_metrics_summary.csv"
            progress_csv.write_text(
                "\n".join(
                    [
                        "fuzzer,runs,seqps_runs,coverage_runs,corpus_runs,favored_runs,failure_rate_runs,seqps_p50,seqps_p25,seqps_p75,coverage_p50,coverage_p25,coverage_p75,corpus_p50,corpus_p25,corpus_p75,favored_p50,favored_p25,favored_p75,failure_rate_p50,failure_rate_p25,failure_rate_p75",
                        "medusa,2,2,2,2,0,2,120,100,140,500,480,520,75,70,80,,,,0.01,0.005,0.02",
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
                    "--progress-metrics-summary-csv",
                    str(progress_csv),
                ]
            )

            report = (out_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("## No data", report)
            self.assertIn("## Throughput metrics (if supported by log format)", report)
            self.assertIn("## Progress metrics from logs (fuzzer-specific proxies)", report)
            self.assertTrue((out_dir / "progress_metrics_levels.png").exists())
            self.assertTrue((out_dir / "progress_metrics_availability.png").exists())

    def test_cli_generates_metric_timeseries_charts_from_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            csv_path = tmp_dir / "cumulative.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "fuzzer,run_id,time_hours,bugs_found",
                        "foundry,run-1,0,0",
                        "foundry,run-1,1,1",
                        "foundry,run-2,0,0",
                        "foundry,run-2,1,1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            throughput_samples_csv = tmp_dir / "throughput_samples.csv"
            throughput_samples_csv.write_text(
                "\n".join(
                    [
                        "run_id,instance_id,fuzzer,fuzzer_label,elapsed_seconds,tx_per_second,gas_per_second,source,log_path",
                        "run-1,i-1,foundry,foundry,0,100,1000,text-rate,a.log",
                        "run-1,i-1,foundry,foundry,3600,120,1400,text-rate,a.log",
                        "run-1,i-2,foundry,foundry,0,90,900,text-rate,b.log",
                        "run-1,i-2,foundry,foundry,3600,110,1300,text-rate,b.log",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            progress_samples_csv = tmp_dir / "progress_metrics_samples.csv"
            progress_samples_csv.write_text(
                "\n".join(
                    [
                        "run_id,instance_id,fuzzer,fuzzer_label,elapsed_seconds,seq_per_second,coverage_proxy,corpus_size,favored_items,failure_rate,source,log_path",
                        "run-1,i-1,foundry,foundry,0,5,100,50,20,0.10,text-metrics,a.log",
                        "run-1,i-1,foundry,foundry,3600,6,130,70,24,0.05,text-metrics,a.log",
                        "run-1,i-2,foundry,foundry,0,4,90,45,18,0.12,text-metrics,b.log",
                        "run-1,i-2,foundry,foundry,3600,5,120,66,22,0.06,text-metrics,b.log",
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
                    "1",
                    "--ks",
                    "1",
                    "--throughput-samples-csv",
                    str(throughput_samples_csv),
                    "--progress-metrics-samples-csv",
                    str(progress_samples_csv),
                ]
            )

            self.assertTrue((out_dir / "tx_per_second_over_time.png").exists())
            self.assertTrue((out_dir / "gas_per_second_over_time.png").exists())
            self.assertTrue((out_dir / "seq_per_second_over_time.png").exists())
            self.assertTrue((out_dir / "coverage_proxy_over_time.png").exists())
            self.assertTrue((out_dir / "corpus_size_over_time.png").exists())
            self.assertTrue((out_dir / "favored_items_over_time.png").exists())
            self.assertTrue((out_dir / "failure_rate_over_time.png").exists())


if __name__ == "__main__":
    unittest.main()
