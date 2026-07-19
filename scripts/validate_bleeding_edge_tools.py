#!/usr/bin/env python3
"""Preflight opt-in Echidna artifacts and Medusa source refs before Terraform."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


GITHUB_REPO_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
COMMIT_RE = re.compile(r"^[A-Fa-f0-9]{40}$")
DIGEST_RE = re.compile(r"^[A-Fa-f0-9]{64}$")


class ValidationError(RuntimeError):
    """Raised when an opt-in source no longer matches its pinned inputs."""


def github_repo_path(repo_url: str) -> str:
    match = GITHUB_REPO_RE.fullmatch(repo_url)
    if not match:
        raise ValidationError(f"invalid GitHub repository URL: {repo_url!r}")
    return f"{match.group('owner')}/{match.group('repo')}"


def github_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "scfuzzbench-tool-source-preflight",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise ValidationError(f"GitHub metadata request failed for {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"GitHub metadata response was not an object: {url}")
    return value


def validate_echidna_artifact(
    *,
    repo_url: str,
    run_id: str,
    artifact_name: str,
    artifact_sha256: str,
    expected_commit: str,
) -> dict[str, str]:
    repo_path = github_repo_path(repo_url)
    if not run_id.isdigit() or int(run_id) <= 0:
        raise ValidationError("Echidna CI run ID must be a positive integer")
    if not COMMIT_RE.fullmatch(expected_commit):
        raise ValidationError("Echidna CI commit must be a full 40-character SHA")
    if not DIGEST_RE.fullmatch(artifact_sha256):
        raise ValidationError("Echidna CI artifact digest must be a SHA-256")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", artifact_name) or "linux" not in artifact_name.lower():
        raise ValidationError("Echidna CI artifact must be a Linux artifact name without spaces")

    api_base = f"https://api.github.com/repos/{repo_path}"
    run = github_json(f"{api_base}/actions/runs/{run_id}")
    resolved_repo = str((run.get("repository") or {}).get("full_name") or "")
    resolved_commit = str(run.get("head_sha") or "").lower()
    expected_commit = expected_commit.lower()
    if resolved_repo.lower() != repo_path.lower():
        raise ValidationError(
            f"Echidna run repository mismatch: expected {repo_path}, got {resolved_repo or 'missing'}"
        )
    if resolved_commit != expected_commit:
        raise ValidationError(
            f"Echidna run commit drift: expected {expected_commit}, got {resolved_commit or 'missing'}"
        )
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValidationError(f"Echidna run {run_id} is not completed successfully")

    encoded_name = urllib.parse.quote(artifact_name, safe="")
    artifacts = github_json(
        f"{api_base}/actions/runs/{run_id}/artifacts?name={encoded_name}&per_page=100"
    )
    matches = [
        item
        for item in artifacts.get("artifacts", [])
        if isinstance(item, dict) and item.get("name") == artifact_name
    ]
    if len(matches) != 1:
        raise ValidationError(
            f"expected one Echidna artifact named {artifact_name!r}, found {len(matches)}"
        )
    artifact = matches[0]
    if artifact.get("expired") is not False:
        raise ValidationError(f"Echidna artifact {artifact_name!r} is expired")
    expires_at = str(artifact.get("expires_at") or "")
    if expires_at:
        try:
            expiry = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"invalid Echidna artifact expiry: {expires_at}") from exc
        if expiry <= dt.datetime.now(dt.timezone.utc):
            raise ValidationError(f"Echidna artifact {artifact_name!r} has expired")
    artifact_commit = str((artifact.get("workflow_run") or {}).get("head_sha") or "").lower()
    if artifact_commit != expected_commit:
        raise ValidationError(
            f"Echidna artifact commit mismatch: expected {expected_commit}, got {artifact_commit or 'missing'}"
        )
    api_digest = str(artifact.get("digest") or "").lower()
    expected_digest = f"sha256:{artifact_sha256.lower()}"
    if api_digest != expected_digest:
        raise ValidationError(
            f"Echidna artifact digest mismatch: expected {expected_digest}, got {api_digest or 'missing'}"
        )
    artifact_id = str(artifact.get("id") or "")
    if not artifact_id.isdigit():
        raise ValidationError("Echidna artifact metadata is missing a numeric ID")

    return {
        "repository": repo_path,
        "run_id": run_id,
        "commit": expected_commit,
        "artifact_id": artifact_id,
        "artifact_name": artifact_name,
        "artifact_digest": expected_digest,
        "expires_at": expires_at,
    }


def resolve_git_ref(repo_url: str, git_ref: str) -> str:
    github_repo_path(repo_url)
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", git_ref):
        raise ValidationError("Medusa git ref contains unsupported characters")
    with tempfile.TemporaryDirectory(prefix="scfuzzbench-medusa-ref-") as directory:
        checkout = Path(directory)
        commands = [
            ["git", "init", "-q", str(checkout)],
            ["git", "-C", str(checkout), "remote", "add", "origin", repo_url],
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--no-tags",
                "--depth",
                "1",
                "origin",
                git_ref,
            ],
            ["git", "-C", str(checkout), "rev-parse", "FETCH_HEAD^{commit}"],
        ]
        output = ""
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env={"PATH": str(Path("/usr/local/bin")) + ":/usr/bin:/bin"},
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValidationError(f"could not resolve Medusa ref {git_ref!r}: {exc}") from exc
            output = result.stdout.strip()
    if not COMMIT_RE.fullmatch(output):
        raise ValidationError(f"Medusa ref resolved to an invalid commit: {output!r}")
    return output.lower()


def validate_medusa_source(*, repo_url: str, git_ref: str, expected_commit: str) -> dict[str, str]:
    if not COMMIT_RE.fullmatch(expected_commit):
        raise ValidationError("Medusa git commit must be a full 40-character SHA")
    resolved_commit = resolve_git_ref(repo_url, git_ref)
    if resolved_commit != expected_commit.lower():
        raise ValidationError(
            f"Medusa ref drift: {git_ref} resolves to {resolved_commit}, expected {expected_commit.lower()}"
        )
    return {
        "repository": github_repo_path(repo_url),
        "requested_ref": git_ref,
        "commit": resolved_commit,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--echidna-ci-repo", default="")
    parser.add_argument("--echidna-ci-run-id", default="")
    parser.add_argument("--echidna-ci-artifact-name", default="")
    parser.add_argument("--echidna-ci-artifact-sha256", default="")
    parser.add_argument("--echidna-ci-commit", default="")
    parser.add_argument("--medusa-git-repo", default="")
    parser.add_argument("--medusa-git-ref", default="")
    parser.add_argument("--medusa-git-commit", default="")
    args = parser.parse_args()

    results: dict[str, dict[str, str]] = {}
    try:
        if args.echidna_ci_repo:
            results["echidna"] = validate_echidna_artifact(
                repo_url=args.echidna_ci_repo,
                run_id=args.echidna_ci_run_id,
                artifact_name=args.echidna_ci_artifact_name,
                artifact_sha256=args.echidna_ci_artifact_sha256,
                expected_commit=args.echidna_ci_commit,
            )
        if args.medusa_git_repo:
            results["medusa"] = validate_medusa_source(
                repo_url=args.medusa_git_repo,
                git_ref=args.medusa_git_ref,
                expected_commit=args.medusa_git_commit,
            )
    except ValidationError as exc:
        parser.exit(1, f"error: {exc}\n")

    print(json.dumps(results, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
