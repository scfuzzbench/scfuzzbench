#!/usr/bin/env python3
"""Verify and install the immutable scfuzzbench EC2 bootstrap bundle."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import gzip
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any


CANONICAL_REPOSITORY = "https://github.com/scfuzzbench/scfuzzbench"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_DECLARED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 160 * 1024 * 1024
MAX_REQUIRED_FILE_BYTES = 16 * 1024 * 1024
MAX_REQUIRED_TOTAL_BYTES = 32 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class BootstrapError(RuntimeError):
    """Raised when bootstrap provenance or archive contents are unsafe."""


class _BoundedReader:
    """Forward-only reader that caps all decompressed tar bytes."""

    def __init__(self, stream: gzip.GzipFile, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.total
        if size < 0 or size > remaining + 1:
            size = remaining + 1
        data = self.stream.read(size)
        self.total += len(data)
        if self.total > self.limit:
            raise BootstrapError(
                "bootstrap archive exceeds the uncompressed byte limit"
            )
        return data


def _decode_manifest(
    encoded: str,
    expected_digest: str,
    expected_repository: str,
    expected_commit: str,
) -> dict[str, dict[str, Any]]:
    if not SHA256_RE.fullmatch(expected_digest):
        raise BootstrapError("bootstrap manifest digest is not a lowercase SHA-256")
    if expected_repository != CANONICAL_REPOSITORY:
        raise BootstrapError("bootstrap repository is not the canonical public URL")
    if not COMMIT_RE.fullmatch(expected_commit):
        raise BootstrapError("bootstrap commit is not a full lowercase SHA")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise BootstrapError("bootstrap manifest is not strict base64") from exc
    actual_digest = hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_digest:
        raise BootstrapError(
            "bootstrap manifest digest mismatch: "
            f"expected {expected_digest}, got {actual_digest}"
        )
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("bootstrap manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise BootstrapError("unsupported bootstrap manifest schema")
    if manifest.get("repository") != expected_repository:
        raise BootstrapError("bootstrap manifest repository mismatch")
    if manifest.get("commit") != expected_commit:
        raise BootstrapError("bootstrap manifest commit mismatch")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, dict) or not 1 <= len(raw_files) <= 64:
        raise BootstrapError("bootstrap manifest files must be a non-empty object")

    files: dict[str, dict[str, Any]] = {}
    destinations: set[str] = set()
    for source, metadata in raw_files.items():
        _validate_relative_path(source, "source")
        if not isinstance(metadata, dict):
            raise BootstrapError(f"invalid bootstrap metadata for {source!r}")
        if set(metadata) != {"destination", "executable", "sha256"}:
            raise BootstrapError(f"unexpected bootstrap metadata for {source!r}")
        destination = metadata.get("destination")
        digest = metadata.get("sha256")
        executable = metadata.get("executable")
        _validate_relative_path(destination, "destination")
        if destination in destinations:
            raise BootstrapError(f"duplicate bootstrap destination: {destination}")
        destinations.add(destination)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise BootstrapError(f"invalid bootstrap digest for {source!r}")
        if not isinstance(executable, bool):
            raise BootstrapError(f"invalid executable flag for {source!r}")
        files[source] = {
            "destination": destination,
            "executable": executable,
            "sha256": digest,
        }
    return files


def _validate_relative_path(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise BootstrapError(f"invalid bootstrap {label} path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BootstrapError(f"unsafe bootstrap {label} path: {value!r}")
    if str(path) != value:
        raise BootstrapError(f"non-canonical bootstrap {label} path: {value!r}")
    return path.parts


def _validated_member_name(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name or "\\" in name:
        raise BootstrapError(f"unsafe archive member name: {name!r}")
    trimmed = name[:-1] if name.endswith("/") else name
    path = PurePosixPath(trimmed)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BootstrapError(f"unsafe archive member path: {name!r}")
    if str(path) != trimmed:
        raise BootstrapError(f"non-canonical archive member path: {name!r}")
    return path.parts


def _stable_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def verify_archive(
    archive_path: Path,
    manifest_b64: str,
    manifest_sha256: str,
    repository: str,
    commit: str,
) -> dict[str, tuple[dict[str, Any], bytes]]:
    """Return verified file bytes only after the whole archive passes."""
    files = _decode_manifest(
        manifest_b64, manifest_sha256, repository, commit
    )
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        archive_fd = os.open(archive_path, flags)
    except OSError as exc:
        raise BootstrapError("cannot open bootstrap archive without symlinks") from exc
    try:
        before = os.fstat(archive_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_ARCHIVE_BYTES
        ):
            raise BootstrapError("bootstrap archive is not a bounded private file")

        with os.fdopen(os.dup(archive_fd), "rb") as archive_file:
            try:
                compressed = gzip.GzipFile(fileobj=archive_file, mode="rb")
                bounded = _BoundedReader(
                    compressed, MAX_ARCHIVE_UNCOMPRESSED_BYTES
                )
                archive = tarfile.open(fileobj=bounded, mode="r|")
            except (gzip.BadGzipFile, tarfile.TarError, OSError) as exc:
                raise BootstrapError("bootstrap archive is not a valid gzip tar") from exc
            with compressed, archive:
                seen_names: set[str] = set()
                top_level: str | None = None
                declared_file_bytes = 0
                member_count = 0
                required_bytes = 0
                verified: dict[str, tuple[dict[str, Any], bytes]] = {}

                for member in archive:
                    member_count += 1
                    if member_count > MAX_ARCHIVE_MEMBERS:
                        raise BootstrapError(
                            "bootstrap archive member count exceeds the limit"
                        )
                    parts = _validated_member_name(member.name)
                    normalized = "/".join(parts)
                    if normalized in seen_names:
                        raise BootstrapError(
                            f"duplicate archive member: {normalized}"
                        )
                    seen_names.add(normalized)
                    if top_level is None:
                        top_level = parts[0]
                    elif parts[0] != top_level:
                        raise BootstrapError(
                            "bootstrap archive must have one top-level directory"
                        )

                    if member.isdir():
                        if member.size != 0:
                            raise BootstrapError(
                                f"archive directory has a payload: {member.name!r}"
                            )
                        continue
                    if not member.isfile():
                        raise BootstrapError(
                            f"unsupported archive member type: {member.name!r}"
                        )
                    if member.size < 0 or member.size > MAX_REQUIRED_FILE_BYTES:
                        raise BootstrapError(
                            f"oversized archive member: {member.name!r}"
                        )
                    declared_file_bytes += member.size
                    if declared_file_bytes > MAX_ARCHIVE_DECLARED_BYTES:
                        raise BootstrapError(
                            "archive declared regular-file bytes exceed the limit"
                        )
                    if len(parts) < 2:
                        continue
                    source = "/".join(parts[1:])
                    metadata = files.get(source)
                    if metadata is None:
                        continue
                    if source in verified:
                        raise BootstrapError(
                            f"duplicate required bootstrap file: {source}"
                        )
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise BootstrapError(
                            f"cannot read required bootstrap file: {source}"
                        )
                    with extracted:
                        contents = extracted.read(MAX_REQUIRED_FILE_BYTES + 1)
                    if len(contents) != member.size:
                        raise BootstrapError(
                            f"required bootstrap file changed while reading: {source}"
                        )
                    required_bytes += len(contents)
                    if required_bytes > MAX_REQUIRED_TOTAL_BYTES:
                        raise BootstrapError(
                            "required bootstrap files exceed the total byte limit"
                        )
                    digest = hashlib.sha256(contents).hexdigest()
                    if digest != metadata["sha256"]:
                        raise BootstrapError(
                            f"bootstrap file digest mismatch for {source}: "
                            f"expected {metadata['sha256']}, got {digest}"
                        )
                    verified[source] = (metadata, contents)

                if member_count == 0 or top_level is None:
                    raise BootstrapError("bootstrap archive is empty")
                missing = sorted(set(files) - set(verified))
                if missing:
                    raise BootstrapError(
                        f"required bootstrap file is missing: {missing[0]}"
                    )

        after = os.fstat(archive_fd)
        if not _stable_stat(before, after):
            raise BootstrapError("bootstrap archive changed while being verified")
        return verified
    finally:
        os.close(archive_fd)


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
FILE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
if hasattr(os, "O_NOFOLLOW"):
    DIRECTORY_FLAGS |= os.O_NOFOLLOW
    FILE_CREATE_FLAGS |= os.O_NOFOLLOW


def _open_or_create_directories(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for component in parts:
            try:
                os.mkdir(component, mode=0o755, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _write_all(fd: int, contents: bytes) -> None:
    view = memoryview(contents)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise BootstrapError("short write while installing bootstrap file")
        view = view[written:]


def _remove_tree_at(parent_fd: int, name: str) -> None:
    child_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        with os.scandir(child_fd) as iterator:
            entries = list(iterator)
        for entry in entries:
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode):
                _remove_tree_at(child_fd, entry.name)
            else:
                os.unlink(entry.name, dir_fd=child_fd)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _rename_noreplace(directory_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BootstrapError("renameat2 is required for no-replace publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        1,  # RENAME_NOREPLACE
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise BootstrapError("bootstrap destination already exists")
        raise OSError(error, os.strerror(error), destination)


def install_verified_files(
    destination: Path,
    verified: dict[str, tuple[dict[str, Any], bytes]],
) -> None:
    destination_text = os.path.abspath(destination)
    destination = Path(destination_text)
    if destination.name in {"", ".", ".."}:
        raise BootstrapError("invalid bootstrap destination")
    parent = destination.parent
    if os.path.realpath(parent) != os.fspath(parent):
        raise BootstrapError("bootstrap destination parent contains a symlink")
    parent_before = os.stat(parent, follow_symlinks=False)
    if not stat.S_ISDIR(parent_before.st_mode):
        raise BootstrapError("bootstrap destination parent is not a directory")
    parent_fd = os.open(parent, DIRECTORY_FLAGS)
    stage_name = f".scfuzzbench-bootstrap-{secrets.token_hex(12)}"
    published = False
    created_stage = False
    try:
        parent_open = os.fstat(parent_fd)
        if (
            parent_before.st_dev,
            parent_before.st_ino,
        ) != (
            parent_open.st_dev,
            parent_open.st_ino,
        ):
            raise BootstrapError("bootstrap destination parent changed")
        os.mkdir(stage_name, mode=0o700, dir_fd=parent_fd)
        created_stage = True
        stage_fd = os.open(stage_name, DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            for _, (metadata, contents) in sorted(verified.items()):
                parts = _validate_relative_path(
                    metadata["destination"], "destination"
                )
                output_parent_fd = _open_or_create_directories(
                    stage_fd, parts[:-1]
                )
                try:
                    output_fd = os.open(
                        parts[-1],
                        FILE_CREATE_FLAGS,
                        0o600,
                        dir_fd=output_parent_fd,
                    )
                    try:
                        _write_all(output_fd, contents)
                        os.fchmod(
                            output_fd,
                            0o755 if metadata["executable"] else 0o644,
                        )
                        os.fsync(output_fd)
                    finally:
                        os.close(output_fd)
                finally:
                    os.close(output_parent_fd)
            os.fchmod(stage_fd, 0o755)
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        _rename_noreplace(parent_fd, stage_name, destination.name)
        published = True
        os.fsync(parent_fd)
    finally:
        if created_stage and not published:
            try:
                _remove_tree_at(parent_fd, stage_name)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest-b64", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install")
    _common_arguments(install)
    install.add_argument("--archive", type=Path, required=True)
    install.add_argument("--destination", type=Path, required=True)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "install":
            verified = verify_archive(
                args.archive,
                args.manifest_b64,
                args.manifest_sha256,
                args.repository,
                args.commit,
            )
            install_verified_files(args.destination, verified)
        else:
            raise BootstrapError(f"unsupported command: {args.command}")
    except (
        BootstrapError,
        OSError,
        tarfile.TarError,
    ) as exc:
        print(f"bootstrap verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
