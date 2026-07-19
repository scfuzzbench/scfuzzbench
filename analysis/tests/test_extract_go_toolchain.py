import importlib.util
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


def load_extractor():
    path = Path(__file__).resolve().parents[2] / "fuzzers" / "medusa" / "extract_go_toolchain.py"
    spec = importlib.util.spec_from_file_location("extract_go_toolchain", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.mode = 0o755
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = content.decode()
                archive.addfile(info)
            else:
                raise ValueError(kind)


class ExtractGoToolchainTests(unittest.TestCase):
    def setUp(self):
        self.module = load_extractor()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.archive = self.root / "go.tar.gz"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_extracts_regular_go_binary(self):
        write_archive(self.archive, [("go/bin/go", b"binary", "file")])

        binary = self.module.extract_go_toolchain(self.archive, self.root / "out")

        self.assertEqual(b"binary", binary.read_bytes())

    def test_rejects_traversal_and_non_go_root(self):
        write_archive(self.archive, [("other/go", b"binary", "file")])

        with self.assertRaisesRegex(self.module.ToolchainArchiveError, "outside go/"):
            self.module.extract_go_toolchain(self.archive, self.root / "out")

    def test_rejects_links(self):
        write_archive(self.archive, [("go/bin/go", b"elsewhere", "symlink")])

        with self.assertRaisesRegex(self.module.ToolchainArchiveError, "links and special"):
            self.module.extract_go_toolchain(self.archive, self.root / "out")

    def test_enforces_entry_cap(self):
        write_archive(
            self.archive,
            [
                ("go/a", b"a", "file"),
                ("go/bin/go", b"binary", "file"),
            ],
        )

        with self.assertRaisesRegex(self.module.ToolchainArchiveError, "too many entries"):
            self.module.extract_go_toolchain(
                self.archive,
                self.root / "out",
                max_entries=1,
            )

    def test_enforces_depth_and_expanded_size_caps(self):
        write_archive(self.archive, [("go/a/b/c/bin/go", b"123456", "file")])
        with self.assertRaisesRegex(self.module.ToolchainArchiveError, "maximum depth"):
            self.module.extract_go_toolchain(
                self.archive,
                self.root / "depth",
                max_depth=4,
            )

        write_archive(self.archive, [("go/bin/go", b"123456", "file")])
        with self.assertRaisesRegex(self.module.ToolchainArchiveError, "expands beyond"):
            self.module.extract_go_toolchain(
                self.archive,
                self.root / "size",
                max_bytes=5,
            )


if __name__ == "__main__":
    unittest.main()
