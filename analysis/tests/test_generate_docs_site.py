import importlib.util
import sys
import unittest
from pathlib import Path


def load_generate_docs_site():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "generate_docs_site.py"
    spec = importlib.util.spec_from_file_location("generate_docs_site", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GenerateDocsSiteTests(unittest.TestCase):
    def test_rewrite_headings_increases_heading_depth(self):
        module = load_generate_docs_site()
        source = "# Title\n\n## Section\nplain text\n"
        rewritten = module.rewrite_headings(source, add=2)

        self.assertIn("### Title", rewritten)
        self.assertIn("#### Section", rewritten)
        self.assertIn("plain text", rewritten)

    def test_first_markdown_image_returns_first_image(self):
        module = load_generate_docs_site()
        lines = [
            "# Title",
            "",
            "![first alt](https://example.com/first.png)",
            "![second alt](https://example.com/second.png)",
        ]
        image = module.first_markdown_image(lines)

        self.assertEqual(("https://example.com/first.png", "first alt"), image)

    def test_with_social_preview_head_uses_first_image(self):
        module = load_generate_docs_site()
        lines = [
            "# Run `1`",
            "",
            "![Bugs Over Time](https://bucket.s3.us-east-1.amazonaws.com/analysis/abc/1/bugs_over_time.png)",
            "![Another](https://bucket.s3.us-east-1.amazonaws.com/analysis/abc/1/other.png)",
        ]

        rendered = module.with_social_preview_head(
            lines,
            page_path="/runs/1/abc/",
            title="scfuzzbench run 1 - org/repo",
            description="Benchmark abc at 2026-03-07 00:00:00Z.",
        )
        joined = "\n".join(rendered)

        self.assertTrue(rendered[0] == "---")
        self.assertIn("property: og:image", joined)
        self.assertIn(
            'content: "https://bucket.s3.us-east-1.amazonaws.com/analysis/abc/1/bugs_over_time.png"',
            joined,
        )
        self.assertIn('content: "scfuzzbench run 1 - org/repo"', joined)
        self.assertIn('content: "Benchmark abc at 2026-03-07 00:00:00Z."', joined)
        self.assertIn("name: twitter:title", joined)
        self.assertIn("name: twitter:description", joined)
        self.assertIn("name: twitter:image", joined)

    def test_run_social_description_is_url_specific(self):
        module = load_generate_docs_site()
        run = module.Run(
            run_id=1772801774,
            benchmark_uuid="454f886c9668a94e8595de32219ce2b9",
            manifest_key="runs/1772801774/454f886c9668a94e8595de32219ce2b9/manifest.json",
            manifest={
                "target_repo_url": "https://github.com/Recon-Fuzz/scfuzzbench",
                "target_commit": "0123456789abcdef",
                "fuzzer_keys": ["foundry", "echidna", "medusa"],
            },
            timeout_hours=24.0,
            analyzed=True,
            analysis_kind="analysis",
            analysis_prefix="analysis/454f886c9668a94e8595de32219ce2b9/1772801774",
        )

        desc = module.run_social_description(run)

        self.assertIn("Benchmark 454f886c9668a94e8595de32219ce2b9", desc)
        self.assertIn("Timeout 24h", desc)
        self.assertIn("Target Recon-Fuzz/scfuzzbench", desc)
        self.assertIn("Commit 0123456789", desc)
        self.assertIn("Fuzzers foundry, echidna, medusa", desc)


if __name__ == "__main__":
    unittest.main()
