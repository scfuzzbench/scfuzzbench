#!/usr/bin/env python3
"""Run-state admission, lifecycle, and Terraform plan safety helpers.

The GitHub workflows deliberately keep the repository-wide admission critical
section outside this module: a short job-level Actions concurrency group
serializes calls to ``admit``.  Once serialized, S3's strongly consistent
object listing plus one active reservation object per run provides an atomic
capacity reservation without a long-running workflow or another AWS service.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
BACKEND_KEY_RE = re.compile(r"^runs/([A-Za-z0-9][A-Za-z0-9._-]{0,79})/terraform\.tfstate$")
ACTIVE_PREFIX = "run-state/admissions/active/"
RUN_STATE_PREFIX = "run-state/runs/"
MAX_CONCURRENT_RUNS_LIMIT = 20

TAGGABLE_RUN_RESOURCE_TYPES = {
    "aws_iam_instance_profile",
    "aws_iam_role",
    "aws_instance",
    "aws_internet_gateway",
    "aws_key_pair",
    "aws_route_table",
    "aws_security_group",
    "aws_subnet",
    "aws_vpc",
}
ALLOWED_RUN_RESOURCE_TYPES = TAGGABLE_RUN_RESOURCE_TYPES | {
    "aws_iam_role_policy",
    "aws_route_table_association",
    "local_sensitive_file",
    "random_id",
    "time_static",
    "tls_private_key",
}


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
        )
    return run_id


def backend_key_for(run_id: str) -> str:
    return f"runs/{validate_run_id(run_id)}/terraform.tfstate"


def validate_backend_key(run_id: str, backend_key: str) -> str:
    expected = backend_key_for(run_id)
    if backend_key != expected:
        raise ValueError(
            f"backend key {backend_key!r} does not match run identity; expected {expected!r}"
        )
    return backend_key


def make_identity(github_run_id: str, github_run_attempt: str, started_at_epoch: int) -> dict[str, Any]:
    if not github_run_id.isdigit() or not github_run_attempt.isdigit():
        raise ValueError("GitHub run id and attempt must be decimal integers")
    if int(github_run_attempt) < 1:
        raise ValueError("GitHub run attempt must be positive")
    if started_at_epoch <= 0:
        raise ValueError("started_at_epoch must be positive")
    run_id = validate_run_id(f"gh-{github_run_id}-{github_run_attempt}")
    return {
        "run_id": run_id,
        "run_started_at_epoch": started_at_epoch,
        "terraform_backend_key": backend_key_for(run_id),
        "reservation_key": f"{ACTIVE_PREFIX}{run_id}.json",
        "metadata_key": f"{RUN_STATE_PREFIX}{run_id}/metadata.json",
        "cleanup_tfvars_key": f"{RUN_STATE_PREFIX}{run_id}/cleanup.auto.tfvars.json",
    }


def parse_max_concurrent_runs(raw: str | int | None) -> int:
    value = "1" if raw is None or str(raw).strip() == "" else str(raw).strip()
    if not value.isdigit():
        raise ValueError("max concurrent runs must be an integer")
    parsed = int(value)
    if not 1 <= parsed <= MAX_CONCURRENT_RUNS_LIMIT:
        raise ValueError(
            f"max concurrent runs must be in [1, {MAX_CONCURRENT_RUNS_LIMIT}]"
        )
    return parsed


def reservation_run_id(key: str) -> str | None:
    if not key.startswith(ACTIVE_PREFIX) or not key.endswith(".json"):
        return None
    candidate = key[len(ACTIVE_PREFIX) : -len(".json")]
    try:
        return validate_run_id(candidate)
    except ValueError:
        return None


def tags_by_key(instance: dict[str, Any]) -> dict[str, str]:
    return {
        str(tag.get("Key", "")): str(tag.get("Value", ""))
        for tag in instance.get("Tags", [])
        if tag.get("Key")
    }


def occupied_run_ids(
    reservation_keys: Iterable[str], active_instances: Iterable[dict[str, Any]]
) -> set[str]:
    """Return capacity occupants, de-duplicating tagged instances by RunId.

    Legacy active instances have no trustworthy run boundary, so each one is
    conservatively counted as its own occupant. This can only under-admit while
    legacy infrastructure drains; it can never over-admit.
    """

    occupied = {
        reservation_run_id(key) or f"invalid-reservation:{key}"
        for key in reservation_keys
    }
    for instance in active_instances:
        tags = tags_by_key(instance)
        run_id = tags.get("RunId", "")
        if run_id:
            try:
                occupied.add(validate_run_id(run_id))
                continue
            except ValueError:
                pass
        occupied.add(f"legacy:{instance.get('InstanceId', 'unknown')}")
    return occupied


def can_admit(
    run_id: str,
    max_concurrent_runs: int,
    reservation_keys: Iterable[str],
    active_instances: Iterable[dict[str, Any]],
) -> tuple[bool, set[str]]:
    validate_run_id(run_id)
    limit = parse_max_concurrent_runs(max_concurrent_runs)
    occupied = occupied_run_ids(reservation_keys, active_instances)
    if run_id in occupied:
        raise ValueError(f"run {run_id} already has an active reservation or instance")
    return len(occupied) < limit, occupied


def run_started_at_epoch(run_id: str, manifest: dict[str, Any]) -> int:
    """Read the explicit start time, retaining legacy timestamp IDs."""

    raw = manifest.get("run_started_at_epoch")
    if raw not in (None, ""):
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("manifest run_started_at_epoch is not an integer") from exc
        if parsed <= 0:
            raise ValueError("manifest run_started_at_epoch must be positive")
        return parsed
    if run_id.isdigit():
        parsed = int(run_id)
        if parsed > 0:
            return parsed
    raise ValueError(
        f"run {run_id!r} has no run_started_at_epoch and is not a legacy timestamp id"
    )


def cleanup_eligibility(
    metadata: dict[str, Any],
    *,
    active_instance_count: int,
    final_archive_count: int,
    now_epoch: int,
) -> str | None:
    timeout_hours = float(metadata.get("timeout_hours", 0))
    started_at = int(metadata["run_started_at_epoch"])
    orphan_after = int(
        metadata.get(
            "orphan_after_epoch",
            started_at + int(timeout_hours * 3600) + 3 * 3600,
        )
    )
    fuzzers = metadata.get("fuzzers", [])
    expected = int(metadata.get("instances_per_fuzzer", 0)) * len(fuzzers)
    if (
        active_instance_count == 0
        and expected > 0
        and final_archive_count >= expected
    ):
        return "terminal-artifacts"
    if now_epoch >= orphan_after:
        return "orphan-deadline"
    return None


def _aws_json(*args: str) -> dict[str, Any]:
    output = subprocess.check_output(
        ["aws", *args, "--output", "json"], text=True
    )
    return json.loads(output) if output.strip() else {}


def _aws_text(*args: str) -> str:
    return subprocess.check_output(["aws", *args], text=True)


def list_s3_keys(bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token: str | None = None
    while True:
        args = [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
        ]
        if token:
            args += ["--continuation-token", token]
        page = _aws_json(*args)
        keys.extend(
            str(entry["Key"])
            for entry in page.get("Contents", [])
            if entry.get("Key")
        )
        if not page.get("IsTruncated"):
            return keys
        token = str(page.get("NextContinuationToken", ""))
        if not token:
            raise RuntimeError("truncated S3 listing omitted continuation token")


def active_benchmark_instances() -> list[dict[str, Any]]:
    response = _aws_json(
        "ec2",
        "describe-instances",
        "--filters",
        "Name=tag:Project,Values=scfuzzbench",
        "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped",
    )
    return [
        instance
        for reservation in response.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]


def _s3_read_json(bucket: str, key: str) -> dict[str, Any]:
    return json.loads(_aws_text("s3", "cp", f"s3://{bucket}/{key}", "-"))


def _s3_write_file(bucket: str, key: str, path: Path) -> None:
    subprocess.check_call(
        [
            "aws",
            "s3",
            "cp",
            str(path),
            f"s3://{bucket}/{key}",
            "--only-show-errors",
            "--content-type",
            "application/json",
        ]
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def cmd_identity(args: argparse.Namespace) -> int:
    identity = make_identity(
        args.github_run_id,
        args.github_run_attempt,
        args.started_at_epoch or int(time.time()),
    )
    if args.github_output:
        with Path(args.github_output).open("a") as output:
            for key, value in identity.items():
                output.write(f"{key}={value}\n")
    print(json.dumps(identity, sort_keys=True))
    return 0


def cmd_admit(args: argparse.Namespace) -> int:
    reservation_path = Path(args.reservation_file)
    reservation = json.loads(reservation_path.read_text())
    run_id = validate_run_id(str(reservation["run_id"]))
    validate_backend_key(run_id, str(reservation["terraform_backend_key"]))
    limit = parse_max_concurrent_runs(args.max_concurrent_runs)
    keys = list_s3_keys(args.bucket, ACTIVE_PREFIX)
    instances = active_benchmark_instances()
    allowed, occupied = can_admit(run_id, limit, keys, instances)
    if not allowed:
        print(
            "Benchmark admission denied: "
            f"{len(occupied)} capacity occupant(s) meet the configured limit {limit}: "
            + ", ".join(sorted(occupied)),
            file=sys.stderr,
        )
        return 2
    reservation_key = f"{ACTIVE_PREFIX}{run_id}.json"
    metadata_key = f"{RUN_STATE_PREFIX}{run_id}/metadata.json"
    _s3_write_file(args.bucket, reservation_key, reservation_path)
    _s3_write_file(args.bucket, metadata_key, reservation_path)
    print(
        json.dumps(
            {
                "admitted": True,
                "run_id": run_id,
                "max_concurrent_runs": limit,
                "occupied_before": sorted(occupied),
                "reservation_key": reservation_key,
            },
            sort_keys=True,
        )
    )
    return 0


def _update_metadata(
    bucket: str, run_id: str, updates: dict[str, Any], output_path: Path
) -> dict[str, Any]:
    active_key = f"{ACTIVE_PREFIX}{validate_run_id(run_id)}.json"
    metadata = _s3_read_json(bucket, active_key)
    validate_backend_key(run_id, str(metadata["terraform_backend_key"]))
    metadata.update(updates)
    metadata["updated_at_epoch"] = int(time.time())
    _write_json(output_path, metadata)
    _s3_write_file(bucket, active_key, output_path)
    _s3_write_file(bucket, f"{RUN_STATE_PREFIX}{run_id}/metadata.json", output_path)
    return metadata


def cmd_mark_running(args: argparse.Namespace) -> int:
    outputs = json.loads(Path(args.terraform_outputs).read_text())
    flat_outputs = {
        key: value.get("value") for key, value in outputs.items() if isinstance(value, dict)
    }
    metadata = _update_metadata(
        args.bucket,
        args.run_id,
        {
            "status": "running",
            "benchmark_uuid": flat_outputs.get("benchmark_uuid", ""),
            "terraform_outputs": flat_outputs,
        },
        Path(args.output),
    )
    print(json.dumps(metadata, sort_keys=True))
    return 0


def cmd_mark_failed(args: argparse.Namespace) -> int:
    retry_cleanup_at = int(time.time()) + 15 * 60
    metadata = _update_metadata(
        args.bucket,
        args.run_id,
        {
            "status": "provisioning-failed",
            "failure_workflow_url": args.workflow_url,
            "orphan_after_epoch": retry_cleanup_at,
        },
        Path(args.output),
    )
    print(json.dumps(metadata, sort_keys=True))
    return 0


def cmd_discover_cleanup(args: argparse.Namespace) -> int:
    now_epoch = args.now_epoch or int(time.time())
    keys = list_s3_keys(args.bucket, ACTIVE_PREFIX)
    instances = active_benchmark_instances()
    instances_by_run: dict[str, list[dict[str, Any]]] = {}
    for instance in instances:
        run_id = tags_by_key(instance).get("RunId", "")
        if run_id:
            instances_by_run.setdefault(run_id, []).append(instance)

    include: list[dict[str, Any]] = []
    for key in keys:
        run_id = reservation_run_id(key)
        if run_id is None:
            continue
        try:
            metadata = _s3_read_json(args.bucket, key)
            validate_backend_key(run_id, str(metadata["terraform_backend_key"]))
            benchmark_uuid = str(metadata.get("benchmark_uuid", ""))
            final_count = 0
            if benchmark_uuid:
                final_count = sum(
                    item.endswith(".zip")
                    for item in list_s3_keys(
                        args.bucket, f"logs/{run_id}/{benchmark_uuid}/"
                    )
                )
            reason = cleanup_eligibility(
                metadata,
                active_instance_count=len(instances_by_run.get(run_id, [])),
                final_archive_count=final_count,
                now_epoch=now_epoch,
            )
            if reason:
                include.append(
                    {
                        "run_id": run_id,
                        "terraform_backend_key": metadata["terraform_backend_key"],
                        "reason": reason,
                    }
                )
        except Exception as exc:
            print(f"Skipping malformed cleanup reservation {key}: {exc}", file=sys.stderr)
    print(json.dumps({"include": include}, separators=(",", ":"), sort_keys=True))
    return 0


def cmd_mark_cleaned(args: argparse.Namespace) -> int:
    run_id = validate_run_id(args.run_id)
    active_key = f"{ACTIVE_PREFIX}{run_id}.json"
    metadata = _s3_read_json(args.bucket, active_key)
    validate_backend_key(run_id, str(metadata["terraform_backend_key"]))
    metadata.update(
        {
            "status": "cleaned",
            "cleanup_reason": args.reason,
            "cleaned_at_epoch": int(time.time()),
        }
    )
    output_path = Path(args.output)
    _write_json(output_path, metadata)
    _s3_write_file(args.bucket, f"{RUN_STATE_PREFIX}{run_id}/metadata.json", output_path)
    subprocess.check_call(
        [
            "aws",
            "s3api",
            "delete-object",
            "--bucket",
            args.bucket,
            "--key",
            active_key,
        ]
    )
    print(json.dumps(metadata, sort_keys=True))
    return 0


def validate_terraform_plan(plan: dict[str, Any], mode: str, run_id: str) -> list[str]:
    errors: list[str] = []
    validate_run_id(run_id)
    for change in plan.get("resource_changes", []):
        address = str(change.get("address", ""))
        resource_mode = str(change.get("mode", "managed"))
        resource_type = str(change.get("type", ""))
        actions = list(change.get("change", {}).get("actions", []))
        before = change.get("change", {}).get("before") or {}
        after = change.get("change", {}).get("after") or {}

        if resource_mode == "managed" and resource_type not in ALLOWED_RUN_RESOURCE_TYPES:
            errors.append(f"{address}: resource type {resource_type!r} is not run-owned")
        if resource_type.startswith("aws_s3_bucket"):
            errors.append(f"{address}: run plans may not own shared S3 resources")

        action_set = set(actions)
        if mode == "apply" and action_set.intersection({"delete", "update"}):
            errors.append(f"{address}: fresh run plan is not create-only ({actions})")
        if mode == "cleanup" and action_set.intersection({"create", "update"}):
            errors.append(f"{address}: cleanup plan is not delete-only ({actions})")

        if resource_type not in TAGGABLE_RUN_RESOURCE_TYPES:
            continue
        values = before if mode == "cleanup" else after
        tags = values.get("tags_all") or values.get("tags") or {}
        if tags.get("Project") != "scfuzzbench":
            errors.append(f"{address}: missing Project=scfuzzbench tag")
        if tags.get("RunId") != run_id:
            errors.append(
                f"{address}: RunId tag {tags.get('RunId')!r} does not match {run_id!r}"
            )
        if not tags.get("BenchmarkUuid"):
            errors.append(f"{address}: missing BenchmarkUuid tag")
    return errors


def cmd_validate_plan(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan_json).read_text())
    errors = validate_terraform_plan(plan, args.mode, args.run_id)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"Terraform {args.mode} plan is run-scoped for {args.run_id}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    identity = subparsers.add_parser("identity")
    identity.add_argument("--github-run-id", required=True)
    identity.add_argument("--github-run-attempt", required=True)
    identity.add_argument("--started-at-epoch", type=int, default=0)
    identity.add_argument("--github-output", default="")
    identity.set_defaults(func=cmd_identity)

    admit = subparsers.add_parser("admit")
    admit.add_argument("--bucket", required=True)
    admit.add_argument("--max-concurrent-runs", default="1")
    admit.add_argument("--reservation-file", required=True)
    admit.set_defaults(func=cmd_admit)

    mark_running = subparsers.add_parser("mark-running")
    mark_running.add_argument("--bucket", required=True)
    mark_running.add_argument("--run-id", required=True)
    mark_running.add_argument("--terraform-outputs", required=True)
    mark_running.add_argument("--output", default="run-state-metadata.json")
    mark_running.set_defaults(func=cmd_mark_running)

    mark_failed = subparsers.add_parser("mark-failed")
    mark_failed.add_argument("--bucket", required=True)
    mark_failed.add_argument("--run-id", required=True)
    mark_failed.add_argument("--workflow-url", required=True)
    mark_failed.add_argument("--output", default="run-state-metadata.json")
    mark_failed.set_defaults(func=cmd_mark_failed)

    discover = subparsers.add_parser("discover-cleanup")
    discover.add_argument("--bucket", required=True)
    discover.add_argument("--now-epoch", type=int, default=0)
    discover.set_defaults(func=cmd_discover_cleanup)

    cleaned = subparsers.add_parser("mark-cleaned")
    cleaned.add_argument("--bucket", required=True)
    cleaned.add_argument("--run-id", required=True)
    cleaned.add_argument("--reason", required=True)
    cleaned.add_argument("--output", default="run-state-metadata.json")
    cleaned.set_defaults(func=cmd_mark_cleaned)

    validate_plan = subparsers.add_parser("validate-plan")
    validate_plan.add_argument("--plan-json", required=True)
    validate_plan.add_argument("--mode", choices=("apply", "cleanup"), required=True)
    validate_plan.add_argument("--run-id", required=True)
    validate_plan.set_defaults(func=cmd_validate_plan)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
