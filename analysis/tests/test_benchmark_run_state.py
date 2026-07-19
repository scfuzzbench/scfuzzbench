import importlib.util
import sys
import unittest
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_run_state.py"
    spec = importlib.util.spec_from_file_location("benchmark_run_state", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BenchmarkRunStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_identity_is_unique_per_actions_attempt_and_matches_backend(self):
        first = self.module.make_identity("123456", "1", 1_800_000_000)
        retry = self.module.make_identity("123456", "2", 1_800_000_001)

        self.assertEqual("gh-123456-1", first["run_id"])
        self.assertEqual(
            "runs/gh-123456-1/terraform.tfstate",
            first["terraform_backend_key"],
        )
        self.assertNotEqual(first["run_id"], retry["run_id"])
        self.assertNotEqual(
            first["terraform_backend_key"], retry["terraform_backend_key"]
        )

    def test_two_overlapping_admissions_cannot_both_take_one_slot(self):
        first_allowed, first_occupied = self.module.can_admit(
            "gh-100-1", 1, [], []
        )
        self.assertTrue(first_allowed)
        self.assertEqual(set(), first_occupied)

        # The short serialized first admission has now written its reservation.
        second_allowed, second_occupied = self.module.can_admit(
            "gh-101-1",
            1,
            ["run-state/admissions/active/gh-100-1.json"],
            [],
        )
        self.assertFalse(second_allowed)
        self.assertEqual({"gh-100-1"}, second_occupied)

    def test_reservations_and_instances_for_same_run_use_one_slot(self):
        allowed, occupied = self.module.can_admit(
            "gh-101-1",
            2,
            ["run-state/admissions/active/gh-100-1.json"],
            [
                {
                    "InstanceId": "i-one",
                    "Tags": [
                        {"Key": "Project", "Value": "scfuzzbench"},
                        {"Key": "RunId", "Value": "gh-100-1"},
                    ],
                }
            ],
        )
        self.assertTrue(allowed)
        self.assertEqual({"gh-100-1"}, occupied)

    def test_legacy_active_instance_conservatively_consumes_capacity(self):
        allowed, occupied = self.module.can_admit(
            "gh-101-1",
            1,
            [],
            [{"InstanceId": "i-legacy", "Tags": [{"Key": "Project", "Value": "scfuzzbench"}]}],
        )
        self.assertFalse(allowed)
        self.assertEqual({"legacy:i-legacy"}, occupied)

    def test_malformed_active_reservation_conservatively_consumes_capacity(self):
        allowed, occupied = self.module.can_admit(
            "gh-101-1",
            1,
            ["run-state/admissions/active/not/a/run.json"],
            [],
        )
        self.assertFalse(allowed)
        self.assertEqual(
            {
                "invalid-reservation:"
                "run-state/admissions/active/not/a/run.json"
            },
            occupied,
        )

    def test_cleanup_accepts_complete_artifacts_or_orphan_deadline(self):
        metadata = {
            "run_started_at_epoch": 1_000,
            "timeout_hours": 1,
            "orphan_after_epoch": 10_000,
            "instances_per_fuzzer": 2,
            "fuzzers": ["echidna", "medusa"],
        }
        self.assertEqual(
            "terminal-artifacts",
            self.module.cleanup_eligibility(
                metadata,
                active_instance_count=0,
                final_archive_count=4,
                now_epoch=5_000,
            ),
        )
        self.assertIsNone(
            self.module.cleanup_eligibility(
                metadata,
                active_instance_count=1,
                final_archive_count=4,
                now_epoch=5_000,
            )
        )
        self.assertEqual(
            "orphan-deadline",
            self.module.cleanup_eligibility(
                metadata,
                active_instance_count=1,
                final_archive_count=0,
                now_epoch=10_000,
            ),
        )

    def test_apply_plan_rejects_cross_run_and_shared_bucket(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_instance.fuzzer[\"echidna-0\"]",
                    "type": "aws_instance",
                    "change": {
                        "actions": ["create"],
                        "before": None,
                        "after": {
                            "tags": {
                                "Project": "scfuzzbench",
                                "RunId": "gh-run-a-1",
                                "BenchmarkUuid": "a" * 32,
                            }
                        },
                    },
                },
                {
                    "address": "aws_s3_bucket.logs[0]",
                    "type": "aws_s3_bucket",
                    "change": {
                        "actions": ["create"],
                        "before": None,
                        "after": {},
                    },
                },
            ]
        }
        errors = self.module.validate_terraform_plan(plan, "apply", "gh-run-b-1")
        self.assertTrue(any("RunId tag" in error for error in errors))
        self.assertTrue(any("shared S3" in error for error in errors))

    def test_cleanup_plan_rejects_cross_run_and_non_delete_change(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_vpc.main",
                    "type": "aws_vpc",
                    "change": {
                        "actions": ["delete", "create"],
                        "before": {
                            "tags": {
                                "Project": "scfuzzbench",
                                "RunId": "gh-run-a-1",
                                "BenchmarkUuid": "a" * 32,
                            }
                        },
                        "after": {
                            "tags": {
                                "Project": "scfuzzbench",
                                "RunId": "gh-run-b-1",
                                "BenchmarkUuid": "b" * 32,
                            }
                        },
                    },
                }
            ]
        }
        errors = self.module.validate_terraform_plan(
            plan, "cleanup", "gh-run-b-1"
        )
        self.assertTrue(any("delete-only" in error for error in errors))
        self.assertTrue(any("RunId tag" in error for error in errors))

    def test_plan_rejects_unexpected_managed_resource_type(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_dynamodb_table.shared",
                    "mode": "managed",
                    "type": "aws_dynamodb_table",
                    "change": {
                        "actions": ["create"],
                        "before": None,
                        "after": {},
                    },
                }
            ]
        }
        errors = self.module.validate_terraform_plan(plan, "apply", "gh-run-b-1")
        self.assertTrue(any("not run-owned" in error for error in errors))

    def test_new_run_start_requires_manifest_timestamp(self):
        self.assertEqual(
            1_800_000_000,
            self.module.run_started_at_epoch(
                "gh-123-1", {"run_started_at_epoch": 1_800_000_000}
            ),
        )
        self.assertEqual(
            1_774_000_000,
            self.module.run_started_at_epoch("1774000000", {}),
        )
        with self.assertRaises(ValueError):
            self.module.run_started_at_epoch("gh-123-1", {})


if __name__ == "__main__":
    unittest.main()
