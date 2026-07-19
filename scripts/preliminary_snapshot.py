#!/usr/bin/env python3
"""Create an immutable, internally checksummed preliminary log snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile


SCHEMA = "scfuzzbench-preliminary-snapshot/v1"
UUID_RE = re.compile(r"^[0-9a-f]{32}$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def utc_iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
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


def copy_prefix_complete_lines(source: Path, destination: Path, captured_size: int) -> dict:
    """Copy exactly captured_size bytes and remove a partial trailing record."""
    if captured_size < 0:
        raise ValueError("captured_size must be non-negative")
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    with source.open("rb") as src, destination.open("xb") as dst:
        remaining = captured_size
        while remaining:
            chunk = src.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            dst.write(chunk)
            copied += len(chunk)
            remaining -= len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    partial_removed = _truncate_partial_final_line(destination)
    return {
        "source_size_at_capture": captured_size,
        "captured_bytes_before_line_trim": copied,
        "snapshot_size": destination.stat().st_size,
        "partial_final_line_removed": partial_removed,
        "sha256": sha256_file(destination),
    }


def snapshot_one_file(source: Path, destination: Path) -> dict:
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
        destination.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        with os.fdopen(os.dup(fd), "rb", closefd=True) as src, destination.open(
            "xb"
        ) as dst:
            remaining = captured_size
            while remaining:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
                remaining -= len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
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


def _candidate_files(log_dir: Path) -> list[Path]:
    if not log_dir.is_dir():
        return []
    return sorted(
        path
        for path in log_dir.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() in {".log", ".csv"}
    )


def _zip_tree(root: Path, archive_path: Path, *, epoch: int) -> None:
    # ZIP cannot encode dates before 1980. Normalizing timestamps makes a retry
    # from the same capture byte-for-byte stable.
    stamp = dt.datetime.fromtimestamp(max(epoch, 315532800), tz=dt.timezone.utc)
    date_time = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    tmp_path = archive_path.with_name(f".{archive_path.name}.tmp-{os.getpid()}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(
            tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for path in sorted(p for p in root.rglob("*") if p.is_file()):
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=date_time)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100600 << 16
                archive.writestr(info, path.read_bytes())
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp_path, archive_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _validate_identity(
    *, run_id: str, benchmark_uuid: str, instance_id: str, fuzzer_key: str, run_index: str
) -> None:
    if not COMPONENT_RE.fullmatch(run_id):
        raise ValueError("run_id is not a safe object-key component")
    if not UUID_RE.fullmatch(benchmark_uuid):
        raise ValueError("benchmark_uuid must be 32 lowercase hexadecimal characters")
    for name, value in (
        ("instance_id", instance_id),
        ("fuzzer_key", fuzzer_key),
        ("run_index", run_index),
    ):
        if not COMPONENT_RE.fullmatch(value):
            raise ValueError(f"{name} is not a safe object-key component")


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
) -> dict:
    _validate_identity(
        run_id=run_id,
        benchmark_uuid=benchmark_uuid,
        instance_id=instance_id,
        fuzzer_key=fuzzer_key,
        run_index=run_index,
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

    object_identity = f"{fuzzer_key}-{run_index}-{instance_id}"
    if not COMPONENT_RE.fullmatch(object_identity):
        raise ValueError("derived object identity is unsafe")

    with tempfile.TemporaryDirectory(prefix="scfuzzbench-preliminary-") as tmp:
        root = Path(tmp)
        copied_files: list[dict] = []
        skipped_files: list[dict] = []
        for source in _candidate_files(log_dir):
            relative = source.relative_to(log_dir)
            destination = root / "logs" / relative
            try:
                details = snapshot_one_file(source, destination)
            except (FileNotFoundError, OSError, ValueError) as exc:
                skipped_files.append({"path": relative.as_posix(), "reason": type(exc).__name__})
                continue
            copied_files.append({"path": f"logs/{relative.as_posix()}", **details})

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
            "skipped_files": skipped_files,
        }
        (root / "checkpoint.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _zip_tree(root, archive_path, epoch=captured_at_epoch)

    return {
        "archive": str(archive_path),
        "sha256": sha256_file(archive_path),
        "object_identity": object_identity,
        "files": len(copied_files),
        "skipped_files": len(skipped_files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
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
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
