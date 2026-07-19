import importlib.util
import io
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


def load_extractor():
    script_path = Path(__file__).resolve().parents[2] / "fuzzers" / "echidna" / "extract_ci_artifact.py"
    spec = importlib.util.spec_from_file_location("extract_ci_artifact", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def tar_payload(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
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
    return stream.getvalue()


class ExtractEchidnaArtifactTests(unittest.TestCase):
    def setUp(self):
        self.module = load_extractor()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_zip(self, members):
        artifact = self.root / "artifact.zip"
        with zipfile.ZipFile(artifact, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for member in members:
                if isinstance(member, zipfile.ZipInfo):
                    archive.writestr(member, b"link target")
                else:
                    name, content = member
                    archive.writestr(name, content)
        return artifact

    def test_extracts_single_binary_from_nested_tar(self):
        elf = bytearray(64)
        elf[:4] = b"\x7fELF"
        elf[4] = 2
        elf[5] = 1
        elf[18:20] = (62).to_bytes(2, byteorder="little")
        nested = tar_payload([("bin/echidna", bytes(elf), "file")])
        artifact = self.write_zip([("echidna.tar.gz", nested)])

        binary = self.module.extract_echidna(artifact, self.root / "out")

        self.assertEqual("echidna", binary.name)
        self.assertEqual(bytes(elf), binary.read_bytes())

    def test_rejects_non_elf_binary(self):
        nested = tar_payload([("echidna", b"not an executable", "file")])
        artifact = self.write_zip([("echidna.tar.gz", nested)])

        with self.assertRaisesRegex(self.module.ArtifactError, "not an ELF"):
            self.module.extract_echidna(artifact, self.root / "out")

    def test_rejects_zip_path_traversal(self):
        artifact = self.write_zip([("../echidna", b"binary")])

        with self.assertRaisesRegex(self.module.ArtifactError, "unsafe archive path"):
            self.module.extract_echidna(artifact, self.root / "out")

    def test_rejects_zip_symlink(self):
        link = zipfile.ZipInfo("echidna")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        artifact = self.write_zip([link])

        with self.assertRaisesRegex(self.module.ArtifactError, "links are not allowed"):
            self.module.extract_echidna(artifact, self.root / "out")

    def test_rejects_nested_tar_symlink(self):
        nested = tar_payload([("echidna", b"elsewhere", "symlink")])
        artifact = self.write_zip([("echidna.tar.gz", nested)])

        with self.assertRaisesRegex(self.module.ArtifactError, "links and special files"):
            self.module.extract_echidna(artifact, self.root / "out")

    def test_rejects_multiple_binary_candidates(self):
        nested = tar_payload(
            [
                ("bin/echidna", b"one", "file"),
                ("legacy/echidna-test", b"two", "file"),
            ]
        )
        artifact = self.write_zip([("echidna.tar.gz", nested)])

        with self.assertRaisesRegex(self.module.ArtifactError, "multiple Echidna binaries"):
            self.module.extract_echidna(artifact, self.root / "out")

    def test_enforces_expanded_size_cap(self):
        artifact = self.write_zip([("echidna", b"x" * 300)])

        with self.assertRaisesRegex(self.module.ArtifactError, "expands beyond"):
            self.module.extract_echidna(artifact, self.root / "out", max_bytes=200)


if __name__ == "__main__":
    unittest.main()
