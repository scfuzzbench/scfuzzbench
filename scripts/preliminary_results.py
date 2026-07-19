#!/usr/bin/env python3
"""Discover, verify, analyze, watermark, and publish preliminary checkpoints."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile


RUN_SCHEMA = "scfuzzbench-preliminary-run/v1"
SNAPSHOT_SCHEMA = "scfuzzbench-preliminary-snapshot/v1"
RESULT_SCHEMA = "scfuzzbench-preliminary-analysis/v1"
FINALIZED_SCHEMA = "scfuzzbench-preliminary-finalized/v1"
RUN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}"
UUID_PATTERN = r"[0-9a-f]{32}"
RUN_KEY_RE = re.compile(
    rf"^preliminary/(?P<run>{RUN_ID_PATTERN})/(?P<uuid>{UUID_PATTERN})/run\.json$"
)
SNAPSHOT_KEY_RE = re.compile(
    rf"^preliminary/(?P<run>{RUN_ID_PATTERN})/(?P<uuid>{UUID_PATTERN})/"
    r"snapshots/(?P<checkpoint>[0-9]{6})/(?P<identity>[A-Za-z0-9][A-Za-z0-9._-]{0,255})/snapshot\.zip$"
)
ANALYSIS_META_RE = re.compile(
    rf"^preliminary/(?P<run>{RUN_ID_PATTERN})/(?P<uuid>{UUID_PATTERN})/"
    r"analysis/(?P<checkpoint>[0-9]{6})/preliminary\.json$"
)
FINALIZED_KEY_RE = re.compile(
    rf"^preliminary/(?P<run>{RUN_ID_PATTERN})/(?P<uuid>{UUID_PATTERN})/finalized\.json$"
)
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")


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


def run_prefix(run_id: str, benchmark_uuid: str) -> str:
    key = f"preliminary/{run_id}/{benchmark_uuid}"
    if not RUN_KEY_RE.fullmatch(f"{key}/run.json"):
        raise ValueError("unsafe preliminary run identity")
    return key


def analysis_prefix(run_id: str, benchmark_uuid: str, checkpoint: int) -> str:
    if checkpoint < 1 or checkpoint > 999999:
        raise ValueError("checkpoint is out of range")
    return f"{run_prefix(run_id, benchmark_uuid)}/analysis/{checkpoint:06d}"


def assert_preliminary_key(key: str) -> None:
    if not key.startswith("preliminary/"):
        raise ValueError(f"refusing non-preliminary S3 key: {key}")
    if any(part in {"", ".", ".."} for part in PurePosixPath(key).parts):
        raise ValueError(f"unsafe S3 key: {key}")


def aws_json(args: list[str]) -> dict:
    output = subprocess.check_output(["aws", *args, "--output", "json"], text=True)
    return json.loads(output) if output.strip() else {}


def list_keys(bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token = ""
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix]
        if token:
            args.extend(["--continuation-token", token])
        payload = aws_json(args)
        keys.extend(str(item["Key"]) for item in payload.get("Contents", []))
        if not payload.get("IsTruncated"):
            return keys
        token = str(payload.get("NextContinuationToken", ""))
        if not token:
            raise RuntimeError("S3 pagination was truncated without a continuation token")


def read_s3_json(bucket: str, key: str) -> dict:
    assert_preliminary_key(key)
    output = subprocess.check_output(
        ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-"], text=True
    )
    value = json.loads(output)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must contain a JSON object")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if parsed <= 0 or str(value).strip() not in {str(parsed), f"{parsed}.0"}:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def validate_run_manifest(manifest: dict, *, run_id: str, benchmark_uuid: str) -> dict:
    if manifest.get("schema") != RUN_SCHEMA:
        raise ValueError("invalid preliminary run schema")
    if manifest.get("run_id") != run_id:
        raise ValueError("run manifest run_id does not match its key")
    if manifest.get("benchmark_uuid") != benchmark_uuid:
        raise ValueError("run manifest benchmark_uuid does not match its key")
    started = _positive_int(manifest.get("run_started_at_epoch"), "run_started_at_epoch")
    try:
        timeout_hours = float(manifest.get("timeout_hours"))
    except (TypeError, ValueError):
        raise ValueError("timeout_hours must be positive") from None
    if not math.isfinite(timeout_hours) or timeout_hours <= 0:
        raise ValueError("timeout_hours must be positive")
    preliminary = manifest.get("preliminary")
    if not isinstance(preliminary, dict):
        raise ValueError("run manifest is missing preliminary settings")
    enabled_raw = preliminary.get("enabled")
    if not isinstance(enabled_raw, bool):
        raise ValueError("preliminary.enabled must be a boolean")
    enabled = enabled_raw
    interval_raw = preliminary.get("interval_seconds", 0)
    if enabled:
        interval = _positive_int(interval_raw, "preliminary.interval_seconds")
    else:
        try:
            interval = int(interval_raw)
        except (TypeError, ValueError):
            raise ValueError("disabled preliminary interval must be zero") from None
        if isinstance(interval_raw, bool) or str(interval_raw).strip() not in {
            "0",
            "0.0",
        }:
            raise ValueError("disabled preliminary interval must be zero")
        if interval != 0:
            raise ValueError("disabled preliminary interval must be zero")
    fuzzers = manifest.get("fuzzer_keys")
    if not isinstance(fuzzers, list) or not fuzzers:
        raise ValueError("fuzzer_keys must be a non-empty list")
    if any(not isinstance(item, str) or not SAFE_LABEL_RE.fullmatch(item) for item in fuzzers):
        raise ValueError("fuzzer_keys contains an unsafe value")
    if len(fuzzers) != len(set(fuzzers)):
        raise ValueError("fuzzer_keys must be unique")
    instances = _positive_int(manifest.get("instances_per_fuzzer"), "instances_per_fuzzer")
    return {
        **manifest,
        "run_started_at_epoch": started,
        "timeout_hours": timeout_hours,
        "preliminary": {
            **preliminary,
            "enabled": enabled,
            "interval_seconds": interval,
        },
        "fuzzer_keys": fuzzers,
        "instances_per_fuzzer": instances,
    }


def expected_legs(manifest: dict) -> set[tuple[str, str]]:
    return {
        (fuzzer, str(index))
        for fuzzer in manifest["fuzzer_keys"]
        for index in range(manifest["instances_per_fuzzer"])
    }


def select_active_checkpoints(
    *,
    keys: list[str],
    manifests: dict[str, dict],
    now_epoch: int,
    settle_seconds: int = 300,
    requested_run_id: str = "",
    requested_benchmark_uuid: str = "",
) -> list[dict]:
    if bool(requested_run_id) != bool(requested_benchmark_uuid):
        raise ValueError("run_id and benchmark_uuid must be supplied together")
    snapshots: dict[tuple[str, str], set[int]] = {}
    published: set[tuple[str, str, int]] = set()
    finalized: set[tuple[str, str]] = set()
    for key in keys:
        if match := SNAPSHOT_KEY_RE.fullmatch(key):
            snapshots.setdefault((match["run"], match["uuid"]), set()).add(
                int(match["checkpoint"])
            )
        elif match := ANALYSIS_META_RE.fullmatch(key):
            published.add((match["run"], match["uuid"], int(match["checkpoint"])))
        elif match := FINALIZED_KEY_RE.fullmatch(key):
            finalized.add((match["run"], match["uuid"]))

    selected: list[dict] = []
    for key, raw_manifest in sorted(manifests.items()):
        match = RUN_KEY_RE.fullmatch(key)
        if not match:
            raise ValueError(f"invalid preliminary run-manifest key: {key}")
        run_id, uuid = match["run"], match["uuid"]
        if requested_run_id and (run_id, uuid) != (
            requested_run_id,
            requested_benchmark_uuid,
        ):
            continue
        manifest = validate_run_manifest(
            raw_manifest, run_id=run_id, benchmark_uuid=uuid
        )
        if not manifest["preliminary"]["enabled"] or (run_id, uuid) in finalized:
            continue
        started = manifest["run_started_at_epoch"]
        deadline = started + int(manifest["timeout_hours"] * 3600)
        if now_epoch < started or now_epoch >= deadline:
            continue
        interval = manifest["preliminary"]["interval_seconds"]
        settled = (now_epoch - started - max(0, settle_seconds)) // interval
        if settled < 1:
            continue
        candidates = [
            checkpoint
            for checkpoint in snapshots.get((run_id, uuid), set())
            if checkpoint <= settled
        ]
        if not candidates:
            continue
        checkpoint = max(candidates)
        if (run_id, uuid, checkpoint) in published:
            continue
        selected.append(
            {
                "run_id": run_id,
                "benchmark_uuid": uuid,
                "checkpoint": checkpoint,
                "checkpoint_padded": f"{checkpoint:06d}",
                "run_started_at_epoch": started,
                "timeout_hours": manifest["timeout_hours"],
                "interval_seconds": interval,
                "expected_snapshots": len(expected_legs(manifest)),
            }
        )
    return selected


def build_run_manifest(terraform_outputs: dict) -> dict:
    def output(name: str):
        item = terraform_outputs.get(name)
        if not isinstance(item, dict) or "value" not in item:
            raise ValueError(f"Terraform output is missing {name}")
        return item["value"]

    manifest = output("benchmark_manifest")
    if not isinstance(manifest, dict):
        raise ValueError("benchmark_manifest Terraform output must be an object")
    run_id = str(output("run_id"))
    uuid = str(output("benchmark_uuid"))
    started = _positive_int(output("run_started_at_epoch"), "run_started_at_epoch")
    run_prefix(run_id, uuid)
    interval = int(manifest.get("preliminary_interval_seconds", 3600))
    if interval < 0:
        raise ValueError("preliminary interval cannot be negative")
    result = {
        **manifest,
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "benchmark_uuid": uuid,
        "run_started_at_epoch": started,
        "run_started_at_utc": utc_iso(started),
        "preliminary": {
            "enabled": interval > 0,
            "interval_seconds": interval,
            "non_terminal": True,
            "comparative_decisions_allowed": False,
            "optional_stopping_allowed": False,
        },
    }
    return validate_run_manifest(result, run_id=run_id, benchmark_uuid=uuid)


def content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def put_immutable(
    *,
    bucket: str,
    key: str,
    source: Path,
    max_attempts: int = 5,
    retry_delay: float = 5.0,
) -> str:
    assert_preliminary_key(key)
    digest = sha256_file(source)
    for attempt in range(1, max_attempts + 1):
        result = subprocess.run(
            [
                "aws",
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                key,
                "--body",
                str(source),
                "--if-none-match",
                "*",
                "--metadata",
                f"sha256={digest}",
                "--content-type",
                content_type(source),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return digest
        try:
            head = aws_json(
                ["s3api", "head-object", "--bucket", bucket, "--key", key]
            )
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            head = {}
        remote = str(head.get("Metadata", {}).get("sha256", ""))
        if remote == digest:
            return digest
        if remote:
            raise RuntimeError(
                f"refusing to overwrite divergent immutable object {key}: "
                f"local {digest}, remote {remote}"
            )
        if attempt == max_attempts:
            raise RuntimeError(
                f"failed to upload immutable object {key}: {result.stderr.strip()}"
            )
        time.sleep(retry_delay)
    raise AssertionError("unreachable")


def publish_run_manifest(*, bucket: str, manifest_path: Path) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_id = str(manifest.get("run_id", ""))
    uuid = str(manifest.get("benchmark_uuid", ""))
    validate_run_manifest(manifest, run_id=run_id, benchmark_uuid=uuid)
    key = f"{run_prefix(run_id, uuid)}/run.json"
    put_immutable(bucket=bucket, key=key, source=manifest_path)
    return key


def _safe_zip_names(bundle: zipfile.ZipFile) -> list[str]:
    names = bundle.namelist()
    if len(names) > 10_000:
        raise ValueError("snapshot archive contains too many entries")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"unsafe snapshot archive member: {name}")
        if name != "checkpoint.json" and not name.startswith("logs/"):
            raise ValueError(f"unexpected snapshot archive member: {name}")
    if names.count("checkpoint.json") != 1:
        raise ValueError("snapshot archive must contain one checkpoint.json")
    return names


def _download_verified_object(bucket: str, key: str, destination: Path) -> None:
    assert_preliminary_key(key)
    head = aws_json(["s3api", "head-object", "--bucket", bucket, "--key", key])
    expected_sha = str(head.get("Metadata", {}).get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError(f"{key} is missing immutable SHA-256 metadata")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [
            "aws",
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            key,
            str(destination),
        ],
        stdout=subprocess.DEVNULL,
    )
    actual_sha = sha256_file(destination)
    if actual_sha != expected_sha:
        raise ValueError(f"downloaded checksum mismatch for {key}")


def verify_and_extract_snapshot(
    *,
    archive_path: Path,
    output_dir: Path,
    run_manifest: dict,
    object_key: str,
    checkpoint: int,
) -> dict:
    match = SNAPSHOT_KEY_RE.fullmatch(object_key)
    if not match:
        raise ValueError(f"invalid snapshot object key: {object_key}")
    if (
        match["run"] != run_manifest["run_id"]
        or match["uuid"] != run_manifest["benchmark_uuid"]
        or int(match["checkpoint"]) != checkpoint
    ):
        raise ValueError("snapshot object key disagrees with requested run/checkpoint")
    with zipfile.ZipFile(archive_path) as bundle:
        names = _safe_zip_names(bundle)
        metadata = json.loads(bundle.read("checkpoint.json"))
        if not isinstance(metadata, dict) or metadata.get("schema") != SNAPSHOT_SCHEMA:
            raise ValueError("invalid snapshot checkpoint metadata")
        expected_schedule = (
            run_manifest["run_started_at_epoch"]
            + checkpoint * run_manifest["preliminary"]["interval_seconds"]
        )
        required_matches = {
            "run_id": run_manifest["run_id"],
            "run_started_at_epoch": run_manifest["run_started_at_epoch"],
            "benchmark_uuid": run_manifest["benchmark_uuid"],
            "checkpoint": checkpoint,
            "interval_seconds": run_manifest["preliminary"]["interval_seconds"],
            "scheduled_at_epoch": expected_schedule,
            "object_identity": match["identity"],
            "complete": False,
        }
        for field, expected in required_matches.items():
            if metadata.get(field) != expected:
                raise ValueError(
                    f"snapshot metadata field {field} disagrees with object/run metadata"
                )
        fuzzer_key = metadata.get("fuzzer_key")
        run_index = str(metadata.get("run_index", ""))
        if (fuzzer_key, run_index) not in expected_legs(run_manifest):
            raise ValueError("snapshot identifies an unexpected fuzzer replicate")
        instance_id = str(metadata.get("instance_id", ""))
        fuzzer_label = str(metadata.get("fuzzer_label", ""))
        if not re.fullmatch(r"i-[0-9a-f]+", instance_id):
            raise ValueError("snapshot instance_id is invalid")
        if not SAFE_LABEL_RE.fullmatch(fuzzer_label):
            raise ValueError("snapshot fuzzer_label is invalid")

        declared: dict[str, dict] = {}
        for entry in metadata.get("files", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("invalid checkpoint file entry")
            path = entry["path"]
            if path in declared:
                raise ValueError(f"duplicate checkpoint file entry: {path}")
            declared[path] = entry
        actual_files = {name for name in names if name != "checkpoint.json"}
        if actual_files != set(declared):
            raise ValueError("checkpoint file inventory does not match archive members")
        instance_dir = output_dir / f"{instance_id}-{fuzzer_label}"
        for name in sorted(actual_files):
            data = bundle.read(name)
            entry = declared[name]
            if len(data) != entry.get("snapshot_size"):
                raise ValueError(f"snapshot size mismatch for {name}")
            if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                raise ValueError(f"snapshot checksum mismatch for {name}")
            relative = PurePosixPath(name).relative_to("logs")
            destination = instance_dir / "logs" / Path(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    return metadata


def materialize_checkpoint(
    *,
    bucket: str,
    run_id: str,
    benchmark_uuid: str,
    checkpoint: int,
    destination: Path,
) -> dict:
    manifest_key = f"{run_prefix(run_id, benchmark_uuid)}/run.json"
    manifest = validate_run_manifest(
        read_s3_json(bucket, manifest_key),
        run_id=run_id,
        benchmark_uuid=benchmark_uuid,
    )
    prefix = f"{run_prefix(run_id, benchmark_uuid)}/snapshots/{checkpoint:06d}/"
    keys = sorted(
        key
        for key in list_keys(bucket, prefix)
        if SNAPSHOT_KEY_RE.fullmatch(key)
    )
    if not keys:
        raise ValueError(f"no snapshot bundles found under {prefix}")
    destination.mkdir(parents=True, exist_ok=False)
    zips_dir = destination / "zips"
    unzipped_dir = destination / "unzipped"
    seen_legs: set[tuple[str, str]] = set()
    captures: list[dict] = []
    for key in keys:
        identity = SNAPSHOT_KEY_RE.fullmatch(key)["identity"]  # type: ignore[index]
        archive = zips_dir / f"{identity}.zip"
        _download_verified_object(bucket, key, archive)
        metadata = verify_and_extract_snapshot(
            archive_path=archive,
            output_dir=unzipped_dir,
            run_manifest=manifest,
            object_key=key,
            checkpoint=checkpoint,
        )
        leg = (str(metadata["fuzzer_key"]), str(metadata["run_index"]))
        if leg in seen_legs:
            raise ValueError(f"duplicate snapshot for logical replicate {leg}")
        seen_legs.add(leg)
        captures.append(metadata)

    expected = expected_legs(manifest)
    missing = sorted(expected - seen_legs)
    scheduled = (
        manifest["run_started_at_epoch"]
        + checkpoint * manifest["preliminary"]["interval_seconds"]
    )
    summary = {
        "schema": "scfuzzbench-preliminary-snapshot-set/v1",
        "run_id": run_id,
        "benchmark_uuid": benchmark_uuid,
        "checkpoint": checkpoint,
        "as_of_epoch": scheduled,
        "as_of_utc": utc_iso(scheduled),
        "elapsed_seconds": scheduled - manifest["run_started_at_epoch"],
        "planned_timeout_seconds": int(manifest["timeout_hours"] * 3600),
        "expected_snapshots": len(expected),
        "present_snapshots": len(seen_legs),
        "missing_replicates": [
            {"fuzzer_key": fuzzer, "run_index": index} for fuzzer, index in missing
        ],
        "incomplete": True,
        "non_terminal": True,
        "comparative_decisions_allowed": False,
        "optional_stopping_allowed": False,
        "captures": captures,
    }
    (destination / "snapshot-set.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m"


def preliminary_banner(context: dict) -> str:
    missing = context["expected_snapshots"] - context["present_snapshots"]
    return "\n".join(
        [
            "> [!CAUTION]",
            "> **PRELIMINARY — INCOMPLETE — DO NOT COMPARE OR STOP**",
            f"> As of **{context['as_of_utc']}**. "
            f"Elapsed **{format_duration(context['elapsed_seconds'])}** of "
            f"**{format_duration(context['planned_timeout_seconds'])}**.",
            f"> Snapshot coverage: **{context['present_snapshots']}/{context['expected_snapshots']}** "
            f"replicates ({missing} missing).",
            "> This non-terminal view cannot support rankings, pass/fail decisions, or optional stopping.",
            "",
        ]
    )


def _watermark_png(path: Path, text_lines: list[str]) -> None:
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(path) as source:
        image = source.convert("RGBA")
    banner_height = max(84, 24 * len(text_lines) + 12)
    output = Image.new("RGBA", (image.width, image.height + banner_height), "#8b0000")
    output.paste(image, (0, banner_height))
    draw = ImageDraw.Draw(output)
    font = ImageFont.load_default()
    y = 8
    for line in text_lines:
        draw.text((12, y), line, fill="white", font=font)
        y += 22
    output.convert("RGB").save(path, format="PNG")


def watermark_tree(*, source: Path, destination: Path, snapshot_set: dict) -> dict:
    if source.resolve() == destination.resolve():
        raise ValueError("preliminary watermarking requires a copied output tree")
    if destination.exists():
        raise FileExistsError(destination)
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("preliminary output source must not contain symlinks")
    shutil.copytree(source, destination)
    # Wall-clock performance of the Actions analysis job is operational debug
    # data, not benchmark data, and is intentionally nondeterministic. Keep it
    # out of the immutable checkpoint publication.
    for timing_file in destination.rglob("analysis_timing.json"):
        timing_file.unlink()
    banner = preliminary_banner(snapshot_set)
    watermarked: list[str] = []
    for path in sorted(destination.rglob("*.md")):
        original = path.read_text(encoding="utf-8")
        path.write_text(f"{banner}\n{original}", encoding="utf-8")
        watermarked.append(path.relative_to(destination).as_posix())
    chart_lines = [
        "PRELIMINARY - INCOMPLETE - DO NOT COMPARE OR STOP",
        f"As of {snapshot_set['as_of_utc']}",
        f"Elapsed {format_duration(snapshot_set['elapsed_seconds'])} of "
        f"{format_duration(snapshot_set['planned_timeout_seconds'])}",
        f"Replicates {snapshot_set['present_snapshots']}/{snapshot_set['expected_snapshots']}",
    ]
    for path in sorted(destination.rglob("*.png")):
        _watermark_png(path, chart_lines)
        watermarked.append(path.relative_to(destination).as_posix())
    metadata = {
        **snapshot_set,
        "schema": RESULT_SCHEMA,
        "watermarked_files": sorted(watermarked),
    }
    (destination / "preliminary.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (destination / "PRELIMINARY.md").write_text(
        banner
        + "\nNo preliminary result is a terminal benchmark result. "
        "Wait for the canonical release before comparing fuzzers.\n",
        encoding="utf-8",
    )
    return metadata


def validate_watermarked_tree(root: Path) -> dict:
    metadata_path = root / "preliminary.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("schema") != RESULT_SCHEMA:
        raise ValueError("missing preliminary analysis metadata")
    actual = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".png"}
        and path.name != "PRELIMINARY.md"
    )
    if actual != sorted(metadata.get("watermarked_files", [])):
        raise ValueError("watermark inventory does not cover every Markdown/PNG result")
    marker = "PRELIMINARY — INCOMPLETE — DO NOT COMPARE OR STOP"
    for path in root.rglob("*.md"):
        if marker not in path.read_text(encoding="utf-8"):
            raise ValueError(f"Markdown result is missing its preliminary warning: {path}")
    return metadata


def publish_tree(*, bucket: str, source: Path) -> str:
    metadata = validate_watermarked_tree(source)
    prefix = analysis_prefix(
        str(metadata["run_id"]),
        str(metadata["benchmark_uuid"]),
        int(metadata["checkpoint"]),
    )
    # Publish metadata last. Its existence is the atomic discovery marker that
    # tells docs and future workflow runs the whole immutable tree is available.
    files = sorted(path for path in source.rglob("*") if path.is_file())
    files.sort(key=lambda path: path.name == "preliminary.json")
    for path in files:
        relative = path.relative_to(source).as_posix()
        put_immutable(
            bucket=bucket,
            key=f"{prefix}/{relative}",
            source=path,
        )
    return prefix


def mark_finalized(
    *, bucket: str, run_id: str, benchmark_uuid: str, canonical_tag: str
) -> str:
    prefix = run_prefix(run_id, benchmark_uuid)
    payload = {
        "schema": FINALIZED_SCHEMA,
        "run_id": run_id,
        "benchmark_uuid": benchmark_uuid,
        "canonical_release_tag": canonical_tag,
        "preliminary_stream_closed": True,
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "finalized.json"
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        key = f"{prefix}/finalized.json"
        put_immutable(bucket=bucket, key=key, source=path)
    return key


def _write_github_output(path: str, *, has_runs: bool, matrix: dict) -> None:
    text = (
        f"has_runs={'true' if has_runs else 'false'}\n"
        f"matrix={json.dumps(matrix, separators=(',', ':'))}\n"
    )
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    write_run = sub.add_parser("write-run-manifest")
    write_run.add_argument("--terraform-output", required=True, type=Path)
    write_run.add_argument("--out", required=True, type=Path)

    publish_run = sub.add_parser("publish-run")
    publish_run.add_argument("--bucket", required=True)
    publish_run.add_argument("--manifest", required=True, type=Path)

    discover = sub.add_parser("discover")
    discover.add_argument("--bucket", required=True)
    discover.add_argument("--run-id", default="")
    discover.add_argument("--benchmark-uuid", default="")
    discover.add_argument("--settle-seconds", type=int, default=300)
    discover.add_argument("--now-epoch", type=int, default=0)
    discover.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))

    materialize = sub.add_parser("materialize")
    materialize.add_argument("--bucket", required=True)
    materialize.add_argument("--run-id", required=True)
    materialize.add_argument("--benchmark-uuid", required=True)
    materialize.add_argument("--checkpoint", required=True, type=int)
    materialize.add_argument("--dest", required=True, type=Path)

    watermark = sub.add_parser("watermark")
    watermark.add_argument("--source", required=True, type=Path)
    watermark.add_argument("--dest", required=True, type=Path)
    watermark.add_argument("--snapshot-set", required=True, type=Path)

    publish = sub.add_parser("publish")
    publish.add_argument("--bucket", required=True)
    publish.add_argument("--source", required=True, type=Path)

    finalized = sub.add_parser("mark-finalized")
    finalized.add_argument("--bucket", required=True)
    finalized.add_argument("--run-id", required=True)
    finalized.add_argument("--benchmark-uuid", required=True)
    finalized.add_argument("--canonical-tag", required=True)

    args = parser.parse_args()
    if args.command == "write-run-manifest":
        outputs = json.loads(args.terraform_output.read_text(encoding="utf-8"))
        manifest = build_run_manifest(outputs)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif args.command == "publish-run":
        print(publish_run_manifest(bucket=args.bucket, manifest_path=args.manifest))
    elif args.command == "discover":
        keys = list_keys(args.bucket, "preliminary/")
        manifests = {
            key: read_s3_json(args.bucket, key)
            for key in keys
            if RUN_KEY_RE.fullmatch(key)
        }
        selected = select_active_checkpoints(
            keys=keys,
            manifests=manifests,
            now_epoch=args.now_epoch or int(time.time()),
            settle_seconds=args.settle_seconds,
            requested_run_id=args.run_id,
            requested_benchmark_uuid=args.benchmark_uuid,
        )
        matrix = {"include": selected}
        _write_github_output(
            args.github_output, has_runs=bool(selected), matrix=matrix
        )
    elif args.command == "materialize":
        summary = materialize_checkpoint(
            bucket=args.bucket,
            run_id=args.run_id,
            benchmark_uuid=args.benchmark_uuid,
            checkpoint=args.checkpoint,
            destination=args.dest,
        )
        print(json.dumps(summary, sort_keys=True))
    elif args.command == "watermark":
        snapshot_set = json.loads(args.snapshot_set.read_text(encoding="utf-8"))
        metadata = watermark_tree(
            source=args.source, destination=args.dest, snapshot_set=snapshot_set
        )
        print(json.dumps(metadata, sort_keys=True))
    elif args.command == "publish":
        print(publish_tree(bucket=args.bucket, source=args.source))
    elif args.command == "mark-finalized":
        print(
            mark_finalized(
                bucket=args.bucket,
                run_id=args.run_id,
                benchmark_uuid=args.benchmark_uuid,
                canonical_tag=args.canonical_tag,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
