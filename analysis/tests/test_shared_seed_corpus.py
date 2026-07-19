import hashlib
import json
import os
import shlex
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
        return subprocess.run(
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
                str(root / "snapshot"),
                "--metadata",
                str(root / "snapshot.json"),
                "--max-files",
                str(max_files),
                "--max-bytes",
                str(max_bytes),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def write_mock_manifest_aws(self, root: Path) -> Path:
        mock_bin = root / "manifest-bin"
        mock_bin.mkdir()
        aws = mock_bin / "aws"
        aws.write_text(
            """#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
remote = Path(os.environ["MOCK_MANIFEST_S3"])

def value(flag):
    return args[args.index(flag) + 1]

key = value("--key")
stored = remote.joinpath(*key.split("/"))
if args[:2] == ["s3api", "put-object"]:
    if stored.exists():
        raise SystemExit(1)
    stored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(value("--body"), stored)
elif args[:2] == ["s3api", "get-object"]:
    if not stored.exists():
        raise SystemExit(1)
    output = Path(args[-1])
    shutil.copyfile(stored, output)
else:
    raise SystemExit("unexpected args: " + repr(args))
"""
        )
        aws.chmod(0o755)
        return mock_bin

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
        script = f"source {shlex.quote(str(COMMON_SH))}\nprepare_workspace\nprepare_shared_seed_corpus\n"
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

    def test_canonical_manifest_is_create_once_and_byte_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            remote = root / "remote"
            remote.mkdir()
            mock_bin = self.write_mock_manifest_aws(root)
            manifest = root / "manifest.json"
            manifest.write_text('{"target_commit":"abc"}\n')
            env = {
                **os.environ,
                "SCFUZZBENCH_LOCAL_MODE": "1",
                "SCFUZZBENCH_ROOT": str(root),
                "SCFUZZBENCH_LOG_DIR": str(root / "logs"),
                "MOCK_MANIFEST_S3": str(remote),
                "PATH": f"{mock_bin}:{os.environ['PATH']}",
            }
            command = (
                f"source {shlex.quote(str(COMMON_SH))}\n"
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
            self.assertIn("Refusing to overwrite", different.stderr)
            self.assertEqual(
                '{"target_commit":"abc"}\n',
                (remote / "logs" / "run" / "manifest.json").read_text(),
            )


if __name__ == "__main__":
    unittest.main()
