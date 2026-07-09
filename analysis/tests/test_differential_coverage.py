import csv
import json
import random
import tempfile
import unittest
from pathlib import Path

from analysis import analyze

try:
    # Reference implementation for the parity checks below. analyze.py's hot
    # CSV path now computes relscore/relcov locally, but the verdict path still
    # uses this package, so it remains a runtime dependency.
    from differential_coverage import DifferentialCoverage
except ImportError:
    DifferentialCoverage = None


@unittest.skipUnless(
    DifferentialCoverage is not None, "differential_coverage not installed"
)
class RelscoreParityTests(unittest.TestCase):
    """The local linear calculate_relscores/relcovs must match the upstream library."""

    @staticmethod
    def _random_campaign(rng):
        edge_universe = [f"e{i}" for i in range(rng.randint(1, 40))]
        campaign = {}
        for a in range(rng.randint(1, 5)):
            trials = {}
            for t in range(rng.randint(1, 6)):
                k = rng.randint(1, len(edge_universe))
                trials[f"t{t}"] = set(rng.sample(edge_universe, k))  # always non-empty
            campaign[f"approach{a}"] = trials
        return campaign

    def _assert_relscore_parity(self, campaign):
        assert DifferentialCoverage is not None  # guaranteed by skipUnless
        expected = dict(DifferentialCoverage(campaign).relscores())
        actual = analyze.calculate_relscores(campaign)
        self.assertEqual(set(expected), set(actual))
        for approach in expected:
            self.assertAlmostEqual(expected[approach], actual[approach], places=9)

    def _assert_relcov_parity(self, campaign):
        assert DifferentialCoverage is not None  # guaranteed by skipUnless
        dc = DifferentialCoverage(campaign)
        expected = {
            a: {r: dc.approaches[a].relcov(dc.approaches[r]) for r in dc.approaches}
            for a in dc.approaches
        }
        actual = analyze.calculate_relcovs(campaign)
        self.assertEqual(set(expected), set(actual))
        for a in expected:
            self.assertEqual(set(expected[a]), set(actual[a]))
            for r in expected[a]:
                self.assertAlmostEqual(expected[a][r], actual[a][r], places=9)

    def test_matches_library_on_random_campaigns(self):
        rng = random.Random(1234)
        for _ in range(500):
            campaign = self._random_campaign(rng)
            self._assert_relscore_parity(campaign)
            self._assert_relcov_parity(campaign)

    def test_matches_library_on_hand_picked_cases(self):
        cases = [
            # single approach -> every relscore is 0
            {"only": {"t1": {"a", "b"}}},
            # disjoint coverage
            {"x": {"t1": {"a"}}, "y": {"t1": {"b"}}},
            # multiple trials with partial overlap
            {
                "x": {"t1": {"a", "b"}, "t2": {"b", "c"}},
                "y": {"t1": {"a"}, "t2": {"a", "c"}, "t3": {"d"}},
            },
        ]
        for campaign in cases:
            self._assert_relscore_parity(campaign)
            self._assert_relcov_parity(campaign)


class DifferentialCoverageTests(unittest.TestCase):
    @staticmethod
    def _seeded_campaign(
        feature_unique_counts,
        shared_count=200,
        master_unique_count=1,
    ):
        shared = {f"shared-{idx}" for idx in range(shared_count)}
        master = {}
        feature = {}
        for idx, feature_unique_count in enumerate(feature_unique_counts, 1):
            sample_id = f"seed-{idx}"
            master[sample_id] = shared | {
                f"master-{idx}-{unique_idx}"
                for unique_idx in range(master_unique_count)
            }
            feature[sample_id] = shared | {
                f"feature-{idx}-{unique_idx}"
                for unique_idx in range(feature_unique_count)
            }
        return {"master": master, "pr-1": feature}

    @staticmethod
    def _region(prefix, idx, count=10):
        return {f"{prefix}{idx}-{edge}" for edge in range(count)}

    @staticmethod
    def _non_saturating_paired_campaign(
        feature_transform,
        region_count=3,
        repeats=2,
    ):
        master = {}
        feature = {}
        rounds = region_count * repeats
        for idx in range(1, rounds + 1):
            region_idx = (idx - 1) % region_count
            baseline_edges = DifferentialCoverageTests._region("c", region_idx)
            sample_id = f"seed-{idx}"
            master[sample_id] = set(baseline_edges)
            feature[sample_id] = set(feature_transform(idx, region_idx, baseline_edges))
        return {"master": master, "pr-1": feature}

    @staticmethod
    def _unpaired_campaign(baseline_trials, feature_trials):
        return {
            "master": {f"m{i}": set(edges) for i, edges in enumerate(baseline_trials, 1)},
            "pr-1": {f"p{i}": set(edges) for i, edges in enumerate(feature_trials, 1)},
        }

    @staticmethod
    def _summary_for(campaign_name, campaign):
        return analyze.build_differential_coverage_summary_rows(
            campaign_name,
            analyze.calculate_relscores(campaign),
            analyze.calculate_relcovs(campaign),
        )

    def _relscore_row(self, campaign_name, campaign, **kwargs):
        rows, directive = analyze.build_differential_coverage_verdict_rows(
            self._summary_for(campaign_name, campaign),
            {campaign_name: campaign},
            **kwargs,
        )
        return next(row for row in rows if row["metric"] == "relscore"), directive

    def test_writes_normalized_showmap_campaigns_and_relscores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master_showmap = (
                root
                / "logs"
                / "i-aaa-foundry-master"
                / "showmap"
                / "foundry-master__Suite__invariant_ok"
            )
            feature_showmap = (
                root
                / "logs"
                / "i-bbb-foundry-feature"
                / "showmap"
                / "foundry-feature__Suite__invariant_ok"
            )
            master_showmap.mkdir(parents=True)
            feature_showmap.mkdir(parents=True)
            (master_showmap / "trial-1.txt").write_text("1:1\n2:1\n3:0\n", encoding="utf-8")
            (feature_showmap / "trial-1.txt").write_text("1:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)

            master_combined = (
                out_dir
                / "showmap_campaigns"
                / "combined"
                / "foundry-master"
                / "i-aaa-foundry-master__trial-1.txt"
            )
            feature_by_test = (
                out_dir
                / "showmap_campaigns"
                / "by_test"
                / "Suite__invariant_ok"
                / "foundry-feature"
                / "i-bbb-foundry-feature__trial-1.txt"
            )
            self.assertEqual(master_combined.read_text(encoding="utf-8"), "1:1\n2:1\n")
            self.assertEqual(feature_by_test.read_text(encoding="utf-8"), "1:1\n")

            with (out_dir / "differential_coverage_relscores.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            scores = {
                (row["campaign"], row["approach"]): row
                for row in rows
            }
            self.assertEqual(scores[("combined", "foundry-master")]["relscore"], "1.000000")
            self.assertEqual(scores[("combined", "foundry-feature")]["relscore"], "0.000000")
            self.assertEqual(scores[("combined", "foundry-master")]["trials"], "1")
            self.assertEqual(scores[("combined", "foundry-master")]["covered_edges"], "2")
            self.assertEqual(
                scores[("by_test/Suite__invariant_ok", "foundry-master")]["relscore"],
                "1.000000",
            )
            with (out_dir / "differential_coverage_relcov.csv").open(newline="") as handle:
                relcov_rows = list(csv.DictReader(handle))
            relcovs = {
                (row["campaign"], row["approach"], row["reference_approach"]): row["relcov"]
                for row in relcov_rows
            }
            self.assertNotIn(("combined", "foundry-master", "foundry-master"), relcovs)
            self.assertEqual(
                relcovs[("combined", "foundry-master", "foundry-feature")],
                "1.000000",
            )
            self.assertEqual(
                relcovs[("combined", "foundry-feature", "foundry-master")],
                "0.500000",
            )
            with (out_dir / "differential_coverage_summary.csv").open(newline="") as handle:
                summary_rows = list(csv.DictReader(handle))
            summary = {(row["campaign"], row["feature"]): row for row in summary_rows}
            self.assertEqual(
                summary[("combined", "foundry-feature")]["verdict"],
                "inconclusive",
            )
            self.assertEqual(summary[("combined", "foundry-feature")]["verdict_reason"], "too few runs")
            self.assertNotIn("feature_covers_baseline", summary[("combined", "foundry-feature")])
            self.assertNotIn("relscore_ratio", summary[("combined", "foundry-feature")])
            self.assertIn("baseline_reliability", summary[("combined", "foundry-feature")])
            self.assertIn("feature_performance", summary[("combined", "foundry-feature")])
            self.assertIn("noninferiority_delta", summary[("combined", "foundry-feature")])
            self.assertIn("relcov_status", summary[("combined", "foundry-feature")])

            manifest = json.loads(
                (out_dir / "showmap_campaign_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["raw_trials"], 2)
            self.assertEqual(manifest["skipped"], [])
            self.assertIn("combined", manifest["campaigns"])
            self.assertIn("work_items", manifest["campaigns"]["combined"])

    def test_excludes_filtered_fuzzers_from_showmap_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master_showmap = root / "logs" / "i-aaa-foundry-master" / "showmap" / "foundry-master"
            feature_showmap = (
                root / "logs" / "i-bbb-foundry-feature" / "showmap" / "foundry-feature"
            )
            master_showmap.mkdir(parents=True)
            feature_showmap.mkdir(parents=True)
            (master_showmap / "trial-1.txt").write_text("1:1\n2:1\n", encoding="utf-8")
            (feature_showmap / "trial-1.txt").write_text("1:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(
                root / "logs", out_dir, {"foundry-feature"}
            )

            with (out_dir / "differential_coverage_relscores.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["campaign"], "combined")
            self.assertEqual(rows[0]["approach"], "foundry-master")
            self.assertEqual(rows[0]["relscore"], "0.000000")
            with (out_dir / "differential_coverage_relcov.csv").open(newline="") as handle:
                relcov_rows = list(csv.DictReader(handle))
            self.assertEqual(relcov_rows, [])
            self.assertTrue(
                (out_dir / "showmap_campaigns" / "combined" / "foundry-master").is_dir()
            )
            self.assertFalse(
                (out_dir / "showmap_campaigns" / "combined" / "foundry-feature").exists()
            )

    def test_clears_stale_showmap_campaigns_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master_showmap = root / "logs" / "i-aaa-foundry-master" / "showmap" / "foundry-master"
            feature_showmap = (
                root / "logs" / "i-bbb-foundry-feature" / "showmap" / "foundry-feature"
            )
            master_showmap.mkdir(parents=True)
            feature_showmap.mkdir(parents=True)
            (master_showmap / "trial-1.txt").write_text("1:1\n2:1\n", encoding="utf-8")
            (feature_showmap / "trial-1.txt").write_text("1:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)
            self.assertTrue(
                (out_dir / "showmap_campaigns" / "combined" / "foundry-feature").exists()
            )

            analyze.write_differential_coverage_outputs(
                root / "logs", out_dir, {"foundry-feature"}
            )
            self.assertTrue(
                (out_dir / "showmap_campaigns" / "combined" / "foundry-master").exists()
            )
            self.assertFalse(
                (out_dir / "showmap_campaigns" / "combined" / "foundry-feature").exists()
            )
            manifest = json.loads(
                (out_dir / "showmap_campaign_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                sorted(manifest["campaigns"]["combined"]["approaches"].keys()),
                ["foundry-master"],
            )

    def test_parses_invariant_showmap_dirs_as_suite_campaigns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master_showmap = (
                root
                / "logs"
                / "i-aaa-foundry-master"
                / "showmap"
                / "foundry-master__test_ShowmapCounter.t.sol_ShowmapCounterTest"
            )
            feature_showmap = (
                root
                / "logs"
                / "i-bbb-foundry-feature"
                / "showmap"
                / "foundry-feature__test_ShowmapCounter.t.sol_ShowmapCounterTest"
            )
            master_showmap.mkdir(parents=True)
            feature_showmap.mkdir(parents=True)
            (master_showmap / "trial-1.txt").write_text("1:1\n2:1\n", encoding="utf-8")
            (feature_showmap / "trial-1.txt").write_text("1:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)

            suite_name = "test_ShowmapCounter.t.sol_ShowmapCounterTest"
            self.assertTrue(
                (
                    out_dir
                    / "showmap_campaigns"
                    / "by_test"
                    / suite_name
                    / "foundry-master"
                    / "i-aaa-foundry-master__trial-1.txt"
                ).is_file()
            )
            self.assertFalse(
                (
                    out_dir
                    / "showmap_campaigns"
                    / "combined"
                    / "foundry-master__test_ShowmapCounter.t.sol_ShowmapCounterTest"
                ).exists()
            )

            with (out_dir / "differential_coverage_relscores.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            scores = {(row["campaign"], row["approach"]): row["relscore"] for row in rows}
            self.assertEqual(scores[("combined", "foundry-master")], "1.000000")
            self.assertEqual(scores[(f"by_test/{suite_name}", "foundry-feature")], "0.000000")

    def test_by_test_campaigns_excluded_from_verdicts_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_name = "test_ShowmapCounter.t.sol_ShowmapCounterTest"
            master_showmap = (
                root / "logs" / "i-aaa-foundry-master" / "showmap" / f"foundry-master__{suite_name}"
            )
            feature_showmap = (
                root / "logs" / "i-bbb-foundry-feature" / "showmap" / f"foundry-feature__{suite_name}"
            )
            master_showmap.mkdir(parents=True)
            feature_showmap.mkdir(parents=True)
            (master_showmap / "trial-1.txt").write_text("1:1\n2:1\n", encoding="utf-8")
            (feature_showmap / "trial-1.txt").write_text("1:1\n", encoding="utf-8")

            # Default: by_test campaigns get no bootstrap verdict row...
            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)

            stats = json.loads((out_dir / "differential_coverage_statistics.json").read_text())
            stats_campaigns = {row["campaign"] for row in stats["rows"]}
            self.assertNotIn(f"by_test/{suite_name}", stats_campaigns)
            self.assertIn("combined", stats_campaigns)

            manifest = json.loads((out_dir / "showmap_campaign_manifest.json").read_text())
            self.assertFalse(manifest["verdict_by_test"])

            # ...but their point relscores are still emitted...
            with (out_dir / "differential_coverage_relscores.csv").open(newline="") as handle:
                scores = {(r["campaign"], r["approach"]): r["relscore"] for r in csv.DictReader(handle)}
            self.assertIn((f"by_test/{suite_name}", "foundry-master"), scores)

            # ...and they appear in the summary CSV as cheap inconclusive rows.
            with (out_dir / "differential_coverage_summary.csv").open(newline="") as handle:
                summary = {(r["campaign"], r["metric"]): r for r in csv.DictReader(handle)}
            by_test_row = summary[(f"by_test/{suite_name}", "relscore")]
            self.assertEqual(by_test_row["verdict"], "inconclusive")
            self.assertEqual(by_test_row["p_value"], "")

    def test_by_test_verdicts_can_be_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_name = "test_ShowmapCounter.t.sol_ShowmapCounterTest"
            master_showmap = (
                root / "logs" / "i-aaa-foundry-master" / "showmap" / f"foundry-master__{suite_name}"
            )
            feature_showmap = (
                root / "logs" / "i-bbb-foundry-feature" / "showmap" / f"foundry-feature__{suite_name}"
            )
            master_showmap.mkdir(parents=True)
            feature_showmap.mkdir(parents=True)
            (master_showmap / "trial-1.txt").write_text("1:1\n2:1\n", encoding="utf-8")
            (feature_showmap / "trial-1.txt").write_text("1:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(
                root / "logs", out_dir, verdict_by_test=True
            )

            stats = json.loads((out_dir / "differential_coverage_statistics.json").read_text())
            stats_campaigns = {row["campaign"] for row in stats["rows"]}
            self.assertIn(f"by_test/{suite_name}", stats_campaigns)
            manifest = json.loads((out_dir / "showmap_campaign_manifest.json").read_text())
            self.assertTrue(manifest["verdict_by_test"])

    def test_parses_real_foundry_showmap_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            showmap_root = root / "logs" / "i-live-foundry-live" / "showmap"
            invariant_dir = (
                showmap_root
                / "foundry-live__test_CryticToFoundry.t.sol_CryticToFoundry"
            )
            fuzz_dir = (
                showmap_root
                / "foundry-live__test_CryticToFoundry.t.sol_CryticToFoundry__testFuzz_SetNumber"
            )
            invariant_dir.mkdir(parents=True)
            fuzz_dir.mkdir(parents=True)
            (invariant_dir / "trial-live.txt").write_text("a:1\n", encoding="utf-8")
            (fuzz_dir / "trial-live.txt").write_text("b:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)

            invariant_campaign = (
                out_dir
                / "showmap_campaigns"
                / "by_test"
                / "test_CryticToFoundry.t.sol_CryticToFoundry"
                / "foundry-live"
                / "i-live-foundry-live__trial-live.txt"
            )
            fuzz_campaign = (
                out_dir
                / "showmap_campaigns"
                / "by_test"
                / "test_CryticToFoundry.t.sol_CryticToFoundry__testFuzz_SetNumber"
                / "foundry-live"
                / "i-live-foundry-live__trial-live.txt"
            )
            self.assertEqual(invariant_campaign.read_text(encoding="utf-8"), "a:1\n")
            self.assertEqual(fuzz_campaign.read_text(encoding="utf-8"), "b:1\n")

    def test_combined_campaign_merges_multiple_raw_foundry_dirs_per_approach(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            showmap_root = root / "logs" / "i-live" / "showmap"
            raw_dirs = {
                "foundry-master__Suite": "a:1\nb:1\n",
                "foundry-master__Suite__testFuzz_x": "c:1\n",
                "foundry-feature__Suite": "a:1\n",
                "foundry-feature__Suite__testFuzz_x": "d:1\n",
            }
            for dirname, body in raw_dirs.items():
                path = showmap_root / dirname
                path.mkdir(parents=True)
                (path / "trial-1.txt").write_text(body, encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)

            combined = out_dir / "showmap_campaigns" / "combined"
            self.assertEqual(
                sorted(path.name for path in combined.iterdir() if path.is_dir()),
                ["foundry-feature", "foundry-master"],
            )
            self.assertEqual(
                (combined / "foundry-master" / "i-live__trial-1.txt").read_text(
                    encoding="utf-8"
                ),
                "a:1\nb:1\nc:1\n",
            )
            self.assertEqual(
                (combined / "foundry-feature" / "i-live__trial-1.txt").read_text(
                    encoding="utf-8"
                ),
                "a:1\nd:1\n",
            )

            manifest = json.loads(
                (out_dir / "showmap_campaign_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["raw_trials"], 4)
            self.assertEqual(
                sorted(manifest["campaigns"]["combined"]["approaches"].keys()),
                ["foundry-feature", "foundry-master"],
            )

            with (out_dir / "differential_coverage_relscores.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            scores = {
                (row["campaign"], row["approach"]): row["relscore"]
                for row in rows
            }
            self.assertEqual(scores[("combined", "foundry-master")], "2.000000")
            self.assertEqual(scores[("combined", "foundry-feature")], "1.000000")
            with (out_dir / "differential_coverage_relcov.csv").open(newline="") as handle:
                relcov_rows = list(csv.DictReader(handle))
            relcovs = {
                (row["campaign"], row["approach"], row["reference_approach"]): row["relcov"]
                for row in relcov_rows
            }
            self.assertEqual(
                relcovs[("combined", "foundry-master", "foundry-feature")],
                "0.500000",
            )
            self.assertEqual(
                relcovs[("combined", "foundry-feature", "foundry-master")],
                "0.333333",
            )

    def test_writes_human_summary_for_master_pr_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            showmap_root = root / "logs"
            master_showmap = showmap_root / "i-aaa-master" / "showmap" / "master__Suite"
            pr_showmap = showmap_root / "i-bbb-pr-15206" / "showmap" / "pr-15206__Suite"
            master_showmap.mkdir(parents=True)
            pr_showmap.mkdir(parents=True)
            (master_showmap / "trial-1.txt").write_text(
                "a:1\nb:1\nc:1\nd:1\n", encoding="utf-8"
            )
            (pr_showmap / "trial-1.txt").write_text(
                "a:1\nb:1\nc:1\ne:1\n", encoding="utf-8"
            )

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)

            with (out_dir / "differential_coverage_relcov.csv").open(newline="") as handle:
                relcov_rows = list(csv.DictReader(handle))
            self.assertEqual(len(relcov_rows), 4)
            self.assertFalse(
                any(row["approach"] == row["reference_approach"] for row in relcov_rows)
            )

            with (out_dir / "differential_coverage_summary.csv").open(newline="") as handle:
                summary_rows = list(csv.DictReader(handle))

            combined = next(row for row in summary_rows if row["campaign"] == "combined")
            self.assertEqual(combined["baseline"], "master")
            self.assertEqual(combined["feature"], "pr-15206")
            self.assertEqual(combined["verdict"], "inconclusive")
            self.assertEqual(combined["verdict_reason"], "too few runs")
            self.assertEqual(combined["feature_performance"], "0.750000")
            self.assertNotIn("feature_covers_baseline", combined)
            self.assertNotIn("baseline_covers_feature", combined)
            self.assertNotIn("relscore_ratio", combined)

    def test_target_labels_create_target_campaign_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            for target, master_edges, pr_edges in [
                ("aave", "a:1\nb:1\n", "a:1\nc:1\n"),
                ("nerite", "x:1\ny:1\n", "x:1\ny:1\nz:1\n"),
            ]:
                master = logs / f"i-master__target-{target}" / "showmap" / "master__Suite"
                pr = logs / f"i-pr__target-{target}" / "showmap" / "pr-1__Suite"
                master.mkdir(parents=True)
                pr.mkdir(parents=True)
                (master / "trial-1.txt").write_text(master_edges, encoding="utf-8")
                (pr / "trial-1.txt").write_text(pr_edges, encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(logs, out_dir)

            with (out_dir / "differential_coverage_summary.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            campaigns = {row["campaign"] for row in rows}

            self.assertIn("by_target/aave", campaigns)
            self.assertIn("by_target/nerite", campaigns)
            self.assertIn("combined", campaigns)

    def test_seed_labels_pair_by_seed_and_collapse_campaign_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            for label, approach, edges_by_suite in [
                ("foundry-master", "foundry-master", {"SuiteA": "a:1\n", "SuiteB": "b:1\n"}),
                ("foundry-feature", "foundry-feature", {"SuiteA": "a:1\n", "SuiteB": "c:1\n"}),
            ]:
                for suite, edges in edges_by_suite.items():
                    showmap = (
                        logs
                        / f"{label}__target-aave__seed-101"
                        / "showmap"
                        / f"{approach}__{suite}"
                    )
                    showmap.mkdir(parents=True)
                    (showmap / "trial-1.txt").write_text(edges, encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(
                logs, out_dir, pairing_mode="paired"
            )

            with (out_dir / "differential_coverage_summary.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            target_row = next(row for row in rows if row["campaign"] == "by_target/aave")
            self.assertEqual(target_row["pairing_mode"], "paired")
            self.assertEqual(target_row["n_trials"], "1")
            self.assertEqual(target_row["paired"], "true")
            self.assertEqual(target_row["pairing_rate"], "1.000000")
            self.assertEqual(target_row["test_name"], "wilcoxon-signed-rank")
            self.assertEqual(target_row["verdict"], "inconclusive")
            self.assertEqual(target_row["verdict_reason"], "too few runs")

            campaign_file = (
                out_dir
                / "showmap_campaigns"
                / "by_target"
                / "aave"
                / "foundry-master"
                / "seed-101.txt"
            )
            self.assertEqual(campaign_file.read_text(encoding="utf-8"), "a:1\nb:1\n")

    def test_seed_labels_do_not_pair_without_paired_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            for label, approach in [
                ("foundry-master", "foundry-master"),
                ("foundry-feature", "foundry-feature"),
            ]:
                showmap = (
                    logs
                    / f"{label}__target-aave__seed-101"
                    / "showmap"
                    / f"{approach}__Suite"
                )
                showmap.mkdir(parents=True)
                (showmap / "trial-1.txt").write_text("a:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(logs, out_dir)

            with (out_dir / "differential_coverage_summary.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            target_row = next(row for row in rows if row["campaign"] == "by_target/aave")
            self.assertEqual(target_row["pairing_mode"], "unpaired")
            self.assertEqual(target_row["paired"], "false")
            self.assertEqual(target_row["test_name"], "mann-whitney-u")

    def test_seed_labels_do_not_fall_back_to_unpaired_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            master = logs / "foundry-master__target-aave__seed-1" / "showmap" / "foundry-master__Suite"
            feature = (
                logs
                / "foundry-feature__target-aave__seed-2"
                / "showmap"
                / "foundry-feature__Suite"
            )
            master.mkdir(parents=True)
            feature.mkdir(parents=True)
            (master / "trial-1.txt").write_text("a:1\n", encoding="utf-8")
            (feature / "trial-1.txt").write_text("a:1\nb:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(
                logs, out_dir, pairing_mode="paired"
            )

            with (out_dir / "differential_coverage_summary.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            target_row = next(row for row in rows if row["campaign"] == "by_target/aave")
            self.assertEqual(target_row["pairing_mode"], "paired")
            self.assertEqual(target_row["n_trials"], "0")
            self.assertEqual(target_row["paired"], "false")
            self.assertEqual(target_row["test_name"], "paired-required")
            self.assertEqual(target_row["verdict"], "inconclusive")

            state = json.loads(
                (out_dir / "differential_coverage_statistics.json").read_text(
                    encoding="utf-8"
                )
            )
            target_state = next(
                row for row in state["rows"] if row["campaign"] == "by_target/aave"
            )
            self.assertIn("unpaired fallback disabled", target_state["reason"])

    def test_differential_coverage_verdict_state_has_no_scheduler_contract(self):
        campaign = {
            "master": {f"s{i}": {"a", "b", "c", "d"} for i in range(3)},
            "pr-1": {f"s{i}": {"a"} for i in range(3)},
        }
        relscores = analyze.calculate_relscores(campaign)
        relcovs = analyze.calculate_relcovs(campaign)
        summary = analyze.build_differential_coverage_summary_rows(
            "by_target/aave", relscores, relcovs
        )
        rows, directive = analyze.build_differential_coverage_verdict_rows(
            summary, {"by_target/aave": campaign}
        )
        self.assertNotIn("decision", rows[0])
        self.assertNotIn("schedule", directive)
        self.assertNotIn("decision", directive["aggregate"])
        self.assertNotIn("automated_winner_enabled", directive["aggregate"])

    def test_differential_coverage_verdict_state_reports_verdict_without_scheduler_fields(self):
        campaign = {
            "master": {f"s{i}": {"a", "b", "c"} for i in range(4)},
            "pr-1": {f"s{i}": {"a", "b", "c", f"x{i}", f"y{i}"} for i in range(4)},
        }
        relscores = analyze.calculate_relscores(campaign)
        relcovs = analyze.calculate_relcovs(campaign)
        summary = analyze.build_differential_coverage_summary_rows(
            "by_target/nerite", relscores, relcovs
        )
        rows, directive = analyze.build_differential_coverage_verdict_rows(
            summary, {"by_target/nerite": campaign}
        )
        self.assertIn(rows[0]["verdict"], {"improvement", "regression", "needs-review", "inconclusive"})
        self.assertIn(directive["aggregate"]["verdict"], {"improvement", "regression", "needs-review", "inconclusive"})

    def test_differential_coverage_verdict_state_records_missing_count_and_does_not_block(self):
        campaign = {
            "master": {"s1": {"a", "b"}, "s2": {"a", "b"}},
            "pr-1": {"s1": {"a", "b"}, "s2": {"a", "b", "c"}},
        }
        relscores = analyze.calculate_relscores(campaign)
        relcovs = analyze.calculate_relcovs(campaign)
        summary = analyze.build_differential_coverage_summary_rows(
            "by_target/slow", relscores, relcovs
        )
        rows, _ = analyze.build_differential_coverage_verdict_rows(summary, {"by_target/slow": campaign})
        self.assertEqual(rows[0]["missing_count"], 0)

    def test_differential_coverage_verdict_state_exercises_paired_and_unpaired_paths(self):
        paired = {
            "master": {"seed-1": {"a"}, "seed-2": {"a"}},
            "pr-1": {"seed-1": {"a", "b"}, "seed-2": {"a", "c"}},
        }
        unpaired = {
            "master": {"m1": {"a"}, "m2": {"a"}},
            "pr-1": {"p1": {"a", "b"}, "p2": {"a", "c"}},
        }
        paired_summary = analyze.build_differential_coverage_summary_rows(
            "by_target/paired", analyze.calculate_relscores(paired), analyze.calculate_relcovs(paired)
        )
        unpaired_summary = analyze.build_differential_coverage_summary_rows(
            "by_target/unpaired", analyze.calculate_relscores(unpaired), analyze.calculate_relcovs(unpaired)
        )
        paired_rows, _ = analyze.build_differential_coverage_verdict_rows(
            paired_summary, {"by_target/paired": paired}, pairing_mode="paired"
        )
        unpaired_rows, _ = analyze.build_differential_coverage_verdict_rows(
            unpaired_summary, {"by_target/unpaired": unpaired}
        )
        self.assertTrue(paired_rows[0]["paired"])
        self.assertEqual(paired_rows[0]["test_name"], "wilcoxon-signed-rank")
        self.assertEqual(paired_rows[1]["test_name"], "wilcoxon-noninferiority")
        self.assertFalse(unpaired_rows[0]["paired"])
        self.assertEqual(unpaired_rows[0]["test_name"], "mann-whitney-u")
        self.assertEqual(unpaired_rows[1]["test_name"], "mann-whitney-u-noninferiority")

    def test_relcov_diagonal_and_cross_are_sourced_from_library_metrics(self):
        campaign = {
            "master": {
                "seed-1": {"a", "b"},
                "seed-2": {"b", "c"},
            },
            "pr-1": {
                "seed-1": {"a"},
                "seed-2": {"d"},
            },
        }
        relcovs = analyze.calculate_relcovs(campaign)
        self.assertAlmostEqual(relcovs["master"]["master"], 2 / 3)
        self.assertAlmostEqual(relcovs["pr-1"]["master"], 1 / 6)

        rows, _ = analyze.build_differential_coverage_verdict_rows(
            self._summary_for("by_target/library", campaign),
            {"by_target/library": campaign},
            pairing_mode="paired",
            min_samples=2,
        )
        row = next(item for item in rows if item["metric"] == "relcov")
        self.assertAlmostEqual(row["baseline_reliability"], 2 / 3)
        self.assertAlmostEqual(row["feature_performance"], 1 / 6)

    def test_non_saturating_a_a_relcov_held_but_verdict_inconclusive(self):
        campaign = self._non_saturating_paired_campaign(
            lambda _idx, _region_idx, baseline_edges: baseline_edges
        )
        row, _ = self._relscore_row("by_target/aa", campaign, pairing_mode="paired")
        self.assertLess(row["baseline_reliability"], 0.95)
        self.assertEqual(row["relcov_status"], "held")
        self.assertEqual(row["verdict"], "inconclusive")

    def test_non_saturating_relscore_up_and_retention_matches_diagonal_is_improvement(self):
        campaign = self._non_saturating_paired_campaign(
            lambda idx, _region_idx, baseline_edges: baseline_edges
            | {f"feature-{idx}-{extra}" for extra in range(4)}
        )
        row, directive = self._relscore_row("by_target/non-saturating", campaign, pairing_mode="paired")
        self.assertLess(row["baseline_reliability"], 0.95)
        self.assertEqual(row["relcov_status"], "held")
        self.assertEqual(row["verdict"], "improvement")
        self.assertEqual(directive["aggregate"]["verdict"], "improvement")

    def test_non_saturating_real_retention_regression_is_flagged(self):
        campaign = self._non_saturating_paired_campaign(
            lambda idx, region_idx, _baseline_edges: {
                f"c{region_idx}-{edge}" for edge in range(5)
            }
            | {f"feature-{idx}-{extra}" for extra in range(8)}
        )
        row, _ = self._relscore_row("by_target/retention-loss", campaign, pairing_mode="paired")
        self.assertEqual(row["relcov_status"], "failed")
        self.assertEqual(row["verdict"], "regression")
        self.assertLess(row["relcov_delta_ci_high"], -row["noninferiority_delta"])
        self.assertGreater(row["feature_sample_mean"], row["baseline_sample_mean"])

    def test_coverage_shift_is_not_masked_by_diagonal_scaling(self):
        campaign = self._non_saturating_paired_campaign(
            lambda _idx, region_idx, _baseline_edges: self._region("shift", region_idx)
        )
        row, _ = self._relscore_row("by_target/shift", campaign, pairing_mode="paired")
        self.assertEqual(row["relcov_status"], "failed")
        self.assertEqual(row["verdict"], "regression")

    def test_saturating_target_behaves_like_absolute_floor(self):
        shared = {f"shared-{idx}" for idx in range(100)}
        master = {f"seed-{idx}": set(shared) for idx in range(1, 7)}
        feature = {
            f"seed-{idx}": {f"shared-{edge}" for edge in range(90)}
            | {f"feature-{idx}-{extra}" for extra in range(8)}
            for idx in range(1, 7)
        }
        campaign = {"master": master, "pr-1": feature}
        row, _ = self._relscore_row("by_target/saturating", campaign, pairing_mode="paired")
        self.assertAlmostEqual(row["baseline_reliability"], 1.0)
        self.assertEqual(row["relcov_status"], "failed")
        self.assertEqual(row["verdict"], "regression")

    def test_unpaired_relcov_noninferiority_can_hold(self):
        regions = [self._region("c", idx) for idx in range(3)]
        baseline_trials = [regions[idx % 3] for idx in range(6)]
        feature_trials = [
            regions[idx % 3] | {f"feature-{idx}-{extra}" for extra in range(4)}
            for idx in range(6)
        ]
        campaign = self._unpaired_campaign(baseline_trials, feature_trials)
        row, _ = self._relscore_row("by_target/unpaired-held", campaign)
        self.assertEqual(row["relcov_status"], "held")
        self.assertEqual(row["verdict"], "improvement")

    def test_statistical_verdict_significant_paired_improvement(self):
        campaign = self._seeded_campaign([3, 3, 3, 3, 3, 3])
        rows, directive = analyze.build_differential_coverage_verdict_rows(
            self._summary_for("by_target/aave", campaign),
            {"by_target/aave": campaign},
            pairing_mode="paired",
        )
        self.assertEqual({row["metric"] for row in rows}, {"relscore", "relcov"})
        row = next(item for item in rows if item["metric"] == "relscore")
        relcov_row = next(item for item in rows if item["metric"] == "relcov")
        self.assertEqual(relcov_row["verdict"], row["verdict"])
        self.assertEqual(relcov_row["reason"], row["reason"])
        self.assertEqual(row["verdict"], "improvement")
        self.assertFalse(row["too_few_samples"])
        self.assertEqual(row["n_samples"], 6)
        self.assertLessEqual(row["p_value_adjusted"], 0.05)
        self.assertGreaterEqual(row["effect_size_a12"], analyze.A12_MEANINGFUL_HIGH)
        self.assertEqual(row["relcov_status"], "held")
        self.assertEqual(directive["aggregate"]["verdict"], "improvement")

    def test_statistical_verdict_noisy_equal_means_inconclusive(self):
        campaign = self._seeded_campaign([1, 1, 1, 1, 1, 1])
        row, _ = self._relscore_row("by_target/noise", campaign, pairing_mode="paired")
        self.assertEqual(row["verdict"], "inconclusive")
        self.assertEqual(row["reason"], "not significant after correction")

    def test_statistical_verdict_too_few_runs_keeps_point_estimates(self):
        campaign = self._seeded_campaign([5])
        row, _ = self._relscore_row("by_target/tiny", campaign, pairing_mode="paired")
        self.assertEqual(row["verdict"], "inconclusive")
        self.assertEqual(row["reason"], "too few runs")
        self.assertEqual(row["n_samples"], 1)
        self.assertTrue(row["too_few_samples"])
        self.assertGreater(row["feature_relscore"], 0.0)
        self.assertGreater(row["feature_performance"], 0.0)

    def test_statistical_verdict_relscores_up_but_relcov_failed(self):
        campaign = self._seeded_campaign(
            [10, 10, 10, 10, 10, 10],
            shared_count=5,
            master_unique_count=3,
        )
        row, _ = self._relscore_row("by_target/shift", campaign, pairing_mode="paired")
        self.assertEqual(row["verdict"], "regression")
        self.assertEqual(row["relcov_status"], "failed")
        self.assertGreater(row["feature_sample_mean"], row["baseline_sample_mean"])

    def test_target_regression_blocks_aggregate_improvement(self):
        good = self._seeded_campaign([3, 3, 3, 3, 3, 3])
        bad = self._seeded_campaign(
            [10, 10, 10, 10, 10, 10],
            shared_count=5,
            master_unique_count=3,
        )
        summary = self._summary_for("by_target/good", good) + self._summary_for(
            "by_target/bad", bad
        )
        rows, directive = analyze.build_differential_coverage_verdict_rows(
            summary,
            {"by_target/good": good, "by_target/bad": bad},
            pairing_mode="paired",
        )
        verdicts = {
            row["campaign"]: row["verdict"]
            for row in rows
            if row["metric"] == "relscore"
        }
        self.assertEqual(verdicts["by_target/good"], "improvement")
        self.assertEqual(verdicts["by_target/bad"], "regression")
        self.assertEqual(directive["aggregate"]["verdict"], "regression")

    def test_relcov_delta_ci_is_non_degenerate_when_samples_vary(self):
        campaign = self._non_saturating_paired_campaign(
            lambda idx, region_idx, _baseline_edges: {
                f"c{region_idx}-{edge}" for edge in range(5 + (idx % 4))
            }
            | {f"feature-{idx}-{extra}" for extra in range(8)}
        )
        row, _ = self._relscore_row("by_target/varying", campaign, pairing_mode="paired")
        self.assertEqual(row["relcov_delta_ci_status"], "ok")
        self.assertNotEqual(row["relcov_delta_ci_low"], row["relcov_delta_ci_high"])

    def test_combined_is_not_a_suite_name_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            showmap_dir = root / "logs" / "i-live" / "showmap" / "foundry-master__combined"
            showmap_dir.mkdir(parents=True)
            (showmap_dir / "trial-1.txt").write_text("a:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)

            self.assertTrue(
                (
                    out_dir
                    / "showmap_campaigns"
                    / "by_test"
                    / "combined"
                    / "foundry-master"
                    / "i-live__trial-1.txt"
                ).is_file()
            )

    def test_sanitizes_special_path_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            showmap_root = root / "logs" / "i-live" / "showmap"
            unsafe_dir = showmap_root / "..__Suite"
            safe_dir = showmap_root / "feature__Suite"
            unsafe_dir.mkdir(parents=True)
            safe_dir.mkdir(parents=True)
            (unsafe_dir / "trial-1.txt").write_text("a:1\n", encoding="utf-8")
            (safe_dir / "trial-1.txt").write_text("b:1\n", encoding="utf-8")

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(root / "logs", out_dir)

            combined = out_dir / "showmap_campaigns" / "combined"
            self.assertTrue((combined / "unknown").is_dir())
            self.assertTrue((combined / "feature").is_dir())
            self.assertFalse((out_dir / "showmap_campaigns" / "i-live__trial-1.txt").exists())

    def test_skips_large_by_test_campaigns_but_keeps_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            showmap_root = root / "logs" / "i-live" / "showmap"
            for approach in ("foundry-master", "foundry-feature"):
                showmap_dir = showmap_root / f"{approach}__Suite"
                showmap_dir.mkdir(parents=True)
                (showmap_dir / "trial-1.txt").write_text(
                    "a:1\nb:1\nc:1\n",
                    encoding="utf-8",
                )

            out_dir = root / "out"
            analyze.write_differential_coverage_outputs(
                root / "logs",
                out_dir,
                max_work_items=1,
            )

            with (out_dir / "differential_coverage_relscores.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                sorted({row["campaign"] for row in rows}),
                ["combined"],
            )

            manifest = json.loads(
                (out_dir / "showmap_campaign_manifest.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "skipped_analysis",
                manifest["campaigns"]["by_test/Suite"],
            )
            self.assertNotIn(
                "skipped_analysis",
                manifest["campaigns"]["combined"],
            )


if __name__ == "__main__":
    unittest.main()
