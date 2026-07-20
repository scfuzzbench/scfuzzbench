#!/usr/bin/env python3
"""Create an immutable, internally checksummed preliminary log snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import zipfile


SCHEMA = "scfuzzbench-preliminary-snapshot/v1"
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]+$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
MAX_CAPTURE_LATENESS_SECONDS = 300
MAX_SNAPSHOT_FILES = 512
MAX_SCANNED_ENTRIES = 4096
MAX_SNAPSHOT_FILE_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


def _default_safe_path_ops() -> Path:
    candidates = (
        Path(__file__).resolve().with_name("safe_path_ops.py"),
        Path(__file__).resolve().parents[1] / "fuzzers" / "_shared" / "safe_path_ops.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError("safe_path_ops.py is unavailable")


def _load_safe_path_ops(path: Path | None = None):
    helper = (path or _default_safe_path_ops()).resolve(strict=True)
    spec = importlib.util.spec_from_file_location(
        "_scfuzzbench_preliminary_safe_path_ops", helper
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"could not load safe path helper: {helper}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def utc_iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _truncate_partial_final_line(path: Path) -> bool:
    """Drop a final partial text record without reading the whole file."""
    size = path.stat().st_size
    if size == 0:
        return False
    with path.open("r+b") as handle:
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return False

        cursor = size
        newline_at = -1
        while cursor > 0:
            read_size = min(64 * 1024, cursor)
            cursor -= read_size
            handle.seek(cursor)
            chunk = handle.read(read_size)
            found = chunk.rfind(b"\n")
            if found >= 0:
                newline_at = cursor + found
                break
        handle.truncate(newline_at + 1)
    return True


def copy_prefix_complete_lines(
    source: Path,
    destination: Path,
    captured_size: int,
    *,
    max_bytes: int = MAX_SNAPSHOT_FILE_BYTES,
) -> dict:
    """Copy exactly captured_size bytes and remove a partial trailing record."""
    if captured_size < 0:
        raise ValueError("captured_size must be non-negative")
    if captured_size > max_bytes:
        raise ValueError("snapshot source exceeds its byte cap")
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            remaining = captured_size
            while remaining:
                chunk = src.read(min(COPY_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                remaining -= len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if copied != captured_size:
            raise ValueError("snapshot source shrank during frozen-prefix copy")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    partial_removed = _truncate_partial_final_line(destination)
    return {
        "source_size_at_capture": captured_size,
        "captured_bytes_before_line_trim": copied,
        "snapshot_size": destination.stat().st_size,
        "partial_final_line_removed": partial_removed,
        "sha256": sha256_file(destination),
    }


def snapshot_one_file(
    source: Path,
    destination: Path,
    *,
    max_bytes: int = MAX_SNAPSHOT_FILE_BYTES,
) -> dict:
    """Open without following symlinks, freeze the size, then copy that prefix."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(source, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"snapshot source is not a regular file: {source}")
        captured_size = info.st_size
        if captured_size > max_bytes:
            raise ValueError("snapshot source exceeds its byte cap")
        destination.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        try:
            with os.fdopen(os.dup(fd), "rb", closefd=True) as src, destination.open(
                "xb"
            ) as dst:
                remaining = captured_size
                while remaining:
                    chunk = src.read(min(COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    copied += len(chunk)
                    remaining -= len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if copied != captured_size:
                raise ValueError("snapshot source shrank during frozen-prefix copy")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        partial_removed = _truncate_partial_final_line(destination)
        return {
            "source_size_at_capture": captured_size,
            "captured_bytes_before_line_trim": copied,
            "snapshot_size": destination.stat().st_size,
            "partial_final_line_removed": partial_removed,
            "sha256": sha256_file(destination),
        }
    finally:
        os.close(fd)


def _snapshot_one_file_at(
    safe,
    log_root_fd: int,
    relative: Path,
    destination: Path,
    *,
    max_bytes: int,
) -> dict:
    parts = tuple(relative.parts)
    with safe._open_parent(log_root_fd, parts) as (parent_fd, name):
        fd = os.open(name, safe.FILE_READ_FLAGS, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            safe._require_regular(info, f"snapshot source {relative.as_posix()}")
            captured_size = info.st_size
            if captured_size > max_bytes:
                raise ValueError("snapshot source exceeds its byte cap")
            destination.parent.mkdir(parents=True, exist_ok=True)
            copied = 0
            try:
                with os.fdopen(os.dup(fd), "rb", closefd=True) as src, destination.open(
                    "xb"
                ) as dst:
                    remaining = captured_size
                    while remaining:
                        chunk = src.read(min(COPY_CHUNK_BYTES, remaining))
                        if not chunk:
                            break
                        dst.write(chunk)
                        copied += len(chunk)
                        remaining -= len(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if copied != captured_size:
                    raise ValueError(
                        "snapshot source shrank during frozen-prefix copy"
                    )
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("snapshot source path changed during copy")
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            partial_removed = _truncate_partial_final_line(destination)
            return {
                "source_size_at_capture": captured_size,
                "captured_bytes_before_line_trim": copied,
                "snapshot_size": destination.stat().st_size,
                "partial_final_line_removed": partial_removed,
                "sha256": sha256_file(destination),
            }
        finally:
            os.close(fd)


def _candidate_files_at(safe, log_root_fd: int) -> tuple[list[Path], list[dict]]:
    """Traverse a pinned log root without following descendant links."""
    candidates: list[Path] = []
    skipped: list[dict] = []
    scanned = 0

    def visit(directory_fd: int, relative_root: Path) -> None:
        nonlocal scanned
        names = sorted(os.listdir(directory_fd), key=os.fsencode)
        scanned += len(names)
        if scanned > MAX_SCANNED_ENTRIES:
            raise ValueError(
                f"snapshot log tree contains more than {MAX_SCANNED_ENTRIES} entries"
            )
        for name in names:
            if name in {"", ".", ".."} or "/" in name or "\x00" in name:
                raise ValueError(f"unsafe log entry name: {name!r}")
            relative = relative_root / name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                skipped.append({"path": relative.as_posix(), "reason": "symlink"})
                continue
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, safe.DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError(
                            f"snapshot directory changed: {relative.as_posix()}"
                        )
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if relative.suffix.lower() not in {".log", ".csv"}:
                continue
            if not stat.S_ISREG(info.st_mode):
                skipped.append(
                    {"path": relative.as_posix(), "reason": "non-regular"}
                )
                continue
            if info.st_nlink != 1:
                skipped.append(
                    {"path": relative.as_posix(), "reason": "hardlink"}
                )
                continue
            candidates.append(relative)
            if len(candidates) > MAX_SNAPSHOT_FILES:
                raise ValueError(
                    f"snapshot contains more than {MAX_SNAPSHOT_FILES} candidate files"
                )

    visit(log_root_fd, Path())
    return sorted(candidates), sorted(skipped, key=lambda item: item["path"])


def _candidate_files(log_dir: Path) -> tuple[list[Path], list[dict]]:
    """Walk without following links and report skipped link entries."""
    if not log_dir.is_dir() or log_dir.is_symlink():
        return [], [{"path": ".", "reason": "unsafe-log-directory"}]
    candidates: list[Path] = []
    skipped: list[dict] = []
    scanned = 0
    for root, directory_names, file_names in os.walk(log_dir, followlinks=False):
        root_path = Path(root)
        scanned += len(directory_names) + len(file_names)
        if scanned > MAX_SCANNED_ENTRIES:
            raise ValueError(
                f"snapshot log tree contains more than {MAX_SCANNED_ENTRIES} entries"
            )
        safe_directories: list[str] = []
        for name in sorted(directory_names):
            path = root_path / name
            relative = path.relative_to(log_dir).as_posix()
            if path.is_symlink():
                skipped.append({"path": relative, "reason": "symlink"})
            else:
                safe_directories.append(name)
        directory_names[:] = safe_directories
        for name in sorted(file_names):
            path = root_path / name
            if path.suffix.lower() not in {".log", ".csv"}:
                continue
            relative = path.relative_to(log_dir).as_posix()
            if path.is_symlink():
                skipped.append({"path": relative, "reason": "symlink"})
                continue
            candidates.append(path)
            if len(candidates) > MAX_SNAPSHOT_FILES:
                raise ValueError(
                    f"snapshot contains more than {MAX_SNAPSHOT_FILES} candidate files"
                )
    return sorted(candidates), sorted(skipped, key=lambda item: item["path"])


def _zip_tree(
    root: Path,
    archive_path: Path,
    *,
    epoch: int,
    safe,
    framework_root_path: str,
    framework_root_anchor: str,
    framework_root_identity: str,
) -> str:
    # ZIP cannot encode dates before 1980. Normalizing timestamps makes a retry
    # from the same capture byte-for-byte stable.
    stamp = dt.datetime.fromtimestamp(max(epoch, 315532800), tz=dt.timezone.utc)
    date_time = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    archive_parts = safe._relative_parts(
        str(archive_path),
        framework_root_path,
        framework_root_anchor,
    )
    source_anchor = str(root.resolve(strict=True))
    source_identity = safe._identity(os.stat(source_anchor, follow_symlinks=False))
    output_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        output_flags |= os.O_NOFOLLOW

    def add_directory(
        archive: zipfile.ZipFile,
        directory_fd: int,
        prefix: Path,
    ) -> None:
        names = sorted(os.listdir(directory_fd), key=os.fsencode)
        for name in names:
            if name in {"", ".", ".."} or "/" in name or "\x00" in name:
                raise ValueError(f"unsafe snapshot entry name: {name!r}")
            relative = prefix / name
            listed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(listed.st_mode):
                raise ValueError(
                    f"snapshot staging entry is a symlink: {relative.as_posix()}"
                )
            if stat.S_ISDIR(listed.st_mode):
                child_fd = os.open(name, safe.DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        listed.st_dev,
                        listed.st_ino,
                    ):
                        raise ValueError(
                            f"snapshot staging directory changed: {relative.as_posix()}"
                        )
                    add_directory(archive, child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            safe._require_regular(listed, f"snapshot entry {relative.as_posix()}")
            source_fd = os.open(name, safe.FILE_READ_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(source_fd)
                if not safe._same_entry(opened, listed):
                    raise ValueError(
                        f"snapshot staging file changed: {relative.as_posix()}"
                    )
                info = zipfile.ZipInfo(relative.as_posix(), date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100600 << 16
                copied = 0
                with archive.open(info, "w") as destination:
                    while True:
                        chunk = os.read(source_fd, COPY_CHUNK_BYTES)
                        if not chunk:
                            break
                        copied += len(chunk)
                        destination.write(chunk)
                if copied != listed.st_size or not safe._same_entry(
                    os.fstat(source_fd), listed
                ):
                    raise ValueError(
                        f"snapshot staging file changed: {relative.as_posix()}"
                    )
            finally:
                os.close(source_fd)

    with safe._open_anchor(
        source_anchor, source_anchor, source_identity
    ) as source_fd:
        with safe._open_anchor(
            framework_root_path,
            framework_root_anchor,
            framework_root_identity,
        ) as framework_fd:
            with safe._open_parent(
                framework_fd, archive_parts, create=False
            ) as (parent_fd, destination_name):
                temporary_name = safe._random_temporary_name(destination_name)
                output_fd = -1
                try:
                    output_fd = os.open(
                        temporary_name,
                        output_flags,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    with os.fdopen(os.dup(output_fd), "w+b") as output:
                        with zipfile.ZipFile(
                            output,
                            "w",
                            compression=zipfile.ZIP_DEFLATED,
                            compresslevel=6,
                        ) as archive:
                            add_directory(archive, source_fd, Path())
                        output.flush()
                        os.fsync(output.fileno())
                    os.fsync(output_fd)
                    os.lseek(output_fd, 0, os.SEEK_SET)
                    digest = hashlib.sha256()
                    for chunk in iter(
                        lambda: os.read(output_fd, COPY_CHUNK_BYTES), b""
                    ):
                        digest.update(chunk)
                    safe._verify_anchor_path(
                        framework_root_anchor, framework_fd
                    )
                    safe._verify_directory_path(
                        framework_fd,
                        archive_parts[:-1],
                        parent_fd,
                        "preliminary archive parent",
                    )
                    safe._publish_temporary(
                        parent_fd,
                        temporary_name,
                        destination_name,
                        output_fd,
                        replace=False,
                    )
                    temporary_name = ""
                    safe._verify_anchor_path(
                        framework_root_anchor, framework_fd
                    )
                    safe._verify_directory_path(
                        framework_fd,
                        archive_parts[:-1],
                        parent_fd,
                        "preliminary archive parent",
                    )
                    return digest.hexdigest()
                finally:
                    if output_fd >= 0:
                        os.close(output_fd)
                    if temporary_name:
                        try:
                            os.unlink(temporary_name, dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass


def _validate_identity(
    *,
    run_id: str,
    benchmark_uuid: str,
    instance_id: str,
    fuzzer_key: str,
    run_index: str,
    fuzzer_label: str,
) -> None:
    if not COMPONENT_RE.fullmatch(run_id):
        raise ValueError("run_id is not a safe object-key component")
    if not UUID_RE.fullmatch(benchmark_uuid):
        raise ValueError("benchmark_uuid must be 32 lowercase hexadecimal characters")
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError("instance_id must be an EC2 instance identifier")
    if not COMPONENT_RE.fullmatch(fuzzer_key):
        raise ValueError("fuzzer_key is not a safe object-key component")
    if not run_index.isdigit():
        raise ValueError("run_index must be a non-negative integer")
    if not SAFE_LABEL_RE.fullmatch(fuzzer_label):
        raise ValueError("fuzzer_label is unsafe")


def capture_snapshot(
    *,
    log_dir: Path,
    archive_path: Path,
    run_id: str,
    run_started_at_epoch: int,
    benchmark_uuid: str,
    checkpoint: int,
    interval_seconds: int,
    scheduled_at_epoch: int,
    captured_at_epoch: int,
    instance_id: str,
    fuzzer_key: str,
    run_index: str,
    fuzzer_label: str,
    timeout_seconds: int,
    log_root_anchor: str | None = None,
    log_root_identity: str | None = None,
    framework_root_path: str | None = None,
    framework_root_anchor: str | None = None,
    framework_root_identity: str | None = None,
    safe_path_ops_path: Path | None = None,
) -> dict:
    _validate_identity(
        run_id=run_id,
        benchmark_uuid=benchmark_uuid,
        instance_id=instance_id,
        fuzzer_key=fuzzer_key,
        run_index=run_index,
        fuzzer_label=fuzzer_label,
    )
    if checkpoint < 1:
        raise ValueError("checkpoint must be positive")
    if run_started_at_epoch <= 0:
        raise ValueError("run_started_at_epoch must be positive")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    expected_schedule = run_started_at_epoch + checkpoint * interval_seconds
    if scheduled_at_epoch != expected_schedule:
        raise ValueError("scheduled checkpoint is inconsistent")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if scheduled_at_epoch >= run_started_at_epoch + timeout_seconds:
        raise ValueError("checkpoint must precede the terminal benchmark deadline")
    if not (
        scheduled_at_epoch
        <= captured_at_epoch
        <= scheduled_at_epoch + MAX_CAPTURE_LATENESS_SECONDS
    ):
        raise ValueError("checkpoint capture is outside the allowed settle window")

    object_identity = f"{fuzzer_key}-{run_index}-{instance_id}"
    if not COMPONENT_RE.fullmatch(object_identity):
        raise ValueError("derived object identity is unsafe")

    safe = _load_safe_path_ops(safe_path_ops_path)
    resolved_log_root = str(log_dir.resolve(strict=True))
    if log_root_anchor is None:
        log_root_anchor = resolved_log_root
    if log_root_identity is None:
        log_root_identity = safe._identity(
            os.stat(log_root_anchor, follow_symlinks=False)
        )
    if framework_root_path is None:
        framework_root_path = str(archive_path.parent.resolve(strict=True))
    if framework_root_anchor is None:
        framework_root_anchor = str(Path(framework_root_path).resolve(strict=True))
    if framework_root_identity is None:
        framework_root_identity = safe._identity(
            os.stat(framework_root_anchor, follow_symlinks=False)
        )

    with tempfile.TemporaryDirectory(prefix="scfuzzbench-preliminary-") as tmp:
        root = Path(tmp)
        copied_files: list[dict] = []
        with safe._open_anchor(
            str(log_dir), log_root_anchor, log_root_identity
        ) as log_root_fd:
            candidates, skipped_files = _candidate_files_at(safe, log_root_fd)
            total_bytes = 0
            for relative in candidates:
                destination = root / "logs" / relative
                try:
                    details = _snapshot_one_file_at(
                        safe,
                        log_root_fd,
                        relative,
                        destination,
                        max_bytes=min(
                            MAX_SNAPSHOT_FILE_BYTES,
                            MAX_SNAPSHOT_TOTAL_BYTES - total_bytes,
                        ),
                    )
                except (FileNotFoundError, OSError, ValueError, safe.SafePathError) as exc:
                    skipped_files.append(
                        {
                            "path": relative.as_posix(),
                            "reason": type(exc).__name__,
                        }
                    )
                    continue
                total_bytes += int(details["snapshot_size"])
                copied_files.append(
                    {"path": f"logs/{relative.as_posix()}", **details}
                )

        metadata = {
            "schema": SCHEMA,
            "run_id": run_id,
            "run_started_at_epoch": run_started_at_epoch,
            "benchmark_uuid": benchmark_uuid,
            "checkpoint": checkpoint,
            "interval_seconds": interval_seconds,
            "scheduled_at_epoch": scheduled_at_epoch,
            "scheduled_at_utc": utc_iso(scheduled_at_epoch),
            "captured_at_epoch": captured_at_epoch,
            "captured_at_utc": utc_iso(captured_at_epoch),
            "elapsed_seconds": max(0, scheduled_at_epoch - run_started_at_epoch),
            "planned_timeout_seconds": timeout_seconds,
            "complete": False,
            "instance_id": instance_id,
            "fuzzer_key": fuzzer_key,
            "run_index": run_index,
            "fuzzer_label": fuzzer_label,
            "object_identity": object_identity,
            "files": copied_files,
            "skipped_files": sorted(skipped_files, key=lambda item: item["path"]),
            "snapshot_limits": {
                "max_files": MAX_SNAPSHOT_FILES,
                "max_file_bytes": MAX_SNAPSHOT_FILE_BYTES,
                "max_total_bytes": MAX_SNAPSHOT_TOTAL_BYTES,
            },
        }
        (root / "checkpoint.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        archive_sha256 = _zip_tree(
            root,
            archive_path,
            epoch=captured_at_epoch,
            safe=safe,
            framework_root_path=framework_root_path,
            framework_root_anchor=framework_root_anchor,
            framework_root_identity=framework_root_identity,
        )

    return {
        "archive": str(archive_path),
        "sha256": archive_sha256,
        "object_identity": object_identity,
        "files": len(copied_files),
        "skipped_files": len(skipped_files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--log-root-anchor", required=True)
    parser.add_argument("--log-root-identity", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--framework-root-path", required=True)
    parser.add_argument("--framework-root-anchor", required=True)
    parser.add_argument("--framework-root-identity", required=True)
    parser.add_argument("--safe-path-ops", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-started-at-epoch", required=True, type=int)
    parser.add_argument("--benchmark-uuid", required=True)
    parser.add_argument("--checkpoint", required=True, type=int)
    parser.add_argument("--interval-seconds", required=True, type=int)
    parser.add_argument("--scheduled-at-epoch", required=True, type=int)
    parser.add_argument("--captured-at-epoch", required=True, type=int)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--fuzzer-key", required=True)
    parser.add_argument("--run-index", required=True)
    parser.add_argument("--fuzzer-label", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    args = parser.parse_args()

    result = capture_snapshot(
        log_dir=args.log_dir,
        archive_path=args.archive,
        run_id=args.run_id,
        run_started_at_epoch=args.run_started_at_epoch,
        benchmark_uuid=args.benchmark_uuid,
        checkpoint=args.checkpoint,
        interval_seconds=args.interval_seconds,
        scheduled_at_epoch=args.scheduled_at_epoch,
        captured_at_epoch=args.captured_at_epoch,
        instance_id=args.instance_id,
        fuzzer_key=args.fuzzer_key,
        run_index=args.run_index,
        fuzzer_label=args.fuzzer_label,
        timeout_seconds=args.timeout_seconds,
        log_root_anchor=args.log_root_anchor,
        log_root_identity=args.log_root_identity,
        framework_root_path=args.framework_root_path,
        framework_root_anchor=args.framework_root_anchor,
        framework_root_identity=args.framework_root_identity,
        safe_path_ops_path=args.safe_path_ops,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
