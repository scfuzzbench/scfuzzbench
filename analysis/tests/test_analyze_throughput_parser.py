import tempfile
import unittest
from pathlib import Path

from analysis import analyze


class ThroughputParserTests(unittest.TestCase):
    def write_log(self, lines):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            tmp.write("\n".join(lines) + "\n")
            tmp.close()
            return Path(tmp.name)
        except Exception:
            tmp.close()
            raise

    def test_parses_medusa_txps_and_gasps_from_actual_status_line(self):
        log_path = self.write_log(
            [
                "fuzz: elapsed: 6s, calls: 61658 (20486/sec), seq/s: 211, branches hit: 537, corpus: 137, failures: 15/762, gas/s: 4500638777",
            ]
        )

        samples = analyze.parse_throughput_log(log_path, "run-1", "i-1", "medusa-vtest")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].fuzzer, "medusa")
        self.assertEqual(samples[0].source, "text-rate")
        self.assertAlmostEqual(samples[0].elapsed_seconds, 6.0)
        self.assertAlmostEqual(samples[0].tx_per_second, 20486.0)
        self.assertAlmostEqual(samples[0].gas_per_second, 4500638777.0)

    def test_parses_echidna_gasps_and_derives_instantaneous_txps(self):
        log_path = self.write_log(
            [
                "[2026-02-24 14:35:10.44] [status] tests: 4/14, fuzzing: 7098/50000, values: [], cov: 4474, corpus: 9, shrinking: W2:1247/5000(4) W1:3851/5000(2) W0:1666/5000(4), gas/s: 12935790057",
                "[2026-02-24 14:35:13.45] [status] tests: 4/14, fuzzing: 16822/50000, values: [], cov: 4474, corpus: 9, shrinking: W2:3263/5000(4) W0:3460/5000(4), gas/s: 16646929823",
            ]
        )

        samples = analyze.parse_throughput_log(log_path, "run-1", "i-1", "echidna-vtest")
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].fuzzer, "echidna")
        # First status line has no predecessor, so it only reports the direct
        # gas/s rate and seeds the tx baseline; no interval tx/s is derived yet.
        self.assertIsNone(samples[0].tx_per_second)
        self.assertAlmostEqual(samples[0].gas_per_second, 12935790057.0)
        # Second line derives an instantaneous rate over the interval:
        # (16822 - 7098) transactions / 3.01 s.
        self.assertEqual(samples[1].source, "text-interval")
        self.assertAlmostEqual(samples[1].elapsed_seconds, 3.01)
        self.assertAlmostEqual(
            samples[1].tx_per_second, (16822.0 - 7098.0) / 3.01, places=4
        )
        self.assertAlmostEqual(samples[1].gas_per_second, 16646929823.0)

    def test_foundry_actual_invariant_lines_do_not_emit_throughput(self):
        log_path = self.write_log(
            [
                '{"timestamp":1771954269,"invariant":"invariant_noop","metrics":{"cumulative_edges_seen":231,"cumulative_features_seen":0,"corpus_count":47,"favored_items":34}}',
                '{"timestamp":1771954274,"invariant":"invariant_noop","metrics":{"cumulative_edges_seen":259,"cumulative_features_seen":0,"corpus_count":65,"favored_items":44}}',
                "No files changed, compilation skipped",
            ]
        )

        samples = analyze.parse_throughput_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(samples, [])

    def test_parses_foundry_oss333_pulse_throughput_aliases(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","contract":"CryticToFoundry","metrics":{"broken_invariants":0,"broken_assertions":0},"total_txs":20,"total_gas":2000,"tps":10.12,"gps":1000.34,"worker":{"id":0,"count":1}}',
                '{"timestamp":105,"event":"pulse","contract":"CryticToFoundry","metrics":{"broken_invariants":1,"broken_assertions":2},"total_txs":80,"total_gas":9000,"tps":16.0,"gps":1800.0,"worker":{"id":0,"count":1}}',
            ]
        )

        samples = analyze.parse_throughput_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].source, "json-rate")
        self.assertAlmostEqual(samples[0].tx_per_second, 10.12)
        self.assertAlmostEqual(samples[0].gas_per_second, 1000.34)
        self.assertAlmostEqual(samples[1].elapsed_seconds, 5.0)
        self.assertAlmostEqual(samples[1].tx_per_second, 16.0)
        self.assertAlmostEqual(samples[1].gas_per_second, 1800.0)

    def test_foundry_ignores_invalid_metrics_but_keeps_zero_rates(self):
        huge_count = "9" * 400
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","tps":"nan","gps":"inf"}',
                '{"timestamp":105,"event":"pulse","tps":-1,"gps":-2}',
                f'{{"timestamp":110,"event":"pulse","total_txs":{huge_count}}}',
                '{"timestamp":115,"event":"pulse","tps":0,"gps":0}',
                '{"timestamp":broken',
            ]
        )

        samples = analyze.parse_throughput_log(
            log_path, "run-1", "i-1", "foundry-git-test"
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].elapsed_seconds, 15.0)
        self.assertEqual(samples[0].tx_per_second, 0.0)
        self.assertEqual(samples[0].gas_per_second, 0.0)

    def test_foundry_reported_rates_remain_authoritative_after_counter_reset(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"event":"pulse","total_txs":100,"total_gas":10000,"tps":10,"gps":1000}',
                '{"timestamp":105,"event":"pulse","total_txs":5,"total_gas":500,"tps":1,"gps":100}',
            ]
        )

        samples = analyze.parse_throughput_log(
            log_path, "run-1", "i-1", "foundry-git-test"
        )
        self.assertEqual(len(samples), 2)
        self.assertEqual(
            [sample.source for sample in samples], ["json-rate", "json-rate"]
        )
        self.assertEqual(samples[-1].tx_per_second, 1.0)
        self.assertEqual(samples[-1].gas_per_second, 100.0)


if __name__ == "__main__":
    unittest.main()
