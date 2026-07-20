import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock


PROTECTED_RUNTIME_ENV_KEYS = (
    "SCFUZZBENCH_AWS_CREDS_REFRESH_PID",
    "SCFUZZBENCH_AWS_CREDS_REFRESH_PID_START_TICKS",
    "SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS",
    "SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION",
    "SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH",
    "SCFUZZBENCH_COMMON_SH",
    "SCFUZZBENCH_CORPUS_DIR",
    "SCFUZZBENCH_GIT_TOKEN",
    "SCFUZZBENCH_INSTANCE_ID",
    "SCFUZZBENCH_LOG_DIR",
    "SCFUZZBENCH_PRELIMINARY_PID",
    "SCFUZZBENCH_PRELIMINARY_PID_START_TICKS",
    "SCFUZZBENCH_RUNNER_METRICS_PID",
    "SCFUZZBENCH_RUNNER_METRICS_PID_START_TICKS",
    "SCFUZZBENCH_SEED_CORPUS_MAX_BYTES",
    "SCFUZZBENCH_SEED_CORPUS_MAX_FILES",
    "SCFUZZBENCH_TIMEOUT_GRACE_SECONDS",
    "SCFUZZBENCH_UPLOAD_DONE",
    "SCFUZZBENCH_WORKERS_RESOLVED",
)


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

    def test_tool_sources_are_normalized_once_before_admission(self):
        medusa_commit = "a" * 40
        normalized = self.module.normalize_tool_sources(
            {
                "MEDUSA_SOURCE_JSON": (
                    '{"medusa_git_repo":"https://github.com/crytic/medusa",'
                    '"medusa_git_ref":"master",'
                    f'"medusa_git_commit":"{medusa_commit}"'
                    "}"
                )
            }
        )

        self.assertEqual(
            "https://github.com/crytic/medusa",
            normalized["medusa_git_repo"],
        )
        self.assertEqual(medusa_commit, normalized["medusa_git_commit"])
        self.assertEqual("1.24.0", normalized["medusa_go_version"])
        self.assertRegex(normalized["medusa_go_sha256"], r"^[0-9a-f]{64}$")

        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            self.module.normalize_tool_sources(
                {
                    "ECHIDNA_CI_JSON": '{"echidna_ci_repo":"json"}',
                    "CALL_ECHIDNA_CI_REPO": "direct",
                }
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

    def test_forced_cleanup_is_explicit_single_run_and_includes_active_run(self):
        commit = "a" * 40
        metadata = {
            "run_id": "gh-100-1",
            "terraform_backend_key": "runs/gh-100-1/terraform.tfstate",
            "existing_bucket_name": "shared-bucket",
            "scfuzzbench_commit": commit,
            "status": "running",
            "benchmark_uuid": "b" * 32,
            "run_started_at_epoch": 1_000,
            "timeout_hours": 4,
            "instances_per_fuzzer": 1,
            "fuzzers": ["foundry"],
        }
        args = mock.Mock(
            bucket="shared-bucket",
            now_epoch=2_000,
            requested_run_id="gh-100-1",
            force_run_id="gh-100-1",
        )
        active = [
            {
                "InstanceId": "i-active",
                "Tags": [
                    {"Key": "Project", "Value": "scfuzzbench"},
                    {"Key": "RunId", "Value": "gh-100-1"},
                ],
            }
        ]
        with (
            mock.patch.object(self.module, "list_s3_keys") as list_keys,
            mock.patch.object(
                self.module, "active_benchmark_instances", return_value=active
            ),
            mock.patch.object(
                self.module, "_s3_read_json", return_value=metadata
            ),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(0, self.module.cmd_discover_cleanup(args))

        list_keys.assert_not_called()
        matrix = json.loads(output.call_args.args[0])
        self.assertEqual(
            [
                {
                    "benchmark_uuid": "b" * 32,
                    "reason": "forced-orphan-cleanup",
                    "run_id": "gh-100-1",
                    "scfuzzbench_commit": commit,
                    "status": "running",
                    "terraform_backend_key": "runs/gh-100-1/terraform.tfstate",
                }
            ],
            matrix["include"],
        )

    def test_forced_cleanup_run_ids_must_match_and_never_fall_back_global(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.module.cmd_discover_cleanup(
                mock.Mock(
                    bucket="shared-bucket",
                    now_epoch=2_000,
                    requested_run_id="gh-100-1",
                    force_run_id="gh-101-1",
                )
            )
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.module.cmd_discover_cleanup(
                mock.Mock(
                    bucket="shared-bucket",
                    now_epoch=2_000,
                    requested_run_id="",
                    force_run_id="../all",
                )
            )

    def test_malformed_forced_reservation_fails_loudly(self):
        args = mock.Mock(
            bucket="shared-bucket",
            now_epoch=2_000,
            requested_run_id="gh-100-1",
            force_run_id="gh-100-1",
        )
        with (
            mock.patch.object(
                self.module, "active_benchmark_instances", return_value=[]
            ),
            mock.patch.object(
                self.module,
                "_s3_read_json",
                side_effect=ValueError("malformed reservation"),
            ),
            self.assertRaisesRegex(ValueError, "malformed reservation"),
        ):
            self.module.cmd_discover_cleanup(args)

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

    def test_bootstrap_source_guard_is_allowed_in_every_plan_mode(self):
        self.assertIn("terraform_data", self.module.ALLOWED_RUN_RESOURCE_TYPES)
        self.assertNotIn("terraform_data", self.module.TAGGABLE_RUN_RESOURCE_TYPES)
        for mode, actions, before, after in (
            ("apply", ["create"], None, {"id": None}),
            ("cleanup", ["delete"], {"id": "guard-id"}, None),
            (
                "inspect",
                ["no-op"],
                {"id": "guard-id"},
                {"id": "guard-id"},
            ),
        ):
            with self.subTest(mode=mode):
                plan = {
                    "resource_changes": [
                        {
                            "address": "terraform_data.bootstrap_source_guard",
                            "mode": "managed",
                            "type": "terraform_data",
                            "change": {
                                "actions": actions,
                                "before": before,
                                "after": after,
                            },
                        }
                    ]
                }
                self.assertEqual(
                    [],
                    self.module.validate_terraform_plan(
                        plan, mode, "gh-run-b-1"
                    ),
                )

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
        for protected in (
            "SCFUZZBENCH_BENCHMARK_TYPE",
            "SCFUZZBENCH_PROPERTIES_PATH",
            *PROTECTED_RUNTIME_ENV_KEYS,
            "AWS_ENDPOINT_URL",
        ):
            with self.subTest(protected=protected):
                with self.assertRaisesRegex(ValueError, "may not override"):
                    self.module.validate_benchmark_inputs(
                        {
                            **values,
                            "FUZZER_ENV_JSON": f'{{"{protected}":"foreign"}}',
                        }
                    )

    def test_framework_owned_runtime_env_is_protected_at_every_boundary(self):
        root = Path(__file__).resolve().parents[2]
        request = (root / ".github/workflows/benchmark-request.yml").read_text()
        benchmark = (root / ".github/workflows/benchmark-run.yml").read_text()
        terraform = (root / "infrastructure/variables.tf").read_text()

        for key in PROTECTED_RUNTIME_ENV_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, self.module.IMMUTABLE_FUZZER_ENV_KEYS)
                self.assertIn(f'"{key}"', request)
                self.assertIn(f'"{key}"', terraform)
        self.assertIn(
            "from scripts.benchmark_run_state import "
            "validate_fuzzer_env_map",
            benchmark,
        )
        self.assertIn("SAFE_SCFUZZBENCH_FUZZER_ENV_KEYS", request)
        self.assertIn('k.startsWith("AWS_")', request)
        self.assertIn('key.startswith("AWS_")', Path(
            self.module.__file__
        ).read_text())
        self.assertIn('!startswith(key, "AWS_")', terraform)
        self.assertIn('!startswith(key, "SCFUZZBENCH_")', terraform)

    def test_generic_framework_tunings_are_allowlisted_and_bounded(self):
        base = {
            "TARGET_REPO_URL": "https://github.com/example/target",
            "TARGET_COMMIT": "a" * 40,
            "BENCHMARK_TYPE": "property",
            "INSTANCE_TYPE": "c6a.4xlarge",
            "INSTANCES_PER_FUZZER": "1",
            "TIMEOUT_HOURS": "4",
            "FUZZERS_JSON": '["foundry"]',
        }
        safe = {
            "SCFUZZBENCH_WORKERS": "256",
            "SCFUZZBENCH_RUNNER_METRICS": "true",
            "SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS": "300",
            "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS": "1",
            "SCFUZZBENCH_FOUNDRY_SHOWMAP": "no",
            "SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS": "3600",
        }
        self.module.validate_benchmark_inputs(
            {**base, "FUZZER_ENV_JSON": json.dumps(safe)}
        )
        for key, value in {
            "SCFUZZBENCH_UNKNOWN_FUTURE_KEY": "1",
            "SCFUZZBENCH_WORKERS": "257",
            "SCFUZZBENCH_RUNNER_METRICS": "sometimes",
            "SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS": "0",
            "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS": "true",
            "SCFUZZBENCH_FOUNDRY_SHOWMAP": "",
            "SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS": "3601",
        }.items():
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    self.module.validate_benchmark_inputs(
                        {
                            **base,
                            "FUZZER_ENV_JSON": json.dumps({key: value}),
                        }
                    )

    def test_generic_env_grammar_and_user_data_encoding_are_one_contract(self):
        root = Path(__file__).resolve().parents[2]
        request = (root / ".github/workflows/benchmark-request.yml").read_text()
        benchmark = (root / ".github/workflows/benchmark-run.yml").read_text()
        terraform = (root / "infrastructure/variables.tf").read_text()
        infrastructure = (root / "infrastructure/main.tf").read_text()
        user_data = (root / "infrastructure/user_data.sh.tftpl").read_text()

        for invalid_key in ("bad-key", "SAFE\nexport SCFUZZBENCH_RUN_ID"):
            with self.subTest(invalid_key=invalid_key):
                with self.assertRaisesRegex(ValueError, "environment key"):
                    self.module.validate_fuzzer_env_entry(invalid_key, "value")
        for invalid_value in (
            'safe"; export SCFUZZBENCH_RUN_ID="foreign',
            "$(touch should-not-run)",
            "line one\nline two",
            "line one\rline two",
            "`touch should-not-run`",
            r"escaped\value",
            "x" * 2001,
        ):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(ValueError, "environment value"):
                    self.module.validate_fuzzer_env_entry(
                        "CUSTOM_SETTING", invalid_value
                    )

        base = {
            "TARGET_REPO_URL": "https://github.com/example/target",
            "TARGET_COMMIT": "a" * 40,
            "BENCHMARK_TYPE": "property",
            "INSTANCE_TYPE": "c6a.4xlarge",
            "INSTANCES_PER_FUZZER": "1",
            "TIMEOUT_HOURS": "4",
            "FUZZERS_JSON": '["foundry"]',
        }
        too_many = {f"CUSTOM_{index}": "x" for index in range(65)}
        with self.assertRaisesRegex(ValueError, "at most 64 entries"):
            self.module.validate_benchmark_inputs(
                {**base, "FUZZER_ENV_JSON": json.dumps(too_many)}
            )

        for boundary in (request, terraform):
            self.assertIn("at most 64 entries", boundary)
        self.assertIn(
            "from scripts.benchmark_run_state import "
            "validate_fuzzer_env_map",
            benchmark,
        )
        self.assertIn("validate_fuzzer_env_map(obj)", benchmark)
        self.assertIn('regex("^[A-Z][A-Z0-9_]{0,63}$"', terraform)
        self.assertIn("length(value) <= 2000", terraform)
        self.assertIn("cannot contain CR, LF, double quotes", terraform)
        self.assertIn("fuzzer_env_b64", infrastructure)
        self.assertIn(
            "for key, value in local.merged_fuzzer_env : key => base64encode(value)",
            infrastructure,
        )
        self.assertIn("base64encode(var.target_repo_url)", infrastructure)
        self.assertIn(
            '"fuzzers/foundry/throughput-progress.patch"',
            infrastructure,
        )
        self.assertIn(
            'filesha256("${path.module}/../${source}")',
            infrastructure,
        )
        self.assertIn("decode_b64_env SCFUZZBENCH_REPO_URL", user_data)
        self.assertNotIn('export ${key}="${value}"', user_data)

        interpolations = re.findall(r"(?<!\$)\$\{([^}]+)\}", user_data)
        self.assertTrue(interpolations)
        for interpolation in interpolations:
            with self.subTest(interpolation=interpolation):
                self.assertTrue(
                    interpolation.endswith("_b64") or interpolation == "key",
                    f"raw user-data template scalar: {interpolation}",
                )

        self.assertIn(
            'can(regex("^[a-z0-9][a-z0-9-]{0,63}$", fuzzer.key))',
            terraform,
        )
        for fuzzer in ("echidna", "foundry", "medusa", "recon-fuzzer"):
            self.assertIn(f'"fuzzers/{fuzzer}/install.sh"', infrastructure)
            self.assertIn(f'"fuzzers/{fuzzer}/run.sh"', infrastructure)

    def test_corpus_overrides_must_be_safe_repo_relative_paths(self):
        root = Path(__file__).resolve().parents[2]
        request = (root / ".github/workflows/benchmark-request.yml").read_text()
        benchmark = (root / ".github/workflows/benchmark-run.yml").read_text()
        terraform = (root / "infrastructure/variables.tf").read_text()
        base = {
            "TARGET_REPO_URL": "https://github.com/example/target",
            "TARGET_COMMIT": "a" * 40,
            "BENCHMARK_TYPE": "property",
            "INSTANCE_TYPE": "c6a.4xlarge",
            "INSTANCES_PER_FUZZER": "1",
            "TIMEOUT_HOURS": "4",
            "FUZZERS_JSON": '["echidna","foundry","medusa","recon-fuzzer"]',
        }
        for key in self.module.CORPUS_OVERRIDE_ENV_KEYS:
            self.assertIn(f'"{key}"', request)
            self.assertIn(f'"{key}"', terraform)
            with self.subTest(key=key, valid=True):
                self.module.validate_benchmark_inputs(
                    {
                        **base,
                        "FUZZER_ENV_JSON": f'{{"{key}":"corpus/custom-{key.lower()}"}}',
                    }
                )
            for unsafe in ("/etc/ssh", "../outside", "corpus/../../outside", "bad path"):
                with self.subTest(key=key, unsafe=unsafe):
                    with self.assertRaisesRegex(ValueError, "repo-relative"):
                        self.module.validate_benchmark_inputs(
                            {
                                **base,
                                "FUZZER_ENV_JSON": json.dumps({key: unsafe}),
                            }
                        )
        self.assertIn("validate_fuzzer_env_map(obj)", benchmark)

    def test_docs_match_opaque_run_and_protected_env_contracts(self):
        root = Path(__file__).resolve().parents[2]
        operations = (root / "docs/operations.md").read_text()
        methodology = (root / "docs/methodology.md").read_text()
        fuzzer_docs = (root / "fuzzers/README.md").read_text()

        self.assertIn("`properties_path`", operations)
        self.assertNotIn(
            "fuzzer_env` values such as `SCFUZZBENCH_PROPERTIES_PATH`",
            operations,
        )
        self.assertIn(
            "run_started_at_epoch + (timeout_hours * 3600) + 3600",
            methodology,
        )
        self.assertNotIn("now >= run_id +", methodology)
        self.assertNotIn("timestamp-first index", methodology)
        self.assertIn(
            'prefix "corpus/$RUN_ID/$BENCHMARK_UUID/"',
            operations,
        )
        self.assertIn("safe allowlist", fuzzer_docs)
        self.assertIn("repo-relative", fuzzer_docs)

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
        self.assertEqual(3600, normalized["preliminary_interval_seconds"])

    def test_cloud_input_validation_covers_new_tool_seed_and_checkpoint_inputs(self):
        values = {
            "TARGET_REPO_URL": "https://github.com/example/target",
            "TARGET_COMMIT": "a" * 40,
            "BENCHMARK_TYPE": "property",
            "INSTANCE_TYPE": "c6a.4xlarge",
            "INSTANCES_PER_FUZZER": "2",
            "TIMEOUT_HOURS": "4",
            "PRELIMINARY_INTERVAL_MINUTES": "60",
            "FUZZERS_JSON": '["echidna","medusa"]',
            "ECHIDNA_CI_REPO": "https://github.com/crytic/echidna",
            "ECHIDNA_CI_RUN_ID": "123",
            "ECHIDNA_CI_ARTIFACT_NAME": "echidna-linux",
            "ECHIDNA_CI_ARTIFACT_SHA256": "b" * 64,
            "ECHIDNA_CI_COMMIT": "c" * 40,
            "ECHIDNA_CI_TOKEN_SSM_PARAMETER_NAME": "/scfuzzbench/echidna",
            "MEDUSA_GIT_REPO": "https://github.com/crytic/medusa",
            "MEDUSA_GIT_REF": "master",
            "MEDUSA_GIT_COMMIT": "d" * 40,
            "MEDUSA_GO_VERSION": "1.24.0",
            "MEDUSA_GO_SHA256": "e" * 64,
            "SHARED_SEED_CORPUS_SOURCE": "s3://seed-bucket/corpus/v1",
        }
        normalized = self.module.validate_benchmark_inputs(values)
        self.assertEqual(3600, normalized["preliminary_interval_seconds"])

        with self.assertRaisesRegex(ValueError, "whole seconds"):
            self.module.validate_benchmark_inputs(
                {**values, "PRELIMINARY_INTERVAL_MINUTES": "1.001"}
            )
        with self.assertRaisesRegex(ValueError, "dot path segments"):
            self.module.validate_benchmark_inputs(
                {**values, "SHARED_SEED_CORPUS_SOURCE": "seed/../foreign"}
            )
        with self.assertRaisesRegex(ValueError, "may not override"):
            self.module.validate_benchmark_inputs(
                {
                    **values,
                    "FUZZER_ENV_JSON": '{"MEDUSA_GIT_REPO":"foreign"}',
                }
            )

    def test_recovery_inputs_require_exact_identity_and_shared_bucket(self):
        commit = "a" * 40
        tfvars = {
            "run_id": "gh-123-1",
            "terraform_backend_key": "runs/gh-123-1/terraform.tfstate",
            "existing_bucket_name": "shared-bucket",
            "scfuzzbench_commit": commit,
        }
        metadata = {
            "run_id": "gh-123-1",
            "terraform_backend_key": "runs/gh-123-1/terraform.tfstate",
            "existing_bucket_name": "shared-bucket",
            "scfuzzbench_commit": commit,
            "status": "reserved",
        }
        self.assertEqual(
            commit,
            self.module.validate_recovery_inputs(
                tfvars,
                metadata,
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                bucket="shared-bucket",
                expected_commit=commit,
            ),
        )
        foreign = dict(metadata, run_id="gh-foreign-1")
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.module.validate_recovery_inputs(
                tfvars,
                foreign,
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                bucket="shared-bucket",
            )
        with self.assertRaisesRegex(ValueError, "scfuzzbench_commit"):
            self.module.validate_recovery_inputs(
                tfvars,
                dict(metadata, scfuzzbench_commit="b" * 40),
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                bucket="shared-bucket",
            )
        with self.assertRaisesRegex(ValueError, "lowercase 40-character"):
            self.module.validate_recovery_inputs(
                dict(tfvars, scfuzzbench_commit="A" * 40),
                metadata,
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                bucket="shared-bucket",
            )
        with self.assertRaisesRegex(ValueError, "status"):
            self.module.validate_recovery_inputs(
                tfvars,
                metadata,
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                bucket="shared-bucket",
                expected_status="running",
            )

    def test_provisioning_commit_must_be_current_main_or_ancestor(self):
        old = "a" * 40
        current = "b" * 40
        ahead = {
            "status": "ahead",
            "base_commit": {"sha": old},
            "merge_base_commit": {"sha": old},
            "head_commit": {"sha": current},
        }
        self.module.validate_provisioning_commit(old, current, ahead)
        self.module.validate_provisioning_commit(
            current,
            current,
            {
                "status": "identical",
                "base_commit": {"sha": current},
                "merge_base_commit": {"sha": current},
                "head_commit": {"sha": current},
            },
        )
        for comparison in (
            {**ahead, "status": "diverged"},
            {**ahead, "status": "behind"},
            {**ahead, "merge_base_commit": {"sha": "c" * 40}},
            {**ahead, "base_commit": {"sha": "c" * 40}},
            {**ahead, "head_commit": {"sha": "c" * 40}},
        ):
            with self.subTest(comparison=comparison):
                with self.assertRaisesRegex(ValueError, "commit|ancestor|base"):
                    self.module.validate_provisioning_commit(
                        old, current, comparison
                    )
        with self.assertRaisesRegex(ValueError, "lowercase 40-character"):
            self.module.validate_provisioning_commit(
                "main", current, ahead
            )

    def test_failure_finalization_is_idempotent_only_when_reservation_absent(self):
        args = mock.Mock(
            bucket="shared-bucket",
            run_id="gh-123-1",
            workflow_url="https://github.com/example/repo/actions/runs/1",
            output="metadata.json",
        )
        with (
            mock.patch.object(self.module, "list_s3_keys", return_value=[]),
            mock.patch.object(self.module, "cmd_mark_failed") as mark_failed,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(
                0, self.module.cmd_mark_failed_if_reserved(args)
            )
            mark_failed.assert_not_called()

        active_key = "run-state/admissions/active/gh-123-1.json"
        with (
            mock.patch.object(
                self.module, "list_s3_keys", return_value=[active_key]
            ),
            mock.patch.object(
                self.module, "cmd_mark_failed", return_value=0
            ) as mark_failed,
        ):
            self.assertEqual(
                0, self.module.cmd_mark_failed_if_reserved(args)
            )
            mark_failed.assert_called_once_with(args)

        with (
            mock.patch.object(
                self.module,
                "list_s3_keys",
                side_effect=RuntimeError("AWS unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "AWS unavailable"),
        ):
            self.module.cmd_mark_failed_if_reserved(args)

    def test_active_reservation_recheck_requires_exact_key_and_surfaces_errors(self):
        active_key = "run-state/admissions/active/gh-123-1.json"
        with mock.patch.object(
            self.module,
            "list_s3_keys",
            return_value=[f"{active_key}.stale"],
        ):
            self.assertFalse(
                self.module.active_reservation_exists(
                    "shared-bucket", "gh-123-1"
                )
            )
        with mock.patch.object(
            self.module, "list_s3_keys", return_value=[active_key]
        ):
            self.assertTrue(
                self.module.active_reservation_exists(
                    "shared-bucket", "gh-123-1"
                )
            )
        with (
            mock.patch.object(
                self.module,
                "list_s3_keys",
                side_effect=RuntimeError("AWS unavailable"),
            ),
            self.assertRaisesRegex(RuntimeError, "AWS unavailable"),
        ):
            self.module.active_reservation_exists(
                "shared-bucket", "gh-123-1"
            )

    def test_failure_finalization_updates_exact_valid_reservation(self):
        metadata = {
            "run_id": "gh-123-1",
            "terraform_backend_key": "runs/gh-123-1/terraform.tfstate",
            "existing_bucket_name": "shared-bucket",
            "scfuzzbench_commit": "a" * 40,
            "status": "reserved",
        }
        args = mock.Mock(
            bucket="shared-bucket",
            run_id="gh-123-1",
            workflow_url="https://github.com/example/repo/actions/runs/1",
            output="metadata.json",
        )
        active_key = "run-state/admissions/active/gh-123-1.json"
        with (
            mock.patch.object(
                self.module, "list_s3_keys", return_value=[active_key]
            ),
            mock.patch.object(
                self.module, "_s3_read_json", return_value=metadata
            ),
            mock.patch.object(self.module, "_write_json") as write_json,
            mock.patch.object(self.module, "_s3_write_file"),
            mock.patch.object(self.module.time, "time", return_value=10_000),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(
                0, self.module.cmd_mark_failed_if_reserved(args)
            )
        updated = write_json.call_args.args[1]
        self.assertEqual("provisioning-failed", updated["status"])
        self.assertEqual(10_900, updated["orphan_after_epoch"])
        self.assertEqual(args.workflow_url, updated["failure_workflow_url"])

    def test_release_identity_and_budget_grammars_reject_injection(self):
        self.assertEqual(
            "gh-123-1", self.module.validate_run_id("gh-123-1")
        )
        self.assertEqual(
            "a" * 32, self.module.validate_benchmark_uuid("a" * 32)
        )
        self.assertEqual(
            4.0, self.module.validate_benchmark_hours("4", "report budget")
        )
        self.assertEqual("", self.module.normalize_excluded_fuzzers(""))
        self.assertEqual(
            "echidna,foundry",
            self.module.normalize_excluded_fuzzers("foundry,echidna"),
        )
        self.assertEqual(
            "recon-fuzzer",
            self.module.normalize_excluded_fuzzers("recon-fuzzer"),
        )
        for run_id in ("$(touch pwned)", "bad\nrun", "bad/run", "a" * 81):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    self.module.validate_run_id(run_id)
        for benchmark_uuid in (
            "A" * 32,
            "a" * 31,
            "g" * 32,
            "a\n" + "b" * 30,
        ):
            with self.subTest(benchmark_uuid=benchmark_uuid):
                with self.assertRaises(ValueError):
                    self.module.validate_benchmark_uuid(benchmark_uuid)
        for budget in ("nan", "inf", "0.1", "73", "4\nx=y"):
            with self.subTest(budget=budget):
                with self.assertRaises(ValueError):
                    self.module.validate_benchmark_hours(
                        budget, "report budget"
                    )
        for exclusions in (
            "foundry;touch-pwned",
            "$(touch pwned)",
            "${MAKE_INJECTION}",
            "foundry, medusa",
            "foundry\tmedusa",
            "foundry\nmedusa",
            " foundry",
            "--foundry",
            "unknown",
            "foundry,foundry",
            ",foundry",
            "foundry,",
        ):
            with self.subTest(exclusions=exclusions):
                with self.assertRaises(ValueError):
                    self.module.normalize_excluded_fuzzers(exclusions)

    def test_recovery_metadata_bucket_and_expected_commit_are_mandatory(self):
        commit = "a" * 40
        base = {
            "run_id": "gh-123-1",
            "terraform_backend_key": "runs/gh-123-1/terraform.tfstate",
            "existing_bucket_name": "shared-bucket",
            "scfuzzbench_commit": commit,
            "status": "reserved",
        }
        with self.assertRaisesRegex(ValueError, "artifact bucket"):
            self.module.validate_recovery_inputs(
                base,
                {key: value for key, value in base.items() if key != "existing_bucket_name"},
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                bucket="shared-bucket",
            )
        with self.assertRaisesRegex(ValueError, "expected"):
            self.module.validate_recovery_inputs(
                base,
                base,
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                bucket="shared-bucket",
                expected_commit="b" * 40,
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

    def test_partial_apply_recovery_allows_missing_but_never_foreign_outputs(self):
        self.module.validate_state_outputs(
            {},
            run_id="gh-123-1",
            backend_key="runs/gh-123-1/terraform.tfstate",
            allow_missing=True,
        )
        self.module.validate_state_outputs(
            {
                "terraform_backend_key": {
                    "value": "runs/gh-123-1/terraform.tfstate"
                }
            },
            run_id="gh-123-1",
            backend_key="runs/gh-123-1/terraform.tfstate",
            allow_missing=True,
        )
        with self.assertRaisesRegex(ValueError, "backend-key"):
            self.module.validate_state_outputs(
                {
                    "terraform_backend_key": {
                        "value": "runs/gh-foreign-1/terraform.tfstate"
                    }
                },
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                allow_missing=True,
            )
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.module.validate_state_outputs(
                {"run_id": {"value": "gh-foreign-1"}},
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                allow_missing=True,
            )
        with self.assertRaisesRegex(ValueError, "benchmark_uuid"):
            self.module.validate_state_outputs(
                {"benchmark_uuid": {"value": "not-a-uuid"}},
                run_id="gh-123-1",
                backend_key="runs/gh-123-1/terraform.tfstate",
                allow_missing=True,
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
        self.assertLess(
            benchmark.index("Normalize bleeding-edge tool inputs before admission"),
            benchmark.index("Atomically reserve repository run capacity"),
        )
        self.assertLess(
            benchmark.index("Preflight bleeding-edge tool pins before admission"),
            benchmark.index("Atomically reserve repository run capacity"),
        )
        self.assertNotIn("inputs.foundry_version", benchmark)
        self.assertNotIn("run_id=\"${run_started_at_epoch}\"", benchmark)
        for expected in (
            '"preliminary_interval_seconds": preliminary_interval_seconds',
            '"shared_seed_corpus_source": os.environ["SHARED_SEED_CORPUS_SOURCE"]',
            '"ECHIDNA_CI_REPO": "echidna_ci_repo"',
            '"MEDUSA_GIT_REPO": "medusa_git_repo"',
            '"PROPERTIES_PATH": "properties_path"',
            'reservation["nonsecret_optional_inputs"]',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, benchmark)
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
            "preliminary/${local.run_id}/${local.benchmark_uuid}/snapshots/*",
            terraform,
        )
        self.assertIn(
            "run-state/heartbeats/${local.run_id}/${local.benchmark_uuid}/*",
            terraform,
        )
        self.assertNotIn(
            "preliminary/${local.run_id}/${local.benchmark_uuid}/*",
            terraform,
        )
        self.assertIn(
            'run_name_token = substr(replace(lower(tostring(local.run_id)), '
            '"/[^a-z0-9-]/", "-"), 0, 18)',
            terraform,
        )
        self.assertRegex(
            terraform,
            r"properties_path\s+= var\.properties_path",
        )
        self.assertEqual(
            1,
            (
                Path(__file__).resolve().parents[2]
                / "infrastructure"
                / "variables.tf"
            )
            .read_text()
            .count('variable "run_started_at_epoch"'),
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
