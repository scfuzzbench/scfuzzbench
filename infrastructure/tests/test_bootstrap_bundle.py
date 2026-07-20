#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "infrastructure" / "bootstrap_bundle.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_bundle", MODULE_PATH)
assert SPEC and SPEC.loader
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
sys.modules["bootstrap_bundle"] = bootstrap
GUARD_PATH = REPO_ROOT / "infrastructure" / "bootstrap_source_guard.py"
GUARD_SPEC = importlib.util.spec_from_file_location(
    "bootstrap_source_guard", GUARD_PATH
)
assert GUARD_SPEC and GUARD_SPEC.loader
source_guard = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(source_guard)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "https://github.com/scfuzzbench/scfuzzbench"


def manifest_for(
    files: dict[str, tuple[str, bytes, bool]],
) -> tuple[str, str]:
    payload = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "commit": COMMIT,
        "files": {
            source: {
                "destination": destination,
                "executable": executable,
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
            for source, (destination, contents, executable) in files.items()
        },
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()


def add_regular(
    archive: tarfile.TarFile,
    name: str,
    contents: bytes,
    *,
    mode: int = 0o644,
) -> None:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.size = len(contents)
    archive.addfile(info, io.BytesIO(contents))


def create_archive(
    path: Path,
    files: dict[str, bytes],
    *,
    extra_members: list[tarfile.TarInfo] | None = None,
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        root = tarfile.TarInfo("scfuzzbench-source/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for name, contents in files.items():
            add_regular(archive, f"scfuzzbench-source/{name}", contents)
        for member in extra_members or []:
            payload = (
                io.BytesIO(b"x" * member.size)
                if member.isfile() and member.size
                else None
            )
            archive.addfile(member, payload)


class BootstrapBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.files = {
            "fuzzers/_shared/common.sh": ("common.sh", b"#!/bin/sh\n", True),
            "scripts/helper.py": ("helpers/helper.py", b"print('ok')\n", False),
        }
        self.manifest_b64, self.manifest_sha256 = manifest_for(self.files)
        self.archive = self.root / "source.tar.gz"
        create_archive(
            self.archive,
            {
                source: contents
                for source, (_, contents, _) in self.files.items()
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def verify(self, archive: Path | None = None):
        return bootstrap.verify_archive(
            archive or self.archive,
            self.manifest_b64,
            self.manifest_sha256,
            REPOSITORY,
            COMMIT,
        )

    def test_valid_archive_verifies_then_installs_atomically(self):
        verified = self.verify()
        destination = self.root / "install"
        bootstrap.install_verified_files(destination, verified)

        self.assertEqual(b"#!/bin/sh\n", (destination / "common.sh").read_bytes())
        self.assertEqual(
            b"print('ok')\n",
            (destination / "helpers" / "helper.py").read_bytes(),
        )
        self.assertTrue((destination / "common.sh").stat().st_mode & stat.S_IXUSR)
        self.assertFalse(
            (destination / "helpers" / "helper.py").stat().st_mode & stat.S_IXUSR
        )
        self.assertEqual([], list(self.root.glob(".scfuzzbench-bootstrap-*")))

    def test_manifest_base64_and_digest_fail_closed(self):
        with self.assertRaisesRegex(bootstrap.BootstrapError, "strict base64"):
            bootstrap.verify_archive(
                self.archive,
                "%%%",
                self.manifest_sha256,
                REPOSITORY,
                COMMIT,
            )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "digest mismatch"):
            bootstrap.verify_archive(
                self.archive,
                self.manifest_b64,
                "0" * 64,
                REPOSITORY,
                COMMIT,
            )

    def test_missing_and_digest_mismatch_write_nothing(self):
        missing = self.root / "missing.tar.gz"
        create_archive(missing, {"fuzzers/_shared/common.sh": b"#!/bin/sh\n"})
        with self.assertRaisesRegex(bootstrap.BootstrapError, "missing"):
            self.verify(missing)

        mismatch = self.root / "mismatch.tar.gz"
        create_archive(
            mismatch,
            {
                "fuzzers/_shared/common.sh": b"changed\n",
                "scripts/helper.py": b"print('ok')\n",
            },
        )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "digest mismatch"):
            self.verify(mismatch)
        self.assertFalse((self.root / "install").exists())

    def test_rejects_traversal_links_special_files_and_duplicate_names(self):
        cases: list[tuple[str, tarfile.TarInfo, str]] = []

        traversal = tarfile.TarInfo("scfuzzbench-source/../escape")
        traversal.size = 1
        cases.append(("traversal", traversal, "unsafe archive member"))

        symlink = tarfile.TarInfo("scfuzzbench-source/link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/etc/passwd"
        cases.append(("symlink", symlink, "unsupported archive member type"))

        hardlink = tarfile.TarInfo("scfuzzbench-source/hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "scfuzzbench-source/fuzzers/_shared/common.sh"
        cases.append(("hardlink", hardlink, "unsupported archive member type"))

        fifo = tarfile.TarInfo("scfuzzbench-source/fifo")
        fifo.type = tarfile.FIFOTYPE
        cases.append(("fifo", fifo, "unsupported archive member type"))

        duplicate = tarfile.TarInfo(
            "scfuzzbench-source/fuzzers/_shared/common.sh"
        )
        duplicate.size = len(b"#!/bin/sh\n")
        cases.append(("duplicate", duplicate, "duplicate archive member"))

        for label, member, message in cases:
            with self.subTest(label=label):
                path = self.root / f"{label}.tar.gz"
                create_archive(
                    path,
                    {
                        source: contents
                        for source, (_, contents, _) in self.files.items()
                    },
                    extra_members=[member],
                )
                with self.assertRaisesRegex(bootstrap.BootstrapError, message):
                    self.verify(path)

    def test_rejects_nonzero_directory_and_declared_payload_limit(self):
        directory = tarfile.TarInfo("scfuzzbench-source/payload-dir/")
        directory.type = tarfile.DIRTYPE
        directory.size = 1
        path = self.root / "directory-payload.tar.gz"
        create_archive(
            path,
            {
                source: contents
                for source, (_, contents, _) in self.files.items()
            },
            extra_members=[directory],
        )
        with self.assertRaisesRegex(bootstrap.BootstrapError, "directory has a payload"):
            self.verify(path)

        bounded = self.root / "bounded.tar.gz"
        create_archive(
            bounded,
            {
                "fuzzers/_shared/common.sh": b"#!/bin/sh\n",
                "scripts/helper.py": b"print('ok')\n",
                "unneeded-a": b"a" * 32,
                "unneeded-b": b"b" * 32,
            },
        )
        with mock.patch.object(bootstrap, "MAX_ARCHIVE_DECLARED_BYTES", 40):
            with self.assertRaisesRegex(
                bootstrap.BootstrapError, "declared regular-file bytes"
            ):
                self.verify(bounded)

    def test_caps_all_decompressed_tar_bytes_while_streaming(self):
        with mock.patch.object(bootstrap, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 700):
            with self.assertRaisesRegex(
                bootstrap.BootstrapError, "uncompressed byte limit"
            ):
                self.verify()

    def test_atomic_publication_never_replaces_existing_destination(self):
        verified = self.verify()
        destination = self.root / "install"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_text("outside", encoding="utf-8")

        with self.assertRaisesRegex(bootstrap.BootstrapError, "already exists"):
            bootstrap.install_verified_files(destination, verified)
        self.assertEqual("outside", sentinel.read_text(encoding="utf-8"))
        self.assertEqual([], list(self.root.glob(".scfuzzbench-bootstrap-*")))

    def test_destination_inserted_at_publication_is_never_replaced(self):
        verified = self.verify()
        destination = self.root / "install"
        original_rename = bootstrap._rename_noreplace

        def insert_destination(
            directory_fd: int, source: str, destination_name: str
        ) -> None:
            os.mkdir(destination_name, mode=0o700, dir_fd=directory_fd)
            destination_fd = os.open(
                destination_name,
                bootstrap.DIRECTORY_FLAGS,
                dir_fd=directory_fd,
            )
            try:
                sentinel_fd = os.open(
                    "sentinel",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=destination_fd,
                )
                try:
                    os.write(sentinel_fd, b"inserted")
                finally:
                    os.close(sentinel_fd)
            finally:
                os.close(destination_fd)
            original_rename(directory_fd, source, destination_name)

        with mock.patch.object(
            bootstrap, "_rename_noreplace", side_effect=insert_destination
        ):
            with self.assertRaisesRegex(
                bootstrap.BootstrapError, "already exists"
            ):
                bootstrap.install_verified_files(destination, verified)

        self.assertEqual(
            b"inserted", (destination / "sentinel").read_bytes()
        )
        self.assertEqual([], list(self.root.glob(".scfuzzbench-bootstrap-*")))

    def test_archive_input_must_be_private_regular_and_bounded(self):
        symlink = self.root / "archive-symlink.tar.gz"
        symlink.symlink_to(self.archive)
        hardlink = self.root / "archive-hardlink.tar.gz"
        os.link(self.archive, hardlink)
        oversized = self.root / "oversized.tar.gz"
        with oversized.open("wb") as handle:
            handle.truncate(bootstrap.MAX_ARCHIVE_BYTES + 1)
        fifo = self.root / "archive.fifo"
        os.mkfifo(fifo)

        cases = (
            (symlink, "cannot open bootstrap archive"),
            (hardlink, "bounded private file"),
            (oversized, "bounded private file"),
            (fifo, "bounded private file"),
        )
        for archive, message in cases:
            with self.subTest(archive=archive.name):
                with self.assertRaisesRegex(bootstrap.BootstrapError, message):
                    self.verify(archive)

    def test_main_manifest_has_the_exact_runtime_file_set(self):
        main = (REPO_ROOT / "infrastructure" / "main.tf").read_text(
            encoding="utf-8"
        )
        block = main.split("bootstrap_file_destinations = {", 1)[1].split(
            "\n  }", 1
        )[0]
        entries = dict(
            re.findall(r'^\s*"([^"]+)"\s*=\s*"([^"]+)"', block, re.MULTILINE)
        )
        expected = {
            "fuzzers/_shared/common.sh": "common.sh",
            "fuzzers/_shared/prepare_seed_corpus.py": "prepare_seed_corpus.py",
            "fuzzers/_shared/safe_path_ops.py": "safe_path_ops.py",
            "fuzzers/_shared/put_manifest_once.py": "put_manifest_once.py",
            "fuzzers/_shared/upload_pinned_file.py": "upload_pinned_file.py",
            "infrastructure/bootstrap_bundle.py": "bootstrap_bundle.py",
            "infrastructure/bootstrap_source_guard.py": (
                "provenance/bootstrap_source_guard.py"
            ),
            "infrastructure/user_data.sh.tftpl": (
                "provenance/user_data.sh.tftpl"
            ),
            "scripts/preliminary_snapshot.py": "preliminary_snapshot.py",
            "fuzzers/echidna/install.sh": "fuzzers/echidna/install.sh",
            "fuzzers/echidna/run.sh": "fuzzers/echidna/run.sh",
            "fuzzers/echidna/extract_ci_artifact.py": (
                "extract_echidna_ci_artifact.py"
            ),
            "fuzzers/medusa/install.sh": "fuzzers/medusa/install.sh",
            "fuzzers/medusa/run.sh": "fuzzers/medusa/run.sh",
            "fuzzers/medusa/extract_go_toolchain.py": (
                "extract_go_toolchain.py"
            ),
            "fuzzers/foundry/install.sh": "fuzzers/foundry/install.sh",
            "fuzzers/foundry/run.sh": "fuzzers/foundry/run.sh",
            "fuzzers/foundry/throughput-progress.patch": (
                "foundry-throughput-progress.patch"
            ),
            "fuzzers/recon-fuzzer/install.sh": (
                "fuzzers/recon-fuzzer/install.sh"
            ),
            "fuzzers/recon-fuzzer/run.sh": "fuzzers/recon-fuzzer/run.sh",
        }
        self.assertEqual(expected, entries)
        for source in entries:
            self.assertTrue((REPO_ROOT / source).is_file(), source)

    def test_user_data_keeps_emergency_poweroff_armed_through_verification(self):
        template = (
            REPO_ROOT / "infrastructure" / "user_data.sh.tftpl"
        ).read_text(encoding="utf-8")
        initial_trap = template.index("trap emergency_poweroff EXIT")
        cleanup_trap = template.index(
            "trap 'cleanup_bootstrap || true; emergency_poweroff' EXIT"
        )
        installer_download = template.index(
            'download_https "$${bootstrap_installer_url}"'
        )
        digest_failure = template.index(
            "Downloaded bootstrap verifier digest mismatch"
        )
        archive_download = template.index('download_https "$${bootstrap_url}"')
        verified_install = template.index(
            'python3 "$${bootstrap_installer}" install'
        )
        normal_trap = template.index("trap 'shutdown_instance' EXIT")

        self.assertLess(initial_trap, cleanup_trap)
        self.assertLess(cleanup_trap, installer_download)
        self.assertLess(installer_download, digest_failure)
        self.assertLess(digest_failure, archive_download)
        self.assertLess(archive_download, verified_install)
        self.assertLess(verified_install, normal_trap)

        harness = subprocess.run(
            [
                "bash",
                "-c",
                "set -e; "
                "cleanup_bootstrap() { return 1; }; "
                "emergency_poweroff() { printf POWERED_OFF; }; "
                "trap 'cleanup_bootstrap || true; emergency_poweroff' EXIT; "
                "false",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, harness.returncode)
        self.assertEqual("POWERED_OFF", harness.stdout)

    def test_source_guard_curl_has_transport_and_size_bounds(self):
        output = self.root / "download.tar.gz"
        with mock.patch.object(subprocess, "run") as run:
            source_guard._download_archive(
                "https://codeload.github.com/example/archive", output
            )
        command = run.call_args.args[0]
        self.assertIn("--proto", command)
        self.assertIn("=https", command)
        self.assertIn("--proto-redir", command)
        self.assertIn("--max-filesize", command)
        size_index = command.index("--max-filesize") + 1
        self.assertEqual(
            str(bootstrap.MAX_ARCHIVE_BYTES), command[size_index]
        )
        self.assertEqual(os.fspath(output), command[command.index("--output") + 1])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_source_guard_works_without_git_and_rejects_local_drift(self):
        checkout = self.root / "checkout"
        for source, (_, contents, _) in self.files.items():
            path = checkout / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        self.assertFalse((checkout / ".git").exists())

        def copy_archive(_url: str, output: Path) -> None:
            shutil.copyfile(self.archive, output)

        with mock.patch.object(source_guard, "_download_archive", copy_archive):
            source_guard.verify_published_source(
                checkout,
                self.manifest_b64,
                self.manifest_sha256,
                REPOSITORY,
                COMMIT,
            )

            (checkout / "scripts/helper.py").write_text(
                "print('dirty')\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                bootstrap.BootstrapError, "local bootstrap source digest mismatch"
            ):
                source_guard.verify_published_source(
                    checkout,
                    self.manifest_b64,
                    self.manifest_sha256,
                    REPOSITORY,
                    COMMIT,
                )


if __name__ == "__main__":
    unittest.main()
