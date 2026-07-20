#!/usr/bin/env python3
"""Verify that Terraform's local bootstrap bundle exists at the exact commit."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import bootstrap_bundle


def _download_archive(url: str, output: Path) -> None:
    subprocess.run(
        [
            "curl",
            "--fail",
            "--show-error",
            "--silent",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--tlsv1.2",
            "--retry",
            "4",
            "--retry-all-errors",
            "--connect-timeout",
            "15",
            "--max-time",
            "300",
            "--max-filesize",
            str(bootstrap_bundle.MAX_ARCHIVE_BYTES),
            "--output",
            os.fspath(output),
            url,
        ],
        check=True,
    )


def _open_source(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            child = os.open(
                component,
                bootstrap_bundle.DIRECTORY_FLAGS,
                dir_fd=current,
            )
            os.close(current)
            current = child
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return os.open(parts[-1], flags, dir_fd=current)
    finally:
        os.close(current)


def verify_published_source(
    repository_root: Path,
    manifest_b64: str,
    manifest_sha256: str,
    repository: str,
    commit: str,
) -> None:
    files = bootstrap_bundle._decode_manifest(
        manifest_b64, manifest_sha256, repository, commit
    )
    repository_root = repository_root.resolve(strict=True)
    root_fd = os.open(repository_root, bootstrap_bundle.DIRECTORY_FLAGS)
    try:
        for source, metadata in files.items():
            parts = bootstrap_bundle._validate_relative_path(source, "source")
            try:
                source_fd = _open_source(root_fd, parts)
            except OSError as exc:
                raise bootstrap_bundle.BootstrapError(
                    f"cannot open local bootstrap source without symlinks: {source}"
                ) from exc
            try:
                before = os.fstat(source_fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise bootstrap_bundle.BootstrapError(
                        "local bootstrap source is not a private regular file: "
                        f"{source}"
                    )
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                after = os.fstat(source_fd)
                if not bootstrap_bundle._stable_stat(before, after):
                    raise bootstrap_bundle.BootstrapError(
                        f"local bootstrap source changed while reading: {source}"
                    )
                if digest.hexdigest() != metadata["sha256"]:
                    raise bootstrap_bundle.BootstrapError(
                        f"local bootstrap source digest mismatch: {source}"
                    )
            finally:
                os.close(source_fd)
    finally:
        os.close(root_fd)

    archive_url = (
        "https://codeload.github.com/scfuzzbench/scfuzzbench/tar.gz/"
        f"{commit}"
    )
    with tempfile.TemporaryDirectory(prefix="scfuzzbench-source-check-") as tmp:
        archive_path = Path(tmp) / "source.tar.gz"
        _download_archive(archive_url, archive_path)
        bootstrap_bundle.verify_archive(
            archive_path,
            manifest_b64,
            manifest_sha256,
            repository,
            commit,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--manifest-b64", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        verify_published_source(
            args.repository_root,
            args.manifest_b64,
            args.manifest_sha256,
            args.repository,
            args.commit,
        )
    except (
        bootstrap_bundle.BootstrapError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"bootstrap source verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
