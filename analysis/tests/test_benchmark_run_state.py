import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


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

    def test_cleanup_accepts_complete_artifacts_or_safely_stale_orphan(self):
        metadata = {
            "run_started_at_epoch": 1_000,
            "timeout_hours": 1,
            "orphan_after_epoch": 10_000,
            "updated_at_epoch": 2_000,
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
        self.assertIsNone(
            self.module.cleanup_eligibility(
                metadata,
                active_instance_count=1,
                final_archive_count=0,
                now_epoch=10_000,
                latest_heartbeat_epoch=9_900,
            )
        )
        self.assertIsNone(
            self.module.cleanup_eligibility(
                metadata,
                active_instance_count=0,
                final_archive_count=0,
                now_epoch=10_000,
                latest_heartbeat_epoch=9_900,
            )
        )
        self.assertEqual(
            "orphan-deadline",
            self.module.cleanup_eligibility(
                metadata,
                active_instance_count=0,
                final_archive_count=0,
                now_epoch=12_000,
                latest_heartbeat_epoch=9_900,
            ),
        )

    def test_over_duration_healthy_run_is_never_an_orphan(self):
        metadata = {
            "run_started_at_epoch": 1_000,
            "updated_at_epoch": 1_100,
            "timeout_hours": 1,
            "orphan_after_epoch": 5_000,
            "instances_per_fuzzer": 1,
            "fuzzers": ["foundry"],
        }
        self.assertIsNone(
            self.module.cleanup_eligibility(
                metadata,
                active_instance_count=1,
                final_archive_count=0,
                now_epoch=50_000,
                latest_heartbeat_epoch=49_900,
            )
        )
        # A failed heartbeat upload is also conservative: an active tagged
        # instance alone is sufficient to prohibit scheduled destruction.
        self.assertIsNone(
            self.module.cleanup_eligibility(
                metadata,
                active_instance_count=1,
                final_archive_count=0,
                now_epoch=50_000,
                latest_heartbeat_epoch=None,
            )
        )

    def test_s3_object_listing_uses_explicit_api_pagination(self):
        pages = [
            {
                "Contents": [{"Key": "prefix/one", "LastModified": "2026-01-01T00:00:00Z"}],
                "IsTruncated": True,
                "NextContinuationToken": "opaque-token",
            },
            {
                "Contents": [{"Key": "prefix/two", "LastModified": "2026-01-01T00:01:00Z"}],
                "IsTruncated": False,
            },
        ]
        with mock.patch.object(self.module, "_aws_json", side_effect=pages) as aws:
            objects = self.module.list_s3_objects("bucket", "prefix/")

        self.assertEqual(["prefix/one", "prefix/two"], [item["Key"] for item in objects])
        self.assertIn("--no-paginate", aws.call_args_list[0].args)
        self.assertIn("--continuation-token", aws.call_args_list[1].args)
        self.assertIn("opaque-token", aws.call_args_list[1].args)

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

    def test_plan_modes_require_exact_managed_actions(self):
        base = {
            "address": "random_id.suffix",
            "mode": "managed",
            "type": "random_id",
            "change": {"actions": ["no-op"], "before": {}, "after": {}},
        }
        self.assertTrue(
            self.module.validate_terraform_plan(
                {"resource_changes": [base]}, "apply", "gh-run-b-1"
            )
        )
        self.assertEqual(
            [],
            self.module.validate_terraform_plan(
                {"resource_changes": [base]}, "inspect", "gh-run-b-1"
            ),
        )

    def test_cloud_input_validation_rejects_identity_override(self):
        values = {
            "TARGET_REPO_URL": "https://github.com/example/target",
            "TARGET_COMMIT": "a" * 40,
            "BENCHMARK_TYPE": "property",
            "INSTANCE_TYPE": "c6a.4xlarge",
            "INSTANCES_PER_FUZZER": "2",
            "TIMEOUT_HOURS": "4",
            "FUZZERS_JSON": '["echidna","foundry"]',
            "FUZZER_ENV_JSON": '{"SCFUZZBENCH_RUN_ID":"foreign"}',
        }
        with self.assertRaisesRegex(ValueError, "may not override"):
            self.module.validate_benchmark_inputs(values)

    def test_cloud_input_validation_accepts_supported_request(self):
        values = {
            "TARGET_REPO_URL": "https://github.com/example/target",
            "TARGET_COMMIT": "a" * 40,
            "BENCHMARK_TYPE": "property",
            "INSTANCE_TYPE": "c6a.4xlarge",
            "INSTANCES_PER_FUZZER": "2",
            "TIMEOUT_HOURS": "4",
            "FUZZERS_JSON": '["echidna","foundry"]',
            "FUZZER_ENV_JSON": '{"ECHIDNA_WORKERS":"4"}',
        }
        normalized = self.module.validate_benchmark_inputs(values)
        self.assertEqual(["echidna", "foundry"], normalized["fuzzers"])

    def test_recovery_inputs_require_exact_identity_and_shared_bucket(self):
        payload = {
            "run_id": "gh-123-1",
            "terraform_backend_key": "runs/gh-123-1/terraform.tfstate",
            "existing_bucket_name": "shared-bucket",
        }
        self.module.validate_recovery_inputs(
            payload,
            run_id="gh-123-1",
            backend_key="runs/gh-123-1/terraform.tfstate",
            bucket="shared-bucket",
        )
        foreign = dict(payload, run_id="gh-foreign-1")
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.module.validate_recovery_inputs(
                foreign,
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                bucket="shared-bucket",
            )

    def test_state_outputs_are_mandatory_and_cross_run_state_is_rejected(self):
        outputs = {
            "run_id": {"value": "gh-123-1"},
            "terraform_backend_key": {
                "value": "runs/gh-123-1/terraform.tfstate"
            },
            "benchmark_uuid": {"value": "a" * 32},
        }
        self.module.validate_state_outputs(
            outputs,
            run_id="gh-123-1",
            backend_key="runs/gh-123-1/terraform.tfstate",
            benchmark_uuid="a" * 32,
        )
        outputs["run_id"]["value"] = "gh-foreign-1"
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.module.validate_state_outputs(
                outputs,
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
            )

    def test_workflow_validates_before_reservation_and_guards_recovery(self):
        root = Path(__file__).resolve().parents[2]
        benchmark = (root / ".github/workflows/benchmark-run.yml").read_text()
        cleanup = (root / ".github/workflows/benchmark-cleanup.yml").read_text()
        recovery = (root / ".github/workflows/terraform-cd.yml").read_text()

        self.assertLess(
            benchmark.index("Validate all cloud inputs before admission"),
            benchmark.index("Atomically reserve repository run capacity"),
        )
        self.assertIn("validate-recovery-inputs", cleanup)
        self.assertIn("validate-state-outputs", cleanup)
        self.assertIn("--mode inspect", recovery)
        self.assertIn("validate-state-outputs", recovery)
        self.assertNotIn("tf_args:", recovery)

    def test_run_namespace_keeps_full_identity_digest(self):
        root = Path(__file__).resolve().parents[2]
        terraform = (root / "infrastructure/main.tf").read_text()
        self.assertIn("run_name_hash", terraform)
        self.assertIn("sha256(tostring(local.run_id))", terraform)
        self.assertIn(
            "preliminary/${local.run_id}/${local.benchmark_uuid}/*",
            terraform,
        )

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
