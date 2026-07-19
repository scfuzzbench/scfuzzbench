#!/usr/bin/env python3
"""Create a bounded, immutable snapshot of a local or S3 seed corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any


CHUNK_SIZE = 1024 * 1024
DIGEST_ALGORITHM = "sha256-framed-path-size-content-v1"


class SeedCorpusError(RuntimeError):
    pass


def _strict_utf8(value: str, *, label: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SeedCorpusError(f"{label} is not valid UTF-8") from exc


def _entry_identity(st: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_nlink,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise SeedCorpusError(
            f"seed corpus root must be a real directory (not a symlink): {path}"
        ) from exc


def _directory_entries(directory_fd: int) -> list[tuple[str, os.stat_result]]:
    entries: list[tuple[str, os.stat_result]] = []
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            name = entry.name
            _strict_utf8(name, label=f"seed corpus path component {name!r}")
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise SeedCorpusError(f"unsafe seed corpus path component: {name!r}")
            st = entry.stat(follow_symlinks=False)
            entries.append((name, st))
    entries.sort(key=lambda item: _strict_utf8(item[0], label="seed corpus path"))
    return entries


def snapshot_local_tree(
    source: Path,
    destination: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Copy source without following links and return exact byte/path provenance."""

    if destination.exists() or destination.is_symlink():
        raise SeedCorpusError(f"snapshot destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)

    files: list[dict[str, Any]] = []
    total_bytes = 0
    tree_digest = hashlib.sha256()
    root_fd: int | None = None

    def copy_directory(source_fd: int, relative_parts: tuple[str, ...]) -> None:
        nonlocal total_bytes
        before = _directory_entries(source_fd)

        for name, listed_stat in before:
            relative = PurePosixPath(*relative_parts, name)
            relative_text = relative.as_posix()
            relative_bytes = _strict_utf8(relative_text, label="seed corpus path")
            destination_path = destination.joinpath(*relative.parts)
            mode = listed_stat.st_mode

            if stat.S_ISLNK(mode):
                raise SeedCorpusError(
                    f"seed corpus contains a symlink: {relative_text}"
                )
            if stat.S_ISDIR(mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    child_fd = os.open(name, flags, dir_fd=source_fd)
                except OSError as exc:
                    raise SeedCorpusError(
                        f"seed corpus directory changed while copying: {relative_text}"
                    ) from exc
                try:
                    if _entry_identity(os.fstat(child_fd)) != _entry_identity(listed_stat):
                        raise SeedCorpusError(
                            f"seed corpus directory changed while copying: {relative_text}"
                        )
                    destination_path.mkdir(mode=0o700)
                    copy_directory(child_fd, (*relative_parts, name))
                    if _entry_identity(os.fstat(child_fd)) != _entry_identity(listed_stat):
                        raise SeedCorpusError(
                            f"seed corpus directory changed while copying: {relative_text}"
                        )
                finally:
                    os.close(child_fd)
                continue

            if not stat.S_ISREG(mode):
                raise SeedCorpusError(
                    f"seed corpus contains an unsupported special entry: {relative_text}"
                )
            if listed_stat.st_nlink != 1:
                raise SeedCorpusError(
                    f"seed corpus contains a hard-linked file: {relative_text}"
                )
            if len(files) + 1 > max_files:
                raise SeedCorpusError(
                    f"seed corpus exceeds the fixed {max_files}-file limit"
                )
            if total_bytes + listed_stat.st_size > max_bytes:
                raise SeedCorpusError(
                    f"seed corpus exceeds the fixed {max_bytes}-byte limit"
                )

            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                source_file_fd = os.open(name, flags, dir_fd=source_fd)
            except OSError as exc:
                raise SeedCorpusError(
                    f"seed corpus file changed while copying: {relative_text}"
                ) from exc

            file_digest = hashlib.sha256()
            bytes_copied = 0
            try:
                if _entry_identity(os.fstat(source_file_fd)) != _entry_identity(listed_stat):
                    raise SeedCorpusError(
                        f"seed corpus file changed while copying: {relative_text}"
                    )
                destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination_fd = os.open(
                    destination_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    while True:
                        chunk = os.read(source_file_fd, CHUNK_SIZE)
                        if not chunk:
                            break
                        bytes_copied += len(chunk)
                        if total_bytes + bytes_copied > max_bytes:
                            raise SeedCorpusError(
                                f"seed corpus exceeds the fixed {max_bytes}-byte limit"
                            )
                        file_digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(destination_fd, view)
                            view = view[written:]
                finally:
                    os.close(destination_fd)

                if (
                    bytes_copied != listed_stat.st_size
                    or _entry_identity(os.fstat(source_file_fd))
                    != _entry_identity(listed_stat)
                ):
                    raise SeedCorpusError(
                        f"seed corpus file changed while copying: {relative_text}"
                    )
            finally:
                os.close(source_file_fd)

            total_bytes += bytes_copied
            digest_hex = file_digest.hexdigest()
            tree_digest.update(len(relative_bytes).to_bytes(8, "big"))
            tree_digest.update(relative_bytes)
            tree_digest.update(bytes_copied.to_bytes(8, "big"))
            with destination_path.open("rb") as copied:
                for chunk in iter(lambda: copied.read(CHUNK_SIZE), b""):
                    tree_digest.update(chunk)
            files.append(
                {
                    "path": relative_text,
                    "size_bytes": bytes_copied,
                    "sha256": digest_hex,
                }
            )

        after = _directory_entries(source_fd)
        before_identity = {
            name: _entry_identity(entry_stat) for name, entry_stat in before
        }
        after_identity = {
            name: _entry_identity(entry_stat) for name, entry_stat in after
        }
        if before_identity != after_identity:
            relative_dir = PurePosixPath(*relative_parts).as_posix() or "."
            raise SeedCorpusError(
                f"seed corpus directory changed while copying: {relative_dir}"
            )

    try:
        root_fd = _open_directory(source)
        copy_directory(root_fd, ())
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)

    if not files:
        shutil.rmtree(destination, ignore_errors=True)
        raise SeedCorpusError("configured seed corpus is empty")

    return {
        "file_count": len(files),
        "size_bytes": total_bytes,
        "sha256": tree_digest.hexdigest(),
        "files": files,
    }


def _aws_json(args: list[str], *, attempts: int = 5) -> dict[str, Any]:
    environment = {**os.environ, "AWS_PAGER": ""}
    last_error = ""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            ["aws", "--no-cli-pager", *args, "--output", "json"],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            try:
                parsed = json.loads(result.stdout or "{}")
            except json.JSONDecodeError as exc:
                raise SeedCorpusError(
                    f"AWS CLI returned invalid JSON for {' '.join(args[:2])}"
                ) from exc
            if not isinstance(parsed, dict):
                raise SeedCorpusError(
                    f"AWS CLI returned an invalid response for {' '.join(args[:2])}"
                )
            return parsed
        last_error = (result.stderr or result.stdout).strip()[-1000:]
        if attempt < attempts:
            time.sleep(min(5 * attempt, 20))
    raise SeedCorpusError(
        f"AWS CLI failed after {attempts} attempts for {' '.join(args[:2])}: "
        f"{last_error}"
    )


def _canonical_s3_listing(
    response: dict[str, Any],
    *,
    prefix: str,
    max_files: int,
    max_bytes: int,
) -> tuple[list[dict[str, Any]], str]:
    contents = response.get("Contents", [])
    if contents is None:
        contents = []
    if not isinstance(contents, list):
        raise SeedCorpusError("S3 listing has an invalid Contents field")

    prefix_root = f"{prefix.rstrip('/')}/"
    canonical: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    relative_paths: set[PurePosixPath] = set()
    total_bytes = 0

    for item in contents:
        if not isinstance(item, dict):
            raise SeedCorpusError("S3 listing contains an invalid object")
        key = item.get("Key")
        size = item.get("Size")
        etag = item.get("ETag")
        if (
            not isinstance(key, str)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(etag, str)
            or not etag
        ):
            raise SeedCorpusError("S3 listing object is missing Key, Size, or ETag")
        _strict_utf8(key, label="S3 object key")
        if not key.startswith(prefix_root):
            raise SeedCorpusError(f"S3 returned an object outside the prefix: {key}")
        relative_text = key[len(prefix_root) :]

        canonical.append(
            {
                "key": key,
                "size_bytes": size,
                "etag": etag,
                "last_modified": str(item.get("LastModified", "")),
                "checksum_algorithm": item.get("ChecksumAlgorithm", []),
                "checksum_type": item.get("ChecksumType", ""),
            }
        )

        if relative_text.endswith("/") and size == 0:
            continue
        relative = PurePosixPath(relative_text)
        if (
            not relative_text
            or relative_text.startswith("/")
            or "\\" in relative_text
            or any(part in {"", ".", ".."} for part in relative_text.split("/"))
        ):
            raise SeedCorpusError(f"unsafe S3 seed object key: {key}")
        if relative in relative_paths:
            raise SeedCorpusError(f"colliding S3 seed object key: {key}")
        for parent in relative.parents:
            if parent == PurePosixPath("."):
                break
            if parent in relative_paths:
                raise SeedCorpusError(
                    f"S3 seed file/directory collision at: {relative_text}"
                )
        if any(existing.is_relative_to(relative) for existing in relative_paths):
            raise SeedCorpusError(
                f"S3 seed file/directory collision at: {relative_text}"
            )
        relative_paths.add(relative)
        total_bytes += size
        if len(relative_paths) > max_files:
            raise SeedCorpusError(
                f"seed corpus exceeds the fixed {max_files}-file limit"
            )
        if total_bytes > max_bytes:
            raise SeedCorpusError(
                f"seed corpus exceeds the fixed {max_bytes}-byte limit"
            )
        files.append(
            {
                "key": key,
                "relative_path": relative_text,
                "size_bytes": size,
                "etag": etag,
            }
        )

    canonical.sort(key=lambda item: _strict_utf8(item["key"], label="S3 object key"))
    files.sort(
        key=lambda item: _strict_utf8(item["relative_path"], label="S3 object key")
    )
    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return files, hashlib.sha256(encoded).hexdigest()


def _download_s3_snapshot(
    bucket: str,
    prefix: str,
    destination: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    list_args = [
        "s3api",
        "list-objects-v2",
        "--bucket",
        bucket,
        "--prefix",
        f"{prefix.rstrip('/')}/",
    ]
    listing_before = _aws_json(list_args)
    objects, listing_sha256 = _canonical_s3_listing(
        listing_before,
        prefix=prefix,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    if not objects:
        raise SeedCorpusError("configured S3 seed corpus is empty")

    raw_parent = destination.parent
    raw_parent.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(
        tempfile.mkdtemp(prefix=".seed-corpus-s3-", dir=str(raw_parent))
    )
    object_provenance: list[dict[str, Any]] = []
    try:
        for item in objects:
            key = item["key"]
            relative_path = item["relative_path"]
            expected_etag = item["etag"]
            expected_size = item["size_bytes"]
            head = _aws_json(
                ["s3api", "head-object", "--bucket", bucket, "--key", key]
            )
            if head.get("ETag") != expected_etag or head.get("ContentLength") != expected_size:
                raise SeedCorpusError(
                    f"S3 seed object changed between listing and download: {key}"
                )

            version_id = head.get("VersionId")
            output_path = raw_dir.joinpath(*PurePosixPath(relative_path).parts)
            output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            get_args = [
                "s3api",
                "get-object",
                "--bucket",
                bucket,
                "--key",
                key,
                "--checksum-mode",
                "ENABLED",
            ]
            if isinstance(version_id, str) and version_id:
                get_args.extend(["--version-id", version_id])
            else:
                get_args.extend(["--if-match", expected_etag])

            environment = {**os.environ, "AWS_PAGER": ""}
            last_error = ""
            response: dict[str, Any] | None = None
            for attempt in range(1, 6):
                output_path.unlink(missing_ok=True)
                result = subprocess.run(
                    [
                        "aws",
                        "--no-cli-pager",
                        *get_args,
                        str(output_path),
                        "--output",
                        "json",
                    ],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 0:
                    try:
                        parsed = json.loads(result.stdout or "{}")
                    except json.JSONDecodeError as exc:
                        raise SeedCorpusError(
                            f"AWS CLI returned invalid get-object JSON for {key}"
                        ) from exc
                    if not isinstance(parsed, dict):
                        raise SeedCorpusError(
                            f"AWS CLI returned an invalid get-object response for {key}"
                        )
                    response = parsed
                    break
                last_error = (result.stderr or result.stdout).strip()[-1000:]
                if attempt < 5:
                    time.sleep(min(5 * attempt, 20))
            if response is None:
                raise SeedCorpusError(
                    f"failed to download S3 seed object after 5 attempts: "
                    f"{key}: {last_error}"
                )
            if response.get("ETag") != expected_etag:
                raise SeedCorpusError(f"S3 returned an unexpected ETag for: {key}")
            response_version = response.get("VersionId")
            if (
                isinstance(version_id, str)
                and version_id
                and response_version != version_id
            ):
                raise SeedCorpusError(f"S3 returned an unexpected version for: {key}")
            downloaded_stat = output_path.lstat()
            if (
                not stat.S_ISREG(downloaded_stat.st_mode)
                or downloaded_stat.st_size != expected_size
            ):
                raise SeedCorpusError(
                    f"S3 returned an unexpected size or file type for: {key}"
                )
            object_provenance.append(
                {
                    "key": key,
                    "size_bytes": expected_size,
                    "etag": expected_etag,
                    "version_id": response_version
                    if isinstance(response_version, str) and response_version
                    else None,
                    "checksum_crc32": response.get("ChecksumCRC32"),
                    "checksum_crc32c": response.get("ChecksumCRC32C"),
                    "checksum_sha1": response.get("ChecksumSHA1"),
                    "checksum_sha256": response.get("ChecksumSHA256"),
                }
            )

        listing_after = _aws_json(list_args)
        _, listing_after_sha256 = _canonical_s3_listing(
            listing_after,
            prefix=prefix,
            max_files=max_files,
            max_bytes=max_bytes,
        )
        if listing_after_sha256 != listing_sha256:
            raise SeedCorpusError(
                "S3 seed prefix changed while the snapshot was being downloaded"
            )

        snapshot = snapshot_local_tree(
            raw_dir,
            destination,
            max_files=max_files,
            max_bytes=max_bytes,
        )
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)

    return snapshot, object_provenance, listing_sha256


def _base_metadata(
    snapshot: dict[str, Any],
    *,
    source: str,
    source_type: str,
    max_files: int,
    max_bytes: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": source,
        "source_type": source_type,
        "file_count": snapshot["file_count"],
        "size_bytes": snapshot["size_bytes"],
        "sha256": snapshot["sha256"],
        "digest_algorithm": DIGEST_ALGORITHM,
        "files": snapshot["files"],
        "copy_semantics": "recursive-byte-for-byte",
        "destination_layout": "relative-paths-preserved-at-corpus-root",
        "archives": "opaque-not-extracted",
        "limits": {
            "max_files": max_files,
            "max_bytes": max_bytes,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("local", "s3"), required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--max-files", type=int, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    args = parser.parse_args()

    if args.max_files < 1 or args.max_bytes < 1:
        raise SeedCorpusError("seed corpus limits must be positive")

    if args.mode == "local":
        snapshot = snapshot_local_tree(
            Path(args.source),
            args.destination,
            max_files=args.max_files,
            max_bytes=args.max_bytes,
        )
        metadata = _base_metadata(
            snapshot,
            source=args.source_label,
            source_type="local",
            max_files=args.max_files,
            max_bytes=args.max_bytes,
        )
        metadata["source_immutability"] = "local-tree-stable-during-copy"
    else:
        if "/" not in args.source:
            raise SeedCorpusError("S3 source must contain bucket and prefix")
        bucket, prefix = args.source.split("/", 1)
        if not bucket or not prefix:
            raise SeedCorpusError("S3 source must contain bucket and prefix")
        snapshot, objects, listing_sha256 = _download_s3_snapshot(
            bucket,
            prefix,
            args.destination,
            max_files=args.max_files,
            max_bytes=args.max_bytes,
        )
        metadata = _base_metadata(
            snapshot,
            source=args.source_label,
            source_type="s3",
            max_files=args.max_files,
            max_bytes=args.max_bytes,
        )
        metadata["source_immutability"] = (
            "etag-or-version-bound-objects-and-stable-prefix-listing"
        )
        metadata["s3_listing_sha256"] = listing_sha256
        metadata["s3_objects"] = objects

    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    temporary_metadata = args.metadata.with_name(
        f".{args.metadata.name}.tmp-{os.getpid()}"
    )
    temporary_metadata.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_metadata, args.metadata)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SeedCorpusError as exc:
        print(f"seed corpus error: {exc}", file=sys.stderr)
        raise SystemExit(1)
