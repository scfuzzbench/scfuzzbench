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
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
BACKEND_KEY_RE = re.compile(r"^runs/([A-Za-z0-9][A-Za-z0-9._-]{0,79})/terraform\.tfstate$")
SCFUZZBENCH_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
BENCHMARK_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
ACTIVE_PREFIX = "run-state/admissions/active/"
RUN_STATE_PREFIX = "run-state/runs/"
MAX_CONCURRENT_RUNS_LIMIT = 20
FUZZER_ENV_MAX_ENTRIES = 64
FUZZER_ENV_MAX_UTF8_BYTES = 4096
HEARTBEAT_STALE_SECONDS = 30 * 60
SUPPORTED_FUZZERS = {"echidna", "foundry", "medusa", "recon-fuzzer"}
ACTIVE_RUN_STATUSES = {"reserved", "running", "provisioning-failed"}
IMMUTABLE_FUZZER_ENV_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ECHIDNA_CI_ARTIFACT_NAME",
    "ECHIDNA_CI_ARTIFACT_SHA256",
    "ECHIDNA_CI_COMMIT",
    "ECHIDNA_CI_REPO",
    "ECHIDNA_CI_RUN_ID",
    "ECHIDNA_CI_TOKEN",
    "ECHIDNA_CI_TOKEN_KMS_KEY_ARN",
    "ECHIDNA_CI_TOKEN_SSM_PARAMETER",
    "ECHIDNA_VERSION",
    "FOUNDRY_GIT_REF",
    "FOUNDRY_GIT_REPO",
    "FOUNDRY_VERSION",
    "MEDUSA_GIT_COMMIT",
    "MEDUSA_GIT_REF",
    "MEDUSA_GIT_REPO",
    "MEDUSA_GO_SHA256",
    "MEDUSA_GO_VERSION",
    "MEDUSA_VERSION",
    "RECON_VERSION",
    "SCFUZZBENCH_AWS_CREDS_ENV_FILE",
    "SCFUZZBENCH_AWS_CREDS_REFRESH_PID",
    "SCFUZZBENCH_AWS_CREDS_REFRESH_PID_START_TICKS",
    "SCFUZZBENCH_AWS_CREDS_REFRESH_SECONDS",
    "SCFUZZBENCH_BENCHMARK_MANIFEST_B64",
    "SCFUZZBENCH_BENCHMARK_TYPE",
    "SCFUZZBENCH_BENCHMARK_UUID",
    "SCFUZZBENCH_BIN_DIR",
    "SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION",
    "SCFUZZBENCH_CACHED_AWS_CREDS_EXPIRATION_EPOCH",
    "SCFUZZBENCH_COMMIT",
    "SCFUZZBENCH_COMMON_SH",
    "SCFUZZBENCH_CORPUS_DIR",
    "SCFUZZBENCH_DISABLE_IMDS_CREDENTIAL_CACHE",
    "SCFUZZBENCH_GIT_TOKEN_SSM_PARAMETER",
    "SCFUZZBENCH_GIT_TOKEN",
    "SCFUZZBENCH_LOCAL_MODE",
    "SCFUZZBENCH_FUZZER_KEY",
    "SCFUZZBENCH_FUZZER_LABEL",
    "SCFUZZBENCH_INSTANCE_ID",
    "SCFUZZBENCH_LOG_DIR",
    "SCFUZZBENCH_FOUNDRY_SOURCE_PATCH",
    "SCFUZZBENCH_PRELIMINARY_INTERVAL_SECONDS",
    "SCFUZZBENCH_PRELIMINARY_PID",
    "SCFUZZBENCH_PRELIMINARY_PID_START_TICKS",
    "SCFUZZBENCH_PRELIMINARY_SNAPSHOT_SCRIPT",
    "SCFUZZBENCH_PROPERTIES_PATH",
    "SCFUZZBENCH_REPO_URL",
    "SCFUZZBENCH_ROOT",
    "SCFUZZBENCH_RUNNER_METRICS_PID",
    "SCFUZZBENCH_RUNNER_METRICS_PID_START_TICKS",
    "SCFUZZBENCH_RUN_HEARTBEAT_SECONDS",
    "SCFUZZBENCH_RUN_ID",
    "SCFUZZBENCH_RUN_INDEX",
    "SCFUZZBENCH_RUN_STARTED_AT_EPOCH",
    "SCFUZZBENCH_S3_BUCKET",
    "SCFUZZBENCH_SEED_CORPUS_HELPER",
    "SCFUZZBENCH_SEED_CORPUS_MAX_BYTES",
    "SCFUZZBENCH_SEED_CORPUS_MAX_FILES",
    "SCFUZZBENCH_SEED_CORPUS_METADATA_PATH",
    "SCFUZZBENCH_SEED_CORPUS_PROVENANCE_SOURCE",
    "SCFUZZBENCH_SEED_CORPUS_SOURCE",
    "SCFUZZBENCH_SHUTDOWN_GRACE_SECONDS",
    "SCFUZZBENCH_TIMEOUT_GRACE_SECONDS",
    "SCFUZZBENCH_TIMEOUT_SECONDS",
    "SCFUZZBENCH_UPLOAD_DONE",
    "SCFUZZBENCH_WORKDIR",
    "SCFUZZBENCH_WORKERS_RESOLVED",
}
SAFE_SCFUZZBENCH_FUZZER_ENV_KEYS = {
    "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS",
    "SCFUZZBENCH_FOUNDRY_SHOWMAP",
    "SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS",
    "SCFUZZBENCH_RUNNER_METRICS",
    "SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS",
    "SCFUZZBENCH_WORKERS",
}
CORPUS_OVERRIDE_ENV_KEYS = {
    "ECHIDNA_CORPUS_DIR",
    "FOUNDRY_CORPUS_DIR",
    "MEDUSA_CORPUS_DIR",
    "RECON_CORPUS_DIR",
}

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
    "terraform_data",
    "time_static",
    "tls_private_key",
}


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
        )
    return run_id


def validate_scfuzzbench_commit(value: str) -> str:
    if not SCFUZZBENCH_COMMIT_RE.fullmatch(value):
        raise ValueError(
            "scfuzzbench_commit must be an exact lowercase 40-character Git commit"
        )
    return value


def validate_benchmark_uuid(value: str) -> str:
    if not BENCHMARK_UUID_RE.fullmatch(value):
        raise ValueError(
            "benchmark_uuid must be an exact lowercase 32-character hexadecimal value"
        )
    return value


def validate_benchmark_hours(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or not 0.25 <= parsed <= 72:
        raise ValueError(f"{name} must be in [0.25, 72]")
    return parsed


def normalize_excluded_fuzzers(value: str) -> str:
    if value == "":
        return ""
    if any(character.isspace() for character in value):
        raise ValueError("exclude_fuzzers must not contain whitespace")
    fuzzers = value.split(",")
    if any(not fuzzer for fuzzer in fuzzers):
        raise ValueError("exclude_fuzzers must be a comma-separated list")
    unknown = sorted(set(fuzzers) - SUPPORTED_FUZZERS)
    if unknown:
        raise ValueError(
            "exclude_fuzzers contains unsupported fuzzer key(s): "
            + ", ".join(unknown)
        )
    if len(fuzzers) != len(set(fuzzers)):
        raise ValueError("exclude_fuzzers must not contain duplicate keys")
    return ",".join(sorted(fuzzers))


def backend_key_for(run_id: str) -> str:
    return f"runs/{validate_run_id(run_id)}/terraform.tfstate"


def validate_backend_key(run_id: str, backend_key: str) -> str:
    expected = backend_key_for(run_id)
    if backend_key != expected:
        raise ValueError(
            f"backend key {backend_key!r} does not match run identity; expected {expected!r}"
        )
    return backend_key


def validate_recovery_inputs(
    tfvars_payload: dict[str, Any],
    metadata_payload: dict[str, Any],
    *,
    run_id: str,
    backend_key: str,
    bucket: str,
    expected_commit: str = "",
    expected_status: str = "",
) -> str:
    validate_backend_key(run_id, backend_key)
    for name, payload in (
        ("recovery inputs", tfvars_payload),
        ("run metadata", metadata_payload),
    ):
        if str(payload.get("run_id", "")) != run_id:
            raise ValueError(f"{name} do not match run_id")
        if str(payload.get("terraform_backend_key", "")) != backend_key:
            raise ValueError(f"{name} do not match terraform backend key")
        if str(payload.get("existing_bucket_name", "")) != bucket:
            raise ValueError(f"{name} do not match the shared artifact bucket")
    if "fuzzer_env" in tfvars_payload:
        raise ValueError("recovery inputs must not persist fuzzer_env")
    status = str(metadata_payload.get("status", ""))
    if status not in ACTIVE_RUN_STATUSES:
        raise ValueError(f"run metadata has invalid active status {status!r}")
    if expected_status and status != expected_status:
        raise ValueError(
            f"run metadata status {status!r} does not match {expected_status!r}"
        )
    tfvars_commit = validate_scfuzzbench_commit(
        str(tfvars_payload.get("scfuzzbench_commit", ""))
    )
    metadata_commit = validate_scfuzzbench_commit(
        str(metadata_payload.get("scfuzzbench_commit", ""))
    )
    if tfvars_commit != metadata_commit:
        raise ValueError(
            "recovery inputs and run metadata do not match scfuzzbench_commit"
        )
    if expected_commit:
        expected = validate_scfuzzbench_commit(expected_commit)
        if tfvars_commit != expected:
            raise ValueError(
                "recovery inputs do not match the expected scfuzzbench_commit"
            )
    return tfvars_commit


def validate_provisioning_commit(
    provisioning_commit: str,
    current_main_commit: str,
    comparison: dict[str, Any],
) -> None:
    """Require a persisted provisioning commit to belong to current main.

    The caller obtains ``comparison`` from GitHub's
    ``compare/{provisioning_commit}...{current_main_commit}`` endpoint.  A
    different provisioning commit is safe only when the current main commit is
    ahead and the merge base is exactly the provisioning commit.
    """

    provisioning = validate_scfuzzbench_commit(provisioning_commit)
    current = validate_scfuzzbench_commit(current_main_commit)
    status = str(comparison.get("status", ""))
    base_commit = str((comparison.get("base_commit") or {}).get("sha", ""))
    merge_base = str((comparison.get("merge_base_commit") or {}).get("sha", ""))
    head_commit = str((comparison.get("head_commit") or {}).get("sha", ""))
    if base_commit != provisioning:
        raise ValueError("GitHub comparison base does not match provisioning commit")
    if merge_base != provisioning:
        raise ValueError("provisioning commit is not an ancestor of current main")
    if head_commit != current:
        raise ValueError("GitHub comparison head does not match current main commit")
    if provisioning == current:
        if status != "identical":
            raise ValueError("identical provisioning and main commits did not compare equal")
    elif status != "ahead":
        raise ValueError("provisioning commit is not an ancestor of current main")


def validate_state_outputs(
    outputs: dict[str, Any],
    *,
    run_id: str,
    backend_key: str,
    benchmark_uuid: str = "",
    allow_missing: bool = False,
) -> None:
    def output(name: str) -> Any | None:
        item = outputs.get(name)
        if not isinstance(item, dict) or "value" not in item:
            if allow_missing:
                return None
            raise ValueError(f"state is missing required {name} output")
        return item["value"]

    validate_backend_key(run_id, backend_key)
    state_run_id = output("run_id")
    if state_run_id is not None and str(state_run_id) != run_id:
        raise ValueError("state run_id output does not match cleanup identity")
    state_backend_key = output("terraform_backend_key")
    if state_backend_key is not None and str(state_backend_key) != backend_key:
        raise ValueError("state backend-key output does not match cleanup identity")
    raw_uuid = output("benchmark_uuid")
    if raw_uuid is None:
        return
    actual_uuid = str(raw_uuid)
    validate_benchmark_uuid(actual_uuid)
    if benchmark_uuid and actual_uuid != benchmark_uuid:
        raise ValueError("state benchmark_uuid output does not match run metadata")


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


def normalize_tool_sources(values: dict[str, str]) -> dict[str, str]:
    """Normalize manual JSON and reusable-workflow tool source inputs."""

    groups = {
        "echidna": {
            "raw": str(values.get("ECHIDNA_CI_JSON", "")).strip(),
            "keys": (
                "echidna_ci_repo",
                "echidna_ci_run_id",
                "echidna_ci_artifact_name",
                "echidna_ci_artifact_sha256",
                "echidna_ci_commit",
                "echidna_ci_token_ssm_parameter_name",
                "echidna_ci_token_kms_key_arn",
            ),
            "identity_keys": (
                "echidna_ci_repo",
                "echidna_ci_run_id",
                "echidna_ci_artifact_name",
                "echidna_ci_artifact_sha256",
                "echidna_ci_commit",
                "echidna_ci_token_ssm_parameter_name",
            ),
        },
        "medusa": {
            "raw": str(values.get("MEDUSA_SOURCE_JSON", "")).strip(),
            "keys": (
                "medusa_git_repo",
                "medusa_git_ref",
                "medusa_git_commit",
                "medusa_go_version",
                "medusa_go_sha256",
            ),
            "identity_keys": (
                "medusa_git_repo",
                "medusa_git_ref",
                "medusa_git_commit",
            ),
        },
    }
    output: dict[str, str] = {}
    for group in groups.values():
        keys = group["keys"]
        direct = {
            key: str(values.get(f"CALL_{key.upper()}", ""))
            for key in keys
        }
        raw = group["raw"]
        if raw:
            if any(direct[key] for key in group["identity_keys"]):
                raise ValueError(
                    "manual JSON tool inputs cannot be combined with "
                    "reusable-workflow tool inputs"
                )
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid bleeding-edge tool JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("bleeding-edge tool JSON must be an object")
            unexpected = sorted(set(parsed) - set(keys))
            if unexpected:
                raise ValueError(
                    "unexpected bleeding-edge tool keys: "
                    + ", ".join(unexpected)
                )
            direct = {key: parsed.get(key, "") for key in keys}
        for key, raw_value in direct.items():
            if not isinstance(raw_value, str):
                raise ValueError(f"{key} must be a string")
            if "\n" in raw_value or "\r" in raw_value:
                raise ValueError(f"{key} must be a single line")
            if len(raw_value) > 500:
                raise ValueError(f"{key} is too long")
            output[key] = raw_value

    output["medusa_go_version"] = output["medusa_go_version"] or "1.24.0"
    output["medusa_go_sha256"] = (
        output["medusa_go_sha256"]
        or "dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858"
    )
    return output


def validate_fuzzer_env_entry(key: Any, env_value: Any) -> None:
    if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key):
        raise ValueError(f"invalid fuzzer environment key: {key!r}")
    if key.startswith("AWS_"):
        raise ValueError(f"fuzzer environment may not override {key}")
    if (
        key.startswith("SCFUZZBENCH_")
        and key not in SAFE_SCFUZZBENCH_FUZZER_ENV_KEYS
    ):
        raise ValueError(f"fuzzer environment may not override {key}")
    if key in IMMUTABLE_FUZZER_ENV_KEYS:
        raise ValueError(f"fuzzer environment may not override {key}")
    if not isinstance(env_value, str) or len(env_value) > 2000:
        raise ValueError(f"invalid fuzzer environment value for {key}")
    if re.search(r'[\r\n"`$\\]', env_value):
        raise ValueError(f"invalid fuzzer environment value for {key}")

    bool_values = {"0", "1", "false", "no", "off", "on", "true", "yes"}
    if key in {
        "SCFUZZBENCH_RUNNER_METRICS",
        "SCFUZZBENCH_FOUNDRY_SHOWMAP",
    } and env_value.lower() not in bool_values:
        raise ValueError(f"{key} must be a boolean value")
    if key == "SCFUZZBENCH_FOUNDRY_KEEP_CORPUS" and env_value not in {"0", "1"}:
        raise ValueError(f"{key} must be 0 or 1")
    bounded_integers = {
        "SCFUZZBENCH_WORKERS": (1, 256),
        "SCFUZZBENCH_RUNNER_METRICS_INTERVAL_SECONDS": (1, 300),
        "SCFUZZBENCH_FOUNDRY_SHOWMAP_TIMEOUT_SECONDS": (1, 3600),
    }
    if key in bounded_integers:
        lower, upper = bounded_integers[key]
        if (
            not env_value.isdigit()
            or not lower <= int(env_value) <= upper
        ):
            raise ValueError(f"{key} must be an integer in [{lower}, {upper}]")
    if key in CORPUS_OVERRIDE_ENV_KEYS and env_value:
        if (
            env_value.startswith("/")
            or not re.fullmatch(r"[A-Za-z0-9_.+/-]+", env_value)
            or re.search(r"(^|/)\.\.?(/|$)", env_value)
        ):
            raise ValueError(
                f"{key} must be a safe repo-relative path without dot segments"
            )


def validate_fuzzer_env_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("fuzzer_env_json must be a JSON object")
    if len(value) > FUZZER_ENV_MAX_ENTRIES:
        raise ValueError(
            f"fuzzer_env_json must contain at most {FUZZER_ENV_MAX_ENTRIES} entries"
        )
    aggregate_bytes = 0
    for key, env_value in value.items():
        validate_fuzzer_env_entry(key, env_value)
        aggregate_bytes += len(key.encode("utf-8")) + len(env_value.encode("utf-8"))
    if aggregate_bytes > FUZZER_ENV_MAX_UTF8_BYTES:
        raise ValueError(
            "fuzzer_env_json keys and values must contain at most "
            f"{FUZZER_ENV_MAX_UTF8_BYTES} aggregate UTF-8 bytes"
        )
    return value


def validate_benchmark_inputs(values: dict[str, str]) -> dict[str, Any]:
    """Validate every cloud input before admission creates persistent objects."""

    def value(name: str) -> str:
        raw = str(values.get(name, ""))
        if "\n" in raw or "\r" in raw:
            raise ValueError(f"{name.lower()} must be a single line")
        return raw.strip()

    target_repo_url = value("TARGET_REPO_URL")
    if not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
        target_repo_url,
    ):
        raise ValueError(
            "target_repo_url must be https://github.com/<org>/<repo>"
        )
    target_commit = value("TARGET_COMMIT")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", target_commit):
        raise ValueError("target_commit must be a commit SHA, tag, or branch")
    benchmark_type = value("BENCHMARK_TYPE")
    if benchmark_type not in {"property", "optimization"}:
        raise ValueError("benchmark_type must be property or optimization")
    if not re.fullmatch(r"[a-z0-9]+\.[a-z0-9]+", value("INSTANCE_TYPE")):
        raise ValueError("instance_type must look like c6a.4xlarge")

    instances_raw = value("INSTANCES_PER_FUZZER")
    if not instances_raw.isdigit() or not 1 <= int(instances_raw) <= 20:
        raise ValueError("instances_per_fuzzer must be an integer in [1, 20]")
    try:
        timeout_hours = float(value("TIMEOUT_HOURS"))
    except ValueError as exc:
        raise ValueError("timeout_hours must be a number") from exc
    if not math.isfinite(timeout_hours) or not 0.25 <= timeout_hours <= 72:
        raise ValueError("timeout_hours must be in [0.25, 72]")
    try:
        preliminary_minutes = float(value("PRELIMINARY_INTERVAL_MINUTES") or "60")
    except ValueError as exc:
        raise ValueError("preliminary_interval_minutes must be a number") from exc
    preliminary_seconds = preliminary_minutes * 60
    if (
        not math.isfinite(preliminary_minutes)
        or preliminary_minutes < 0
        or preliminary_minutes > 1440
        or (preliminary_minutes != 0 and preliminary_minutes < 1)
        or not preliminary_seconds.is_integer()
    ):
        raise ValueError(
            "preliminary_interval_minutes must be 0 or a value in [1, 1440] "
            "that resolves to whole seconds"
        )

    try:
        fuzzers = json.loads(
            value("FUZZERS_JSON")
            or '["echidna","medusa","foundry","recon-fuzzer"]'
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"fuzzers_json must be valid JSON: {exc}") from exc
    if (
        not isinstance(fuzzers, list)
        or not fuzzers
        or len(fuzzers) > len(SUPPORTED_FUZZERS)
        or any(not isinstance(item, str) for item in fuzzers)
        or len(set(fuzzers)) != len(fuzzers)
        or not set(fuzzers).issubset(SUPPORTED_FUZZERS)
    ):
        raise ValueError(
            "fuzzers_json must be a unique, non-empty list of supported fuzzers"
        )

    safe_token = re.compile(r"^[A-Za-z0-9._+-]*$")
    for name in (
        "ECHIDNA_VERSION",
        "MEDUSA_VERSION",
        "RECON_VERSION",
    ):
        if not safe_token.fullmatch(value(name)):
            raise ValueError(f"{name.lower()} contains unsupported characters")
    foundry_repo = value("FOUNDRY_GIT_REPO")
    if foundry_repo and not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
        foundry_repo,
    ):
        raise ValueError("foundry_git_repo must be a GitHub HTTPS repository")
    foundry_ref = value("FOUNDRY_GIT_REF")
    if foundry_ref and not re.fullmatch(r"[A-Za-z0-9._/-]+", foundry_ref):
        raise ValueError("foundry_git_ref contains unsupported characters")

    echidna_ci_names = (
        "ECHIDNA_CI_REPO",
        "ECHIDNA_CI_RUN_ID",
        "ECHIDNA_CI_ARTIFACT_NAME",
        "ECHIDNA_CI_ARTIFACT_SHA256",
        "ECHIDNA_CI_COMMIT",
        "ECHIDNA_CI_TOKEN_SSM_PARAMETER_NAME",
    )
    echidna_ci = {name: value(name) for name in echidna_ci_names}
    echidna_count = sum(bool(item) for item in echidna_ci.values())
    if echidna_count not in {0, len(echidna_ci_names)}:
        raise ValueError(
            "Echidna CI mode requires repo, run ID, artifact name, artifact "
            "SHA-256, full commit, and token SSM parameter together"
        )
    echidna_kms_arn = value("ECHIDNA_CI_TOKEN_KMS_KEY_ARN")
    if echidna_count:
        if value("ECHIDNA_VERSION"):
            raise ValueError(
                "echidna_version and Echidna CI artifact mode are mutually exclusive"
            )
        if "echidna" not in fuzzers:
            raise ValueError("Echidna CI artifact mode requires echidna")
        if not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
            echidna_ci["ECHIDNA_CI_REPO"],
        ):
            raise ValueError("echidna_ci_repo must be a GitHub HTTPS repository")
        if (
            not echidna_ci["ECHIDNA_CI_RUN_ID"].isdigit()
            or int(echidna_ci["ECHIDNA_CI_RUN_ID"]) < 1
        ):
            raise ValueError("echidna_ci_run_id must be a positive integer")
        artifact_name = echidna_ci["ECHIDNA_CI_ARTIFACT_NAME"]
        if (
            not re.fullmatch(r"[A-Za-z0-9._-]+", artifact_name)
            or "linux" not in artifact_name.lower()
        ):
            raise ValueError("echidna_ci_artifact_name must identify Linux")
        if not re.fullmatch(
            r"[A-Fa-f0-9]{64}", echidna_ci["ECHIDNA_CI_ARTIFACT_SHA256"]
        ):
            raise ValueError("echidna_ci_artifact_sha256 must be a SHA-256")
        if not re.fullmatch(
            r"[A-Fa-f0-9]{40}", echidna_ci["ECHIDNA_CI_COMMIT"]
        ):
            raise ValueError("echidna_ci_commit must be a full commit SHA")
        if not re.fullmatch(
            r"/scfuzzbench/[A-Za-z0-9_./-]+",
            echidna_ci["ECHIDNA_CI_TOKEN_SSM_PARAMETER_NAME"],
        ):
            raise ValueError(
                "echidna_ci_token_ssm_parameter_name must start with /scfuzzbench/"
            )
    elif echidna_kms_arn:
        raise ValueError(
            "echidna_ci_token_kms_key_arn requires Echidna CI artifact mode"
        )
    if echidna_kms_arn and not re.fullmatch(
        r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Fa-f0-9-]{36}",
        echidna_kms_arn,
    ):
        raise ValueError("echidna_ci_token_kms_key_arn must be an exact KMS key ARN")

    medusa_source_names = (
        "MEDUSA_GIT_REPO",
        "MEDUSA_GIT_REF",
        "MEDUSA_GIT_COMMIT",
    )
    medusa_source = {name: value(name) for name in medusa_source_names}
    medusa_count = sum(bool(item) for item in medusa_source.values())
    if medusa_count not in {0, len(medusa_source_names)}:
        raise ValueError(
            "Medusa source mode requires git repo, git ref, and full commit together"
        )
    medusa_go_version = value("MEDUSA_GO_VERSION") or "1.24.0"
    medusa_go_sha256 = (
        value("MEDUSA_GO_SHA256")
        or "dea9ca38a0b852a74e81c26134671af7c0fbe65d81b0dc1c5bfe22cf7d4c8858"
    )
    if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", medusa_go_version):
        raise ValueError("medusa_go_version must look like 1.24.0")
    if not re.fullmatch(r"[A-Fa-f0-9]{64}", medusa_go_sha256):
        raise ValueError("medusa_go_sha256 must be a SHA-256")
    if medusa_count:
        if value("MEDUSA_VERSION"):
            raise ValueError(
                "medusa_version and Medusa source mode are mutually exclusive"
            )
        if "medusa" not in fuzzers:
            raise ValueError("Medusa source mode requires medusa")
        if not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
            medusa_source["MEDUSA_GIT_REPO"],
        ):
            raise ValueError("medusa_git_repo must be a GitHub HTTPS repository")
        medusa_ref = medusa_source["MEDUSA_GIT_REF"]
        if (
            not re.fullmatch(r"[A-Za-z0-9._/-]+", medusa_ref)
            or medusa_ref.startswith("-")
            or ".." in medusa_ref
            or "//" in medusa_ref
        ):
            raise ValueError("medusa_git_ref contains unsupported characters")
        if not re.fullmatch(
            r"[A-Fa-f0-9]{40}", medusa_source["MEDUSA_GIT_COMMIT"]
        ):
            raise ValueError("medusa_git_commit must be a full commit SHA")
    ssm_name = value("GIT_TOKEN_SSM_PARAMETER_NAME")
    if ssm_name and not re.fullmatch(r"/scfuzzbench/[A-Za-z0-9_./-]+", ssm_name):
        raise ValueError(
            "git_token_ssm_parameter_name must start with /scfuzzbench/"
        )
    properties_path = value("PROPERTIES_PATH")
    if properties_path and (
        properties_path.startswith("/")
        or ".." in properties_path
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", properties_path)
    ):
        raise ValueError("properties_path must be a safe repo-relative path")
    seed_source = value("SHARED_SEED_CORPUS_SOURCE")
    if seed_source:
        if not re.fullmatch(
            r"(?:s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/)?"
            r"[A-Za-z0-9/._+~-]+/?",
            seed_source,
        ):
            raise ValueError(
                "shared_seed_corpus_source must be a safe target-relative "
                "path or s3://bucket/prefix"
            )
        if "/./" in f"/{seed_source}/" or "/../" in f"/{seed_source}/":
            raise ValueError(
                "shared_seed_corpus_source must not contain dot path segments"
            )

    fuzzer_env_raw = value("FUZZER_ENV_JSON")
    fuzzer_env: dict[str, str] = {}
    if fuzzer_env_raw:
        try:
            parsed_env = json.loads(fuzzer_env_raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"fuzzer_env_json must be valid JSON: {exc}") from exc
        fuzzer_env = validate_fuzzer_env_map(parsed_env)

    return {
        "fuzzers": fuzzers,
        "fuzzer_env": fuzzer_env,
        "preliminary_interval_seconds": int(preliminary_seconds),
        "timeout_hours": timeout_hours,
    }


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
    latest_heartbeat_epoch: int | None = None,
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
    lifecycle_epoch = max(
        started_at,
        int(metadata.get("updated_at_epoch", started_at)),
        int(latest_heartbeat_epoch or 0),
    )
    if (
        active_instance_count == 0
        and now_epoch >= orphan_after
        and now_epoch - lifecycle_epoch >= HEARTBEAT_STALE_SECONDS
    ):
        return "orphan-deadline"
    return None


def _aws_json(*args: str) -> dict[str, Any]:
    output = subprocess.check_output(
        ["aws", *args, "--output", "json"], text=True
    )
    return json.loads(output) if output.strip() else {}


def _aws_text(*args: str) -> str:
    return subprocess.check_output(["aws", *args], text=True)


def list_s3_objects(bucket: str, prefix: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        args = [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--no-paginate",
        ]
        if token:
            args += ["--continuation-token", token]
        page = _aws_json(*args)
        objects.extend(
            entry
            for entry in page.get("Contents", [])
            if entry.get("Key")
        )
        if not page.get("IsTruncated"):
            return objects
        token = str(page.get("NextContinuationToken", ""))
        if not token:
            raise RuntimeError("truncated S3 listing omitted continuation token")


def list_s3_keys(bucket: str, prefix: str) -> list[str]:
    return [str(entry["Key"]) for entry in list_s3_objects(bucket, prefix)]


def _s3_timestamp_epoch(raw: Any) -> int:
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def latest_run_heartbeat_epoch(bucket: str, run_id: str) -> int | None:
    prefix = f"run-state/heartbeats/{validate_run_id(run_id)}/"
    epochs = [
        _s3_timestamp_epoch(entry["LastModified"])
        for entry in list_s3_objects(bucket, prefix)
        if entry.get("LastModified")
    ]
    return max(epochs) if epochs else None


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


def cmd_normalize_tools(args: argparse.Namespace) -> int:
    normalized = normalize_tool_sources(dict(os.environ))
    if args.github_output:
        with Path(args.github_output).open("a") as output:
            for key, value in normalized.items():
                output.write(f"{key}={value}\n")
    print(json.dumps(normalized, sort_keys=True))
    return 0


def cmd_normalize_excluded_fuzzers(args: argparse.Namespace) -> int:
    raw = os.environ.get("EXCLUDE_FUZZERS", "")
    normalized = normalize_excluded_fuzzers(raw)
    if args.require_canonical and raw != normalized:
        raise ValueError("exclude_fuzzers is not in canonical sorted form")
    if args.github_output:
        with Path(args.github_output).open("a") as output:
            output.write(f"exclude_fuzzers={normalized}\n")
    print(normalized)
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
    admitted_at = int(time.time())
    reservation["admitted_at_epoch"] = admitted_at
    reservation["updated_at_epoch"] = admitted_at
    _write_json(reservation_path, reservation)
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
    if str(metadata.get("run_id", "")) != run_id:
        raise ValueError("active run metadata does not match run_id")
    validate_backend_key(run_id, str(metadata["terraform_backend_key"]))
    if str(metadata.get("existing_bucket_name", "")) != bucket:
        raise ValueError("active run metadata does not match the shared artifact bucket")
    validate_scfuzzbench_commit(str(metadata.get("scfuzzbench_commit", "")))
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


def active_reservation_exists(bucket: str, run_id: str) -> bool:
    active_key = f"{ACTIVE_PREFIX}{validate_run_id(run_id)}.json"
    return active_key in list_s3_keys(bucket, active_key)


def cmd_reservation_exists(args: argparse.Namespace) -> int:
    run_id = validate_run_id(args.run_id)
    active = active_reservation_exists(args.bucket, run_id)
    if args.github_output:
        with Path(args.github_output).open("a") as output:
            output.write(f"active={str(active).lower()}\n")
    print(json.dumps({"active": active, "run_id": run_id}, sort_keys=True))
    return 0


def cmd_mark_failed_if_reserved(args: argparse.Namespace) -> int:
    run_id = validate_run_id(args.run_id)
    if not active_reservation_exists(args.bucket, run_id):
        print(
            json.dumps(
                {
                    "marked_failed": False,
                    "reason": "no-active-reservation",
                    "run_id": run_id,
                },
                sort_keys=True,
            )
        )
        return 0
    return cmd_mark_failed(args)


def cmd_discover_cleanup(args: argparse.Namespace) -> int:
    now_epoch = args.now_epoch or int(time.time())
    requested_run_id = (
        validate_run_id(args.requested_run_id) if args.requested_run_id else ""
    )
    force_run_id = validate_run_id(args.force_run_id) if args.force_run_id else ""
    if force_run_id and requested_run_id and force_run_id != requested_run_id:
        raise ValueError("forced cleanup run_id does not match requested run_id")
    selected_run_id = force_run_id or requested_run_id
    if selected_run_id:
        active_key = f"{ACTIVE_PREFIX}{selected_run_id}.json"
        if not active_reservation_exists(args.bucket, selected_run_id):
            print(
                json.dumps(
                    {"include": []}, separators=(",", ":"), sort_keys=True
                )
            )
            return 0
        keys = [active_key]
    else:
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
            if str(metadata.get("run_id", "")) != run_id:
                raise ValueError("run metadata does not match reservation key identity")
            validate_backend_key(run_id, str(metadata["terraform_backend_key"]))
            if str(metadata.get("existing_bucket_name", "")) != args.bucket:
                raise ValueError("run metadata does not match the shared artifact bucket")
            provisioning_commit = validate_scfuzzbench_commit(
                str(metadata.get("scfuzzbench_commit", ""))
            )
            status = str(metadata.get("status", ""))
            if status not in ACTIVE_RUN_STATUSES:
                raise ValueError(f"run metadata has invalid active status {status!r}")
            benchmark_uuid = str(metadata.get("benchmark_uuid", ""))
            if benchmark_uuid:
                validate_benchmark_uuid(benchmark_uuid)
            elif status != "provisioning-failed":
                raise ValueError(
                    "run metadata may omit benchmark_uuid only after provisioning failure"
                )
            if force_run_id:
                reason = "forced-orphan-cleanup"
            else:
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
                    latest_heartbeat_epoch=latest_run_heartbeat_epoch(
                        args.bucket, run_id
                    ),
                )
            if reason:
                include.append(
                    {
                        "run_id": run_id,
                        "terraform_backend_key": metadata["terraform_backend_key"],
                        "scfuzzbench_commit": provisioning_commit,
                        "benchmark_uuid": benchmark_uuid,
                        "status": status,
                        "reason": reason,
                    }
                )
        except Exception as exc:
            if selected_run_id:
                raise
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

        if resource_mode == "managed":
            expected_actions = {
                "apply": ["create"],
                "cleanup": ["delete"],
                "inspect": ["no-op"],
            }[mode]
            if actions != expected_actions:
                if mode == "apply":
                    description = "fresh run plan is not create-only"
                elif mode == "cleanup":
                    description = "cleanup plan is not delete-only"
                else:
                    description = "inspection plan contains managed changes"
                errors.append(f"{address}: {description} ({actions})")

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


def cmd_validate_inputs(args: argparse.Namespace) -> int:
    normalized = validate_benchmark_inputs(dict(os.environ))
    print(json.dumps(normalized, sort_keys=True))
    return 0


def cmd_validate_recovery_inputs(args: argparse.Namespace) -> int:
    tfvars_payload = json.loads(Path(args.tfvars_json).read_text())
    metadata_payload = json.loads(Path(args.metadata_json).read_text())
    commit = validate_recovery_inputs(
        tfvars_payload,
        metadata_payload,
        run_id=args.run_id,
        backend_key=args.backend_key,
        bucket=args.bucket,
        expected_commit=args.expected_commit,
        expected_status=args.expected_status,
    )
    if args.github_output:
        with Path(args.github_output).open("a") as output:
            output.write(f"scfuzzbench_commit={commit}\n")
    print("Recovery inputs match the immutable run identity.")
    return 0


def cmd_validate_provisioning_commit(args: argparse.Namespace) -> int:
    comparison = json.loads(Path(args.compare_json).read_text())
    validate_provisioning_commit(
        args.provisioning_commit,
        args.current_main_commit,
        comparison,
    )
    print("Provisioning commit is current main or an ancestor of current main.")
    return 0


def cmd_validate_state_outputs(args: argparse.Namespace) -> int:
    outputs = json.loads(Path(args.outputs_json).read_text())
    validate_state_outputs(
        outputs,
        run_id=args.run_id,
        backend_key=args.backend_key,
        benchmark_uuid=args.benchmark_uuid,
        allow_missing=args.allow_missing,
    )
    print("Terraform state outputs match the immutable run identity.")
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

    normalize_tools = subparsers.add_parser("normalize-tools")
    normalize_tools.add_argument("--github-output", default="")
    normalize_tools.set_defaults(func=cmd_normalize_tools)

    normalize_exclusions = subparsers.add_parser("normalize-excluded-fuzzers")
    normalize_exclusions.add_argument("--github-output", default="")
    normalize_exclusions.add_argument(
        "--require-canonical", action="store_true"
    )
    normalize_exclusions.set_defaults(func=cmd_normalize_excluded_fuzzers)

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

    mark_failed_if_reserved = subparsers.add_parser("mark-failed-if-reserved")
    mark_failed_if_reserved.add_argument("--bucket", required=True)
    mark_failed_if_reserved.add_argument("--run-id", required=True)
    mark_failed_if_reserved.add_argument("--workflow-url", required=True)
    mark_failed_if_reserved.add_argument(
        "--output", default="run-state-metadata.json"
    )
    mark_failed_if_reserved.set_defaults(func=cmd_mark_failed_if_reserved)

    reservation_exists = subparsers.add_parser("reservation-exists")
    reservation_exists.add_argument("--bucket", required=True)
    reservation_exists.add_argument("--run-id", required=True)
    reservation_exists.add_argument("--github-output", default="")
    reservation_exists.set_defaults(func=cmd_reservation_exists)

    discover = subparsers.add_parser("discover-cleanup")
    discover.add_argument("--bucket", required=True)
    discover.add_argument("--now-epoch", type=int, default=0)
    discover.add_argument("--requested-run-id", default="")
    discover.add_argument("--force-run-id", default="")
    discover.set_defaults(func=cmd_discover_cleanup)

    cleaned = subparsers.add_parser("mark-cleaned")
    cleaned.add_argument("--bucket", required=True)
    cleaned.add_argument("--run-id", required=True)
    cleaned.add_argument("--reason", required=True)
    cleaned.add_argument("--output", default="run-state-metadata.json")
    cleaned.set_defaults(func=cmd_mark_cleaned)

    validate_inputs = subparsers.add_parser("validate-inputs")
    validate_inputs.set_defaults(func=cmd_validate_inputs)

    recovery_inputs = subparsers.add_parser("validate-recovery-inputs")
    recovery_inputs.add_argument("--tfvars-json", required=True)
    recovery_inputs.add_argument("--metadata-json", required=True)
    recovery_inputs.add_argument("--run-id", required=True)
    recovery_inputs.add_argument("--backend-key", required=True)
    recovery_inputs.add_argument("--bucket", required=True)
    recovery_inputs.add_argument("--expected-commit", default="")
    recovery_inputs.add_argument("--expected-status", default="")
    recovery_inputs.add_argument("--github-output", default="")
    recovery_inputs.set_defaults(func=cmd_validate_recovery_inputs)

    provisioning_commit = subparsers.add_parser("validate-provisioning-commit")
    provisioning_commit.add_argument("--provisioning-commit", required=True)
    provisioning_commit.add_argument("--current-main-commit", required=True)
    provisioning_commit.add_argument("--compare-json", required=True)
    provisioning_commit.set_defaults(func=cmd_validate_provisioning_commit)

    state_outputs = subparsers.add_parser("validate-state-outputs")
    state_outputs.add_argument("--outputs-json", required=True)
    state_outputs.add_argument("--run-id", required=True)
    state_outputs.add_argument("--backend-key", required=True)
    state_outputs.add_argument("--benchmark-uuid", default="")
    state_outputs.add_argument("--allow-missing", action="store_true")
    state_outputs.set_defaults(func=cmd_validate_state_outputs)

    validate_plan = subparsers.add_parser("validate-plan")
    validate_plan.add_argument("--plan-json", required=True)
    validate_plan.add_argument(
        "--mode", choices=("apply", "cleanup", "inspect"), required=True
    )
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
