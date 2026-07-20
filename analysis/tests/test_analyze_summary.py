import csv
import tempfile
import unittest
from pathlib import Path

from analysis import analyze


class AnalyzeSummaryTests(unittest.TestCase):
    @staticmethod
    def event(instance_id: str, event: str, elapsed_seconds: float) -> analyze.Event:
        return analyze.Event(
            run_id="benchmark-1",
            instance_id=instance_id,
            fuzzer="echidna",
            fuzzer_label="echidna-0",
            event=event,
            elapsed_seconds=elapsed_seconds,
            source="test",
            log_path=f"{instance_id}.log",
        )

    def test_summary_counts_distinct_events_with_shared_timestamps(self):
        events = [
            self.event("i-run-1", "invariant_alpha", 10.0),
            self.event("i-run-1", "invariant_beta", 10.0),
            self.event("i-run-1", "invariant_gamma", 10.0),
            self.event("i-run-1", "invariant_alpha", 4.0),
            self.event("i-run-1", "invariant_alpha", 12.0),
            self.event("i-run-2", "invariant_alpha", 7.0),
            self.event("i-run-2", "invariant_alpha", 5.0),
        ]

        runs = analyze.build_runs(events)
        self.assertEqual(
            [4.0, 10.0, 10.0],
            runs["echidna"]["benchmark-1:i-run-1:echidna-0"],
        )
        self.assertEqual(
            [5.0],
            runs["echidna"]["benchmark-1:i-run-2:echidna-0"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.csv"
            analyze.write_summary_csv(events, summary_path)
            with summary_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual("2", row["runs"])
        self.assertEqual("3", row["unique_bugs"])
        self.assertEqual("2.000", row["mean_bugs_per_run"])
        self.assertEqual("2.000", row["median_bugs_per_run"])
        self.assertEqual("1.414", row["stdev_bugs_per_run"])
        self.assertEqual("1", row["min_bugs_per_run"])
        self.assertEqual("3", row["max_bugs_per_run"])
        self.assertEqual("4.500", row["mean_ttfb_seconds"])
        self.assertEqual("4.500", row["median_ttfb_seconds"])


if __name__ == "__main__":
    unittest.main()
