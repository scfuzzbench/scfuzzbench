#!/usr/bin/env python3
"""Safely extract one Echidna binary from a GitHub Actions artifact.

GitHub Actions returns an outer ZIP. Echidna's workflows currently put a
``.tar.gz`` inside that ZIP, while older/custom workflows may upload the binary
directly. This extractor supports both layouts without trusting archive paths,
links, entry counts, or advertised uncompressed sizes.
"""

from __future__ import annotations

import argparse
import stat
import tarfile
import zipfile
from pathlib import Path


DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 2_000
CHUNK_SIZE = 1024 * 1024
BINARY_NAMES = frozenset({"echidna", "echidna-test"})
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz")


class ArtifactError(RuntimeError):
    """Raised when an artifact cannot be extracted safely."""


def _relative_parts(name: str) -> tuple[str, ...]:
    normalized = name.rstrip("/")
    if not normalized:
        raise ArtifactError("archive contains an empty path")
    if "\\" in normalized or normalized.startswith("/"):
        raise ArtifactError(f"unsafe archive path: {name!r}")
    parts = tuple(normalized.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactError(f"unsafe archive path: {name!r}")
    if ":" in parts[0]:
        raise ArtifactError(f"unsafe archive path: {name!r}")
    return parts


def _copy_limited(source, destination: Path, expected_size: int, max_bytes: int) -> int:
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > expected_size or written > max_bytes:
                raise ArtifactError(f"archive entry exceeds its declared or allowed size: {destination.name}")
            output.write(chunk)
    if written != expected_size:
        raise ArtifactError(
            f"archive entry size mismatch for {destination.name}: expected {expected_size}, got {written}"
        )
    return written


def _extract_zip(archive: Path, destination: Path, max_bytes: int, max_entries: int) -> int:
    try:
        bundle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArtifactError(f"invalid GitHub Actions ZIP: {exc}") from exc

    with bundle:
        entries = bundle.infolist()
        if not entries:
            raise ArtifactError("GitHub Actions artifact is empty")
        if len(entries) > max_entries:
            raise ArtifactError(f"artifact has too many entries ({len(entries)} > {max_entries})")

        total_size = 0
        for entry in entries:
            parts = _relative_parts(entry.filename)
            mode = entry.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFLNK:
                raise ArtifactError(f"links are not allowed in artifacts: {entry.filename!r}")
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ArtifactError(f"special files are not allowed in artifacts: {entry.filename!r}")
            if entry.flag_bits & 0x1:
                raise ArtifactError(f"encrypted entries are not allowed: {entry.filename!r}")
            if entry.file_size < 0:
                raise ArtifactError(f"invalid entry size: {entry.filename!r}")
            total_size += entry.file_size
            if total_size > max_bytes:
                raise ArtifactError(f"artifact expands beyond {max_bytes} bytes")

            output_path = destination.joinpath(*parts)
            if entry.is_dir() or file_type == stat.S_IFDIR:
                output_path.mkdir(parents=True, exist_ok=True)
                continue
            with bundle.open(entry, "r") as source:
                _copy_limited(source, output_path, entry.file_size, max_bytes)

    return total_size


def _extract_tar(
    archive: Path,
    destination: Path,
    max_bytes: int,
    max_entries: int,
) -> int:
    try:
        bundle = tarfile.open(archive, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise ArtifactError(f"invalid nested tar archive {archive.name!r}: {exc}") from exc

    with bundle:
        entries = bundle.getmembers()
        if len(entries) > max_entries:
            raise ArtifactError(f"nested archive has too many entries ({len(entries)} > {max_entries})")

        total_size = 0
        for entry in entries:
            parts = _relative_parts(entry.name)
            if not (entry.isdir() or entry.isreg()):
                raise ArtifactError(f"links and special files are not allowed: {entry.name!r}")
            if entry.size < 0:
                raise ArtifactError(f"invalid entry size: {entry.name!r}")
            total_size += entry.size
            if total_size > max_bytes:
                raise ArtifactError(f"nested archive expands beyond {max_bytes} bytes")

            output_path = destination.joinpath(*parts)
            if entry.isdir():
                output_path.mkdir(parents=True, exist_ok=True)
                continue
            source = bundle.extractfile(entry)
            if source is None:
                raise ArtifactError(f"could not read nested archive entry: {entry.name!r}")
            with source:
                _copy_limited(source, output_path, entry.size, max_bytes)

    return total_size


def _binary_candidates(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name in BINARY_NAMES
    )


def _require_linux_x86_64_elf(binary: Path) -> None:
    with binary.open("rb") as handle:
        header = handle.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise ArtifactError("Echidna binary is not an ELF executable")
    if header[4] != 2:
        raise ArtifactError("Echidna binary is not a 64-bit ELF executable")
    if header[5] not in {1, 2}:
        raise ArtifactError("Echidna ELF has an invalid byte order")
    byteorder = "little" if header[5] == 1 else "big"
    machine = int.from_bytes(header[18:20], byteorder=byteorder)
    if machine != 62:
        raise ArtifactError(f"Echidna ELF architecture is not x86-64 (machine={machine})")


def extract_echidna(
    artifact: Path,
    destination: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> Path:
    if max_bytes <= 0 or max_entries <= 0:
        raise ArtifactError("extraction limits must be positive")
    if artifact.stat().st_size > max_bytes:
        raise ArtifactError(f"artifact download exceeds {max_bytes} bytes")

    destination.mkdir(parents=True, exist_ok=False)
    outer = destination / "outer"
    outer.mkdir()
    consumed = _extract_zip(artifact, outer, max_bytes, max_entries)

    candidates = _binary_candidates(outer)
    nested_archives = sorted(
        path
        for path in outer.rglob("*")
        if path.is_file() and path.name.lower().endswith(TAR_SUFFIXES)
    )
    for index, nested_archive in enumerate(nested_archives):
        nested_destination = destination / f"nested-{index}"
        nested_destination.mkdir()
        remaining = max_bytes - consumed
        if remaining <= 0:
            raise ArtifactError(f"artifact expands beyond {max_bytes} bytes")
        consumed += _extract_tar(nested_archive, nested_destination, remaining, max_entries)
        candidates.extend(_binary_candidates(nested_destination))

    unique_candidates = sorted(set(candidates))
    if not unique_candidates:
        raise ArtifactError("artifact does not contain an echidna or echidna-test binary")
    if len(unique_candidates) != 1:
        rendered = ", ".join(str(path.relative_to(destination)) for path in unique_candidates)
        raise ArtifactError(f"artifact contains multiple Echidna binaries: {rendered}")

    binary = unique_candidates[0]
    _require_linux_x86_64_elf(binary)
    return binary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    args = parser.parse_args()

    try:
        binary = extract_echidna(
            args.artifact,
            args.destination,
            max_bytes=args.max_bytes,
            max_entries=args.max_entries,
        )
    except (ArtifactError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(binary.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
