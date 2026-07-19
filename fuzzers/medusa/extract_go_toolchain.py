#!/usr/bin/env python3
"""Stream-extract a bounded official Go linux-amd64 archive."""

from __future__ import annotations

import argparse
import shutil
import tarfile
from pathlib import Path


DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 20_000
DEFAULT_MAX_DEPTH = 16
CHUNK_SIZE = 1024 * 1024


class ToolchainArchiveError(RuntimeError):
    """Raised when the Go distribution violates the extraction contract."""


def _safe_parts(name: str, max_depth: int) -> tuple[str, ...]:
    normalized = name.rstrip("/")
    if not normalized or normalized.startswith("/") or "\\" in normalized:
        raise ToolchainArchiveError(f"unsafe Go archive path: {name!r}")
    parts = tuple(normalized.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ToolchainArchiveError(f"unsafe Go archive path: {name!r}")
    if parts[0] != "go":
        raise ToolchainArchiveError(f"Go archive entry is outside go/: {name!r}")
    if len(parts) > max_depth:
        raise ToolchainArchiveError(
            f"Go archive path exceeds maximum depth ({len(parts)} > {max_depth})"
        )
    return parts


def extract_go_toolchain(
    archive: Path,
    destination: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Path:
    if max_bytes <= 0 or max_entries <= 0 or max_depth <= 0:
        raise ToolchainArchiveError("extraction limits must be positive")
    destination.mkdir(parents=True, exist_ok=False)

    expanded = 0
    entries = 0
    try:
        bundle = tarfile.open(archive, mode="r|gz")
    except (OSError, tarfile.TarError) as exc:
        raise ToolchainArchiveError(f"invalid Go distribution: {exc}") from exc

    try:
        with bundle:
            for member in bundle:
                entries += 1
                if entries > max_entries:
                    raise ToolchainArchiveError(
                        f"Go archive has too many entries ({entries} > {max_entries})"
                    )
                parts = _safe_parts(member.name, max_depth)
                if not (member.isdir() or member.isreg()):
                    raise ToolchainArchiveError(
                        f"links and special files are not allowed: {member.name!r}"
                    )
                if member.size < 0:
                    raise ToolchainArchiveError(
                        f"invalid Go archive entry size: {member.name!r}"
                    )
                expanded += member.size
                if expanded > max_bytes:
                    raise ToolchainArchiveError(
                        f"Go archive expands beyond {max_bytes} bytes"
                    )

                output = destination.joinpath(*parts)
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue

                source = bundle.extractfile(member)
                if source is None:
                    raise ToolchainArchiveError(
                        f"could not read Go archive entry: {member.name!r}"
                    )
                output.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with source, output.open("xb") as target:
                    while True:
                        chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > member.size or expanded - member.size + written > max_bytes:
                            raise ToolchainArchiveError(
                                f"Go archive entry exceeds its declared or allowed size: {member.name!r}"
                            )
                        target.write(chunk)
                if written != member.size:
                    raise ToolchainArchiveError(
                        f"Go archive entry size mismatch for {member.name!r}"
                    )
                output.chmod(member.mode & 0o777)
    except (OSError, tarfile.TarError, shutil.ReadError) as exc:
        raise ToolchainArchiveError(f"could not extract Go distribution: {exc}") from exc

    go_binary = destination / "go" / "bin" / "go"
    if not go_binary.is_file() or go_binary.is_symlink():
        raise ToolchainArchiveError("Go distribution does not contain a regular go/bin/go")
    return go_binary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    args = parser.parse_args()

    try:
        binary = extract_go_toolchain(
            args.archive,
            args.destination,
            max_bytes=args.max_bytes,
            max_entries=args.max_entries,
            max_depth=args.max_depth,
        )
    except (OSError, ToolchainArchiveError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(binary.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
