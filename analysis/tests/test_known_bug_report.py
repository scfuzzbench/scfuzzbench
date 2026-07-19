import csv
import json
from pathlib import Path
import tempfile
import unittest

from analysis import known_bug_report


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "benchmarks" / "known_bugs.json"
TARGETS_PATH = REPO_ROOT / "benchmarks" / "targets.json"
EVENTS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "known_bug_events.csv"


class KnownBugReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
        cls.drips = next(
            target for target in cls.catalog["targets"] if target["target_id"] == "drips"
        )

    def test_normalize_event_handles_cross_fuzzer_variants(self):
        variants = (
            "CryticTester.assert_canary_ASSERTION_CANARY(uint256)",
            "assert_canary_ASSERTION_CANARY(uint256)",
            "invariant_assertion_failure_assert_canary_ASSERTION_CANARY",
        )

        self.assertEqual(
            {"assert_canary"},
            {known_bug_report.normalize_event(value) for value in variants},
        )

    def test_mapping_deduplicates_aliases_and_keeps_unknowns_explicit(self):
        events = known_bug_report.load_events_csv(EVENTS_FIXTURE)

        findings = known_bug_report.map_events(
            events,
            target=self.drips,
            excluded_fuzzers=set(),
        )

        first_foundry_replicate = [
            row
            for row in findings
            if row["run_id"] == "run-1"
            and row["instance_id"] == "i-a1"
            and row["fuzzer"] == "foundry"
        ]
        known_bug = next(
            row
            for row in first_foundry_replicate
            if row["finding_class"] == "known_bug"
        )
        assertion_canary = next(
            row
            for row in first_foundry_replicate
            if row["ground_truth_id"] == "drips/assertion-canary"
        )
        unmapped = next(
            row
            for row in first_foundry_replicate
            if row["finding_class"] == "unmapped"
        )

        self.assertEqual("drips/pre-update-time-squeeze", known_bug["ground_truth_id"])
        self.assertEqual(2, known_bug["observations"])
        self.assertEqual(2, assertion_canary["observations"])
        self.assertEqual("testUnexpected", unmapped["event"])
        self.assertEqual(4, len(first_foundry_replicate))

    def test_summary_uses_per_replicate_opportunities_and_separate_canaries(self):
        events = known_bug_report.load_events_csv(EVENTS_FIXTURE)
        findings = known_bug_report.map_events(
            events,
            target=self.drips,
            excluded_fuzzers=set(),
        )
        replicates = [
            known_bug_report.ReplicateRecord(
                "run-1", "i-a1", "foundry", "foundry"
            ),
            known_bug_report.ReplicateRecord(
                "run-1", "i-a3", "foundry", "foundry"
            ),
            known_bug_report.ReplicateRecord(
                "run-1", "i-a2", "echidna", "echidna"
            ),
        ]

        summary = known_bug_report.summarize_findings(
            findings,
            replicates=replicates,
            target=self.drips,
        )
        foundry = next(row for row in summary if row["fuzzer"] == "foundry")
        echidna = next(row for row in summary if row["fuzzer"] == "echidna")

        self.assertEqual(2, foundry["replicates"])
        self.assertEqual(1, foundry["known_bug_replicate_hits"])
        self.assertEqual(2, foundry["known_bug_replicate_opportunities"])
        self.assertEqual("0.500000", foundry["known_bug_hit_rate"])
        self.assertEqual(3, foundry["canary_replicate_hits"])
        self.assertEqual(4, foundry["canary_replicate_opportunities"])
        self.assertEqual("0.750000", foundry["canary_hit_rate"])
        self.assertEqual(2, foundry["unmapped_event_findings"])
        self.assertEqual("1.000000", echidna["known_bug_hit_rate"])

    def test_build_replicates_accounts_for_configured_replicates_with_no_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            (logs_dir / "i-a1-foundry").mkdir()
            manifest = {
                "run_id": "run-1",
                "instances_per_fuzzer": 2,
                "fuzzer_keys": ["foundry", "medusa"],
            }

            replicates = known_bug_report.build_replicates(
                [],
                logs_dir=logs_dir,
                default_run_id="run-1",
                run_manifest=manifest,
                excluded_fuzzers=set(),
            )

        self.assertEqual(
            2,
            sum(replicate.fuzzer == "foundry" for replicate in replicates),
        )
        self.assertEqual(
            2,
            sum(replicate.fuzzer == "medusa" for replicate in replicates),
        )
        self.assertEqual(
            3,
            sum(
                replicate.instance_id.startswith("missing-")
                for replicate in replicates
            ),
        )

    def test_cli_resolves_pinned_run_manifest_and_writes_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs_dir = root / "analysis"
            logs_dir.mkdir()
            (logs_dir / "i-a1-foundry").mkdir()
            manifest = {
                "run_id": "run-1",
                "target_repo_url": "https://github.com/scfuzzbench/drips-fuzzing-scfuzzbench",
                "target_commit": self.drips["target_commit"],
                "instances_per_fuzzer": 1,
                "fuzzer_keys": ["foundry"],
            }
            (logs_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            summary_path = root / "summary.csv"
            findings_path = root / "findings.csv"
            report_path = root / "report.md"

            result = known_bug_report.main(
                [
                    "--events-csv",
                    str(EVENTS_FIXTURE),
                    "--logs-dir",
                    str(logs_dir),
                    "--summary-out",
                    str(summary_path),
                    "--findings-out",
                    str(findings_path),
                    "--report-out",
                    str(report_path),
                    "--catalog",
                    str(CATALOG_PATH),
                    "--targets-manifest",
                    str(TARGETS_PATH),
                ]
            )

            with summary_path.open(newline="", encoding="utf-8") as handle:
                summary_rows = list(csv.DictReader(handle))
            report = report_path.read_text(encoding="utf-8")

        self.assertEqual(0, result)
        self.assertTrue(summary_rows)
        self.assertIn("Canaries are reported separately", report)
        self.assertIn("Crash inputs and corpus files are not used", report)

    def test_revision_mismatch_refuses_to_apply_aliases(self):
        run_manifest = {
            "target_repo_url": "https://github.com/scfuzzbench/drips-fuzzing-scfuzzbench",
            "target_commit": "0" * 40,
        }

        target, status = known_bug_report.resolve_target(
            catalog=self.catalog,
            targets_manifest=self.targets,
            run_manifest=run_manifest,
            explicit_target_id=None,
        )

        self.assertIsNone(target)
        self.assertIn("not the evidence-pinned catalog revision", status)

    def test_explicit_target_cannot_bypass_revision_verification(self):
        target, status = known_bug_report.resolve_target(
            catalog=self.catalog,
            targets_manifest=self.targets,
            run_manifest=None,
            explicit_target_id="drips",
        )

        self.assertIsNone(target)
        self.assertIn("cannot bypass", status)


if __name__ == "__main__":
    unittest.main()
