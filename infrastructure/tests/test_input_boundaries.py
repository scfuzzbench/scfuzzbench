#!/usr/bin/env python3
from __future__ import annotations

import base64
import gzip
import json
import os
import random
import re
import shutil
import string
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INFRASTRUCTURE = REPO_ROOT / "infrastructure"
TERRAFORM = os.environ.get("TERRAFORM_BIN", "terraform")
EC2_USER_DATA_LIMIT = 16_384
FUZZER_ENV_MAX_UTF8_BYTES = 4096


def run_terraform(
    directory: Path,
    *args: str,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [TERRAFORM, f"-chdir={directory}", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def incompressible_fuzzer_env() -> dict[str, str]:
    rng = random.Random(205)
    alphabet = string.ascii_letters + string.digits + "._+-/:=@%!?[]{}()"

    def value(length: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(length))

    result = {
        "CUSTOM_A": value(2000),
        "CUSTOM_B": value(2000),
        "CUSTOM_C": value(72),
    }
    assert (
        sum(len(key.encode()) + len(item.encode()) for key, item in result.items())
        == FUZZER_ENV_MAX_UTF8_BYTES
    )
    return result


@unittest.skipUnless(shutil.which(TERRAFORM), "terraform is not installed")
class TerraformInputBoundaryTests(unittest.TestCase):
    def test_benchmark_manifest_output_declassifies_only_public_metadata(self):
        outputs = (INFRASTRUCTURE / "outputs.tf").read_text(encoding="utf-8")
        match = re.search(
            r'^output "benchmark_manifest" \{.*?^\}',
            outputs,
            flags=re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match)
        output_block = match.group(0)
        self.assertIn(
            "value       = local.benchmark_manifest",
            output_block,
        )
        self.assertNotIn(
            "nonsensitive(local.benchmark_manifest)",
            output_block,
        )
        self.assertNotRegex(
            output_block,
            r"(?m)^\s*sensitive\s*=\s*true\s*$",
        )

        main = (INFRASTRUCTURE / "main.tf").read_text(encoding="utf-8")
        ami_local = re.search(
            r"^  ubuntu_ami_id\s+= (.+)$",
            main,
            flags=re.MULTILINE,
        )
        self.assertIsNotNone(ami_local)
        self.assertEqual(
            "nonsensitive(data.aws_ssm_parameter.ubuntu_ami.value)",
            ami_local.group(1),
        )
        manifest_locals = main.split(
            "  benchmark_definition = merge({", 1
        )[1].split("\n\n  # Keep names", 1)[0]

        # Keep this an explicit public-metadata allowlist: adding a field to the
        # declassified output must require a corresponding security review here.
        self.assertEqual(
            {
                "artifact_prefix",
                "aws_region",
                "benchmark_type",
                "bootstrap_installer_sha256",
                "bootstrap_manifest_sha256",
                "echidna_ci_artifact",
                "echidna_ci_commit",
                "echidna_ci_repo",
                "echidna_ci_run_id",
                "echidna_ci_sha256",
                "echidna_ci_token_kms_key_arn",
                "echidna_version",
                "foundry_git_ref",
                "foundry_git_repo",
                "foundry_source_patch",
                "foundry_version",
                "fuzzer_keys",
                "instance_type",
                "instances_per_fuzzer",
                "medusa_git_commit",
                "medusa_git_ref",
                "medusa_git_repo",
                "medusa_go_sha256",
                "medusa_go_version",
                "medusa_version",
                "preliminary_interval_seconds",
                "properties_path",
                "recon_version",
                "run_id",
                "run_started_at_epoch",
                "run_state_metadata_key",
                "scfuzzbench_commit",
                "scfuzzbench_repository",
                "seed_corpus",
                "target_commit",
                "target_repo_url",
                "terraform_backend_key",
                "timeout_hours",
                "ubuntu_ami_id",
                "user_data_template_sha256",
            },
            set(
                re.findall(
                    r"^    ([a-z][a-z0-9_]*)\s+=",
                    manifest_locals,
                    flags=re.MULTILINE,
                )
            ),
        )
        self.assertEqual(
            set(),
            set(
                re.findall(
                    r"data\.aws_ssm_parameter\.[A-Za-z0-9_]+\.value",
                    manifest_locals,
                )
            ),
        )
        variables = (INFRASTRUCTURE / "variables.tf").read_text(encoding="utf-8")
        self.assertIn(
            'default     = "/aws/service/canonical/ubuntu/server/24.04/'
            'stable/current/amd64/hvm/ebs-gp3/ami-id"',
            variables,
        )
        benchmark_workflow = (
            REPO_ROOT / ".github" / "workflows" / "benchmark-run.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ubuntu_ami_ssm_parameter", benchmark_workflow)
        ami_field = re.search(
            r"^    ubuntu_ami_id\s+= (.+)$",
            manifest_locals,
            flags=re.MULTILINE,
        )
        self.assertIsNotNone(ami_field)
        self.assertEqual(
            "local.ubuntu_ami_id",
            ami_field.group(1),
        )
        ami_data = main.split(
            'data "aws_ssm_parameter" "ubuntu_ami" {', 1
        )[1].split('\ndata "aws_caller_identity"', 1)[0]
        self.assertIn("with_decryption = false", ami_data)
        self.assertIn(
            'can(regex("^ami-[0-9a-f]{8}([0-9a-f]{9})?$", '
            "nonsensitive(self.value)))",
            ami_data,
        )
        self.assertEqual(2, main.count("local.ubuntu_ami_id"))
        for secret_source in (
            "var.echidna_ci_token_ssm_parameter_name",
            "var.git_token_ssm_parameter_name",
            "local_sensitive_file.",
            "tls_private_key.",
        ):
            with self.subTest(secret_source=secret_source):
                self.assertNotIn(secret_source, manifest_locals)

        # Exercise Terraform's plan-time nested sensitivity propagation using
        # the production AMI field and output blocks. Without the field-level
        # nonsensitive(...), plan fails with "Output refers to sensitive
        # values" before any resource is created. Declassifying the whole
        # object is both broader and invalid in Terraform 1.5 because only the
        # nested AMI attribute is marked sensitive.
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            (fixture / "main.tf").write_text(
                """
variable "provider_sensitive_ami" {
  type      = string
  sensitive = true
}

locals {
  ubuntu_ami_id = """
                + ami_local.group(1).replace(
                    "data.aws_ssm_parameter.ubuntu_ami.value",
                    "var.provider_sensitive_ami",
                )
                + """
  benchmark_definition = merge({
    benchmark_type = "property"
    ubuntu_ami_id  = """
                + ami_field.group(1)
                + """
  }, {})
  benchmark_manifest = merge(local.benchmark_definition, {
    run_id = "sensitivity-regression"
  })
}

"""
                + output_block
                + "\n",
                encoding="utf-8",
            )
            plan_path = fixture / "run.tfplan"
            run_terraform(
                fixture,
                "init",
                "-backend=false",
                "-input=false",
                check=True,
            )
            plan = run_terraform(
                fixture,
                "plan",
                "-input=false",
                "-lock=false",
                "-refresh=false",
                f"-out={plan_path}",
                "-var=provider_sensitive_ami=ami-0123456789abcdef0",
            )
            self.assertEqual(0, plan.returncode, plan.stderr)

            plan_json = json.loads(
                run_terraform(
                    fixture,
                    "show",
                    "-json",
                    plan_path,
                    check=True,
                ).stdout
            )
            planned_output = plan_json["planned_values"]["outputs"][
                "benchmark_manifest"
            ]
            self.assertFalse(planned_output["sensitive"])
            self.assertEqual(
                {
                    "benchmark_type": "property",
                    "run_id": "sensitivity-regression",
                    "ubuntu_ami_id": "ami-0123456789abcdef0",
                },
                planned_output["value"],
            )

    def test_direct_fuzzer_env_and_custom_fuzzer_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            shutil.copy2(INFRASTRUCTURE / "variables.tf", fixture / "variables.tf")
            run_terraform(fixture, "init", "-backend=false", "-input=false", check=True)

            def plan(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
                tfvars = fixture / "case.tfvars.json"
                tfvars.write_text(json.dumps(payload), encoding="utf-8")
                return run_terraform(
                    fixture,
                    "plan",
                    "-input=false",
                    "-lock=false",
                    "-refresh=false",
                    f"-var-file={tfvars}",
                )

            valid = plan({"fuzzer_env": incompressible_fuzzer_env()})
            self.assertEqual(0, valid.returncode, valid.stderr)

            over_budget = incompressible_fuzzer_env()
            over_budget["CUSTOM_C"] += "x"
            invalid_maps = [
                {"BAD-KEY": "value"},
                {"SAFE\nexport SCFUZZBENCH_RUN_ID": "value"},
                {"CUSTOM_SETTING": 'safe"; export SCFUZZBENCH_RUN_ID="foreign'},
                {"CUSTOM_SETTING": "$(touch should-not-run)"},
                {"CUSTOM_SETTING": "line one\nline two"},
                {"CUSTOM_SETTING": "line one\rline two"},
                {"CUSTOM_SETTING": "`touch should-not-run`"},
                {"CUSTOM_SETTING": r"escaped\value"},
                {"CUSTOM_SETTING": "x" * 2001},
                {f"CUSTOM_{index}": "x" for index in range(65)},
                over_budget,
                {"AWS_REGION": "us-west-2"},
                {"SCFUZZBENCH_ROOT": "elsewhere"},
                {"FOUNDRY_GIT_REPO": "https://example.invalid/foundry"},
                {"SCFUZZBENCH_WORKERS": "0"},
                {"SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS": "301"},
                {"ECHIDNA_CORPUS_DIR": "../escape"},
                {"MEDUSA_CORPUS_DIR": "/absolute"},
            ]
            for invalid in invalid_maps:
                with self.subTest(invalid=invalid):
                    result = plan({"fuzzer_env": invalid})
                    self.assertNotEqual(0, result.returncode, result.stdout)

            custom = plan(
                {
                    "custom_fuzzer_definitions": [
                        {
                            "key": "custom",
                            "install_path": "fuzzers/custom/install.sh",
                            "run_path": "fuzzers/custom/run.sh",
                        }
                    ]
                }
            )
            self.assertNotEqual(0, custom.returncode)
            self.assertIn("local-only", custom.stderr)

            invalid_repository = plan(
                {"scfuzzbench_repository": "https://github.com/example/fork"}
            )
            self.assertNotEqual(0, invalid_repository.returncode)
            self.assertIn(
                "https://github.com/scfuzzbench/scfuzzbench",
                invalid_repository.stderr,
            )

            uppercase_commit = plan({"scfuzzbench_commit": "A" * 40})
            self.assertNotEqual(0, uppercase_commit.returncode)
            self.assertIn("lowercase", uppercase_commit.stderr)

    def test_all_modes_fit_ec2_limit_and_every_scalar_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            shutil.copy2(
                INFRASTRUCTURE / "tests" / "user_data_render_fixture.tf",
                fixture / "main.tf",
            )
            marker = fixture / "injected-command-ran"
            malicious = (
                f'https://example.invalid/$(touch "{marker}")/"quoted"\n'
                "second line\n"
            )
            tfvars = fixture / "render.tfvars.json"
            tfvars.write_text(
                json.dumps(
                    {
                        "template_path": str(
                            INFRASTRUCTURE / "user_data.sh.tftpl"
                        ),
                        "repository_root": str(REPO_ROOT),
                        "malicious_value": malicious,
                        "fuzzer_env": incompressible_fuzzer_env(),
                    }
                ),
                encoding="utf-8",
            )

            run_terraform(fixture, "init", "-backend=false", "-input=false", check=True)
            run_terraform(
                fixture,
                "apply",
                "-auto-approve",
                "-input=false",
                f"-var-file={tfvars}",
                check=True,
            )
            rendered_by_mode = json.loads(
                run_terraform(
                    fixture, "output", "-json", "rendered", check=True
                ).stdout
            )
            encoded_by_mode = json.loads(
                run_terraform(
                    fixture, "output", "-json", "user_data_base64", check=True
                ).stdout
            )
            sizes = json.loads(
                run_terraform(
                    fixture, "output", "-json", "gzip_bytes", check=True
                ).stdout
            )
            print(
                "EC2 user-data gzip bytes: "
                + ", ".join(
                    f"{mode}={sizes[mode]}" for mode in sorted(sizes)
                )
            )

            expected_modes = {
                "echidna-stable",
                "echidna-ci",
                "medusa-stable",
                "medusa-source",
                "foundry",
                "recon",
            }
            self.assertEqual(expected_modes, set(rendered_by_mode))
            self.assertEqual(expected_modes, set(encoded_by_mode))
            self.assertEqual(expected_modes, set(sizes))
            for mode in sorted(expected_modes):
                with self.subTest(mode=mode):
                    compressed = base64.b64decode(
                        encoded_by_mode[mode].encode(), validate=True
                    )
                    self.assertEqual(sizes[mode], len(compressed))
                    self.assertLessEqual(len(compressed), EC2_USER_DATA_LIMIT)
                    self.assertEqual(
                        rendered_by_mode[mode].encode(),
                        gzip.decompress(compressed),
                    )
                    syntax = subprocess.run(
                        ["bash", "-n"],
                        input=rendered_by_mode[mode],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(0, syntax.returncode, syntax.stderr)

            rendered = rendered_by_mode["echidna-ci"]
            self.assertNotIn(malicious, rendered)
            self.assertFalse(marker.exists())

            function_start = rendered.index("decode_b64_into() {")
            call_start = rendered.index("decode_b64_into fuzzer_key")
            decoder_functions = rendered[function_start:call_start]
            calls = re.findall(
                r"^(decode_b64_(?:into|env) ([A-Z_a-z][A-Z_a-z0-9]*) '([^']*)')$",
                rendered,
                flags=re.MULTILINE,
            )
            self.assertGreater(len(calls), 30)
            for call, variable, encoded in calls:
                with self.subTest(variable=variable):
                    expected = base64.b64decode(encoded.encode(), validate=True)
                    harness = (
                        "set -euo pipefail\n"
                        f"{decoder_functions}\n"
                        f"{call}\n"
                        f"printf '%s' \"${{{variable}}}\" | base64 -w0\n"
                    )
                    decoded = subprocess.run(
                        ["bash"],
                        input=harness,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(0, decoded.returncode, decoded.stderr)
                    self.assertEqual(
                        expected,
                        base64.b64decode(decoded.stdout.encode(), validate=True),
                    )

            invalid_harness = (
                "set -u\n"
                f"{decoder_functions}\n"
                "set +e\n"
                "decode_b64_into invalid '%%%'\n"
                "printf '%s\\n' \"$?\"\n"
            )
            invalid = subprocess.run(
                ["bash"],
                input=invalid_harness,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, invalid.returncode, invalid.stderr)
            self.assertNotEqual("0", invalid.stdout.strip())
            self.assertFalse(marker.exists())

    def test_template_embeds_only_verified_bootstrap_code_and_encoded_scalars(self):
        main = (INFRASTRUCTURE / "main.tf").read_text(encoding="utf-8")
        template = (INFRASTRUCTURE / "user_data.sh.tftpl").read_text(
            encoding="utf-8"
        )
        variables = (INFRASTRUCTURE / "variables.tf").read_text(encoding="utf-8")
        render_fixture = (
            INFRASTRUCTURE / "tests" / "user_data_render_fixture.tf"
        ).read_text(encoding="utf-8")

        def destination_entries(text: str, marker: str) -> dict[str, str]:
            block = text.split(marker, 1)[1].split("\n  }", 1)[0]
            return dict(
                re.findall(
                    r'^\s*"([^"]+)"\s*=\s*"([^"]+)"',
                    block,
                    flags=re.MULTILINE,
                )
            )

        self.assertEqual(
            destination_entries(main, "bootstrap_file_destinations = {"),
            destination_entries(render_fixture, "file_destinations = {"),
        )

        for removed in (
            "shared_sh_b64",
            "seed_corpus_helper_b64",
            "install_sh_b64",
            "run_sh_b64",
            "preliminary_snapshot_py_b64",
            "echidna_ci_extractor_b64",
            "medusa_go_extractor_b64",
            "foundry_source_patch_b64",
        ):
            self.assertNotIn(removed, main)
            self.assertNotIn(removed, template)
        self.assertIn("bootstrap_file_destinations", main)
        self.assertIn('filesha256("${path.module}/../${source}")', main)
        self.assertIn("bootstrap_manifest_sha256", main)
        self.assertIn("instance_user_data_gzip_bytes", main)
        self.assertIn("<= 16384", main)
        self.assertIn("terraform_data", main)
        self.assertIn("python3 bootstrap_source_guard.py", main)
        self.assertIn("custom_fuzzer_definitions are local-only", variables)
        self.assertIn("4096 aggregate UTF-8 bytes", variables)
        self.assertIn("for bootstrap_command in base64 curl python3 sha256sum timeout", template)
        self.assertIn("raw.githubusercontent.com/scfuzzbench/scfuzzbench", template)
        self.assertIn("--proto-redir '=https'", template)
        self.assertIn("--max-filesize", template)

        interpolations = re.findall(r"(?<!\$)\$\{([^}]+)\}", template)
        self.assertTrue(interpolations)
        for interpolation in interpolations:
            with self.subTest(interpolation=interpolation):
                self.assertTrue(
                    interpolation.endswith("_b64") or interpolation == "key",
                    f"raw user-data template scalar: {interpolation}",
                )


if __name__ == "__main__":
    unittest.main()
