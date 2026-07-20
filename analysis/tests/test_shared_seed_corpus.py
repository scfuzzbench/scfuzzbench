import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON_SH = REPO_ROOT / "fuzzers" / "_shared" / "common.sh"
SEED_HELPER = REPO_ROOT / "fuzzers" / "_shared" / "prepare_seed_corpus.py"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        size = path.stat().st_size
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(size.to_bytes(8, "big"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class SharedSeedCorpusTests(unittest.TestCase):
    def write_mock_aws(self, root: Path) -> Path:
        mock_bin = root / "bin"
        mock_bin.mkdir()
        aws = mock_bin / "aws"
        aws.write_text(
            """#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "--no-cli-pager":
    args = args[1:]
operation = args[:2]
source = Path(os.environ["MOCK_S3_SOURCE"])
prefix = os.environ.get("MOCK_S3_PREFIX", "corpora/v1").rstrip("/")

def value(flag):
    return args[args.index(flag) + 1]

def object_data(key):
    relative = key[len(prefix) + 1:]
    path = source.joinpath(*relative.split("/"))
    data = path.read_bytes()
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    return path, data, etag

if operation == ["s3api", "list-objects-v2"]:
    unsafe = os.environ.get("MOCK_S3_UNSAFE_KEY")
    contents = []
    if unsafe:
        contents.append({
            "Key": prefix + "/" + unsafe,
            "Size": 4,
            "ETag": '"unsafe"',
            "LastModified": "2026-01-01T00:00:00Z",
        })
    else:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            relative = path.relative_to(source).as_posix()
            _, data, etag = object_data(prefix + "/" + relative)
            contents.append({
                "Key": prefix + "/" + relative,
                "Size": len(data),
                "ETag": etag,
                "LastModified": "2026-01-01T00:00:00Z",
            })
    counter_path = os.environ.get("MOCK_S3_LIST_COUNTER")
    if counter_path:
        counter = Path(counter_path)
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        if os.environ.get("MOCK_S3_MUTATE_LISTING") and count > 0 and contents:
            contents[0]["LastModified"] = "2026-01-02T00:00:00Z"
    print(json.dumps({"Contents": contents}))
elif operation == ["s3api", "head-object"]:
    _, data, etag = object_data(value("--key"))
    print(json.dumps({
        "ContentLength": len(data),
        "ETag": etag,
        "VersionId": "version-1",
    }))
elif operation == ["s3api", "get-object"]:
    path, _, etag = object_data(value("--key"))
    output = Path(args[args.index("--output") - 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, output)
    print(json.dumps({
        "ETag": etag,
        "VersionId": "version-1",
        "ChecksumSHA256": "mock-checksum",
    }))
else:
    raise SystemExit("unexpected aws args: " + repr(args))
"""
        )
        aws.chmod(0o755)
        return mock_bin

    def run_helper(
        self,
        root: Path,
        *,
        source: Path,
        max_files: int,
        max_bytes: int,
    ) -> subprocess.CompletedProcess[str]:
        destination = root / "snapshot"
        destination.mkdir()
        resolved = root.resolve(strict=True)
        info = resolved.stat()
        result = subprocess.run(
            [
                sys.executable,
                str(SEED_HELPER),
                "--mode",
                "local",
                "--source",
                str(source),
                "--source-label",
                "local-test://seeds",
                "--destination",
                str(destination),
                "--destination-root-path",
                str(root),
                "--destination-root-anchor",
                str(resolved),
                "--destination-root-identity",
                f"{info.st_dev}:{info.st_ino}",
                "--max-files",
                str(max_files),
                "--max-bytes",
                str(max_bytes),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            shutil.rmtree(destination, ignore_errors=True)
        return result

    def write_mock_manifest_aws(self, root: Path) -> Path:
        helper = root / "mock-put-manifest.py"
        helper.write_text(
            """#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--bucket", required=True)
parser.add_argument("--key", required=True)
args = parser.parse_args()
remote = Path(os.environ["MOCK_MANIFEST_S3"])
stored = remote.joinpath(*args.key.split("/"))
data = sys.stdin.buffer.read()
if stored.exists():
    if stored.read_bytes() != data:
        raise SystemExit("different manifest already exists")
else:
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(data)
"""
        )
        helper.chmod(0o755)
        return helper

    def run_prepare(
        self,
        root: Path,
        *,
        source: str = "",
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        workdir = root / "work"
        logdir = root / "logs"
        target = workdir / "target"
        target.mkdir(parents=True, exist_ok=True)
        corpus = target / "corpus"
        corpus.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "SCFUZZBENCH_LOCAL_MODE": "1",
            "SCFUZZBENCH_ROOT": str(root),
            "SCFUZZBENCH_WORKDIR": str(workdir),
            "SCFUZZBENCH_LOG_DIR": str(logdir),
            "SCFUZZBENCH_CORPUS_DIR": str(corpus),
            "SCFUZZBENCH_SEED_CORPUS_SOURCE": source,
        }
        if extra_env:
            env.update(extra_env)
        script = (
            f"source {shlex.quote(str(COMMON_SH))}\n"
            "prepare_workspace\n"
            "capture_target_workspace_anchor\n"
            "prepare_shared_seed_corpus\n"
        )
        return subprocess.run(
            ["bash", "-c", script],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def write_path_swap_hook(self, root: Path) -> Path:
        mock_bin = root / "swap-bin"
        mock_bin.mkdir()
        hook = mock_bin / "swap-path"
        hook.write_text(
            """#!/usr/bin/env python3
import os
from pathlib import Path

marker = Path(os.environ["MOCK_SWAP_MARKER"])
if marker.exists():
    raise SystemExit(0)
original = Path(os.environ["MOCK_SWAP_PATH"])
saved = Path(os.environ["MOCK_SWAP_SAVED"])
replacement = Path(os.environ["MOCK_SWAP_REPLACEMENT"])
original.rename(saved)
original.symlink_to(replacement, target_is_directory=True)
marker.write_text("swapped")
"""
        )
        hook.chmod(0o755)
        return hook

    def run_save_results(
        self,
        root: Path,
        *,
        log_dir: Path,
        corpus_dir: Path | None,
        prelude: str = "",
    ) -> subprocess.CompletedProcess[str]:
        mock_bin = root / "save-bin"
        mock_bin.mkdir(exist_ok=True)
        zip_command = mock_bin / "zip"
        zip_command.write_text(
            """#!/usr/bin/env python3
import sys
from pathlib import Path

operands = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
if len(operands) < 2:
    raise SystemExit("expected output and input operands")
output = Path(operands[0])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b"PK\\x05\\x06" + b"\\x00" * 18)
"""
        )
        zip_command.chmod(0o755)
        env = {
            **os.environ,
            "SCFUZZBENCH_LOCAL_MODE": "1",
            "SCFUZZBENCH_ROOT": str(root),
            "SCFUZZBENCH_WORKDIR": str(root / "work"),
            "SCFUZZBENCH_LOG_DIR": str(log_dir),
            "SCFUZZBENCH_CORPUS_DIR": str(corpus_dir) if corpus_dir else "",
            "SCFUZZBENCH_LOCAL_OUTPUT_DIR": str(root / "output"),
            "SCFUZZBENCH_FUZZER_LABEL": "test-fuzzer",
            "SCFUZZBENCH_REPO_URL": "https://example.invalid/target.git",
            "PATH": f"{mock_bin}:{os.environ['PATH']}",
        }
        script = (
            f"source {shlex.quote(str(COMMON_SH))}\n"
            "stop_runner_metrics() { :; }\n"
            "cache_instance_id() { :; }\n"
            "prepare_workspace\n"
            "capture_target_workspace_anchor\n"
            'if [[ -n "${SCFUZZBENCH_CORPUS_DIR:-}" ]]; then '
            "capture_corpus_workspace_anchor; fi\n"
            f"{prelude}\n"
            "save_results_local\n"
        )
        return subprocess.run(
            ["bash", "-c", script],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_empty_default_resets_corpus_without_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "work" / "target" / "corpus"
            corpus.mkdir(parents=True)
            (corpus / ".stale").write_text("old")

            result = self.run_prepare(root)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], list(corpus.iterdir()))
            self.assertFalse((root / "logs" / "seed_corpus.json").exists())

    def test_corpus_reset_refuses_paths_outside_target_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protected = root / "protected"
            protected.mkdir()
            sentinel = protected / "do-not-delete"
            sentinel.write_text("keep")

            result = self.run_prepare(
                root,
                extra_env={"SCFUZZBENCH_CORPUS_DIR": str(protected)},
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("outside target workspace", result.stderr)
            self.assertEqual("keep", sentinel.read_text())

    def test_corpus_reset_refuses_symlink_escape_from_target_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "work" / "target"
            target.mkdir(parents=True)
            protected = root / "protected"
            protected.mkdir()
            sentinel = protected / "do-not-delete"
            sentinel.write_text("keep")
            escape = target / "corpus-escape"
            escape.symlink_to(protected, target_is_directory=True)

            result = self.run_prepare(
                root,
                extra_env={"SCFUZZBENCH_CORPUS_DIR": str(escape)},
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("outside target workspace", result.stderr)
            self.assertEqual("keep", sentinel.read_text())

    def test_parent_swap_before_corpus_move_preserves_outside_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "work" / "target"
            nested = target / "nested"
            corpus = nested / "corpus"
            corpus.mkdir(parents=True)
            (corpus / "old").write_text("old")
            protected = root / "protected"
            protected_corpus = protected / "corpus"
            protected_corpus.mkdir(parents=True)
            sentinel = protected_corpus / "do-not-delete"
            sentinel.write_text("keep")
            hook = self.write_path_swap_hook(root)

            result = self.run_prepare(
                root,
                extra_env={
                    "SCFUZZBENCH_CORPUS_DIR": str(corpus),
                    "MOCK_SWAP_MARKER": str(root / "swap-done"),
                    "MOCK_SWAP_PATH": str(nested),
                    "MOCK_SWAP_SAVED": str(target / "nested-original"),
                    "MOCK_SWAP_REPLACEMENT": str(protected),
                    "SCFUZZBENCH_SAFE_PATH_TEST_HOOK": str(hook),
                },
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("safe path operation failed", result.stderr)
            self.assertEqual("keep", sentinel.read_text())

    def test_target_root_swap_before_corpus_move_preserves_outside_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "work" / "target"
            corpus = target / "corpus"
            corpus.mkdir(parents=True)
            protected_target = root / "protected-target"
            protected_corpus = protected_target / "corpus"
            protected_corpus.mkdir(parents=True)
            sentinel = protected_corpus / "do-not-delete"
            sentinel.write_text("keep")
            hook = self.write_path_swap_hook(root)

            result = self.run_prepare(
                root,
                extra_env={
                    "MOCK_SWAP_MARKER": str(root / "target-swap-done"),
                    "MOCK_SWAP_PATH": str(target),
                    "MOCK_SWAP_SAVED": str(root / "work" / "target-original"),
                    "MOCK_SWAP_REPLACEMENT": str(protected_target),
                    "SCFUZZBENCH_SAFE_PATH_TEST_HOOK": str(hook),
                },
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("trusted root path changed", result.stderr)
            self.assertEqual("keep", sentinel.read_text())

    def test_local_source_is_copied_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input-seeds"
            (source / "nested").mkdir(parents=True)
            (source / "one.json").write_bytes(b'{"call": 1}\n')
            (source / "nested" / "two.bin").write_bytes(b"\x00\xffseed")

            result = self.run_prepare(root, source=str(source))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                (source / "one.json").read_bytes(),
                (root / "work" / "target" / "corpus" / "one.json").read_bytes(),
            )
            self.assertEqual(
                (source / "nested" / "two.bin").read_bytes(),
                (
                    root / "work" / "target" / "corpus" / "nested" / "two.bin"
                ).read_bytes(),
            )
            metadata = json.loads((root / "logs" / "seed_corpus.json").read_text())
            self.assertRegex(
                metadata["source"],
                r"^local-sha256://[0-9a-f]{64}$",
            )
            self.assertEqual("local", metadata["source_type"])
            self.assertEqual(2, metadata["file_count"])
            self.assertEqual(18, metadata["size_bytes"])
            self.assertEqual(tree_digest(source), metadata["sha256"])
            self.assertEqual("recursive-byte-for-byte", metadata["copy_semantics"])
            self.assertEqual(
                "relative-paths-preserved-at-corpus-root",
                metadata["destination_layout"],
            )
            self.assertEqual("opaque-not-extracted", metadata["archives"])
            self.assertEqual(
                ["nested/two.bin", "one.json"],
                [entry["path"] for entry in metadata["files"]],
            )

    def test_target_relative_source_keeps_full_collision_free_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "work" / "target" / "benchmark-seeds" / "v1"
            source.mkdir(parents=True)
            (source / "seed").write_text("seed")

            result = self.run_prepare(root, source="benchmark-seeds/v1")

            self.assertEqual(0, result.returncode, result.stderr)
            metadata = json.loads((root / "logs" / "seed_corpus.json").read_text())
            self.assertEqual("target://benchmark-seeds/v1", metadata["source"])

    def test_s3_prefix_is_downloaded_from_stable_version_bound_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_source = root / "mock-s3"
            (mock_source / "nested").mkdir(parents=True)
            (mock_source / "nested" / "seed.json").write_text("{}\n")
            mock_bin = self.write_mock_aws(root)

            result = self.run_prepare(
                root,
                source="s3://seed-bucket/corpora/v1",
                extra_env={
                    "MOCK_S3_SOURCE": str(mock_source),
                    "MOCK_S3_PREFIX": "corpora/v1",
                    "PATH": f"{mock_bin}:{os.environ['PATH']}",
                },
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                "{}\n",
                (
                    root
                    / "work"
                    / "target"
                    / "corpus"
                    / "nested"
                    / "seed.json"
                ).read_text(),
            )
            metadata = json.loads((root / "logs" / "seed_corpus.json").read_text())
            self.assertEqual("s3://seed-bucket/corpora/v1", metadata["source"])
            self.assertEqual("s3", metadata["source_type"])
            self.assertEqual("version-1", metadata["s3_objects"][0]["version_id"])
            self.assertEqual(
                "etag-or-version-bound-objects-and-stable-prefix-listing",
                metadata["source_immutability"],
            )

    def test_s3_traversal_key_is_rejected_without_touching_live_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_source = root / "mock-s3"
            mock_source.mkdir()
            mock_bin = self.write_mock_aws(root)
            corpus = root / "work" / "target" / "corpus"
            corpus.mkdir(parents=True)
            (corpus / "existing").write_text("keep")

            result = self.run_prepare(
                root,
                source="s3://seed-bucket/corpora/v1",
                extra_env={
                    "MOCK_S3_SOURCE": str(mock_source),
                    "MOCK_S3_PREFIX": "corpora/v1",
                    "MOCK_S3_UNSAFE_KEY": "../escape",
                    "PATH": f"{mock_bin}:{os.environ['PATH']}",
                },
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("unsafe S3 seed object key", result.stderr)
            self.assertEqual("keep", (corpus / "existing").read_text())
            self.assertFalse((root / "escape").exists())
            self.assertFalse((root / "shared-seed-corpus").exists())

    def test_s3_listing_change_is_rejected_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_source = root / "mock-s3"
            mock_source.mkdir()
            (mock_source / "seed").write_text("seed")
            mock_bin = self.write_mock_aws(root)
            corpus = root / "work" / "target" / "corpus"
            corpus.mkdir(parents=True)
            (corpus / "existing").write_text("keep")

            result = self.run_prepare(
                root,
                source="s3://seed-bucket/corpora/v1",
                extra_env={
                    "MOCK_S3_SOURCE": str(mock_source),
                    "MOCK_S3_PREFIX": "corpora/v1",
                    "MOCK_S3_LIST_COUNTER": str(root / "list-count"),
                    "MOCK_S3_MUTATE_LISTING": "1",
                    "PATH": f"{mock_bin}:{os.environ['PATH']}",
                },
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("prefix changed", result.stderr)
            self.assertEqual("keep", (corpus / "existing").read_text())
            self.assertFalse((root / "shared-seed-corpus").exists())

    def test_source_with_symlink_is_rejected_before_corpus_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input-seeds"
            source.mkdir()
            (source / "seed").write_text("seed")
            (source / "link").symlink_to(source / "seed")
            corpus = root / "work" / "target" / "corpus"
            corpus.mkdir(parents=True)
            (corpus / "existing").write_text("keep until valid input is staged")

            result = self.run_prepare(root, source=str(source))

            self.assertNotEqual(0, result.returncode)
            self.assertTrue((corpus / "existing").exists())
            self.assertIn("contains a symlink", result.stderr)
            self.assertFalse((root / "shared-seed-corpus").exists())

    def test_symlink_source_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_source = root / "real-seeds"
            real_source.mkdir()
            (real_source / "seed").write_text("seed")
            source = root / "input-seeds"
            source.symlink_to(real_source, target_is_directory=True)

            result = self.run_prepare(root, source=str(source))

            self.assertNotEqual(0, result.returncode)
            self.assertIn("not a symlink", result.stderr)
            self.assertFalse((root / "shared-seed-corpus").exists())

    def test_hardlink_and_fifo_are_rejected(self):
        for entry_type in ("hardlink", "fifo"):
            with self.subTest(entry_type=entry_type), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "input-seeds"
                source.mkdir()
                seed = source / "seed"
                seed.write_text("seed")
                if entry_type == "hardlink":
                    os.link(seed, source / "linked")
                else:
                    os.mkfifo(source / "pipe")

                result = self.run_prepare(root, source=str(source))

                self.assertNotEqual(0, result.returncode)
                expected = "hard-linked" if entry_type == "hardlink" else "special entry"
                self.assertIn(expected, result.stderr)
                self.assertFalse((root / "shared-seed-corpus").exists())

    def test_helper_enforces_file_and_byte_caps_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input-seeds"
            source.mkdir()
            (source / "one").write_bytes(b"1234")
            (source / "two").write_bytes(b"5678")

            too_many = self.run_helper(
                root,
                source=source,
                max_files=1,
                max_bytes=100,
            )

            self.assertNotEqual(0, too_many.returncode)
            self.assertIn("1-file limit", too_many.stderr)
            self.assertFalse((root / "snapshot").exists())
            self.assertFalse((root / "snapshot.json").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input-seeds"
            source.mkdir()
            (source / "large").write_bytes(b"12345")

            too_large = self.run_helper(
                root,
                source=source,
                max_files=10,
                max_bytes=4,
            )

            self.assertNotEqual(0, too_large.returncode)
            self.assertIn("4-byte limit", too_large.stderr)
            self.assertFalse((root / "snapshot").exists())

    def test_helper_rejects_destination_parent_swap_without_writing_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "seed").write_text("trusted", encoding="utf-8")
            parent = root / "nested"
            destination = parent / "snapshot"
            destination.mkdir(parents=True)
            outside = root / "outside"
            outside_destination = outside / "snapshot"
            outside_destination.mkdir(parents=True)
            sentinel = outside_destination / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            hook = root / "swap-destination.py"
            hook.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                f"parent = Path({str(parent)!r})\n"
                f"saved = Path({str(root / 'nested-saved')!r})\n"
                f"outside = Path({str(outside)!r})\n"
                "parent.rename(saved)\n"
                "parent.symlink_to(outside, target_is_directory=True)\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            resolved = root.resolve(strict=True)
            info = resolved.stat()

            result = subprocess.run(
                [
                    sys.executable,
                    str(SEED_HELPER),
                    "--mode",
                    "local",
                    "--source",
                    str(source),
                    "--source-label",
                    "local-test://seeds",
                    "--destination",
                    str(destination),
                    "--destination-root-path",
                    str(root),
                    "--destination-root-anchor",
                    str(resolved),
                    "--destination-root-identity",
                    f"{info.st_dev}:{info.st_ino}",
                    "--max-files",
                    "10",
                    "--max-bytes",
                    "1024",
                ],
                env={
                    **os.environ,
                    "SCFUZZBENCH_SAFE_PATH_TEST_HOOK": str(hook),
                },
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
            self.assertEqual([], list((root / "nested-saved" / "snapshot").iterdir()))

    def test_metadata_parent_swap_does_not_write_outside_log_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "seed").write_text("trusted", encoding="utf-8")
            outside = root / "outside-logs"
            outside.mkdir()
            sentinel = outside / "seed_corpus.json.tmp-sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            hook = root / "swap-metadata-parent.py"
            hook.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                f"root = Path({str(root)!r})\n"
                f"outside = Path({str(outside)!r})\n"
                "stage = root / 'shared-seed-corpus'\n"
                "logs = root / 'logs'\n"
                "saved = root / 'logs-saved'\n"
                "marker = root / 'metadata-parent-swapped'\n"
                "if not marker.exists() and stage.exists() and any(stage.iterdir()):\n"
                "    logs.rename(saved)\n"
                "    logs.symlink_to(outside, target_is_directory=True)\n"
                "    marker.write_text('swapped')\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)

            result = self.run_prepare(
                root,
                source=str(source),
                extra_env={
                    "SCFUZZBENCH_SAFE_PATH_TEST_HOOK": str(hook),
                },
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
            self.assertEqual(
                ["seed_corpus.json.tmp-sentinel"],
                sorted(path.name for path in outside.iterdir()),
            )

    def test_archives_are_copied_as_opaque_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input-seeds"
            source.mkdir()
            archive_bytes = b"PK\x03\x04not-expanded"
            (source / "seeds.zip").write_bytes(archive_bytes)

            result = self.run_prepare(root, source=str(source))

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                archive_bytes,
                (
                    root / "work" / "target" / "corpus" / "seeds.zip"
                ).read_bytes(),
            )
            self.assertEqual(
                ["seeds.zip"],
                [
                    entry.name
                    for entry in (
                        root / "work" / "target" / "corpus"
                    ).iterdir()
                ],
            )

    def test_every_builtin_runner_prepares_shared_seed_corpus(self):
        for fuzzer in ("echidna", "medusa", "foundry", "recon-fuzzer"):
            script = (REPO_ROOT / "fuzzers" / fuzzer / "run.sh").read_text()
            self.assertEqual(
                1,
                script.count("prepare_shared_seed_corpus"),
                f"{fuzzer} must prepare the corpus exactly once",
            )
            self.assertEqual(
                1,
                script.count("resolve_target_corpus_dir"),
                f"{fuzzer} must resolve corpus paths beneath the target",
            )
            self.assertEqual(
                1,
                script.count("capture_target_workspace_anchor"),
                f"{fuzzer} must capture the target anchor exactly once",
            )
            self.assertLess(
                script.index("clone_target"),
                script.index("capture_target_workspace_anchor"),
            )
            self.assertLess(
                script.index("capture_target_workspace_anchor"),
                script.index("apply_benchmark_type"),
            )
            self.assertLess(
                script.index("capture_target_workspace_anchor"),
                script.index("build_target"),
            )

    def test_foundry_cache_reset_refuses_intermediate_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "work" / "target"
            target.mkdir(parents=True)
            protected = root / "protected"
            invariant = protected / "invariant"
            invariant.mkdir(parents=True)
            sentinel = invariant / "do-not-delete"
            sentinel.write_text("keep")
            (target / "cache").symlink_to(protected, target_is_directory=True)
            script = (
                f"source {shlex.quote(str(COMMON_SH))}\n"
                f"repo_root=$(realpath -e -- {shlex.quote(str(target))})\n"
                f"repo_identity=$(stat -Lc '%d:%i' -- {shlex.quote(str(target))})\n"
                "remove_strict_descendant_tree "
                f"{shlex.quote(str(target / 'cache' / 'invariant'))} "
                f"{shlex.quote(str(target))} 'Foundry invariant cache' "
                '"${repo_root}" "${repo_identity}"\n'
            )

            result = subprocess.run(
                ["bash", "-c", script],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("safe path operation failed", result.stderr)
            self.assertEqual("keep", sentinel.read_text())
            foundry = (
                REPO_ROOT / "fuzzers" / "foundry" / "run.sh"
            ).read_text()
            self.assertEqual(2, foundry.count("remove_strict_descendant_tree"))
            self.assertNotIn('rm -rf "${repo_dir}/cache', foundry)

    def test_local_result_save_accepts_contained_log_and_corpus_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            corpus_dir = root / "work" / "target" / "corpus"
            log_dir.mkdir(parents=True)
            corpus_dir.mkdir(parents=True)
            (log_dir / "run.log").write_text("log")
            (corpus_dir / "seed").write_text("seed")

            result = self.run_save_results(
                root,
                log_dir=log_dir,
                corpus_dir=corpus_dir,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(1, len(list((root / "output").rglob("logs.zip"))))
            self.assertEqual(1, len(list((root / "output").rglob("corpus.zip"))))

    def test_local_result_save_rejects_late_log_parent_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            log_dir = runtime / "logs"
            log_dir.mkdir(parents=True)
            target = root / "work" / "target"
            target.mkdir(parents=True)
            protected = root.parent / f"{root.name}-protected-logs"
            protected.mkdir()
            self.addCleanup(shutil.rmtree, protected, True)
            protected_logs = protected / "logs"
            protected_logs.mkdir()
            sentinel = protected_logs / "outside.log"
            sentinel.write_text("keep")
            prelude = (
                f"mv -- {shlex.quote(str(runtime))} "
                f"{shlex.quote(str(root / 'runtime-original'))}\n"
                f"ln -s -- {shlex.quote(str(protected))} "
                f"{shlex.quote(str(runtime))}"
            )

            result = self.run_save_results(
                root,
                log_dir=log_dir,
                corpus_dir=None,
                prelude=prelude,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("log directory outside allowed workspace", result.stderr)
            self.assertEqual("keep", sentinel.read_text())

    def test_local_result_save_rejects_late_corpus_parent_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            target = root / "work" / "target"
            nested = target / "nested"
            corpus_dir = nested / "corpus"
            corpus_dir.mkdir(parents=True)
            protected = root / "protected-corpus"
            protected_corpus = protected / "corpus"
            protected_corpus.mkdir(parents=True)
            sentinel = protected_corpus / "outside-seed"
            sentinel.write_text("keep")
            prelude = (
                f"mv -- {shlex.quote(str(nested))} "
                f"{shlex.quote(str(target / 'nested-original'))}\n"
                f"ln -s -- {shlex.quote(str(protected))} "
                f"{shlex.quote(str(nested))}"
            )

            result = self.run_save_results(
                root,
                log_dir=log_dir,
                corpus_dir=corpus_dir,
                prelude=prelude,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("corpus directory", result.stderr)
            self.assertEqual("keep", sentinel.read_text())

    def test_cloud_and_local_artifact_reads_recheck_anchored_paths(self):
        common = COMMON_SH.read_text()
        upload = common.split("upload_results() {", 1)[1].split(
            "save_results_local() {", 1
        )[0]
        local_save = common.split("save_results_local() {", 1)[1]
        for body in (upload, local_save):
            self.assertGreaterEqual(
                body.count('"${framework_root}" "${framework_root_identity}"'),
                2,
            )
            self.assertIn(
                '"corpus directory" "${target_root}" "${target_root_identity}"',
                body,
            )
            self.assertIn("assert_path_anchor", body)

    def test_runtime_provenance_updates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "seeds"
            source.mkdir()
            (source / "seed").write_text("seed")
            result = self.run_prepare(root, source=str(source))
            self.assertEqual(0, result.returncode, result.stderr)

            manifest = root / "manifest.json"
            manifest.write_text('{"target_commit":"abc","seed_corpus":{"source":"local://seeds"}}\n')
            env = {
                **os.environ,
                "SCFUZZBENCH_LOCAL_MODE": "1",
                "SCFUZZBENCH_ROOT": str(root),
                "SCFUZZBENCH_LOG_DIR": str(root / "logs"),
            }
            script = (
                f"source {shlex.quote(str(COMMON_SH))}\n"
                "prepare_workspace\n"
                f"record_seed_corpus_in_manifest {shlex.quote(str(manifest))}\n"
            )
            patched = subprocess.run(
                ["bash", "-c", script],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, patched.returncode, patched.stderr)
            data = json.loads(manifest.read_text())
            self.assertEqual(1, data["seed_corpus"]["file_count"])
            self.assertEqual(tree_digest(source), data["seed_corpus"]["sha256"])

    def test_manifest_update_rejects_seed_metadata_symlink_outside_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            outside = root / "outside-seed-metadata.json"
            outside.write_text('{"source":"outside"}\n')
            (logs / "seed_corpus.json").symlink_to(outside)
            manifest = root / "manifest.json"
            manifest.write_text('{"target_commit":"abc"}\n')
            env = {
                **os.environ,
                "SCFUZZBENCH_LOCAL_MODE": "1",
                "SCFUZZBENCH_ROOT": str(root),
                "SCFUZZBENCH_WORKDIR": str(root / "work"),
                "SCFUZZBENCH_LOG_DIR": str(logs),
            }
            script = (
                f"source {shlex.quote(str(COMMON_SH))}\n"
                "prepare_workspace\n"
                f"record_seed_corpus_in_manifest {shlex.quote(str(manifest))}\n"
            )

            result = subprocess.run(
                ["bash", "-c", script],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("outside allowed workspace", result.stderr)
            self.assertEqual('{"target_commit":"abc"}\n', manifest.read_text())
            self.assertEqual('{"source":"outside"}\n', outside.read_text())

    def test_canonical_manifest_is_create_once_and_byte_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            remote = root / "remote"
            remote.mkdir()
            helper = self.write_mock_manifest_aws(root)
            manifest = root / "manifest.json"
            manifest.write_text('{"target_commit":"abc"}\n')
            env = {
                **os.environ,
                "SCFUZZBENCH_LOCAL_MODE": "1",
                "SCFUZZBENCH_ROOT": str(root),
                "SCFUZZBENCH_LOG_DIR": str(root / "logs"),
                "MOCK_MANIFEST_S3": str(remote),
                "SCFUZZBENCH_MANIFEST_OBJECT_HELPER": str(helper),
            }
            command = (
                f"source {shlex.quote(str(COMMON_SH))}\n"
                "prepare_workspace\n"
                f"upload_manifest_once_or_verify {shlex.quote(str(manifest))} bucket logs/run/manifest.json\n"
            )

            first = subprocess.run(
                ["bash", "-c", command],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            identical = subprocess.run(
                ["bash", "-c", command],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            manifest.write_text('{"target_commit":"different"}\n')
            different = subprocess.run(
                ["bash", "-c", command],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, identical.returncode, identical.stderr)
            self.assertNotEqual(0, different.returncode)
            self.assertIn("different manifest already exists", different.stderr)
            self.assertEqual(
                '{"target_commit":"abc"}\n',
                (remote / "logs" / "run" / "manifest.json").read_text(),
            )


if __name__ == "__main__":
    unittest.main()
