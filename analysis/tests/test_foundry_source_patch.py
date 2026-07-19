import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "fuzzers" / "foundry" / "throughput-progress.patch"
PREIMAGE = (
    ROOT
    / "analysis"
    / "tests"
    / "fixtures"
    / "foundry-invariant-progress-before.rs"
)
EXPECTED_PATCH_SHA256 = "2ee9e69b77c8007c78c816eb9ca791684aa5ecede0651b63f86cdd2e055eb17e"
EXPECTED_FOUNDRY_REF = "02c05d970d2801da0aef8b82486ce84b01ede36d"


class FoundrySourcePatchTests(unittest.TestCase):
    def test_patch_digest_and_pin_are_wired_into_the_installer(self):
        self.assertEqual(
            hashlib.sha256(PATCH.read_bytes()).hexdigest(),
            EXPECTED_PATCH_SHA256,
        )

        common = (ROOT / "fuzzers" / "_shared" / "common.sh").read_text(
            encoding="utf-8"
        )
        variables = (ROOT / "infrastructure" / "variables.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'throughput_patch_ref="{EXPECTED_FOUNDRY_REF}"', common)
        self.assertIn(f'default     = "{EXPECTED_FOUNDRY_REF}"', variables)
        self.assertIn(
            f'throughput_patch_sha256="{EXPECTED_PATCH_SHA256}"',
            common,
        )
        self.assertIn('sha256sum -- "${throughput_patch}"', common)
        self.assertIn('apply --check -- "${throughput_patch}"', common)
        self.assertIn('apply --reverse --check -- "${throughput_patch}"', common)
        self.assertIn("refusing a drifted build", common)

    def test_patch_applies_to_the_pinned_progress_branch_and_is_detectable_afterward(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            source = (
                checkout
                / "crates"
                / "evm"
                / "evm"
                / "src"
                / "executors"
                / "invariant"
                / "mod.rs"
            )
            source.parent.mkdir(parents=True)
            shutil.copyfile(PREIMAGE, source)

            subprocess.check_call(
                ["git", "-C", str(checkout), "apply", "--check", str(PATCH)]
            )
            subprocess.check_call(
                ["git", "-C", str(checkout), "apply", str(PATCH)]
            )
            subprocess.check_call(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "apply",
                    "--reverse",
                    "--check",
                    str(PATCH),
                ]
            )

            patched = source.read_text(encoding="utf-8")
            self.assertIn("if let Some(progress) = progress", patched)
            self.assertNotIn("} else if edge_coverage_enabled", patched)
            self.assertIn(
                "if campaign_state.should_emit_metrics_report("
                "DURATION_BETWEEN_METRICS_REPORT)",
                patched,
            )

    def test_cloud_and_local_install_paths_ship_the_same_patch(self):
        terraform = (ROOT / "infrastructure" / "main.tf").read_text(encoding="utf-8")
        user_data = (ROOT / "infrastructure" / "user_data.sh.tftpl").read_text(
            encoding="utf-8"
        )
        local_run = (ROOT / "scripts" / "local-run.sh").read_text(encoding="utf-8")

        self.assertIn("filesha256(local.foundry_throughput_patch_path)", terraform)
        self.assertIn("foundry_source_patch", terraform)
        self.assertIn("${foundry_source_patch}", user_data)
        self.assertIn("SCFUZZBENCH_FOUNDRY_SOURCE_PATCH", user_data)
        self.assertIn("fuzzers/foundry/throughput-progress.patch", local_run)


if __name__ == "__main__":
    unittest.main()
