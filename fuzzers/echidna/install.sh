#!/usr/bin/env bash
set -euo pipefail

source "${SCFUZZBENCH_COMMON_SH:-/opt/scfuzzbench/common.sh}"

prepare_workspace
install_base_packages
install_foundry
install_crytic_compile
install_slither_analyzer

require_env ECHIDNA_VERSION
if [[ ! "${ECHIDNA_VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]; then
  log "Invalid ECHIDNA_VERSION: expected a release version without spaces or path separators"
  exit 1
fi
log "Installing Echidna ${ECHIDNA_VERSION}"

tmp_dir=$(mktemp -d)
cleanup() {
  rm -rf -- "${tmp_dir}"
}
trap cleanup EXIT

archive="echidna-${ECHIDNA_VERSION}-x86_64-linux.tar.gz"
url="https://github.com/crytic/echidna/releases/download/v${ECHIDNA_VERSION}/${archive}"

curl -fsSL "${url}" -o "${tmp_dir}/${archive}"

# Read only the supported executable from the release archive. Avoid extracting
# archive paths onto disk, and reject ambiguous, linked, or non-x86_64 payloads.
# Older releases may use echidna-test as the member name; it is accepted only
# here and is immediately normalized to the canonical echidna command.
python3 - "${tmp_dir}/${archive}" "${tmp_dir}/echidna" <<'PY'
import shutil
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

archive_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
supported_names = {"echidna", "echidna-test"}
max_binary_size = 256 * 1024 * 1024

try:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        candidates = []
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"unsafe archive path: {member.name!r}")
            if member_path.name in supported_names:
                if not member.isfile():
                    raise ValueError(
                        f"supported binary entry is not a regular file: {member.name!r}"
                    )
                candidates.append(member)

        if len(candidates) != 1:
            names = ", ".join(repr(member.name) for member in candidates) or "none"
            raise ValueError(
                "expected exactly one regular echidna or echidna-test binary; "
                f"found {names}"
            )

        member = candidates[0]
        if member.size <= 0 or member.size > max_binary_size:
            raise ValueError(f"unexpected Echidna binary size: {member.size} bytes")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError(f"could not read archive member: {member.name!r}")

        header = source.read(20)
        if (
            len(header) < 20
            or header[:4] != b"\x7fELF"
            or header[4] != 2  # ELFCLASS64
            or header[5] != 1  # ELFDATA2LSB
            or int.from_bytes(header[18:20], "little") != 62  # EM_X86_64
        ):
            raise ValueError("Echidna release binary is not a 64-bit x86_64 ELF")

        with output_path.open("wb") as output:
            output.write(header)
            shutil.copyfileobj(source, output)
        output_path.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
except (OSError, tarfile.TarError, ValueError) as exc:
    raise SystemExit(f"Invalid Echidna release archive: {exc}") from exc
PY

installed_version=$("${tmp_dir}/echidna" --version)
if [[ "${installed_version}" != *"${ECHIDNA_VERSION}"* ]]; then
  log "Installed Echidna version does not match ${ECHIDNA_VERSION}: ${installed_version}"
  exit 1
fi

install -m 0755 "${tmp_dir}/echidna" "${SCFUZZBENCH_BIN_DIR}/echidna"
rm -f -- "${SCFUZZBENCH_BIN_DIR}/echidna-test"

log "Installed ${installed_version}"
command -v echidna >/dev/null
