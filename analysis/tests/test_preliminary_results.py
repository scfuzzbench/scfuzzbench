import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from analysis import benchmark_report, selector_analytics


def load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "preliminary_results.py"
    spec = importlib.util.spec_from_file_location("preliminary_results", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_manifest(module, *, interval=3600, timeout_hours=24):
    return {
        "schema": module.RUN_SCHEMA,
        "run_id": "gh-24680-1",
        "benchmark_uuid": "b" * 32,
        "run_started_at_epoch": 1_800_000_000,
        "timeout_hours": timeout_hours,
        "instances_per_fuzzer": 2,
        "fuzzer_keys": ["echidna", "foundry"],
        "preliminary": {
            "enabled": interval > 0,
            "interval_seconds": interval,
        },
    }


def snapshot_set(module, manifest, *, checkpoint=1):
    scheduled = (
        manifest["run_started_at_epoch"]
        + checkpoint * manifest["preliminary"]["interval_seconds"]
    )
    captured = scheduled + 5
    expected = len(module.expected_legs(manifest))
    return {
        "schema": module.SNAPSHOT_SET_SCHEMA,
        "run_id": manifest["run_id"],
        "benchmark_uuid": manifest["benchmark_uuid"],
        "checkpoint": checkpoint,
        "scheduled_at_epoch": scheduled,
        "scheduled_at_utc": module.utc_iso(scheduled),
        "capture_window_start_epoch": captured,
        "capture_window_end_epoch": captured,
        "as_of_epoch": captured,
        "as_of_utc": module.utc_iso(captured),
        "elapsed_seconds": captured - manifest["run_started_at_epoch"],
        "planned_timeout_seconds": int(manifest["timeout_hours"] * 3600),
        "expected_snapshots": expected,
        "present_snapshots": expected,
        "missing_replicates": [],
        "incomplete": True,
        "non_terminal": True,
        "comparative_decisions_allowed": False,
        "optional_stopping_allowed": False,
    }


class PreliminaryResultsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_verified_download_is_bounded_and_pinned_to_head_identity(self):
        value = b"immutable snapshot"
        head = {
            "ContentLength": len(value),
            "Metadata": {"sha256": hashlib.sha256(value).hexdigest()},
            "VersionId": "version-1",
            "ETag": '"etag-1"',
        }
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "snapshot.zip"

            def download(args, **_kwargs):
                destination.write_bytes(value)
                return 0

            with mock.patch.object(
                self.module, "aws_json", return_value=head
            ), mock.patch.object(
                self.module.subprocess, "check_call", side_effect=download
            ) as check_call:
                size = self.module._download_verified_object(
                    "bucket",
                    "preliminary/gh-1-1/" + "b" * 32
                    + "/snapshots/000001/echidna-1/snapshot.zip",
                    destination,
                    max_bytes=64,
                )

        self.assertEqual(len(value), size)
        command = check_call.call_args.args[0]
        self.assertEqual("bytes=0-64", command[command.index("--range") + 1])
        self.assertEqual("version-1", command[command.index("--version-id") + 1])
        self.assertEqual('"etag-1"', command[command.index("--if-match") + 1])

    def test_verified_download_rejects_oversized_mutated_response(self):
        original = b"head"
        head = {
            "ContentLength": len(original),
            "Metadata": {"sha256": hashlib.sha256(original).hexdigest()},
            "ETag": '"original-etag"',
        }
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "snapshot.zip"

            def oversized_download(args, **_kwargs):
                self.assertEqual("bytes=0-4", args[args.index("--range") + 1])
                destination.write_bytes(b"heads")
                return 0

            with mock.patch.object(
                self.module, "aws_json", return_value=head
            ), mock.patch.object(
                self.module.subprocess,
                "check_call",
                side_effect=oversized_download,
            ), self.assertRaisesRegex(ValueError, "downloaded object exceeds"):
                self.module._download_verified_object(
                    "bucket",
                    "preliminary/gh-1-1/" + "b" * 32
                    + "/snapshots/000001/echidna-1/snapshot.zip",
                    destination,
                    max_bytes=4,
                )

    def test_verified_download_propagates_identity_mismatch(self):
        value = b"head"
        head = {
            "ContentLength": len(value),
            "Metadata": {"sha256": hashlib.sha256(value).hexdigest()},
            "ETag": '"old-etag"',
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.module, "aws_json", return_value=head
        ), mock.patch.object(
            self.module.subprocess,
            "check_call",
            side_effect=subprocess.CalledProcessError(255, ["aws"]),
        ), self.assertRaises(subprocess.CalledProcessError):
            self.module._download_verified_object(
                "bucket",
                "preliminary/gh-1-1/" + "b" * 32
                + "/snapshots/000001/echidna-1/snapshot.zip",
                Path(tmp) / "snapshot.zip",
                max_bytes=4,
            )

    def test_verified_download_requires_stable_head_identity(self):
        value = b"head"
        head = {
            "ContentLength": len(value),
            "Metadata": {"sha256": hashlib.sha256(value).hexdigest()},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.module, "aws_json", return_value=head
        ), mock.patch.object(
            self.module.subprocess, "check_call"
        ) as check_call, self.assertRaisesRegex(ValueError, "stable S3 object identity"):
            self.module._download_verified_object(
                "bucket",
                "preliminary/gh-1-1/" + "b" * 32
                + "/snapshots/000001/echidna-1/snapshot.zip",
                Path(tmp) / "snapshot.zip",
                max_bytes=4,
            )
        check_call.assert_not_called()

    def test_run_manifest_defaults_to_hourly_and_supports_opaque_run_id(self):
        terraform_outputs = {
            "run_id": {"value": "gh-24680-1"},
            "run_started_at_epoch": {"value": "1800000000"},
            "benchmark_uuid": {"value": "b" * 32},
            "benchmark_manifest": {
                "value": {
                    "timeout_hours": 24,
                    "instances_per_fuzzer": 2,
                    "fuzzer_keys": ["echidna", "foundry"],
                }
            },
        }

        manifest = self.module.build_run_manifest(terraform_outputs)

        self.assertEqual("gh-24680-1", manifest["run_id"])
        self.assertEqual(1_800_000_000, manifest["run_started_at_epoch"])
        self.assertEqual(3600, manifest["preliminary"]["interval_seconds"])
        self.assertTrue(manifest["preliminary"]["enabled"])
        self.assertFalse(manifest["preliminary"]["optional_stopping_allowed"])

    def test_zero_interval_explicitly_disables_discovery(self):
        manifest = run_manifest(self.module, interval=0)
        key = f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}/run.json"
        snapshot = (
            f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}/"
            "snapshots/000001/echidna-0-i-a/snapshot.zip"
        )

        selected = self.module.select_active_checkpoints(
            keys=[key, snapshot],
            manifests={key: manifest},
            now_epoch=manifest["run_started_at_epoch"] + 7200,
        )

        self.assertEqual([], selected)

    def test_discovery_uses_latest_settled_coherent_checkpoint(self):
        manifest = run_manifest(self.module)
        prefix = f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}"
        run_key = f"{prefix}/run.json"
        keys = [
            run_key,
            f"{prefix}/snapshots/000001/echidna-0-i-a/snapshot.zip",
            f"{prefix}/snapshots/000002/echidna-0-i-a/snapshot.zip",
            # A third checkpoint exists but has not passed the settle window.
            f"{prefix}/snapshots/000003/echidna-0-i-a/snapshot.zip",
        ]

        selected = self.module.select_active_checkpoints(
            keys=keys,
            manifests={run_key: manifest},
            now_epoch=manifest["run_started_at_epoch"] + 2 * 3600 + 600,
            settle_seconds=300,
        )

        self.assertEqual(1, len(selected))
        self.assertEqual(2, selected[0]["checkpoint"])
        self.assertEqual(4, selected[0]["expected_snapshots"])

    def test_discovery_does_not_backfill_older_checkpoint_after_latest_is_published(self):
        manifest = run_manifest(self.module)
        prefix = f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}"
        run_key = f"{prefix}/run.json"
        keys = [
            run_key,
            f"{prefix}/snapshots/000001/echidna-0-i-a/snapshot.zip",
            f"{prefix}/snapshots/000002/echidna-0-i-a/snapshot.zip",
            f"{prefix}/analysis/000002/preliminary.json",
        ]

        selected = self.module.select_active_checkpoints(
            keys=keys,
            manifests={run_key: manifest},
            now_epoch=manifest["run_started_at_epoch"] + 2 * 3600 + 600,
        )

        self.assertEqual([], selected)

    def test_materializer_persists_verified_run_manifest_for_analysis(self):
        manifest = run_manifest(self.module)
        key = (
            f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}/"
            "snapshots/000001/echidna-0-i-0123456789abcdef0/snapshot.zip"
        )
        metadata = {
            "fuzzer_key": "echidna",
            "run_index": "0",
            "captured_at_epoch": manifest["run_started_at_epoch"] + 3605,
            "files": [],
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.module, "read_s3_json", return_value=manifest
        ), mock.patch.object(
            self.module, "list_keys", return_value=[key]
        ), mock.patch.object(
            self.module, "_download_verified_object", return_value=10
        ), mock.patch.object(
            self.module, "verify_and_extract_snapshot", return_value=metadata
        ):
            destination = Path(tmp) / "materialized"
            summary = self.module.materialize_checkpoint(
                bucket="bucket",
                run_id=manifest["run_id"],
                benchmark_uuid=manifest["benchmark_uuid"],
                checkpoint=1,
                destination=destination,
            )
            saved = json.loads((destination / "run.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["run_id"], saved["run_id"])
        self.assertEqual(
            manifest["run_started_at_epoch"], saved["run_started_at_epoch"]
        )
        self.assertEqual(1, summary["present_snapshots"])

    def test_preliminary_workflow_runs_current_analysis_surface(self):
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github"
            / "workflows"
            / "benchmark-preliminary.yml"
        ).read_text(encoding="utf-8")

        for expected in (
            "report-known-bugs",
            "COVERAGE_OVER_TIME=1",
            'SELECTOR_CORPUS_DIR="${DEST}/snapshots/corpus/unzipped"',
            'KNOWN_BUG_RUN_MANIFEST="${DEST}/snapshots/run.json"',
            'THROUGHPUT_SAMPLES_CSV="${DEST}/raw/data/throughput_samples.csv"',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, workflow)

    def test_s3_key_listing_follows_every_continuation_page(self):
        pages = [
            {
                "Contents": [{"Key": "preliminary/a"}],
                "IsTruncated": True,
                "NextContinuationToken": "next-token",
            },
            {
                "Contents": [{"Key": "preliminary/b"}],
                "IsTruncated": False,
            },
        ]
        with mock.patch.object(self.module, "aws_json", side_effect=pages) as aws_json:
            keys = self.module.list_keys("bucket", "preliminary/")

        self.assertEqual(["preliminary/a", "preliminary/b"], keys)
        self.assertIn("--continuation-token", aws_json.call_args_list[1].args[0])
        self.assertIn("next-token", aws_json.call_args_list[1].args[0])

    def test_s3_listing_caps_are_exact_and_never_silently_truncate(self):
        page_one = {
            "Contents": [{"Key": "preliminary/a"}],
            "IsTruncated": True,
            "NextContinuationToken": "two",
        }
        page_two = {
            "Contents": [{"Key": "preliminary/b"}],
            "IsTruncated": False,
        }
        with mock.patch.object(self.module, "MAX_LIST_PAGES", 2), mock.patch.object(
            self.module, "MAX_LIST_KEYS", 2
        ), mock.patch.object(
            self.module, "aws_json", side_effect=[page_one, page_two]
        ):
            self.assertEqual(
                ["preliminary/a", "preliminary/b"],
                self.module.list_keys("bucket", "preliminary/"),
            )

        page_two_truncated = {
            **page_two,
            "IsTruncated": True,
            "NextContinuationToken": "three",
        }
        with mock.patch.object(self.module, "MAX_LIST_PAGES", 2), mock.patch.object(
            self.module, "aws_json", side_effect=[page_one, page_two_truncated]
        ), self.assertRaisesRegex(ValueError, "exceeded 2 pages"):
            self.module.list_keys("bucket", "preliminary/")

        with mock.patch.object(self.module, "MAX_LIST_KEYS", 2), mock.patch.object(
            self.module,
            "aws_json",
            return_value={
                "Contents": [
                    {"Key": "preliminary/a"},
                    {"Key": "preliminary/b"},
                    {"Key": "preliminary/c"},
                ],
                "IsTruncated": False,
            },
        ), self.assertRaisesRegex(ValueError, "exceeded 2 keys"):
            self.module.list_keys("bucket", "preliminary/")

        with mock.patch.object(
            self.module,
            "aws_json",
            return_value={"Contents": [], "IsTruncated": True},
        ), self.assertRaisesRegex(RuntimeError, "without a continuation token"):
            self.module.list_keys("bucket", "preliminary/")

    def test_discovery_caps_run_manifests_and_selected_matrix_exactly(self):
        first = run_manifest(self.module)
        second = {
            **run_manifest(self.module),
            "run_id": "gh-24681-1",
            "benchmark_uuid": "c" * 32,
        }
        first_key = (
            f"preliminary/{first['run_id']}/{first['benchmark_uuid']}/run.json"
        )
        second_key = (
            f"preliminary/{second['run_id']}/{second['benchmark_uuid']}/run.json"
        )
        with mock.patch.object(self.module, "MAX_RUN_MANIFESTS", 1), self.assertRaisesRegex(
            ValueError, "exceeded 1 run manifests"
        ):
            self.module.select_active_checkpoints(
                keys=[],
                manifests={first_key: first, second_key: second},
                now_epoch=first["run_started_at_epoch"] + 4000,
            )

        snapshot_keys = [
            (
                f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}/"
                "snapshots/000001/echidna-0-i-abc/snapshot.zip"
            )
            for manifest in (first, second)
        ]
        with mock.patch.object(self.module, "MAX_SELECTED_RUNS", 1), self.assertRaisesRegex(
            ValueError, "exceeded 1 selected runs"
        ):
            self.module.select_active_checkpoints(
                keys=snapshot_keys,
                manifests={first_key: first, second_key: second},
                now_epoch=first["run_started_at_epoch"] + 4000,
            )

    def test_requested_discovery_lists_only_the_exact_run_prefix(self):
        manifest = run_manifest(self.module)
        prefix = f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}"
        run_key = f"{prefix}/run.json"
        with mock.patch.object(
            self.module.sys,
            "argv",
            [
                "preliminary_results.py",
                "discover",
                "--bucket",
                "bucket",
                "--run-id",
                manifest["run_id"],
                "--benchmark-uuid",
                manifest["benchmark_uuid"],
                "--now-epoch",
                str(manifest["run_started_at_epoch"] + 4000),
            ],
        ), mock.patch.object(
            self.module, "list_keys", return_value=[run_key]
        ) as list_keys, mock.patch.object(
            self.module, "read_s3_json", return_value=manifest
        ), mock.patch.object(
            self.module, "_write_github_output"
        ):
            self.assertEqual(0, self.module.main())

        list_keys.assert_called_once_with("bucket", f"{prefix}/")

    def test_finalized_marker_must_be_valid_before_it_hides_a_run(self):
        manifest = run_manifest(self.module)
        prefix = f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}"
        run_key = f"{prefix}/run.json"
        snapshot = (
            f"{prefix}/snapshots/000001/echidna-0-i-abc/snapshot.zip"
        )
        finalized_key = f"{prefix}/finalized.json"
        marker = {
            "schema": self.module.FINALIZED_SCHEMA,
            "run_id": manifest["run_id"],
            "benchmark_uuid": manifest["benchmark_uuid"],
            "canonical_release_tag": (
                f"scfuzzbench-{manifest['benchmark_uuid']}-{manifest['run_id']}"
            ),
            "preliminary_stream_closed": True,
        }
        self.module.validate_finalized_marker(
            marker,
            run_id=manifest["run_id"],
            benchmark_uuid=manifest["benchmark_uuid"],
        )
        with self.assertRaisesRegex(ValueError, "canonical release tag"):
            self.module.validate_finalized_marker(
                {**marker, "canonical_release_tag": "forged"},
                run_id=manifest["run_id"],
                benchmark_uuid=manifest["benchmark_uuid"],
            )

        selected_without_validated_marker = self.module.select_active_checkpoints(
            keys=[run_key, snapshot, finalized_key],
            manifests={run_key: manifest},
            now_epoch=manifest["run_started_at_epoch"] + 4000,
        )
        selected_with_validated_marker = self.module.select_active_checkpoints(
            keys=[run_key, snapshot, finalized_key],
            manifests={run_key: manifest},
            finalized_runs={(manifest["run_id"], manifest["benchmark_uuid"])},
            now_epoch=manifest["run_started_at_epoch"] + 4000,
        )
        self.assertEqual(1, len(selected_without_validated_marker))
        self.assertEqual([], selected_with_validated_marker)

    def test_materializer_caps_snapshot_count_and_cumulative_bytes(self):
        manifest = run_manifest(self.module)
        prefix = f"preliminary/{manifest['run_id']}/{manifest['benchmark_uuid']}"
        keys = [
            f"{prefix}/snapshots/000001/echidna-0-i-aaa/snapshot.zip",
            f"{prefix}/snapshots/000001/foundry-0-i-bbb/snapshot.zip",
        ]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.module, "read_s3_json", return_value=manifest
        ), mock.patch.object(
            self.module, "list_keys", return_value=keys
        ), mock.patch.object(
            self.module, "MAX_SNAPSHOTS_PER_CHECKPOINT", 1
        ), self.assertRaisesRegex(ValueError, "more than 1 snapshot"):
            self.module.materialize_checkpoint(
                bucket="bucket",
                run_id=manifest["run_id"],
                benchmark_uuid=manifest["benchmark_uuid"],
                checkpoint=1,
                destination=Path(tmp) / "too-many",
            )

        metadata = [
            {
                "fuzzer_key": "echidna",
                "run_index": "0",
                "captured_at_epoch": manifest["run_started_at_epoch"] + 3605,
                "files": [{"snapshot_size": 5}],
            },
            {
                "fuzzer_key": "foundry",
                "run_index": "0",
                "captured_at_epoch": manifest["run_started_at_epoch"] + 3605,
                "files": [{"snapshot_size": 5}],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.module, "read_s3_json", return_value=manifest
        ), mock.patch.object(
            self.module, "list_keys", return_value=keys
        ), mock.patch.object(
            self.module, "_download_verified_object", side_effect=[5, 5]
        ) as download, mock.patch.object(
            self.module, "verify_and_extract_snapshot", side_effect=metadata
        ) as verify, mock.patch.object(
            self.module, "MAX_CHECKPOINT_ARCHIVE_BYTES", 10
        ), mock.patch.object(
            self.module, "MAX_CHECKPOINT_EXPANDED_BYTES", 10
        ):
            summary = self.module.materialize_checkpoint(
                bucket="bucket",
                run_id=manifest["run_id"],
                benchmark_uuid=manifest["benchmark_uuid"],
                checkpoint=1,
                destination=Path(tmp) / "exact-boundary",
            )
        self.assertEqual(10, summary["archive_bytes"])
        self.assertEqual(10, summary["expanded_bytes"])
        self.assertEqual(5, download.call_args_list[1].kwargs["max_bytes"])
        self.assertEqual(5, verify.call_args_list[1].kwargs["max_expanded_bytes"])

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.module, "read_s3_json", return_value=manifest
        ), mock.patch.object(
            self.module, "list_keys", return_value=keys
        ), mock.patch.object(
            self.module, "_download_verified_object", side_effect=[5, 6]
        ), mock.patch.object(
            self.module, "verify_and_extract_snapshot", side_effect=metadata
        ), mock.patch.object(
            self.module, "MAX_CHECKPOINT_ARCHIVE_BYTES", 10
        ), self.assertRaisesRegex(ValueError, "archives exceed"):
            self.module.materialize_checkpoint(
                bucket="bucket",
                run_id=manifest["run_id"],
                benchmark_uuid=manifest["benchmark_uuid"],
                checkpoint=1,
                destination=Path(tmp) / "archive-over",
            )

        expanded_over = [metadata[0], {**metadata[1], "files": [{"snapshot_size": 6}]}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            self.module, "read_s3_json", return_value=manifest
        ), mock.patch.object(
            self.module, "list_keys", return_value=keys
        ), mock.patch.object(
            self.module, "_download_verified_object", side_effect=[1, 1]
        ), mock.patch.object(
            self.module, "verify_and_extract_snapshot", side_effect=expanded_over
        ), mock.patch.object(
            self.module, "MAX_CHECKPOINT_EXPANDED_BYTES", 10
        ), self.assertRaisesRegex(ValueError, "snapshots exceed"):
            self.module.materialize_checkpoint(
                bucket="bucket",
                run_id=manifest["run_id"],
                benchmark_uuid=manifest["benchmark_uuid"],
                checkpoint=1,
                destination=Path(tmp) / "expanded-over",
            )

    def test_one_hour_checkpoint_uses_one_hour_analysis_budget_for_four_hour_run(self):
        import pandas as pd

        manifest = run_manifest(self.module, timeout_hours=4)
        context = snapshot_set(self.module, manifest, checkpoint=1)

        budget = self.module.checkpoint_analysis_budget_hours(manifest, context)
        self.assertEqual(1.0, budget)
        bugs = pd.DataFrame(
            [
                ("foundry", "run-1", 0.0, 0),
                ("foundry", "run-1", 1.0, 1),
            ],
            columns=["fuzzer", "run_id", "time_hours", "bugs_found"],
        )
        samples = pd.DataFrame(
            [("foundry", "run-1:i-abc", 0.95)],
            columns=["fuzzer", "series_id", "time_hours"],
        )
        self.assertEqual(
            [],
            benchmark_report.build_run_health_warnings(
                bugs,
                [samples],
                budget=budget,
            ),
        )
        self.assertIn(
            "terminated early",
            benchmark_report.build_run_health_warnings(
                bugs,
                [samples],
                budget=manifest["timeout_hours"],
            )[0],
        )
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github"
            / "workflows"
            / "benchmark-preliminary.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "REPORT_BUDGET: ${{ steps.analysis_budget.outputs.report_budget_hours }}",
            workflow,
        )
        self.assertNotIn("REPORT_BUDGET: ${{ matrix.timeout_hours }}", workflow)

    def test_preliminary_log_only_selector_input_is_semantically_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "snapshot-logs"
            for label in (
                "i-aaaa-echidna-v2.3.1",
                "i-bbbb-medusa-v1.4.1",
                "i-cccc-foundry-git-deadbee",
                "i-dddd-recon-v0.4.6",
            ):
                (logs / label / "logs").mkdir(parents=True)
            guaranteed_absent = root / "guaranteed-absent-corpus"

            rows, summary = selector_analytics.analyze_selector_artifacts(
                corpus_dir=guaranteed_absent,
                logs_dir=logs,
                run_id="gh-24680-1",
            )

        self.assertFalse(guaranteed_absent.exists())
        self.assertEqual([], rows)
        self.assertEqual(
            {"unavailable"},
            {item["status"] for item in summary["instances"]},
        )
        self.assertEqual(
            {
                "echidna",
                "foundry",
                "medusa",
                "recon-fuzzer",
            },
            {item["fuzzer"] for item in summary["instances"]},
        )
        self.assertIn(
            "cannot be reconstructed from logs alone",
            "\n".join(summary["limitations"]),
        )

    def test_runner_iam_can_write_only_own_artifact_and_snapshot_paths(self):
        terraform = (
            Path(__file__).resolve().parents[2] / "infrastructure" / "main.tf"
        ).read_text(encoding="utf-8")
        policy = terraform.split(
            'data "aws_iam_policy_document" "s3_access" {', 1
        )[1].split('data "aws_iam_policy_document" "public_read" {', 1)[0]

        for resource in (
            "/logs/${local.run_id}/${local.benchmark_uuid}/*",
            "/corpus/${local.run_id}/${local.benchmark_uuid}/*",
            "/runs/${local.run_id}/${local.benchmark_uuid}/manifest.json",
            "/preliminary/${local.run_id}/${local.benchmark_uuid}/snapshots/*",
        ):
            with self.subTest(resource=resource):
                self.assertIn(resource, policy)
        self.assertNotIn(
            '"arn:aws:s3:::${local.bucket_name}/*"',
            policy,
        )
        self.assertNotIn(
            "/preliminary/${local.run_id}/${local.benchmark_uuid}/analysis/",
            policy,
        )
        self.assertNotIn(
            "/preliminary/${local.run_id}/${local.benchmark_uuid}/finalized.json",
            policy,
        )
        self.assertNotIn(
            "/preliminary/${local.run_id}/${local.benchmark_uuid}/run.json",
            policy,
        )
        self.assertIn('"s3:GetObject", "s3:GetObjectVersion"', policy)
        self.assertIn('variable = "s3:prefix"', policy)
        self.assertIn(
            "source_policy_documents = [data.aws_iam_policy_document.s3_access.json]",
            terraform,
        )

    def test_prefix_guard_refuses_canonical_artifact_paths(self):
        for key in (
            "logs/gh-1/abc/snapshot.zip",
            "analysis/abc/gh-1/REPORT.md",
            "runs/gh-1/abc/manifest.json",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                ValueError, "non-preliminary"
            ):
                self.module.assert_preliminary_key(key)

    def test_immutable_put_retries_and_never_drops_precondition(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "object.json"
            source.write_text('{"ok":true}\n', encoding="utf-8")
            expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            responses = [
                SimpleNamespace(returncode=1, stderr="temporary"),
                SimpleNamespace(returncode=0, stderr=""),
            ]
            with mock.patch.object(
                self.module.subprocess, "run", side_effect=responses
            ) as run, mock.patch.object(
                self.module,
                "aws_json",
                side_effect=self.module.subprocess.CalledProcessError(1, ["aws"]),
            ), mock.patch.object(self.module.time, "sleep"):
                digest = self.module.put_immutable(
                    bucket="bucket",
                    key="preliminary/gh-1/" + "a" * 32 + "/run.json",
                    source=source,
                    retry_delay=0,
                )

        self.assertEqual(expected_digest, digest)
        self.assertEqual(2, run.call_count)
        for call in run.call_args_list:
            self.assertIn("--if-none-match", call.args[0])
            self.assertIn("*", call.args[0])

    def test_immutable_put_rejects_existing_object_without_checksum_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "object.json"
            source.write_text('{"ok":true}\n', encoding="utf-8")
            failed = SimpleNamespace(returncode=1, stderr="precondition failed")
            with mock.patch.object(
                self.module.subprocess, "run", return_value=failed
            ) as run, mock.patch.object(
                self.module,
                "aws_json",
                return_value={"ContentLength": source.stat().st_size, "Metadata": {}},
            ), self.assertRaisesRegex(RuntimeError, "missing SHA-256"):
                self.module.put_immutable(
                    bucket="bucket",
                    key="preliminary/gh-1/" + "a" * 32 + "/run.json",
                    source=source,
                    retry_delay=0,
                )
            self.assertEqual(1, run.call_count)

    def test_watermark_covers_all_markdown_and_chart_paths_in_copy_only(self):
        from PIL import Image

        context = {
            "schema": self.module.SNAPSHOT_SET_SCHEMA,
            "run_id": "gh-24680-1",
            "benchmark_uuid": "b" * 32,
            "checkpoint": 2,
            "scheduled_at_epoch": 1_800_007_200,
            "scheduled_at_utc": "2027-01-15T10:00:00Z",
            "capture_window_start_epoch": 1_800_007_200,
            "capture_window_end_epoch": 1_800_007_200,
            "as_of_epoch": 1_800_007_200,
            "as_of_utc": "2027-01-15T10:00:00Z",
            "elapsed_seconds": 7200,
            "planned_timeout_seconds": 86400,
            "expected_snapshots": 4,
            "present_snapshots": 3,
            "missing_replicates": [{"fuzzer_key": "foundry", "run_index": "1"}],
            "incomplete": True,
            "non_terminal": True,
            "comparative_decisions_allowed": False,
            "optional_stopping_allowed": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "canonical-shaped-output"
            (source / "data").mkdir(parents=True)
            (source / "images" / "nested").mkdir(parents=True)
            report = source / "data" / "REPORT.md"
            report.write_text("# Benchmark report\n", encoding="utf-8")
            chart_a = source / "images" / "bugs_over_time.png"
            chart_b = source / "images" / "nested" / "optional.png"
            Image.new("RGB", (80, 40), "white").save(chart_a)
            Image.new("RGB", (80, 40), "blue").save(chart_b)
            before = {
                path.relative_to(source).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in source.rglob("*")
                if path.is_file()
            }
            destination = root / "preliminary-output"

            metadata = self.module.watermark_tree(
                source=source, destination=destination, snapshot_set=context
            )

            after_source = {
                path.relative_to(source).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in source.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after_source)
            self.assertEqual(
                [
                    "PRELIMINARY.md",
                    "data/REPORT.md",
                    "images/bugs_over_time.png",
                    "images/nested/optional.png",
                ],
                metadata["watermarked_files"],
            )
            self.assertIn(
                "PRELIMINARY — INCOMPLETE — DO NOT COMPARE OR STOP",
                (destination / "data" / "REPORT.md").read_text(encoding="utf-8"),
            )
            with Image.open(destination / "images" / "bugs_over_time.png") as marked:
                self.assertGreater(marked.height, 40)
                self.assertEqual((139, 0, 0), marked.convert("RGB").getpixel((0, 0)))

    def test_materializer_fails_closed_on_archive_file_checksum_mismatch(self):
        snapshot_path = (
            Path(__file__).resolve().parents[2] / "scripts" / "preliminary_snapshot.py"
        )
        spec = importlib.util.spec_from_file_location(
            "preliminary_snapshot_for_verification", snapshot_path
        )
        snapshot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(snapshot)
        manifest = self.module.validate_run_manifest(
            run_manifest(self.module),
            run_id="gh-24680-1",
            benchmark_uuid="b" * 32,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            (logs / "foundry.log").write_text("line one\n", encoding="utf-8")
            archive = root / "snapshot.zip"
            snapshot.capture_snapshot(
                log_dir=logs,
                archive_path=archive,
                run_id="gh-24680-1",
                run_started_at_epoch=1_800_000_000,
                benchmark_uuid="b" * 32,
                checkpoint=1,
                interval_seconds=3600,
                scheduled_at_epoch=1_800_003_600,
                captured_at_epoch=1_800_003_605,
                instance_id="i-0123456789abcdef0",
                fuzzer_key="foundry",
                run_index="0",
                fuzzer_label="foundry-v1",
                timeout_seconds=86400,
            )
            with zipfile.ZipFile(archive) as source:
                entries = {name: source.read(name) for name in source.namelist()}
            entries["logs/foundry.log"] = b"line two\n"
            with zipfile.ZipFile(archive, "w") as changed:
                for name, data in entries.items():
                    changed.writestr(name, data)
            key = (
                "preliminary/gh-24680-1/"
                + "b" * 32
                + "/snapshots/000001/foundry-0-i-0123456789abcdef0/snapshot.zip"
            )

            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.module.verify_and_extract_snapshot(
                    archive_path=archive,
                    output_dir=root / "out",
                    run_manifest=manifest,
                    object_key=key,
                    checkpoint=1,
                )

    def test_zip_inventory_rejects_duplicate_and_oversize_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "snapshot.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("checkpoint.json", b"{}")
                bundle.writestr("logs/run.log", b"one\n")
                bundle.writestr("logs/run.log", b"two\n")
            with zipfile.ZipFile(archive) as bundle, self.assertRaisesRegex(
                ValueError, "duplicate"
            ):
                self.module._safe_zip_entries(bundle)

            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("checkpoint.json", b"{}")
                bundle.writestr("logs/run.log", b"12345")
            with mock.patch.object(self.module, "MAX_SNAPSHOT_FILE_BYTES", 4):
                with zipfile.ZipFile(archive) as bundle, self.assertRaisesRegex(
                    ValueError, "byte cap"
                ):
                    self.module._safe_zip_entries(bundle)

    def test_result_metadata_rejects_missing_noncomparative_guards(self):
        metadata = {
            "schema": self.module.RESULT_SCHEMA,
            "run_id": "gh-24680-1",
            "benchmark_uuid": "b" * 32,
            "checkpoint": 1,
            "scheduled_at_epoch": 1_800_003_600,
            "scheduled_at_utc": self.module.utc_iso(1_800_003_600),
            "capture_window_start_epoch": 1_800_003_601,
            "capture_window_end_epoch": 1_800_003_601,
            "as_of_epoch": 1_800_003_601,
            "as_of_utc": self.module.utc_iso(1_800_003_601),
            "elapsed_seconds": 3601,
            "planned_timeout_seconds": 86400,
            "expected_snapshots": 1,
            "present_snapshots": 1,
            "missing_replicates": [],
            "incomplete": True,
            "non_terminal": True,
            "comparative_decisions_allowed": True,
            "optional_stopping_allowed": False,
            "watermarked_files": ["PRELIMINARY.md"],
            "source_file_sha256": {},
            "published_file_sha256": {"PRELIMINARY.md": "a" * 64},
        }

        with self.assertRaisesRegex(ValueError, "comparative_decisions_allowed"):
            self.module.validate_result_metadata(metadata)


if __name__ == "__main__":
    unittest.main()
