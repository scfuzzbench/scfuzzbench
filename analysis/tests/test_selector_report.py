import tempfile
import unittest
from pathlib import Path

from analysis import benchmark_report


def _summary():
    return {
        "measurement": (
            "Counts are occurrences in unique saved corpus sequences, not runtime "
            "execution frequencies"
        ),
        "expected_set": {
            "kind": "peer-consensus-heuristic",
            "status": "available",
            "source": (
                "peer-consensus heuristic: observed by at least two fuzzer families; "
                "not benchmark ground truth"
            ),
            "selectors": [{"selector": "0xa9059cbb"}],
        },
        "fuzzers": [
            {
                "fuzzer": "medusa",
                "status": "available",
                "instances": 2,
                "available_instances": 2,
                "saved_corpus_calls": 20,
                "unique_selectors": 1,
                "distribution": [
                    {
                        "comparison_key": "0xa9059cbb",
                        "selector": "0xa9059cbb",
                        "function_signatures": ["transfer(address,uint256)"],
                        "function_names": ["transfer"],
                        "saved_corpus_calls": 20,
                        "share": 1.0,
                    }
                ],
            },
            {
                "fuzzer": "foundry",
                "status": "unavailable",
                "instances": 2,
                "available_instances": 0,
                "saved_corpus_calls": 0,
                "unique_selectors": 0,
                "distribution": [],
            },
        ],
        "health_warnings": [
            "i-abcd-medusa-v1.4.1: missing expected selector(s) 0x12345678"
        ],
        "limitations": [
            "Foundry selector distribution is unavailable because no persisted corpus was present"
        ],
    }


class SelectorReportTests(unittest.TestCase):
    def test_selector_section_preserves_provenance_status_and_distribution(self):
        lines = []
        benchmark_report.append_selector_analytics_section(lines, _summary())
        report = "\n".join(lines)

        self.assertIn("Function selector sanity checks", report)
        self.assertIn("not runtime execution frequencies", report)
        self.assertIn("not benchmark ground truth", report)
        self.assertIn("| foundry | unavailable | 0/2 | 0 | 0 |", report)
        self.assertIn("`0xa9059cbb`", report)
        self.assertIn("`transfer(address,uint256)`", report)
        self.assertIn("Selector health warnings", report)
        self.assertIn("Selector telemetry limitations", report)

    def test_no_bug_data_report_still_includes_selector_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "REPORT.md"
            benchmark_report.write_no_data_report(
                budget=1.0,
                checkpoints=[1.0],
                ks=[1],
                outpath=out,
                csv_path=Path("cumulative.csv"),
                selector_summary=_summary(),
            )
            report = out.read_text(encoding="utf-8")
            self.assertIn("## No data", report)
            self.assertIn("## Function selector sanity checks", report)
            self.assertIn("missing expected selector(s)", report)


if __name__ == "__main__":
    unittest.main()
