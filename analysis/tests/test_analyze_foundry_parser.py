import tempfile
import unittest
from pathlib import Path

from analysis import analyze


class FoundryParserTests(unittest.TestCase):
    def write_log(self, lines):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            tmp.write("\n".join(lines) + "\n")
            tmp.close()
            return Path(tmp.name)
        except Exception:
            tmp.close()
            raise

    def test_parses_only_invariant_failure_records(self):
        log_path = self.write_log(
            [
                '{"type":"invariant_metrics","timestamp":100,"invariant":"invariant_a","failed_current":0,"failed_total":0,"metrics":{"cumulative_edges_seen":1}}',
                '{"type":"invariant_failure","timestamp":101,"invariant":"invariant_a","failed_total":1}',
                '{"type":"invariant_failure","timestamp":102,"invariant":"invariant_a","failed_total":1}',
                '{"type":"invariant_failure","timestamp":103,"invariant":"invariant_b","failed_total":2}',
                '{"timestamp":104,"invariant":"legacy_invariant","failed":1,"metrics":{"cumulative_edges_seen":2}}',
                "[FAIL: legacy] legacy_invariant()",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual([event.event for event in events], ["invariant_a", "invariant_b"])
        self.assertEqual(
            [event.source for event in events],
            ["foundry-invariant-failure", "foundry-invariant-failure"],
        )
        self.assertAlmostEqual(events[0].elapsed_seconds, 1.0)
        self.assertAlmostEqual(events[1].elapsed_seconds, 3.0)

    def test_ignores_legacy_foundry_records_without_type(self):
        log_path = self.write_log(
            [
                '{"timestamp":100,"invariant":"legacy_invariant","failed":1,"metrics":{"cumulative_edges_seen":1}}',
                "[FAIL: legacy] legacy_invariant()",
            ]
        )

        events = analyze.parse_foundry_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(events, [])

    def test_parses_throughput_from_json_cumulative_metrics(self):
        log_path = self.write_log(
            [
                '{"type":"invariant_metrics","timestamp":100,"invariant":"invariant_a","metrics":{"cumulative_tx_count":20,"cumulative_gas_used":2000}}',
                '{"type":"invariant_metrics","timestamp":110,"invariant":"invariant_a","metrics":{"cumulative_tx_count":140,"cumulative_gas_used":15400}}',
            ]
        )

        samples = analyze.parse_throughput_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].source, "json-cumulative")
        self.assertAlmostEqual(samples[0].elapsed_seconds, 10.0)
        self.assertAlmostEqual(samples[0].tx_per_second, 14.0)
        self.assertAlmostEqual(samples[0].gas_per_second, 1540.0)

    def test_parses_throughput_from_json_rate_metrics(self):
        log_path = self.write_log(
            [
                '{"type":"invariant_metrics","timestamp":200,"invariant":"invariant_a","metrics":{"tx_per_second":11.5,"gas_per_second":900}}',
            ]
        )

        samples = analyze.parse_throughput_log(log_path, "run-1", "i-1", "foundry-git-test")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].source, "json-rate")
        self.assertAlmostEqual(samples[0].tx_per_second, 11.5)
        self.assertAlmostEqual(samples[0].gas_per_second, 900.0)


if __name__ == "__main__":
    unittest.main()
