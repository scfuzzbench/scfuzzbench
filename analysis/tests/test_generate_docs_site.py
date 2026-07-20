import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
    def test_preliminary_text_download_is_bounded_and_identity_pinned(self):
        module = load_generate_docs_site()
        value = b"verified preliminary text\n"
        head = {
            "ContentLength": len(value),
            "Metadata": {"sha256": hashlib.sha256(value).hexdigest()},
            "VersionId": "version-1",
            "ETag": '"etag-1"',
        }

        def download(args, **_kwargs):
            Path(args[-1]).write_bytes(value)
            return 0

        with mock.patch.object(
            module, "preliminary_object_head", return_value=head
        ), mock.patch.object(
            module.subprocess, "check_call", side_effect=download
        ) as check_call:
            rendered = module.preliminary_text(
                "bucket",
                "preliminary/gh-1-1/" + "b" * 32 + "/run.json",
                profile="docs",
                max_bytes=64,
            )

        self.assertEqual(value.decode("utf-8"), rendered)
        command = check_call.call_args.args[0]
        self.assertEqual("bytes=0-64", command[command.index("--range") + 1])
        self.assertEqual("version-1", command[command.index("--version-id") + 1])
        self.assertEqual('"etag-1"', command[command.index("--if-match") + 1])
        self.assertEqual(
            "docs", check_call.call_args.kwargs["env"]["AWS_PROFILE"]
        )

    def test_preliminary_text_rejects_oversized_mutated_response(self):
        module = load_generate_docs_site()
        value = b"head"
        head = {
            "ContentLength": len(value),
            "Metadata": {"sha256": hashlib.sha256(value).hexdigest()},
            "ETag": '"old-etag"',
        }

        def download(args, **_kwargs):
            self.assertEqual("bytes=0-4", args[args.index("--range") + 1])
            Path(args[-1]).write_bytes(b"heads")
            return 0

        with mock.patch.object(
            module, "preliminary_object_head", return_value=head
        ), mock.patch.object(
            module.subprocess, "check_call", side_effect=download
        ), self.assertRaisesRegex(ValueError, "exceeds its docs byte cap"):
            module.preliminary_text(
                "bucket",
                "preliminary/gh-1-1/" + "b" * 32 + "/run.json",
                profile=None,
                max_bytes=4,
            )

    def test_docs_s3_listing_caps_are_exact_and_fail_closed(self):
        module = load_generate_docs_site()
        first = {
            "Contents": [{"Key": "preliminary/a"}],
            "IsTruncated": True,
            "NextContinuationToken": "two",
        }
        second = {
            "Contents": [{"Key": "preliminary/b"}],
            "IsTruncated": False,
        }
        with mock.patch.object(module, "MAX_LIST_PAGES", 2), mock.patch.object(
            module, "MAX_LIST_KEYS", 2
        ), mock.patch.object(
            module, "aws_json", side_effect=[first, second]
        ):
            self.assertEqual(
                ["preliminary/a", "preliminary/b"],
                module.list_keys("bucket", "preliminary/", profile=None),
            )

        with mock.patch.object(module, "MAX_LIST_PAGES", 1), mock.patch.object(
            module, "aws_json", return_value=first
        ), self.assertRaisesRegex(ValueError, "exceeded 1 pages"):
            module.list_keys("bucket", "preliminary/", profile=None)

        with mock.patch.object(module, "MAX_LIST_KEYS", 1), mock.patch.object(
            module,
            "aws_json",
            return_value={
                "Contents": [
                    {"Key": "preliminary/a"},
                    {"Key": "preliminary/b"},
                ],
                "IsTruncated": False,
            },
        ), self.assertRaisesRegex(ValueError, "exceeded 1 keys"):
            module.list_keys("bucket", "preliminary/", profile=None)

        with mock.patch.object(
            module,
            "aws_json",
            return_value={"Contents": [], "IsTruncated": True},
        ), self.assertRaisesRegex(RuntimeError, "without a continuation token"):
            module.list_keys("bucket", "preliminary/", profile=None)

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

    def test_format_fuzzer_lines_maps_recon_version_to_recon_fuzzer_key(self):
        module = load_generate_docs_site()
        manifest = {
            "fuzzer_keys": ["echidna", "foundry", "medusa", "recon-fuzzer"],
            "echidna_version": "2.3.1",
            "medusa_version": "1.4.1",
            "recon_version": "0.4.6",
            "foundry_version": "",
            "foundry_git_ref": "907ba081ce9270e6a4a01ee0e77dfdb9a375ff77",
        }

        lines = module.format_fuzzer_lines(manifest)

        self.assertIn("`recon-fuzzer (0.4.6)`", lines)
        self.assertIn("`echidna (2.3.1)`", lines)
        self.assertIn("`medusa (1.4.1)`", lines)

    def test_format_fuzzer_lines_falls_back_to_foundry_git_ref(self):
        module = load_generate_docs_site()
        manifest = {
            "fuzzer_keys": ["foundry"],
            "foundry_version": "",
            "foundry_git_ref": "907ba081ce9270e6a4a01ee0e77dfdb9a375ff77",
        }

        lines = module.format_fuzzer_lines(manifest)

        self.assertEqual(["`foundry (git:907ba08)`"], lines)

    def test_format_fuzzer_lines_prefers_resolved_foundry_version(self):
        module = load_generate_docs_site()
        manifest = {
            "fuzzer_keys": ["foundry"],
            "foundry_version": "1.3.6-dev",
            "foundry_git_ref": "907ba081ce9270e6a4a01ee0e77dfdb9a375ff77",
        }

        lines = module.format_fuzzer_lines(manifest)

        self.assertEqual(["`foundry (1.3.6-dev)`"], lines)

    def test_format_fuzzer_lines_shows_opt_in_tool_commits(self):
        module = load_generate_docs_site()
        manifest = {
            "fuzzer_keys": ["echidna", "medusa"],
            "echidna_version": "",
            "echidna_ci_commit": "9680bb1ef8bf0e11c00fff3e29c5f244d6eb1c85",
            "medusa_version": "",
            "medusa_git_commit": "3857153837ab90ed73adc484414b4b43703a54fb",
        }

        lines = module.format_fuzzer_lines(manifest)

        self.assertEqual(["`echidna (ci:9680bb1)`", "`medusa (git:3857153)`"], lines)

    def test_format_seed_corpus_lines_reports_provenance(self):
        module = load_generate_docs_site()
        manifest = {
            "seed_corpus": {
                "source": "s3://bench-inputs/seeds/v1",
                "source_type": "s3",
                "file_count": 3,
                "size_bytes": 42,
                "sha256": "a" * 64,
                "copy_semantics": "recursive-byte-for-byte",
            }
        }

        lines = module.format_seed_corpus_lines(manifest)

        self.assertIn("- seed_corpus_source: `s3://bench-inputs/seeds/v1`", lines)
        self.assertIn("- seed_corpus_file_count: `3`", lines)
        self.assertIn(f"- seed_corpus_sha256: `{'a' * 64}`", lines)

    def test_format_seed_corpus_lines_omits_empty_default(self):
        module = load_generate_docs_site()

        self.assertEqual([], module.format_seed_corpus_lines({}))

    def test_run_social_description_is_url_specific(self):
        module = load_generate_docs_site()
        run = module.Run(
            run_id="1772801774",
            run_started_at_epoch=1772801774,
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

    def test_run_manifest_pattern_accepts_isolated_and_legacy_ids(self):
        module = load_generate_docs_site()
        uuid = "a" * 32

        self.assertIsNotNone(
            module.RUN_MANIFEST_RE.match(
                f"runs/gh-123456-2/{uuid}/manifest.json"
            )
        )
        self.assertIsNotNone(
            module.RUN_MANIFEST_RE.match(
                f"runs/1772801774/{uuid}/manifest.json"
            )
        )

    def test_preliminary_page_repeats_non_terminal_warning_and_as_of_context(self):
        module = load_generate_docs_site()
        manifest = {
            "run_id": "gh-24680-1",
            "benchmark_uuid": "b" * 32,
            "run_started_at_epoch": 1_800_000_000,
            "timeout_hours": 24,
        }
        metadata = {
            "as_of_utc": "2027-01-15T10:00:00Z",
            "elapsed_seconds": 7200,
            "planned_timeout_seconds": 86400,
            "present_snapshots": 3,
            "expected_snapshots": 4,
        }

        rendered = module.render_preliminary_page(
            manifest=manifest,
            metadata=metadata,
            report_markdown=(
                "> [!CAUTION]\n"
                "> **PRELIMINARY — INCOMPLETE — DO NOT COMPARE OR STOP**\n\n"
                "# Benchmark report\n"
            ),
            chart_urls=[
                (
                    "Bugs Over Time",
                    "https://bucket.example/preliminary/gh-24680-1/chart.png",
                )
            ],
            generated_at="2027-01-15 10:05:00Z",
        )

        self.assertIn("PRELIMINARY — DO NOT COMPARE OR STOP", rendered)
        self.assertIn("2027-01-15T10:00:00Z", rendered)
        self.assertIn("2h 0m", rendered)
        self.assertIn("24h 0m", rendered)
        self.assertIn("3/4", rendered)
        self.assertIn("rankings, pass/fail decisions, or optional stopping", rendered)
        self.assertIn("![PRELIMINARY — Bugs Over Time]", rendered)
        self.assertIn("### Benchmark report", rendered)

    def test_preliminary_report_sanitizer_neutralizes_html_vue_and_active_links(self):
        module = load_generate_docs_site()
        report = (
            "> **PRELIMINARY — INCOMPLETE — DO NOT COMPARE OR STOP**\n"
            "# Report\n"
            "<script>alert(1)</script>\n"
            "{{ constructor.constructor('alert(2)')() }}\n"
            "[click](javascript:alert(3))\n"
        )

        sanitized = module.sanitize_preliminary_markdown(report)

        self.assertNotIn("<script>", sanitized)
        self.assertNotIn("{{", sanitized)
        self.assertNotIn("javascript:", sanitized.lower())
        self.assertIn("&lt;script&gt;", sanitized)

    def test_docs_only_hide_run_after_checksum_verified_valid_finalized_marker(self):
        module = load_generate_docs_site()
        run_id = "gh-24680-1"
        uuid = "b" * 32
        prefix = f"preliminary/{run_id}/{uuid}"
        run_key = f"{prefix}/run.json"
        finalized_key = f"{prefix}/finalized.json"
        manifest = {
            "schema": "scfuzzbench-preliminary-run/v1",
            "run_id": run_id,
            "benchmark_uuid": uuid,
            "run_started_at_epoch": 1_800_000_000,
            "timeout_hours": 4,
            "instances_per_fuzzer": 1,
            "fuzzer_keys": ["foundry"],
            "preliminary": {"enabled": True, "interval_seconds": 3600},
        }
        marker = {
            "schema": "scfuzzbench-preliminary-finalized/v1",
            "run_id": run_id,
            "benchmark_uuid": uuid,
            "canonical_release_tag": f"scfuzzbench-{uuid}-{run_id}",
            "preliminary_stream_closed": True,
        }

        def preliminary_payload(
            _bucket,
            key,
            *,
            profile,
            max_bytes,
            expected_sha256="",
        ):
            del profile, max_bytes, expected_sha256
            return json.dumps(marker if key == finalized_key else manifest)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            module, "list_keys", return_value=[run_key, finalized_key]
        ), mock.patch.object(
            module, "preliminary_text", side_effect=preliminary_payload
        ) as preliminary_text:
            docs = Path(tmp)
            module.generate_preliminary_pages(
                bucket="bucket",
                region="us-east-1",
                profile=None,
                docs_dir=docs,
                now=manifest["run_started_at_epoch"] + 3600,
                generated_at="2027-01-15 10:05:00Z",
            )
            self.assertFalse((docs / "preliminary" / run_id / uuid).exists())
            marker_calls = [
                call
                for call in preliminary_text.call_args_list
                if call.args[1] == finalized_key
            ]
            self.assertEqual(1, len(marker_calls))

        invalid_marker = {**marker, "canonical_release_tag": "forged"}

        def invalid_payload(
            _bucket,
            key,
            *,
            profile,
            max_bytes,
            expected_sha256="",
        ):
            del profile, max_bytes, expected_sha256
            return json.dumps(invalid_marker if key == finalized_key else manifest)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            module, "list_keys", return_value=[run_key, finalized_key]
        ), mock.patch.object(
            module, "preliminary_text", side_effect=invalid_payload
        ):
            docs = Path(tmp)
            module.generate_preliminary_pages(
                bucket="bucket",
                region="us-east-1",
                profile=None,
                docs_dir=docs,
                now=manifest["run_started_at_epoch"] + 3600,
                generated_at="2027-01-15 10:05:00Z",
            )
            self.assertTrue(
                (docs / "preliminary" / run_id / uuid / "index.md").is_file()
            )

    def test_docs_cap_manifest_inventory_and_active_pages(self):
        module = load_generate_docs_site()
        run_keys = [
            f"preliminary/gh-{number}-1/{str(number) * 32}/run.json"
            for number in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            module, "MAX_RUN_MANIFESTS", 1
        ), mock.patch.object(
            module, "list_keys", return_value=run_keys
        ), self.assertRaisesRegex(ValueError, "exceeded 1 run manifests"):
            module.generate_preliminary_pages(
                bucket="bucket",
                region="us-east-1",
                profile=None,
                docs_dir=Path(tmp),
                now=1_800_000_001,
                generated_at="now",
            )

        manifests = {}
        for number, run_key in zip((1, 2), run_keys):
            run_id = f"gh-{number}-1"
            uuid = str(number) * 32
            manifests[run_key] = {
                "schema": "scfuzzbench-preliminary-run/v1",
                "run_id": run_id,
                "benchmark_uuid": uuid,
                "run_started_at_epoch": 1_800_000_000,
                "timeout_hours": 4,
                "instances_per_fuzzer": 1,
                "fuzzer_keys": ["foundry"],
                "preliminary": {"enabled": True, "interval_seconds": 3600},
            }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            module, "MAX_SELECTED_RUNS", 1
        ), mock.patch.object(
            module, "list_keys", return_value=run_keys
        ), mock.patch.object(
            module,
            "preliminary_text",
            side_effect=lambda _bucket, key, **_kwargs: json.dumps(manifests[key]),
        ), self.assertRaisesRegex(ValueError, "exceeded 1 active runs"):
            module.generate_preliminary_pages(
                bucket="bucket",
                region="us-east-1",
                profile=None,
                docs_dir=Path(tmp),
                now=1_800_000_001,
                generated_at="now",
            )

if __name__ == "__main__":
    unittest.main()
