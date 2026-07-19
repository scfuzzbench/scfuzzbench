import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON_SH = REPO_ROOT / "fuzzers" / "_shared" / "common.sh"


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
    def run_prepare(
        self,
        root: Path,
        *,
        source: str = "",
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        workdir = root / "work"
        logdir = root / "logs"
        corpus = root / "corpus"
        target = workdir / "target"
        target.mkdir(parents=True, exist_ok=True)
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
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / ".stale").write_text("old")

            result = self.run_prepare(root)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], list(corpus.iterdir()))
            self.assertFalse((root / "logs" / "seed_corpus.json").exists())

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
                (root / "corpus" / "one.json").read_bytes(),
            )
            self.assertEqual(
                (source / "nested" / "two.bin").read_bytes(),
                (root / "corpus" / "nested" / "two.bin").read_bytes(),
            )
            metadata = json.loads((root / "logs" / "seed_corpus.json").read_text())
            self.assertEqual("local://input-seeds", metadata["source"])
            self.assertEqual("local", metadata["source_type"])
            self.assertEqual(2, metadata["file_count"])
            self.assertEqual(18, metadata["size_bytes"])
            self.assertEqual(tree_digest(source), metadata["sha256"])
            self.assertEqual("recursive-byte-for-byte", metadata["copy_semantics"])

    def test_s3_prefix_is_downloaded_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_source = root / "mock-s3"
            (mock_source / "nested").mkdir(parents=True)
            (mock_source / "nested" / "seed.json").write_text("{}\n")
            mock_bin = root / "bin"
            mock_bin.mkdir()
            aws = mock_bin / "aws"
            aws.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                '[[ "$1" == "s3" && "$2" == "cp" && "$3" == s3://* ]]\n'
                'cp -a -- "${MOCK_S3_SOURCE}/." "$4/"\n'
            )
            aws.chmod(0o755)

            result = self.run_prepare(
                root,
                source="s3://seed-bucket/corpora/v1",
                extra_env={
                    "MOCK_S3_SOURCE": str(mock_source),
                    "PATH": f"{mock_bin}:{os.environ['PATH']}",
                },
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                "{}\n", (root / "corpus" / "nested" / "seed.json").read_text()
            )
            metadata = json.loads((root / "logs" / "seed_corpus.json").read_text())
            self.assertEqual("s3://seed-bucket/corpora/v1", metadata["source"])
            self.assertEqual("s3", metadata["source_type"])

    def test_source_with_symlink_is_rejected_before_corpus_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input-seeds"
            source.mkdir()
            (source / "seed").write_text("seed")
            (source / "link").symlink_to(source / "seed")
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "existing").write_text("keep until valid input is staged")

            result = self.run_prepare(root, source=str(source))

            self.assertNotEqual(0, result.returncode)
            self.assertTrue((corpus / "existing").exists())
            self.assertIn("unsupported non-regular entry", result.stderr)

    def test_every_builtin_runner_prepares_shared_seed_corpus(self):
        for fuzzer in ("echidna", "medusa", "foundry", "recon-fuzzer"):
            script = (REPO_ROOT / "fuzzers" / fuzzer / "run.sh").read_text()
            self.assertEqual(
                1,
                script.count("prepare_shared_seed_corpus"),
                f"{fuzzer} must prepare the corpus exactly once",
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


if __name__ == "__main__":
    unittest.main()
