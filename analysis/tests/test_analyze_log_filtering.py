import tempfile
import unittest
from pathlib import Path

from analysis import analyze


class AnalyzeLogFilteringTests(unittest.TestCase):
    def test_parse_logs_ignores_runner_commands_log(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            logs_dir = Path(tmp_dir)
            instance_dir = logs_dir / "i-abcd1234-custom-fuzzer"
            instance_dir.mkdir(parents=True)
            (instance_dir / "runner_commands.log").write_text(
                "[2026-03-01 00:00:01] FAILURE should_not_be_parsed\n",
                encoding="utf-8",
            )

            events = analyze.parse_logs(logs_dir, "run-1")
            self.assertEqual(events, [])

    def test_parse_throughput_logs_ignores_runner_commands_log(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            logs_dir = Path(tmp_dir)
            instance_dir = logs_dir / "i-abcd1234-custom-fuzzer"
            instance_dir.mkdir(parents=True)
            (instance_dir / "runner_commands.log").write_text(
                "[2026-03-01 00:00:01] tx/s: 123 gas/s: 456\n",
                encoding="utf-8",
            )

            samples = analyze.parse_throughput_logs(logs_dir, "run-1")
            self.assertEqual(samples, [])

    def test_parse_progress_metrics_logs_ignores_runner_commands_log(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            logs_dir = Path(tmp_dir)
            instance_dir = logs_dir / "i-abcd1234-custom-fuzzer"
            instance_dir.mkdir(parents=True)
            (instance_dir / "runner_commands.log").write_text(
                "[2026-03-01 00:00:01] seq/s: 11 cov: 42 corpus: 7 failures: 1/2\n",
                encoding="utf-8",
            )

            samples = analyze.parse_progress_metrics_logs(logs_dir, "run-1")
            self.assertEqual(samples, [])


if __name__ == "__main__":
    unittest.main()
