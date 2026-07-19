import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


def load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "preliminary_snapshot.py"
    spec = importlib.util.spec_from_file_location("preliminary_snapshot", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PreliminarySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_copy_uses_frozen_size_when_source_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "runner.log"
            destination = root / "copy.log"
            source.write_bytes(b"first\nsecond\n")
            captured_size = source.stat().st_size
            with source.open("ab") as handle:
                handle.write(b"later\n")

            details = self.module.copy_prefix_complete_lines(
                source, destination, captured_size
            )

            self.assertEqual(b"first\nsecond\n", destination.read_bytes())
            self.assertEqual(captured_size, details["source_size_at_capture"])
            self.assertFalse(details["partial_final_line_removed"])

    def test_copy_removes_partial_final_record_without_touching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "runner_metrics.csv"
            destination = root / "copy.csv"
            original = b"header\ncomplete\npartial"
            source.write_bytes(original)

            details = self.module.copy_prefix_complete_lines(
                source, destination, len(original)
            )

            self.assertEqual(original, source.read_bytes())
            self.assertEqual(b"header\ncomplete\n", destination.read_bytes())
            self.assertTrue(details["partial_final_line_removed"])
            self.assertEqual(
                hashlib.sha256(destination.read_bytes()).hexdigest(),
                details["sha256"],
            )

    def test_copy_fails_closed_if_source_shrinks_before_frozen_prefix_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "runner.log"
            destination = root / "copy.log"
            source.write_bytes(b"short\n")

            with self.assertRaisesRegex(ValueError, "shrank"):
                self.module.copy_prefix_complete_lines(
                    source,
                    destination,
                    source.stat().st_size + 10,
                )

            self.assertFalse(destination.exists())

    def test_candidate_walk_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            outside = root / "outside"
            logs.mkdir()
            outside.mkdir()
            real = logs / "runner.log"
            real.write_text("safe\n", encoding="utf-8")
            (outside / "secret.log").write_text("do not copy\n", encoding="utf-8")
            (logs / "linked.log").symlink_to(outside / "secret.log")
            (logs / "linked-dir").symlink_to(outside, target_is_directory=True)

            candidates, skipped = self.module._candidate_files(logs)

            self.assertEqual([real], candidates)
            self.assertEqual(
                [
                    {"path": "linked-dir", "reason": "symlink"},
                    {"path": "linked.log", "reason": "symlink"},
                ],
                skipped,
            )

    def test_capture_has_consistent_identity_and_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "live-logs"
            logs.mkdir()
            (logs / "fuzzer.log").write_text("event one\nevent two\n", encoding="utf-8")
            (logs / "runner_metrics.csv").write_text(
                "timestamp,value\n2026-07-19T00:00:00Z,1\n", encoding="utf-8"
            )
            (logs / "ignored.txt").write_text("not a runner artifact", encoding="utf-8")
            archive = root / "snapshot.zip"

            result = self.module.capture_snapshot(
                log_dir=logs,
                archive_path=archive,
                run_id="gh-12345-1",
                run_started_at_epoch=1784419200,
                benchmark_uuid="a" * 32,
                checkpoint=1,
                interval_seconds=3600,
                scheduled_at_epoch=1784422800,
                captured_at_epoch=1784422807,
                instance_id="i-0123456789abcdef0",
                fuzzer_key="echidna",
                run_index="0",
                fuzzer_label="echidna-v2.3.1",
                timeout_seconds=86400,
            )

            self.assertEqual("echidna-0-i-0123456789abcdef0", result["object_identity"])
            self.assertEqual(
                hashlib.sha256(archive.read_bytes()).hexdigest(), result["sha256"]
            )
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(
                    [
                        "checkpoint.json",
                        "logs/fuzzer.log",
                        "logs/runner_metrics.csv",
                    ],
                    sorted(bundle.namelist()),
                )
                metadata = json.loads(bundle.read("checkpoint.json"))
                self.assertEqual(self.module.SCHEMA, metadata["schema"])
                self.assertEqual("gh-12345-1", metadata["run_id"])
                self.assertEqual(1784419200, metadata["run_started_at_epoch"])
                self.assertFalse(metadata["complete"])
                self.assertEqual(1, metadata["checkpoint"])
                self.assertEqual(1784422800, metadata["scheduled_at_epoch"])
                for item in metadata["files"]:
                    self.assertEqual(
                        hashlib.sha256(bundle.read(item["path"])).hexdigest(),
                        item["sha256"],
                    )
            self.assertEqual("event one\nevent two\n", (logs / "fuzzer.log").read_text())

    def test_capture_rejects_key_path_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "fuzzer_key"):
                self.module.capture_snapshot(
                    log_dir=root,
                    archive_path=root / "snapshot.zip",
                    run_id="gh-12345-1",
                    run_started_at_epoch=1784419200,
                    benchmark_uuid="a" * 32,
                    checkpoint=1,
                    interval_seconds=3600,
                    scheduled_at_epoch=1784422800,
                    captured_at_epoch=1784422800,
                    instance_id="i-0123456789abcdef0",
                    fuzzer_key="../foundry",
                    run_index="0",
                    fuzzer_label="foundry",
                    timeout_seconds=3600,
                )

    def test_capture_rejects_checkpoint_at_terminal_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "terminal"):
                self.module.capture_snapshot(
                    log_dir=root,
                    archive_path=root / "snapshot.zip",
                    run_id="gh-12345-1",
                    run_started_at_epoch=1784419200,
                    benchmark_uuid="a" * 32,
                    checkpoint=1,
                    interval_seconds=3600,
                    scheduled_at_epoch=1784422800,
                    captured_at_epoch=1784422800,
                    instance_id="i-0123456789abcdef0",
                    fuzzer_key="foundry",
                    run_index="0",
                    fuzzer_label="foundry-v1",
                    timeout_seconds=3600,
                )


if __name__ == "__main__":
    unittest.main()
