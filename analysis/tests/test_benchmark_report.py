import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from analysis import benchmark_report
from analysis import plot_palette


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _make_metrics(fuzzer, final_values, runs=None, **kwargs):
    vals = np.array(final_values, dtype=float)
    defaults = dict(
        fuzzer=fuzzer,
        runs=runs if runs is not None else len(vals),
        bugs_p50_t={1.0: int(np.median(vals))},
        bugs_p25_t={1.0: int(np.percentile(vals, 25))},
        bugs_p75_t={1.0: int(np.percentile(vals, 75))},
        auc_norm=0.5,
        plateau_time=0.5,
        late_share=0.1,
        time_to_k_p50={1: 0.5},
        success_rate_k={1: 1.0},
        final_p50=int(np.median(vals)),
        final_iqr=float(np.percentile(vals, 75) - np.percentile(vals, 25)),
        final_values=vals,
    )
    defaults.update(kwargs)
    return benchmark_report.FuzzerMetrics(**defaults)


class BenchmarkReportTests(unittest.TestCase):
    def test_shorten_series_label(self):
        # Matrix labels collapse to "<arm> · <target>", staying distinct.
        self.assertEqual(
            "master · aave-v4",
            benchmark_report.shorten_series_label("foundry-master__target-aave-v4"),
        )
        self.assertEqual(
            "pr-15316 · superform-v2-periphery",
            benchmark_report.shorten_series_label(
                "foundry-pr-15316__target-superform-v2-periphery"
            ),
        )
        # Same arm, different targets stay distinguishable (no collision).
        self.assertNotEqual(
            benchmark_report.shorten_series_label("foundry-master__target-aave-v4"),
            benchmark_report.shorten_series_label("foundry-master__target-liquity"),
        )
        # Anonymized and non-matrix labels pass through unchanged.
        self.assertEqual("Fuzzer A", benchmark_report.shorten_series_label("Fuzzer A"))
        self.assertEqual("foundry", benchmark_report.shorten_series_label("foundry"))
        self.assertEqual(
            "recon-fuzzer",
            benchmark_report.shorten_series_label("recon-fuzzer"),
        )

    def test_build_fuzzer_color_map_is_alphabetical_and_stable(self):
        colors_a = plot_palette.build_fuzzer_color_map(
            ["medusa", "foundry", "echidna", "medusa"]
        )
        colors_b = plot_palette.build_fuzzer_color_map(
            ["echidna", "medusa", "foundry"]
        )
        self.assertEqual(colors_a, colors_b)
        self.assertEqual(["echidna", "foundry", "medusa"], list(colors_a.keys()))

    def test_non_fuzzer_shades_uses_purples(self):
        shades = plot_palette.non_fuzzer_shades(3)
        self.assertEqual(3, len(shades))

        expected = [
            plt.get_cmap("Purples")(0.45),
            plt.get_cmap("Purples")(0.675),
            plt.get_cmap("Purples")(0.9),
        ]
        self.assertEqual(expected, shades)

    def test_build_non_fuzzer_color_map_is_alphabetical(self):
        color_map = plot_palette.build_non_fuzzer_color_map(
            ["medusa", "echidna", "foundry"]
        )
        self.assertEqual(["echidna", "foundry", "medusa"], list(color_map.keys()))

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
                "| foundry | 1 | 0 | n/a | 1 | 85.00 [80.00,90.00] |",
                report,
            )
            self.assertIn("## Coverage over time (opt-in, fuzzer-native signals)", report)
            self.assertIn("Status: **disabled**", report)
            self.assertNotIn("280.00 [260.00,300.00]", report)
            self.assertNotIn("Favored", report)
            self.assertNotIn("Failure-rate", report)

    def test_write_report_documents_enabled_coverage_signals_and_limits(self):
        progress = benchmark_report.load_progress_metrics_summary(
            FIXTURES / "coverage_progress_summary.csv"
        )
        metrics = [
            _make_metrics(fuzzer, [1, 1])
            for fuzzer in ("echidna", "foundry", "medusa", "recon-fuzzer")
        ]

        with tempfile.TemporaryDirectory() as tmp:
            outpath = Path(tmp) / "REPORT.md"
            benchmark_report.write_report(
                metrics=metrics,
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
                progress_metrics_by_fuzzer=progress,
                coverage_over_time=True,
            )
            report = outpath.read_text(encoding="utf-8")

        self.assertIn("Status: **enabled**", report)
        self.assertIn("Source-based coverage is preferred", report)
        self.assertIn("must not be ranked, pooled, or plotted on a shared scale", report)
        self.assertIn("`cov` coverage points", report)
        self.assertIn("`branches hit`", report)
        self.assertIn("`cumulative_edges_seen`", report)
        self.assertIn("`Unique instructions`", report)
        self.assertIn("| echidna | `cov` coverage points (coverage points) | No (runtime proxy)", report)
        self.assertIn("| 2 | 130.00 [125.00,135.00] |", report)

    def test_write_report_omits_scoreboard_without_relative_scores_csv(self):
        metrics = [
            _make_metrics("echidna", [8, 10], final_p50=9),
            _make_metrics("medusa", [3, 5], final_p50=4),
        ]
        progress = {
            "echidna": benchmark_report.ProgressMetricsSummary(
                fuzzer="echidna",
                runs=2,
                seqps_runs=0,
                coverage_runs=2,
                corpus_runs=0,
                favored_runs=0,
                failure_rate_runs=0,
                seqps_p50=None,
                seqps_p25=None,
                seqps_p75=None,
                coverage_p50=300.0,
                coverage_p25=280.0,
                coverage_p75=320.0,
                corpus_p50=None,
                corpus_p25=None,
                corpus_p75=None,
                favored_p50=None,
                favored_p25=None,
                favored_p75=None,
                failure_rate_p50=None,
                failure_rate_p25=None,
                failure_rate_p75=None,
            ),
            "medusa": benchmark_report.ProgressMetricsSummary(
                fuzzer="medusa",
                runs=2,
                seqps_runs=0,
                coverage_runs=2,
                corpus_runs=0,
                favored_runs=0,
                failure_rate_runs=0,
                seqps_p50=None,
                seqps_p25=None,
                seqps_p75=None,
                coverage_p50=150.0,
                coverage_p25=140.0,
                coverage_p75=160.0,
                corpus_p50=None,
                corpus_p25=None,
                corpus_p75=None,
                favored_p50=None,
                favored_p25=None,
                favored_p75=None,
                failure_rate_p50=None,
                failure_rate_p25=None,
                failure_rate_p75=None,
            ),
        }

        with tempfile.TemporaryDirectory() as tmp:
            outpath = Path(tmp) / "REPORT.md"
            benchmark_report.write_report(
                metrics=metrics,
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
                progress_metrics_by_fuzzer=progress,
            )
            report = outpath.read_text(encoding="utf-8")

            # Without an explicit --relative-scores-csv there is no commensurable
            # cross-fuzzer score: per-fuzzer coverage proxies use different units,
            # so the scoreboard must not render at all.
            self.assertNotIn("## Fuzzer scoreboard (higher is better)", report)
            self.assertNotIn("relscore", report)
            self.assertNotIn("relcov", report)

    def test_relative_scores_csv_renders_scoreboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            rel_csv = tmp_dir / "relative.csv"
            rel_csv.write_text(
                "\n".join(
                    [
                        "fuzzer,relscores,relcov",
                        "echidna,0.75,1.0",
                        "medusa,1.0,0.5",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            relative = benchmark_report.load_relative_scores(rel_csv)
            outpath = tmp_dir / "REPORT.md"
            benchmark_report.write_report(
                metrics=[_make_metrics("echidna", [10]), _make_metrics("medusa", [1])],
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
                relative_scores_by_fuzzer=relative,
            )
            report = outpath.read_text(encoding="utf-8")

            self.assertIn("Best bug-finding score: **medusa** (1.000 relscore)", report)
            self.assertIn("Best coverage score: **echidna** (1.000 relcov)", report)
            self.assertIn("| 1 | medusa | 1.000 | 0.500 | compare both scores |", report)

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
            self.assertFalse((out_dir / "progress_metrics_levels.png").exists())
            self.assertFalse((out_dir / "progress_metrics_availability.png").exists())

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
                        "run_id,instance_id,fuzzer,fuzzer_label,elapsed_seconds,seq_per_second,coverage_proxy,corpus_size,source,log_path",
                        "run-1,i-1,foundry,foundry,0,5,100,50,text-metrics,a.log",
                        "run-1,i-1,foundry,foundry,3600,6,130,70,text-metrics,a.log",
                        "run-1,i-2,foundry,foundry,0,4,90,45,text-metrics,b.log",
                        "run-1,i-2,foundry,foundry,3600,5,120,66,text-metrics,b.log",
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
            self.assertFalse((out_dir / "coverage_over_time.png").exists())
            self.assertFalse((out_dir / "coverage_proxy_over_time.png").exists())
            self.assertTrue((out_dir / "corpus_size_over_time.png").exists())
            self.assertFalse((out_dir / "favored_items_over_time.png").exists())
            self.assertFalse((out_dir / "failure_rate_over_time.png").exists())

    def test_cli_generates_opt_in_coverage_chart_with_independent_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            csv_path = FIXTURES / "coverage_cumulative.csv"

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
                    "--progress-metrics-summary-csv",
                    str(FIXTURES / "coverage_progress_summary.csv"),
                    "--progress-metrics-samples-csv",
                    str(FIXTURES / "coverage_progress_samples.csv"),
                    "--coverage-over-time",
                ]
            )

            report = (out_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertTrue((out_dir / "coverage_over_time.png").exists())
            self.assertFalse((out_dir / "coverage_proxy_over_time.png").exists())
            self.assertIn("Status: **enabled**", report)
            self.assertIn("Compatible Foundry JSON pulse builds", report)

    def test_cli_generates_differential_coverage_statistics_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            csv_path = tmp_dir / "cumulative.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "fuzzer,run_id,time_hours,bugs_found",
                        "baseline,run-1,0,0",
                        "baseline,run-1,1,1",
                        "feature,run-1,0,0",
                        "feature,run-1,1,2",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stats_path = tmp_dir / "differential_coverage_statistics.json"
            stats_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "campaign": "by_target/aave-v4",
                                "metric": "relscore",
                                "baseline": "baseline",
                                "feature": "feature",
                                "verdict": "improvement",
                                "baseline_sample_mean": 1.0,
                                "feature_sample_mean": 1.4,
                                "p_value_adjusted": 0.01,
                                "effect_size_a12": 0.82,
                            },
                            {
                                "campaign": "by_target/aave-v4",
                                "metric": "relcov",
                                "baseline": "baseline",
                                "feature": "feature",
                                "relcov_delta_ci_low": -0.01,
                                "relcov_delta_ci_high": 0.08,
                                "noninferiority_delta": 0.05,
                                "relcov_status": "held",
                            },
                        ]
                    }
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
                    "--differential-coverage-statistics-json",
                    str(stats_path),
                ]
            )

            self.assertTrue((out_dir / "differential_coverage_statistics.png").exists())


class LoadRelativeScoresTests(unittest.TestCase):
    def test_header_only_csv_returns_empty(self):
        # The analysis pipeline always writes differential_coverage_relscores.csv,
        # header-only when the run has no showmap campaigns; the report must not die.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "differential_coverage_relscores.csv"
            path.write_text("campaign,approach,relscore,trials,covered_edges\n", encoding="utf-8")
            self.assertEqual({}, benchmark_report.load_relative_scores(path))

    def test_empty_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relscores.csv"
            path.write_text("", encoding="utf-8")
            self.assertEqual({}, benchmark_report.load_relative_scores(path))

    def test_approach_column_is_accepted_as_fuzzer_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relscores.csv"
            path.write_text(
                "campaign,approach,relscore,trials,covered_edges\n"
                "combined,echidna,0.9,4,120\n",
                encoding="utf-8",
            )
            scores = benchmark_report.load_relative_scores(path)
            self.assertIn("echidna", scores)
            self.assertAlmostEqual(0.9, scores["echidna"].relscore)


class RunHealthTests(unittest.TestCase):
    @staticmethod
    def _bugs_df(rows):
        import pandas as pd

        return pd.DataFrame(rows, columns=["fuzzer", "run_id", "time_hours", "bugs_found"])

    @staticmethod
    def _samples_df(rows):
        import pandas as pd

        return pd.DataFrame(rows, columns=["fuzzer", "series_id", "time_hours"])

    def test_flags_leg_with_no_bugs_and_no_samples(self):
        bugs = self._bugs_df(
            [
                ("echidna", "r1", 0.0, 0),
                ("echidna", "r1", 2.0, 0),
                ("foundry", "r1", 0.0, 0),
                ("foundry", "r1", 2.0, 5),
            ]
        )
        samples = self._samples_df([("foundry", "r1:i1", 1.9)])

        warnings = benchmark_report.build_run_health_warnings(bugs, [samples], budget=2.0)

        self.assertEqual(1, len(warnings))
        self.assertIn("echidna", warnings[0])
        self.assertIn("never executed", warnings[0])

    def test_flags_leg_whose_activity_stops_early(self):
        bugs = self._bugs_df(
            [
                ("medusa", "r1", 0.0, 0),
                ("medusa", "r1", 2.0, 3),
            ]
        )
        samples = self._samples_df([("medusa", "r1:i1", 0.3)])

        warnings = benchmark_report.build_run_health_warnings(bugs, [samples], budget=2.0)

        self.assertEqual(1, len(warnings))
        self.assertIn("medusa", warnings[0])
        self.assertIn("terminated early", warnings[0])

    def test_healthy_leg_produces_no_warning(self):
        bugs = self._bugs_df(
            [
                ("foundry", "r1", 0.0, 0),
                ("foundry", "r1", 2.0, 5),
            ]
        )
        samples = self._samples_df([("foundry", "r1:i1", 1.8)])

        warnings = benchmark_report.build_run_health_warnings(bugs, [samples], budget=2.0)

        self.assertEqual([], warnings)

    def test_zero_bug_leg_with_full_length_activity_is_not_flagged(self):
        bugs = self._bugs_df(
            [
                ("recon", "r1", 0.0, 0),
                ("recon", "r1", 2.0, 0),
            ]
        )
        samples = self._samples_df([("recon", "r1:i1", 1.95)])

        warnings = benchmark_report.build_run_health_warnings(bugs, [samples], budget=2.0)

        self.assertEqual([], warnings)

    def test_bug_finding_leg_without_samples_is_not_flagged(self):
        bugs = self._bugs_df(
            [
                ("recon", "r1", 0.0, 0),
                ("recon", "r1", 2.0, 4),
            ]
        )

        warnings = benchmark_report.build_run_health_warnings(bugs, [], budget=2.0)

        self.assertEqual([], warnings)

    def test_report_renders_run_health_section(self):
        metrics = [_make_metrics("echidna", [0, 0])]
        with tempfile.TemporaryDirectory() as tmp:
            outpath = Path(tmp) / "REPORT.md"
            benchmark_report.write_report(
                metrics,
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
                run_health_warnings=["**echidna**: no bugs and no runtime activity samples"],
            )
            content = outpath.read_text(encoding="utf-8")

        self.assertIn("## ⚠️ Run health", content)
        self.assertIn("not comparable", content)
        self.assertIn("echidna", content)


class StatisticalTestsTests(unittest.TestCase):
    def test_significant_difference(self):
        m_a = _make_metrics("echidna", [7, 8, 7, 8, 7, 8, 7, 8, 7, 8])
        m_b = _make_metrics("medusa", [2, 3, 3, 2, 3, 2, 3, 2, 3, 2])
        results, warnings = benchmark_report.compute_statistical_tests([m_a, m_b])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].significant)
        self.assertEqual(results[0].direction, ">")
        self.assertLess(results[0].p_corrected, 0.05)

    def test_identical_distributions(self):
        vals = [5, 5, 5, 5, 5]
        m_a = _make_metrics("fuzzer_a", vals)
        m_b = _make_metrics("fuzzer_b", vals)
        results, warnings = benchmark_report.compute_statistical_tests([m_a, m_b])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].significant)
        self.assertEqual(results[0].p_value, 1.0)
        self.assertEqual(results[0].direction, "=")

    def test_single_fuzzer_returns_empty(self):
        m = _make_metrics("only_one", [5, 6, 7])
        results, warnings = benchmark_report.compute_statistical_tests([m])
        self.assertEqual(len(results), 0)
        self.assertTrue(any("Only one fuzzer" in w for w in warnings))

    def test_small_sample_warning(self):
        m_a = _make_metrics("a", [5, 6, 7])
        m_b = _make_metrics("b", [3, 4, 5])
        results, warnings = benchmark_report.compute_statistical_tests([m_a, m_b])
        self.assertTrue(any("fewer than 5 runs" in w for w in warnings))

    def test_bonferroni_correction(self):
        m_a = _make_metrics("a", [7, 8, 7, 8, 7, 8, 7, 8])
        m_b = _make_metrics("b", [2, 3, 2, 3, 2, 3, 2, 3])
        m_c = _make_metrics("c", [5, 5, 5, 5, 5, 5, 5, 5])
        results, _ = benchmark_report.compute_statistical_tests([m_a, m_b, m_c])
        # 3 pairwise comparisons
        self.assertEqual(len(results), 3)
        for r in results:
            # p_corrected should be p_value * 3, clamped to 1.0
            expected = min(r.p_value * 3, 1.0)
            self.assertAlmostEqual(r.p_corrected, expected, places=10)

    def test_fewer_than_2_runs_skipped(self):
        m_a = _make_metrics("a", [5])
        m_b = _make_metrics("b", [3, 4, 5])
        results, warnings = benchmark_report.compute_statistical_tests([m_a, m_b])
        self.assertEqual(len(results), 0)
        self.assertTrue(any("fewer than 2 runs" in w for w in warnings))

    def test_natural_language_conclusions(self):
        m_a = _make_metrics("echidna", [7, 8, 7, 8, 7, 8, 7, 8, 7, 8])
        m_b = _make_metrics("medusa", [2, 3, 3, 2, 3, 2, 3, 2, 3, 2])
        results, warnings = benchmark_report.compute_statistical_tests([m_a, m_b])
        lines = benchmark_report.format_statistical_report(results, warnings)
        text = "\n".join(lines)
        self.assertIn("echidna finds significantly more bugs than medusa", text)

    def test_no_significant_fallback_text(self):
        m_a = _make_metrics("a", [5, 5, 5, 5, 5])
        m_b = _make_metrics("b", [5, 5, 5, 5, 5])
        results, warnings = benchmark_report.compute_statistical_tests([m_a, m_b])
        lines = benchmark_report.format_statistical_report(results, warnings)
        text = "\n".join(lines)
        self.assertIn("No pairwise comparison reached significance", text)

    def test_write_report_integration(self):
        m_a = _make_metrics("echidna", [7, 8, 7, 8, 7, 8, 7, 8, 7, 8])
        m_b = _make_metrics("medusa", [2, 3, 3, 2, 3, 2, 3, 2, 3, 2])
        results, warnings = benchmark_report.compute_statistical_tests([m_a, m_b])

        with tempfile.TemporaryDirectory() as tmp:
            outpath = Path(tmp) / "REPORT.md"
            benchmark_report.write_report(
                metrics=[m_a, m_b],
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
                stat_results=results,
                stat_warnings=warnings,
            )
            report = outpath.read_text(encoding="utf-8")
            self.assertIn("## Statistical comparison (Mann-Whitney U test)", report)
            self.assertIn("Mann-Whitney U test assesses", report)
            # Should appear after Milestones and before Shape-based
            milestones_pos = report.index("## Milestones")
            stat_pos = report.index("## Statistical comparison")
            shape_pos = report.index("## Shape-based interpretation")
            self.assertLess(milestones_pos, stat_pos)
            self.assertLess(stat_pos, shape_pos)

    def test_write_report_no_stat_section_when_none(self):
        m = _make_metrics("foundry", [1])
        with tempfile.TemporaryDirectory() as tmp:
            outpath = Path(tmp) / "REPORT.md"
            benchmark_report.write_report(
                metrics=[m],
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=outpath,
            )
            report = outpath.read_text(encoding="utf-8")
            self.assertNotIn("## Statistical comparison", report)

    def test_cli_end_to_end_with_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            csv_path = tmp_dir / "cumulative.csv"
            rows = ["fuzzer,run_id,time_hours,bugs_found"]
            for i in range(1, 6):
                rows.append(f"echidna,run-{i},0,0")
                rows.append(f"echidna,run-{i},1,{6 + i}")
                rows.append(f"medusa,run-{i},0,0")
                rows.append(f"medusa,run-{i},1,{i}")
            csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

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
                ]
            )
            report = (out_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("## Statistical comparison (Mann-Whitney U test)", report)
            self.assertIn("echidna", report)
            self.assertIn("medusa", report)


if __name__ == "__main__":
    unittest.main()
