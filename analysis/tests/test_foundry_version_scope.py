import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FoundryVersionScopeTests(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_cloud_dispatch_does_not_expose_or_forward_foundry_version(self):
        workflow = self.read(".github/workflows/benchmark-run.yml")

        self.assertNotIn("foundry_version:", workflow)
        self.assertNotIn("inputs.foundry_version", workflow)
        self.assertNotIn('foundry_version=${FOUNDRY_VERSION}', workflow)

    def test_cloud_request_surfaces_do_not_offer_foundry_version(self):
        issue_template = self.read(".github/ISSUE_TEMPLATE/benchmark-request.md")
        start_page = self.read("docs/.vitepress/components/StartBenchmark.vue")

        self.assertNotIn('"foundry_version"', issue_template)
        self.assertNotIn("foundryVersion", start_page)
        self.assertNotIn("foundry_version", start_page)

    def test_legacy_request_accepts_only_missing_or_blank_string_values(self):
        workflow = self.read(".github/workflows/benchmark-request.yml")

        self.assertIn("foundry_version is local-only", workflow)
        self.assertIn('const foundryVersionRaw = payload["foundry_version"]', workflow)
        self.assertIn('typeof foundryVersionRaw !== "string"', workflow)
        self.assertIn("foundryVersionRaw.trim()", workflow)
        self.assertNotIn('core.setOutput("foundry_version"', workflow)
        self.assertNotIn("needs.prepare.outputs.foundry_version", workflow)

    def test_local_release_override_remains_available(self):
        local_runner = self.read("scripts/local-run.sh")
        terraform_variables = self.read("infrastructure/variables.tf")

        self.assertIn("--foundry-version", local_runner)
        self.assertIn('variable "foundry_version"', terraform_variables)
        self.assertIn(
            'default     = "https://github.com/foundry-rs/foundry"',
            terraform_variables,
        )

    def test_source_build_provenance_does_not_claim_release_tag(self):
        terraform = self.read("infrastructure/main.tf")

        self.assertIn(
            'foundry_release_version = var.foundry_git_repo == "" ? var.foundry_version : ""',
            terraform,
        )
        self.assertIn("foundry_version      = local.foundry_release_version", terraform)
        self.assertIn(
            "foundry_version              = local.foundry_release_version",
            terraform,
        )


if __name__ == "__main__":
    unittest.main()
