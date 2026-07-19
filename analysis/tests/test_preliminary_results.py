import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


def load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "preliminary_results.py"
    spec = importlib.util.spec_from_file_location("preliminary_results", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_manifest(module, *, interval=3600):
    return {
        "schema": module.RUN_SCHEMA,
        "run_id": "gh-24680-1",
        "benchmark_uuid": "b" * 32,
        "run_started_at_epoch": 1_800_000_000,
        "timeout_hours": 24,
        "instances_per_fuzzer": 2,
        "fuzzer_keys": ["echidna", "foundry"],
        "preliminary": {
            "enabled": interval > 0,
            "interval_seconds": interval,
        },
    }


class PreliminaryResultsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

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

    def test_watermark_covers_all_markdown_and_chart_paths_in_copy_only(self):
        from PIL import Image

        context = {
            "run_id": "gh-24680-1",
            "benchmark_uuid": "b" * 32,
            "checkpoint": 2,
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


if __name__ == "__main__":
    unittest.main()
