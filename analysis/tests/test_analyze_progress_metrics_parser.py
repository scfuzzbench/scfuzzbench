import csv
import tempfile
import unittest
from pathlib import Path

from analysis import analyze


class ProgressMetricsParserTests(unittest.TestCase):
    def write_log(self, lines):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            tmp.write("\n".join(lines) + "\n")
            tmp.close()
            return Path(tmp.name)
        except Exception:
            tmp.close()
            raise

    def test_parses_medusa_progress_metrics_from_actual_status_line(self):
        log_path = self.write_log(
            [
                "fuzz: elapsed: 6s, calls: 61658 (20486/sec), seq/s: 211, branches hit: 537, corpus: 137, failures: 15/762, gas/s: 4500638777",
            ]
        )

        samples = analyze.parse_progress_metrics_log(
            log_path, "run-1", "i-1", "medusa-vtest"
        )
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.fuzzer, "medusa")
        self.assertEqual(sample.source, "text-metrics")
        self.assertAlmostEqual(sample.elapsed_seconds, 6.0)
        self.assertAlmostEqual(sample.seq_per_second, 211.0)
        self.assertAlmostEqual(sample.coverage_proxy, 537.0)
        self.assertAlmostEqual(sample.corpus_size, 137.0)
        self.assertIsNone(sample.favored_items)
        self.assertAlmostEqual(sample.failure_rate, 15.0 / 762.0)

    def test_parses_echidna_progress_metrics_from_actual_status_lines(self):
        log_path = self.write_log(
            [
                "[2026-02-24 14:35:10.44] [status] tests: 4/14, fuzzing: 7098/50000, values: [], cov: 4474, corpus: 9, shrinking: W2:1247/5000(4) W1:3851/5000(2) W0:1666/5000(4), gas/s: 12935790057",
                "[2026-02-24 14:35:13.45] [status] tests: 4/14, fuzzing: 16822/50000, values: [], cov: 4474, corpus: 9, shrinking: W2:3263/5000(4) W0:3460/5000(4), gas/s: 16646929823",
            ]
        )

        samples = analyze.parse_progress_metrics_log(
            log_path, "run-1", "i-1", "echidna-vtest"
        )
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].fuzzer, "echidna")
        self.assertEqual(samples[0].source, "text-metrics")
        self.assertAlmostEqual(samples[1].elapsed_seconds, 3.01, places=2)
        self.assertAlmostEqual(samples[1].coverage_proxy, 4474.0)
        self.assertAlmostEqual(samples[1].corpus_size, 9.0)
        self.assertIsNone(samples[1].seq_per_second)
        self.assertIsNone(samples[1].failure_rate)

    def test_parses_foundry_progress_metrics_from_json_metrics(self):
        log_path = self.write_log(
            [
                '{"type":"invariant_metrics","timestamp":100,"invariant":"invariant_noop","failed_current":1,"failed_total":2,"metrics":{"cumulative_edges_seen":231,"cumulative_features_seen":0,"corpus_count":47,"favored_items":34}}',
                '{"type":"invariant_metrics","timestamp":105,"invariant":"invariant_noop","failed_current":2,"failed_total":8,"metrics":{"cumulative_edges_seen":259,"cumulative_features_seen":0,"corpus_count":65,"favored_items":44}}',
            ]
        )

        samples = analyze.parse_progress_metrics_log(
            log_path, "run-1", "i-1", "foundry-git-test"
        )
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[1].fuzzer, "foundry")
        self.assertEqual(samples[1].source, "json-metrics")
        self.assertAlmostEqual(samples[1].elapsed_seconds, 5.0)
        self.assertAlmostEqual(samples[1].coverage_proxy, 259.0)
        self.assertAlmostEqual(samples[1].corpus_size, 65.0)
        self.assertAlmostEqual(samples[1].favored_items, 44.0)
        self.assertAlmostEqual(samples[1].failure_rate, 0.25)

    def test_writes_progress_metrics_summary_csv_with_latest_run_values(self):
        samples = [
            analyze.ProgressMetricsSample(
                run_id="run-1",
                instance_id="i-1",
                fuzzer="medusa",
                fuzzer_label="medusa-v1",
                elapsed_seconds=3.0,
                seq_per_second=100.0,
                coverage_proxy=500.0,
                corpus_size=10.0,
                favored_items=None,
                failure_rate=0.1,
                source="text-metrics",
                log_path="/tmp/medusa-1.log",
            ),
            analyze.ProgressMetricsSample(
                run_id="run-1",
                instance_id="i-1",
                fuzzer="medusa",
                fuzzer_label="medusa-v1",
                elapsed_seconds=6.0,
                seq_per_second=200.0,
                coverage_proxy=700.0,
                corpus_size=20.0,
                favored_items=None,
                failure_rate=0.2,
                source="text-metrics",
                log_path="/tmp/medusa-1.log",
            ),
            analyze.ProgressMetricsSample(
                run_id="run-2",
                instance_id="i-2",
                fuzzer="medusa",
                fuzzer_label="medusa-v1",
                elapsed_seconds=4.0,
                seq_per_second=300.0,
                coverage_proxy=600.0,
                corpus_size=15.0,
                favored_items=None,
                failure_rate=0.3,
                source="text-metrics",
                log_path="/tmp/medusa-2.log",
            ),
            analyze.ProgressMetricsSample(
                run_id="run-3",
                instance_id="i-3",
                fuzzer="foundry",
                fuzzer_label="foundry-git-test",
                elapsed_seconds=5.0,
                seq_per_second=None,
                coverage_proxy=80.0,
                corpus_size=12.0,
                favored_items=9.0,
                failure_rate=1.0,
                source="json-metrics",
                log_path="/tmp/foundry.log",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "progress_metrics_summary.csv"
            analyze.write_progress_metrics_summary_csv(samples, out_path)

            rows = {}
            with out_path.open("r", newline="") as handle:
                for row in csv.DictReader(handle):
                    rows[row["fuzzer"]] = row

        medusa = rows["medusa"]
        self.assertEqual(medusa["runs"], "2")
        self.assertEqual(medusa["seqps_runs"], "2")
        self.assertEqual(medusa["coverage_runs"], "2")
        self.assertEqual(medusa["corpus_runs"], "2")
        self.assertEqual(medusa["favored_runs"], "0")
        self.assertEqual(medusa["failure_rate_runs"], "2")
        self.assertAlmostEqual(float(medusa["seqps_p50"]), 250.0)
        self.assertAlmostEqual(float(medusa["coverage_p50"]), 650.0)
        self.assertAlmostEqual(float(medusa["corpus_p50"]), 17.5)
        self.assertAlmostEqual(float(medusa["failure_rate_p50"]), 0.25)

        foundry = rows["foundry"]
        self.assertEqual(foundry["runs"], "1")
        self.assertEqual(foundry["seqps_runs"], "0")
        self.assertEqual(foundry["favored_runs"], "1")
        self.assertAlmostEqual(float(foundry["coverage_p50"]), 80.0)
        self.assertAlmostEqual(float(foundry["corpus_p50"]), 12.0)
        self.assertAlmostEqual(float(foundry["favored_p50"]), 9.0)
        self.assertAlmostEqual(float(foundry["failure_rate_p50"]), 1.0)


if __name__ == "__main__":
    unittest.main()
