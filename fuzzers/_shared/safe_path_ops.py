#!/usr/bin/env python3
"""Descriptor-relative filesystem operations beneath pre-captured roots.

The shell runner captures each trusted root's canonical path and device/inode
before target code runs.  This helper reopens those roots without following a
final symlink and performs every path walk relative to the resulting file
descriptor.  It never follows a descendant symlink.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import json
import os
import secrets
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator


class SafePathError(RuntimeError):
    """An operation could not prove that it remained beneath its trusted root."""


class _ForwardedSignal(BaseException):
    def __init__(self, signum: int):
        super().__init__(f"received signal {signum}")
        self.signum = signum


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
FILE_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
if hasattr(os, "O_NOFOLLOW"):
    DIRECTORY_FLAGS |= os.O_NOFOLLOW
    FILE_READ_FLAGS |= os.O_NOFOLLOW

COPY_CHUNK_BYTES = 1024 * 1024
DEFAULT_READ_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_WRITE_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_STREAM_MAX_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_ARCHIVE_MAX_FILES = 100_000
DEFAULT_ARCHIVE_MAX_ENTRIES = 120_000
DEFAULT_ARCHIVE_MAX_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 128
MAX_ARCHIVE_PATH_BYTES = 4096
RENAME_NOREPLACE = 1


def _identity(st: os.stat_result) -> str:
    return f"{st.st_dev}:{st.st_ino}"


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
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


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_size,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_size,
    )


def _same_inode_type(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _require_regular(st: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise SafePathError(f"{label} is not a regular file")
    if st.st_nlink != 1:
        raise SafePathError(f"{label} has multiple hard links")


def _require_directory(st: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise SafePathError(f"{label} is not a directory")


@contextmanager
def _open_anchor(
    root_path: str,
    root_anchor: str,
    root_identity: str,
) -> Iterator[int]:
    if not os.path.isabs(root_path) or not os.path.isabs(root_anchor):
        raise SafePathError("root paths must be absolute")
    if os.path.realpath(root_path) != root_anchor:
        raise SafePathError(f"trusted root path changed: {root_path}")
    try:
        fd = os.open(root_anchor, DIRECTORY_FLAGS)
    except OSError as exc:
        raise SafePathError(
            f"cannot open trusted root without symlinks: {root_anchor}"
        ) from exc
    try:
        root_stat = os.fstat(fd)
        _require_directory(root_stat, f"trusted root {root_path}")
        if _identity(root_stat) != root_identity:
            raise SafePathError(f"trusted root identity changed: {root_path}")
        yield fd
    finally:
        os.close(fd)


def _relative_parts(
    candidate: str,
    root_path: str,
    root_anchor: str,
    *,
    allow_root: bool = False,
) -> tuple[str, ...]:
    if not os.path.isabs(candidate):
        raise SafePathError(f"path must be absolute: {candidate}")
    raw_parts = Path(candidate).parts
    if any(part in {".", ".."} for part in raw_parts):
        raise SafePathError(f"path contains a dot segment: {candidate}")

    normalized = os.path.normpath(candidate)
    for base in (os.path.normpath(root_path), os.path.normpath(root_anchor)):
        try:
            if os.path.commonpath((base, normalized)) != base:
                continue
        except ValueError:
            continue
        if normalized == base:
            if allow_root:
                return ()
            continue
        relative = os.path.relpath(normalized, base)
        parts = tuple(Path(relative).parts)
        if parts and all(
            part not in {"", ".", ".."} and "/" not in part and "\x00" not in part
            for part in parts
        ):
            return parts
    relationship = "at or beneath" if allow_root else "a strict descendant of"
    raise SafePathError(
        f"path is not {relationship} its trusted root: {candidate}"
    )


@contextmanager
def _open_parent(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool = False,
    mode: int = 0o700,
) -> Iterator[tuple[int, str]]:
    if not parts:
        raise SafePathError("operation requires a strict descendant")
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            try:
                child = os.open(component, DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=mode, dir_fd=current)
                child = os.open(component, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        yield current, parts[-1]
    finally:
        os.close(current)


def _open_directory(root_fd: int, parts: tuple[str, ...]) -> int:
    if not parts:
        return os.dup(root_fd)
    current = os.dup(root_fd)
    try:
        for component in parts:
            child = os.open(component, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _verify_anchor_path(root_anchor: str, pinned_fd: int) -> None:
    reopened = os.open(root_anchor, DIRECTORY_FLAGS)
    try:
        if not _same_inode_type(os.fstat(pinned_fd), os.fstat(reopened)):
            raise SafePathError(f"trusted root path changed: {root_anchor}")
    finally:
        os.close(reopened)


def _verify_directory_path(
    root_fd: int,
    parts: tuple[str, ...],
    pinned_fd: int,
    label: str,
) -> None:
    reopened = _open_directory(root_fd, parts)
    try:
        if not _same_inode_type(os.fstat(pinned_fd), os.fstat(reopened)):
            raise SafePathError(f"{label} path changed")
    finally:
        os.close(reopened)


def _run_test_hook() -> None:
    hook = os.environ.get("SCFUZZBENCH_SAFE_PATH_TEST_HOOK", "")
    if hook:
        subprocess.run([hook], check=True)


def _root_args(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    option = f"{prefix}-" if prefix else ""
    destination = f"{prefix}_" if prefix else ""
    parser.add_argument(
        f"--{option}root-path", dest=f"{destination}root_path", required=True
    )
    parser.add_argument(
        f"--{option}root-anchor", dest=f"{destination}root_anchor", required=True
    )
    parser.add_argument(
        f"--{option}root-identity",
        dest=f"{destination}root_identity",
        required=True,
    )


def _rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SafePathError("renameat2 is required for no-replace publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination_name)


def _random_temporary_name(destination_name: str) -> str:
    return f".{destination_name}.scfuzzbench-{os.getpid()}-{secrets.token_hex(8)}"


def _publish_temporary(
    parent_fd: int,
    temporary_name: str,
    destination_name: str,
    temporary_fd: int,
    *,
    replace: bool,
) -> None:
    expected = os.fstat(temporary_fd)
    if replace:
        os.replace(
            temporary_name,
            destination_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    else:
        _rename_noreplace(
            parent_fd, temporary_name, parent_fd, destination_name
        )
    published = os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
    # A rename updates ctime, so publication checks the pinned inode, type,
    # link count, and completed byte length rather than timestamp equality.
    if not _same_identity(expected, published):
        raise SafePathError(
            f"published destination identity changed: {destination_name}"
        )
    os.fsync(parent_fd)


def _remove_tree_at(parent_fd: int, name: str) -> None:
    listed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(listed.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    directory_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        if not _same_entry(listed, os.fstat(directory_fd)):
            raise SafePathError(f"directory changed before removal: {name}")
        for child_name in os.listdir(directory_fd):
            if child_name in {"", ".", ".."} or "/" in child_name or "\x00" in child_name:
                raise SafePathError(f"unsafe directory entry name: {child_name!r}")
            child = os.stat(child_name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(child.st_mode):
                _remove_tree_at(directory_fd, child_name)
            else:
                os.unlink(child_name, dir_fd=directory_fd)
        final_parent_entry = os.stat(
            name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            final_parent_entry.st_dev,
            final_parent_entry.st_ino,
        ) != (
            listed.st_dev,
            listed.st_ino,
        ):
            raise SafePathError(f"directory changed during removal: {name}")
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove(args: argparse.Namespace, *, tree: bool) -> None:
    parts = _relative_parts(args.path, args.root_path, args.root_anchor)
    with _open_anchor(args.root_path, args.root_anchor, args.root_identity) as root_fd:
        with _open_parent(root_fd, parts) as (parent_fd, name):
            _run_test_hook()
            _verify_anchor_path(args.root_anchor, root_fd)
            _verify_directory_path(
                root_fd, parts[:-1], parent_fd, "removal parent"
            )
            try:
                entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if tree and stat.S_ISDIR(entry.st_mode):
                _remove_tree_at(parent_fd, name)
            else:
                if not tree and stat.S_ISDIR(entry.st_mode):
                    raise SafePathError(f"refusing to unlink directory as file: {args.path}")
                os.unlink(name, dir_fd=parent_fd)
            _verify_anchor_path(args.root_anchor, root_fd)
            _verify_directory_path(
                root_fd, parts[:-1], parent_fd, "removal parent"
            )


def _mkdir(args: argparse.Namespace) -> None:
    parts = _relative_parts(args.path, args.root_path, args.root_anchor)
    mode = int(args.mode, 8)
    if mode < 0 or mode > 0o777:
        raise SafePathError("mkdir mode is outside 0000..0777")
    with _open_anchor(args.root_path, args.root_anchor, args.root_identity) as root_fd:
        with _open_parent(root_fd, parts, create=args.parents, mode=mode) as (
            parent_fd,
            name,
        ):
            _run_test_hook()
            _verify_anchor_path(args.root_anchor, root_fd)
            _verify_directory_path(
                root_fd, parts[:-1], parent_fd, "mkdir parent"
            )
            try:
                os.mkdir(name, mode=mode, dir_fd=parent_fd)
            except FileExistsError:
                if not args.exist_ok:
                    raise
                child_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
                os.close(child_fd)
            _verify_anchor_path(args.root_anchor, root_fd)
            _verify_directory_path(
                root_fd, parts[:-1], parent_fd, "mkdir parent"
            )


def _open_move_source(
    parent_fd: int,
    name: str,
    expected_type: str,
) -> tuple[int, os.stat_result]:
    if expected_type == "directory":
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    else:
        fd = os.open(name, FILE_READ_FLAGS, dir_fd=parent_fd)
    try:
        source_stat = os.fstat(fd)
        if expected_type == "directory":
            _require_directory(source_stat, "move source")
        else:
            _require_regular(source_stat, "move source")
        return fd, source_stat
    except BaseException:
        os.close(fd)
        raise


def _move(args: argparse.Namespace) -> None:
    source_parts = _relative_parts(
        args.source, args.source_root_path, args.source_root_anchor
    )
    destination_parts = _relative_parts(
        args.destination,
        args.destination_root_path,
        args.destination_root_anchor,
    )
    with _open_anchor(
        args.source_root_path,
        args.source_root_anchor,
        args.source_root_identity,
    ) as source_root_fd:
        with _open_anchor(
            args.destination_root_path,
            args.destination_root_anchor,
            args.destination_root_identity,
        ) as destination_root_fd:
            with _open_parent(source_root_fd, source_parts) as (
                source_parent_fd,
                source_name,
            ):
                source_fd, source_stat = _open_move_source(
                    source_parent_fd, source_name, args.source_type
                )
                try:
                    if args.source_identity and _identity(source_stat) != args.source_identity:
                        raise SafePathError("move source identity changed")
                    with _open_parent(
                        destination_root_fd, destination_parts
                    ) as (destination_parent_fd, destination_name):
                        _run_test_hook()
                        _verify_anchor_path(
                            args.source_root_anchor, source_root_fd
                        )
                        _verify_anchor_path(
                            args.destination_root_anchor,
                            destination_root_fd,
                        )
                        _verify_directory_path(
                            source_root_fd,
                            source_parts[:-1],
                            source_parent_fd,
                            "move source parent",
                        )
                        _verify_directory_path(
                            destination_root_fd,
                            destination_parts[:-1],
                            destination_parent_fd,
                            "move destination parent",
                        )
                        listed = os.stat(
                            source_name,
                            dir_fd=source_parent_fd,
                            follow_symlinks=False,
                        )
                        if not _same_entry(source_stat, listed):
                            raise SafePathError("move source changed before rename")
                        if args.replace:
                            os.replace(
                                source_name,
                                destination_name,
                                src_dir_fd=source_parent_fd,
                                dst_dir_fd=destination_parent_fd,
                            )
                        else:
                            _rename_noreplace(
                                source_parent_fd,
                                source_name,
                                destination_parent_fd,
                                destination_name,
                            )
                        published = os.stat(
                            destination_name,
                            dir_fd=destination_parent_fd,
                            follow_symlinks=False,
                        )
                        if (
                            published.st_dev,
                            published.st_ino,
                            published.st_mode,
                            published.st_nlink,
                        ) != (
                            source_stat.st_dev,
                            source_stat.st_ino,
                            source_stat.st_mode,
                            source_stat.st_nlink,
                        ):
                            raise SafePathError("move destination is not the pinned source")
                        os.fsync(source_parent_fd)
                        if destination_parent_fd != source_parent_fd:
                            os.fsync(destination_parent_fd)
                finally:
                    os.close(source_fd)


def _read_stable_file(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    max_bytes: int,
    run_hook: bool = False,
) -> bytes:
    with _open_parent(root_fd, parts) as (parent_fd, name):
        fd = os.open(name, FILE_READ_FLAGS, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            _require_regular(opened, name)
            if opened.st_size > max_bytes:
                raise SafePathError(f"file exceeds {max_bytes} bytes: {name}")
            if run_hook:
                _run_test_hook()
                _verify_directory_path(
                    root_fd, parts[:-1], parent_fd, "read parent"
                )
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
                copied = 0
                while True:
                    chunk = os.read(fd, min(COPY_CHUNK_BYTES, max_bytes - copied + 1))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise SafePathError(f"file exceeds {max_bytes} bytes: {name}")
                    spool.write(chunk)
                after_read = os.fstat(fd)
                if not _same_entry(opened, after_read):
                    raise SafePathError(f"file changed while it was read: {name}")
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not _same_entry(opened, current):
                    raise SafePathError(f"file path changed while it was read: {name}")
                _verify_directory_path(
                    root_fd, parts[:-1], parent_fd, "read parent"
                )
                spool.seek(0)
                return spool.read()
        finally:
            os.close(fd)


def _read_file(args: argparse.Namespace) -> None:
    parts = _relative_parts(args.path, args.root_path, args.root_anchor)
    with _open_anchor(args.root_path, args.root_anchor, args.root_identity) as root_fd:
        data = _read_stable_file(
            root_fd, parts, max_bytes=args.max_bytes, run_hook=True
        )
        _verify_anchor_path(args.root_anchor, root_fd)
    sys.stdout.buffer.write(data)


def _write_file(args: argparse.Namespace) -> None:
    parts = _relative_parts(args.path, args.root_path, args.root_anchor)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _open_anchor(args.root_path, args.root_anchor, args.root_identity) as root_fd:
        with _open_parent(root_fd, parts, create=args.parents) as (parent_fd, name):
            _run_test_hook()
            _verify_anchor_path(args.root_anchor, root_fd)
            _verify_directory_path(
                root_fd, parts[:-1], parent_fd, "write parent"
            )
            temporary_name = _random_temporary_name(name)
            temporary_fd = -1
            try:
                temporary_fd = os.open(
                    temporary_name, flags, 0o600, dir_fd=parent_fd
                )
                written = 0
                while True:
                    chunk = sys.stdin.buffer.read(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > args.max_bytes:
                        raise SafePathError(
                            f"input exceeds {args.max_bytes} bytes"
                        )
                    view = memoryview(chunk)
                    while view:
                        consumed = os.write(temporary_fd, view)
                        view = view[consumed:]
                os.fsync(temporary_fd)
                _verify_anchor_path(args.root_anchor, root_fd)
                _verify_directory_path(
                    root_fd, parts[:-1], parent_fd, "write parent"
                )
                _publish_temporary(
                    parent_fd,
                    temporary_name,
                    name,
                    temporary_fd,
                    replace=args.replace,
                )
                temporary_name = ""
                _verify_anchor_path(args.root_anchor, root_fd)
                _verify_directory_path(
                    root_fd, parts[:-1], parent_fd, "write parent"
                )
            finally:
                if temporary_fd >= 0:
                    os.close(temporary_fd)
                if temporary_name:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass


def _write_chunks(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        consumed = os.write(fd, view)
        if consumed <= 0:
            raise SafePathError("short write to pinned stream")
        view = view[consumed:]


def _terminate_owned_process_group(
    process: subprocess.Popen[bytes],
    initial_signal: int = signal.SIGTERM,
) -> None:
    """Boundedly stop the child session, including descendants after leader exit."""

    # A completed Popen has already reaped its leader.  Its numeric PID/PGID is
    # no longer an ownership token and must never be signalled.
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, initial_signal)
    except ProcessLookupError:
        pass

    # Observe the leader without reaping it during the grace period.  Even if
    # it exits promptly, retaining the zombie prevents its PID (and therefore
    # the owned process-group ID) from being reused before descendants receive
    # the bounded SIGKILL cleanup.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.waitid(
                os.P_PID,
                process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            # Another waiter reaped the leader, so numeric group ownership was
            # lost.  Do not risk signalling a reused PGID.
            return
        time.sleep(0.02)

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _stream_file(
    args: argparse.Namespace,
    *,
    append: bool,
    copy_stdout: bool,
    execute: list[str] | None = None,
) -> int:
    parts = _relative_parts(args.path, args.root_path, args.root_anchor)
    process: subprocess.Popen[bytes] | None = None
    managed_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    prior_signal_handlers: dict[int, signal.Handlers] = {}
    prior_signal_mask: set[signal.Signals] | None = None
    spawn_complete = False
    pending_forward_signal: int | None = None
    with _open_anchor(args.root_path, args.root_anchor, args.root_identity) as root_fd:
        with _open_parent(root_fd, parts, create=args.parents) as (parent_fd, name):
            _run_test_hook()
            _verify_anchor_path(args.root_anchor, root_fd)
            _verify_directory_path(
                root_fd, parts[:-1], parent_fd, "stream parent"
            )
            temporary_name = ""
            output_fd = -1
            try:
                if append:
                    flags = (
                        os.O_WRONLY
                        | os.O_APPEND
                        | os.O_CREAT
                        | os.O_CLOEXEC
                        | os.O_NONBLOCK
                    )
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    output_fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
                else:
                    temporary_name = _random_temporary_name(name)
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_CLOEXEC
                        | os.O_NONBLOCK
                    )
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    output_fd = os.open(
                        temporary_name, flags, 0o600, dir_fd=parent_fd
                    )
                    os.fsync(output_fd)
                    _publish_temporary(
                        parent_fd,
                        temporary_name,
                        name,
                        output_fd,
                        replace=True,
                    )
                    temporary_name = ""
                opened = os.fstat(output_fd)
                _require_regular(opened, f"stream destination {args.path}")
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not _same_identity(opened, current):
                    raise SafePathError("stream destination path changed before write")

                if execute is not None:
                    if not execute:
                        raise SafePathError("exec-tee requires a command")

                    def forward_signal(signum, _frame):
                        nonlocal pending_forward_signal, spawn_complete
                        if pending_forward_signal is None:
                            pending_forward_signal = signum
                        if spawn_complete:
                            # Prevent a second signal from re-entering cleanup
                            # before the managed signals are blocked there.
                            spawn_complete = False
                            raise _ForwardedSignal(pending_forward_signal)

                    # Install deferring handlers atomically, then unblock these
                    # signals before Popen so the child cannot inherit a caller's
                    # blocked TERM/HUP/INT mask.  A signal received before the
                    # Popen result is assigned is recorded and raised only after
                    # the child becomes an owned process.
                    prior_signal_mask = signal.pthread_sigmask(
                        signal.SIG_BLOCK, managed_signals
                    )
                    for signum in managed_signals:
                        prior_signal_handlers[signum] = signal.getsignal(signum)
                        signal.signal(signum, forward_signal)
                    spawn_mask = set(prior_signal_mask)
                    spawn_mask.difference_update(managed_signals)
                    signal.pthread_sigmask(signal.SIG_SETMASK, spawn_mask)
                    if pending_forward_signal is not None:
                        raise _ForwardedSignal(pending_forward_signal)
                    try:
                        process = subprocess.Popen(
                            execute,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                            restore_signals=True,
                        )
                    except BaseException:
                        spawn_complete = True
                        if pending_forward_signal is not None:
                            raise _ForwardedSignal(pending_forward_signal)
                        raise
                    spawn_complete = True
                    if pending_forward_signal is not None:
                        raise _ForwardedSignal(pending_forward_signal)
                    if process.stdout is None:
                        raise SafePathError("could not capture command output")
                    reader: BinaryIO = process.stdout
                else:
                    reader = sys.stdin.buffer
                read_chunk = getattr(reader, "read1", reader.read)
                written = 0
                while True:
                    chunk = read_chunk(COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > args.max_bytes:
                        raise SafePathError(
                            f"stream exceeds {args.max_bytes} bytes"
                        )
                    _write_chunks(output_fd, chunk)
                    if copy_stdout:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                if process is not None:
                    process.stdout.close()
                    command_status = process.wait()
                    # SCFUZZBENCH_POST_WAIT_SIGNAL_BARRIER
                    signal.pthread_sigmask(signal.SIG_BLOCK, managed_signals)
                    spawn_complete = False
                else:
                    command_status = 0
                os.fsync(output_fd)
                finished = os.fstat(output_fd)
                _require_regular(finished, f"stream destination {args.path}")
                if not _same_inode_type(opened, finished):
                    raise SafePathError("stream destination inode changed")
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not _same_entry(finished, current):
                    raise SafePathError("stream destination path changed while writing")
                _verify_anchor_path(args.root_anchor, root_fd)
                _verify_directory_path(
                    root_fd, parts[:-1], parent_fd, "stream parent"
                )
                if command_status < 0:
                    return 128 - command_status
                return command_status
            except _ForwardedSignal as exc:
                if prior_signal_mask is not None:
                    signal.pthread_sigmask(signal.SIG_BLOCK, managed_signals)
                if process is not None:
                    _terminate_owned_process_group(process, exc.signum)
                return 128 + exc.signum
            except BaseException:
                if prior_signal_mask is not None:
                    signal.pthread_sigmask(signal.SIG_BLOCK, managed_signals)
                if process is not None:
                    _terminate_owned_process_group(process)
                raise
            finally:
                spawn_complete = False
                if prior_signal_mask is not None:
                    signal.pthread_sigmask(signal.SIG_BLOCK, managed_signals)
                for signum, handler in prior_signal_handlers.items():
                    signal.signal(signum, handler)
                if prior_signal_mask is not None:
                    signal.pthread_sigmask(signal.SIG_SETMASK, prior_signal_mask)
                if output_fd >= 0:
                    os.close(output_fd)
                if temporary_name:
                    try:
                        os.unlink(temporary_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass


def _zip_info(name: str, st: os.stat_result, *, directory: bool = False) -> zipfile.ZipInfo:
    bounded_seconds = min(max(st.st_mtime, 315532800), 4354819198)
    timestamp_value = dt.datetime.fromtimestamp(
        bounded_seconds, tz=dt.timezone.utc
    )
    timestamp = (
        timestamp_value.year,
        timestamp_value.month,
        timestamp_value.day,
        timestamp_value.hour,
        timestamp_value.minute,
        timestamp_value.second,
    )
    info = zipfile.ZipInfo(name, timestamp)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = st.st_mode
    if directory:
        mode = stat.S_IFDIR | (stat.S_IMODE(mode) or 0o700)
        info.external_attr = (mode & 0xFFFF) << 16 | 0x10
    else:
        mode = stat.S_IFREG | (stat.S_IMODE(mode) or 0o600)
        info.external_attr = (mode & 0xFFFF) << 16
    return info


@dataclass
class _ArchiveBudget:
    max_files: int
    max_entries: int
    max_bytes: int
    files: int = 0
    entries: int = 0
    bytes: int = 0

    def add_entry(self, name: str, depth: int) -> None:
        self.entries += 1
        if self.entries > self.max_entries:
            raise SafePathError(
                f"archive contains more than {self.max_entries} entries"
            )
        if depth > MAX_ARCHIVE_DEPTH:
            raise SafePathError(
                f"archive nesting exceeds {MAX_ARCHIVE_DEPTH} directories"
            )
        if len(name.encode("utf-8", errors="surrogateescape")) > MAX_ARCHIVE_PATH_BYTES:
            raise SafePathError(
                f"archive path exceeds {MAX_ARCHIVE_PATH_BYTES} bytes"
            )

    def add_file(self, size: int) -> None:
        self.files += 1
        self.bytes += size
        if self.files > self.max_files:
            raise SafePathError(
                f"archive contains more than {self.max_files} regular files"
            )
        if self.bytes > self.max_bytes:
            raise SafePathError(
                f"archive inputs exceed {self.max_bytes} bytes"
            )


def _safe_entry_names(directory_fd: int) -> list[str]:
    names = sorted(os.listdir(directory_fd), key=os.fsencode)
    for name in names:
        if name in {"", ".", ".."} or "/" in name or "\x00" in name:
            raise SafePathError(f"unsafe archive entry name: {name!r}")
    return names


def _archive_directory(
    archive: zipfile.ZipFile,
    directory_fd: int,
    archive_prefix: str,
    budget: _ArchiveBudget,
    depth: int = 0,
) -> None:
    directory_before = os.fstat(directory_fd)
    _require_directory(directory_before, archive_prefix)
    names_before = _safe_entry_names(directory_fd)
    for name in names_before:
        listed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        archive_name = f"{archive_prefix}/{name}"
        budget.add_entry(archive_name, depth)
        if stat.S_ISLNK(listed.st_mode):
            raise SafePathError(f"archive entry is a symlink: {archive_name}")
        if stat.S_ISDIR(listed.st_mode):
            child_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                if not _same_entry(os.fstat(child_fd), listed):
                    raise SafePathError(
                        f"archive directory changed before open: {archive_name}"
                    )
                archive.writestr(
                    _zip_info(f"{archive_name}/", listed, directory=True), b""
                )
                _archive_directory(
                    archive, child_fd, archive_name, budget, depth + 1
                )
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_entry(listed, after):
                    raise SafePathError(
                        f"archive directory path changed: {archive_name}"
                    )
            finally:
                os.close(child_fd)
            continue
        _require_regular(listed, f"archive entry {archive_name}")
        budget.add_file(listed.st_size)
        source_fd = os.open(name, FILE_READ_FLAGS, dir_fd=directory_fd)
        try:
            opened = os.fstat(source_fd)
            if not _same_entry(opened, listed):
                raise SafePathError(
                    f"archive file changed before open: {archive_name}"
                )
            with archive.open(_zip_info(archive_name, listed), "w") as destination:
                copied = 0
                while True:
                    chunk = os.read(source_fd, COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > listed.st_size:
                        raise SafePathError(
                            f"archive file grew while read: {archive_name}"
                        )
                    destination.write(chunk)
            if copied != listed.st_size or not _same_entry(
                os.fstat(source_fd), listed
            ):
                raise SafePathError(f"archive file changed: {archive_name}")
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _same_entry(current, listed):
                raise SafePathError(
                    f"archive file path changed: {archive_name}"
                )
        finally:
            os.close(source_fd)
    if names_before != _safe_entry_names(directory_fd):
        raise SafePathError(f"archive directory entries changed: {archive_prefix}")
    if not _same_entry(directory_before, os.fstat(directory_fd)):
        raise SafePathError(f"archive directory changed: {archive_prefix}")


def _archive(args: argparse.Namespace) -> None:
    source_parts = _relative_parts(
        args.source,
        args.source_root_path,
        args.source_root_anchor,
        allow_root=True,
    )
    destination_parts = _relative_parts(
        args.destination,
        args.destination_root_path,
        args.destination_root_anchor,
    )
    normalized_source = os.path.normpath(args.source)
    normalized_destination = os.path.normpath(args.destination)
    try:
        destination_within_source = (
            os.path.commonpath((normalized_source, normalized_destination))
            == normalized_source
        )
    except ValueError:
        destination_within_source = False
    if destination_within_source:
        raise SafePathError("archive destination must not be inside its source")

    output_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        output_flags |= os.O_NOFOLLOW
    with _open_anchor(
        args.source_root_path,
        args.source_root_anchor,
        args.source_root_identity,
    ) as source_root_fd:
        source_fd = _open_directory(source_root_fd, source_parts)
        try:
            source_stat = os.fstat(source_fd)
            _require_directory(source_stat, "archive source")
            if args.source_identity and _identity(source_stat) != args.source_identity:
                raise SafePathError("archive source identity changed")
            with _open_anchor(
                args.destination_root_path,
                args.destination_root_anchor,
                args.destination_root_identity,
            ) as destination_root_fd:
                with _open_parent(
                    destination_root_fd,
                    destination_parts,
                    create=args.parents,
                ) as (destination_parent_fd, destination_name):
                    _run_test_hook()
                    _verify_anchor_path(
                        args.source_root_anchor, source_root_fd
                    )
                    _verify_anchor_path(
                        args.destination_root_anchor,
                        destination_root_fd,
                    )
                    _verify_directory_path(
                        source_root_fd,
                        source_parts,
                        source_fd,
                        "archive source",
                    )
                    _verify_directory_path(
                        destination_root_fd,
                        destination_parts[:-1],
                        destination_parent_fd,
                        "archive destination parent",
                    )
                    current_source = os.fstat(source_fd)
                    if not _same_entry(source_stat, current_source):
                        raise SafePathError("archive source changed before traversal")
                    temporary_name = _random_temporary_name(destination_name)
                    output_fd = -1
                    try:
                        output_fd = os.open(
                            temporary_name,
                            output_flags,
                            0o600,
                            dir_fd=destination_parent_fd,
                        )
                        with os.fdopen(os.dup(output_fd), "w+b") as output:
                            with zipfile.ZipFile(
                                output,
                                mode="w",
                                compression=zipfile.ZIP_DEFLATED,
                                compresslevel=6,
                                allowZip64=True,
                            ) as archive:
                                prefix = os.path.basename(normalized_source)
                                if prefix in {"", ".", ".."}:
                                    raise SafePathError(
                                        "archive source has no safe basename"
                                    )
                                archive.writestr(
                                    _zip_info(
                                        f"{prefix}/",
                                        source_stat,
                                        directory=True,
                                    ),
                                    b"",
                                )
                                budget = _ArchiveBudget(
                                    args.max_files,
                                    args.max_entries,
                                    args.max_bytes,
                                )
                                _archive_directory(
                                    archive, source_fd, prefix, budget
                                )
                            output.flush()
                            os.fsync(output.fileno())
                        if not _same_entry(source_stat, os.fstat(source_fd)):
                            raise SafePathError("archive source changed during traversal")
                        os.fsync(output_fd)
                        _verify_anchor_path(
                            args.source_root_anchor, source_root_fd
                        )
                        _verify_anchor_path(
                            args.destination_root_anchor,
                            destination_root_fd,
                        )
                        _verify_directory_path(
                            source_root_fd,
                            source_parts,
                            source_fd,
                            "archive source",
                        )
                        _verify_directory_path(
                            destination_root_fd,
                            destination_parts[:-1],
                            destination_parent_fd,
                            "archive destination parent",
                        )
                        _publish_temporary(
                            destination_parent_fd,
                            temporary_name,
                            destination_name,
                            output_fd,
                            replace=args.replace,
                        )
                        temporary_name = ""
                        _verify_anchor_path(
                            args.destination_root_anchor,
                            destination_root_fd,
                        )
                        _verify_directory_path(
                            destination_root_fd,
                            destination_parts[:-1],
                            destination_parent_fd,
                            "archive destination parent",
                        )
                    finally:
                        if output_fd >= 0:
                            os.close(output_fd)
                        if temporary_name:
                            try:
                                os.unlink(
                                    temporary_name,
                                    dir_fd=destination_parent_fd,
                                )
                            except FileNotFoundError:
                                pass
        finally:
            os.close(source_fd)


def _compare_json_fields(args: argparse.Namespace) -> None:
    left_parts = _relative_parts(
        args.left, args.left_root_path, args.left_root_anchor
    )
    right_parts = _relative_parts(
        args.right, args.right_root_path, args.right_root_anchor
    )
    with _open_anchor(
        args.left_root_path,
        args.left_root_anchor,
        args.left_root_identity,
    ) as left_root_fd:
        left_data = _read_stable_file(
            left_root_fd, left_parts, max_bytes=args.max_bytes
        )
    with _open_anchor(
        args.right_root_path,
        args.right_root_anchor,
        args.right_root_identity,
    ) as right_root_fd:
        right_data = _read_stable_file(
            right_root_fd, right_parts, max_bytes=args.max_bytes
        )
    try:
        left = json.loads(left_data)
        right = json.loads(right_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafePathError("compared file is not valid UTF-8 JSON") from exc
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise SafePathError("compared JSON roots must be objects")
    for field in args.field:
        if left.get(field) != right.get(field):
            raise SafePathError(f"JSON field differs: {field}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in (
        "remove-file",
        "remove-tree",
        "mkdir",
        "read-file",
        "write-file",
        "append-file",
        "stream-file",
        "tee-file",
    ):
        subparser = subparsers.add_parser(command)
        _root_args(subparser)
        subparser.add_argument("--path", required=True)
        if command == "mkdir":
            subparser.add_argument("--mode", default="0700")
            subparser.add_argument("--parents", action="store_true")
            subparser.add_argument("--exist-ok", action="store_true")
        if command == "read-file":
            subparser.add_argument(
                "--max-bytes", type=int, default=DEFAULT_READ_MAX_BYTES
            )
        if command == "write-file":
            subparser.add_argument("--parents", action="store_true")
            subparser.add_argument("--replace", action="store_true")
            subparser.add_argument(
                "--max-bytes", type=int, default=DEFAULT_WRITE_MAX_BYTES
            )
        if command in {"append-file", "stream-file", "tee-file"}:
            subparser.add_argument("--parents", action="store_true")
            subparser.add_argument(
                "--max-bytes", type=int, default=DEFAULT_STREAM_MAX_BYTES
            )

    exec_tee = subparsers.add_parser("exec-tee")
    _root_args(exec_tee)
    exec_tee.add_argument("--path", required=True)
    exec_tee.add_argument("--parents", action="store_true")
    exec_tee.add_argument(
        "--max-bytes", type=int, default=DEFAULT_STREAM_MAX_BYTES
    )
    exec_tee.add_argument("execute", nargs=argparse.REMAINDER)

    move = subparsers.add_parser("move")
    _root_args(move, "source")
    _root_args(move, "destination")
    move.add_argument("--source", required=True)
    move.add_argument("--destination", required=True)
    move.add_argument(
        "--source-type", required=True, choices=("directory", "regular")
    )
    move.add_argument("--source-identity", default="")
    move.add_argument("--replace", action="store_true")

    archive = subparsers.add_parser("archive")
    _root_args(archive, "source")
    _root_args(archive, "destination")
    archive.add_argument("--source", required=True)
    archive.add_argument("--source-identity", required=True)
    archive.add_argument("--destination", required=True)
    archive.add_argument("--parents", action="store_true")
    archive.add_argument("--replace", action="store_true")
    archive.add_argument(
        "--max-files", type=int, default=DEFAULT_ARCHIVE_MAX_FILES
    )
    archive.add_argument(
        "--max-entries", type=int, default=DEFAULT_ARCHIVE_MAX_ENTRIES
    )
    archive.add_argument(
        "--max-bytes", type=int, default=DEFAULT_ARCHIVE_MAX_BYTES
    )

    compare = subparsers.add_parser("compare-json-fields")
    _root_args(compare, "left")
    _root_args(compare, "right")
    compare.add_argument("--left", required=True)
    compare.add_argument("--right", required=True)
    compare.add_argument("--field", action="append", required=True)
    compare.add_argument(
        "--max-bytes", type=int, default=DEFAULT_READ_MAX_BYTES
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        for numeric_name in ("max_bytes", "max_files", "max_entries"):
            value = getattr(args, numeric_name, 1)
            if value < 1:
                raise SafePathError(f"{numeric_name} must be positive")
        if args.command == "remove-file":
            _remove(args, tree=False)
        elif args.command == "remove-tree":
            _remove(args, tree=True)
        elif args.command == "mkdir":
            _mkdir(args)
        elif args.command == "move":
            _move(args)
        elif args.command == "read-file":
            _read_file(args)
        elif args.command == "write-file":
            _write_file(args)
        elif args.command == "append-file":
            _stream_file(args, append=True, copy_stdout=False)
        elif args.command == "stream-file":
            _stream_file(args, append=False, copy_stdout=False)
        elif args.command == "tee-file":
            _stream_file(args, append=False, copy_stdout=True)
        elif args.command == "exec-tee":
            execute = args.execute
            if execute[:1] == ["--"]:
                execute = execute[1:]
            return _stream_file(
                args,
                append=False,
                copy_stdout=True,
                execute=execute,
            )
        elif args.command == "archive":
            _archive(args)
        elif args.command == "compare-json-fields":
            _compare_json_fields(args)
        else:
            raise SafePathError(f"unsupported command: {args.command}")
    except (
        OSError,
        RecursionError,
        SafePathError,
        subprocess.CalledProcessError,
        struct.error,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        print(f"safe path operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
