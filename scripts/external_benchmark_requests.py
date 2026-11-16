#!/usr/bin/env python3
"""Turn a `/benchmark` comment on a watched pull request into a request issue.

A fuzzer's own pull requests are where people notice a performance or coverage
question, so this lets them ask for a benchmark from there. The trust model is
deliberately narrow:

  * Authorization data lives in **this** repository (`.github/benchmark-watch.json`),
    never in the watched repository. Granting someone the ability to ask for a
    benchmark takes a reviewed pull request here.
  * A comment can only *propose* a run. The watcher opens a request issue in the
    `benchmark/01-pending` state; a maintainer still applies
    `benchmark/03-approved` before anything is provisioned, so no external actor
    gains spend authority.
  * The proposing account is recorded in the issue as provenance and is
    re-checked against the same allowlist when the request is validated. The
    issue is opened by automation, so its own `author_association` says nothing
    about who asked for it and must never be treated as the authorization.

Comment bodies are untrusted input: the command grammar is strict, every value
is re-validated by the shared request validators, and nothing is interpolated
into a shell.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / ".github" / "benchmark-watch.json"
REQUEST_MARKER = "scfuzzbench-benchmark-request:v1"
PROVENANCE_MARKER = "scfuzzbench-external-request:v1"

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
ORG_RE = LOGIN_RE
COMMAND_RE = re.compile(r"^/[a-z][a-z0-9-]{0,31}$")
COMMIT_RE = re.compile(r"^[A-Fa-f0-9]{40}$")

# Overrides a comment may set. Everything else comes from the watched
# repository's reviewed defaults.
OVERRIDE_PATTERNS = {
    "instance_type": re.compile(r"^[a-z0-9]+\.[a-z0-9]+$"),
    "instances_per_fuzzer": re.compile(r"^[0-9]{1,2}$"),
    "timeout_hours": re.compile(r"^[0-9]{1,2}(?:\.[0-9]{1,2})?$"),
    "target_commit": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"),
}
INTEGER_OVERRIDES = {"instances_per_fuzzer"}
FLOAT_OVERRIDES = {"timeout_hours"}


class ConfigError(RuntimeError):
    """The watch configuration in this repository is malformed."""


class CommandError(RuntimeError):
    """The comment does not carry a usable benchmark command."""


def load_watch_config(path: Optional[Path] = None) -> dict[str, Any]:
    """Load and validate the watch configuration."""
    config_path = path or DEFAULT_CONFIG_PATH
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"missing watch configuration: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid watch configuration JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError("watch configuration must be a JSON object")
    if config.get("schema_version") != 1:
        raise ConfigError("watch configuration schema_version must be 1")
    command = str(config.get("command", "")).strip()
    if not COMMAND_RE.fullmatch(command):
        raise ConfigError(f"invalid watch command: {command!r}")

    repositories = config.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ConfigError("watch configuration must list at least one repository")
    seen: set[str] = set()
    for entry in repositories:
        if not isinstance(entry, dict):
            raise ConfigError("each watched repository must be a JSON object")
        repo = str(entry.get("repo", "")).strip()
        if not REPO_RE.fullmatch(repo):
            raise ConfigError(f"invalid watched repository: {repo!r}")
        if repo.lower() in seen:
            raise ConfigError(f"duplicate watched repository: {repo}")
        seen.add(repo.lower())
        for login in entry.get("requesters", []) or []:
            if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
                raise ConfigError(f"invalid requester login for {repo}: {login!r}")
        for org in entry.get("requester_orgs", []) or []:
            if not isinstance(org, str) or not ORG_RE.fullmatch(org):
                raise ConfigError(f"invalid requester org for {repo}: {org!r}")
        if not (entry.get("requesters") or entry.get("requester_orgs")):
            raise ConfigError(
                f"{repo} must list requesters or requester_orgs; an empty "
                "allowlist would let anyone open benchmark requests"
            )
        if not isinstance(entry.get("defaults"), dict):
            raise ConfigError(f"{repo} must define a defaults object")
    return config


def repository_config(config: dict[str, Any], repo: str) -> Optional[dict[str, Any]]:
    """Return the watch entry for `repo`, if it is watched at all."""
    for entry in config.get("repositories", []):
        if str(entry.get("repo", "")).lower() == repo.lower():
            return entry
    return None


def authorize_requester(
    login: str,
    repo_config: dict[str, Any],
    *,
    org_memberships: Iterable[str] = (),
) -> tuple[bool, str]:
    """Decide whether `login` may propose a benchmark for a watched repository.

    Authorization comes only from this repository's configuration. Membership
    of the watched repository, and the association of the automation account
    that files the issue, are both irrelevant here.
    """
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        return False, "requester login is not a valid GitHub account name"
    requesters = {
        str(item).lower() for item in repo_config.get("requesters", []) or []
    }
    if login.lower() in requesters:
        return True, f"{login} is an allowlisted requester"
    allowed_orgs = {
        str(item).lower() for item in repo_config.get("requester_orgs", []) or []
    }
    memberships = {str(item).lower() for item in org_memberships}
    overlap = sorted(allowed_orgs & memberships)
    if overlap:
        return True, f"{login} is a member of {overlap[0]}"
    return False, (
        f"{login} is not allowed to request benchmarks for "
        f"{repo_config.get('repo')}"
    )


def parse_command(body: str, command: str) -> Optional[dict[str, Any]]:
    """Parse a `/benchmark key=value ...` comment into validated overrides.

    Returns None when the comment does not invoke the command at all.
    """
    if not isinstance(body, str):
        return None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith(command):
            continue
        remainder = line[len(command):]
        if remainder and not remainder[0].isspace():
            # e.g. "/benchmarking" must not trigger "/benchmark".
            continue
        overrides: dict[str, Any] = {}
        for token in remainder.split():
            if "=" not in token:
                raise CommandError(f"expected key=value, got {token!r}")
            key, _, value = token.partition("=")
            pattern = OVERRIDE_PATTERNS.get(key)
            if pattern is None:
                raise CommandError(f"unsupported option: {key!r}")
            if key in overrides:
                raise CommandError(f"duplicate option: {key!r}")
            if not pattern.fullmatch(value):
                raise CommandError(f"invalid value for {key}: {value!r}")
            if key in INTEGER_OVERRIDES:
                overrides[key] = int(value)
            elif key in FLOAT_OVERRIDES:
                overrides[key] = float(value)
            else:
                overrides[key] = value
        return overrides
    return None


# How each watched fuzzer builds an unreleased revision. A new repository is a
# configuration entry, not new code, as long as its fuzzer builds one of these
# ways. Both shapes are the per-variant builds the benchmark already supports.
BUILD_MODES = {
    "ci": {
        "bases": {"echidna"},
        "build_fields": ("run_id", "artifact_name", "artifact_sha256", "commit"),
        "variant_field": "ci",
        # Run-level request fields carrying the baseline build.
        "run_level": {
            "run_id": "echidna_ci_run_id",
            "artifact_name": "echidna_ci_artifact_name",
            "artifact_sha256": "echidna_ci_artifact_sha256",
            "commit": "echidna_ci_commit",
        },
        "repo_field": "echidna_ci_repo",
        # Reviewed defaults this mode needs, copied verbatim from config.
        "extra_defaults": (
            "echidna_ci_token_ssm_parameter_name",
            "echidna_ci_token_kms_key_arn",
        ),
    },
    "source": {
        "bases": {"medusa"},
        "build_fields": ("git_ref", "git_commit"),
        "variant_field": "source",
        "run_level": {
            "git_ref": "medusa_git_ref",
            "git_commit": "medusa_git_commit",
        },
        "repo_field": "medusa_git_repo",
        "extra_defaults": ("medusa_go_version", "medusa_go_sha256"),
    },
}
LOWERCASE_BUILD_FIELDS = {"commit", "artifact_sha256", "git_commit"}


def build_mode_for(repo_config: dict[str, Any]) -> dict[str, Any]:
    """Return the build-mode descriptor for a watched repository."""
    mode = str(repo_config.get("build_mode", "")).strip()
    descriptor = BUILD_MODES.get(mode)
    if descriptor is None:
        raise ConfigError(f"unsupported build_mode: {mode!r}")
    base = str(repo_config.get("base", "")).strip()
    if base not in descriptor["bases"]:
        raise ConfigError(f"build_mode {mode!r} does not apply to base {base!r}")
    return descriptor


def _normalized_build(
    descriptor: dict[str, Any], build: dict[str, str], *, label: str
) -> dict[str, str]:
    missing = [
        field
        for field in descriptor["build_fields"]
        if not str(build.get(field, "") or "").strip()
    ]
    if missing:
        raise ConfigError(f"{label} build is missing: {', '.join(missing)}")
    normalized = {}
    for field in descriptor["build_fields"]:
        value = str(build[field]).strip()
        normalized[field] = value.lower() if field in LOWERCASE_BUILD_FIELDS else value
    return normalized


def build_request_payload(
    repo_config: dict[str, Any],
    *,
    variant_key: str,
    pull_request_build: dict[str, str],
    baseline_build: dict[str, str],
    overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the benchmark request JSON for a watched pull request.

    The watched repository's baseline build and the pull request build run as
    two fuzzer variants of the same fuzzer, so both are measured under
    identical conditions in a single benchmark.
    """
    descriptor = build_mode_for(repo_config)
    base = str(repo_config["base"]).strip()
    pr_build = _normalized_build(descriptor, pull_request_build, label="pull request")
    base_build = _normalized_build(descriptor, baseline_build, label="baseline")

    defaults = dict(repo_config.get("defaults", {}))
    payload: dict[str, Any] = {
        "target_repo_url": defaults.get("target_repo_url", ""),
        "target_commit": defaults.get("target_commit", ""),
        "benchmark_type": defaults.get("benchmark_type", "property"),
        "instance_type": defaults.get("instance_type", "c6a.4xlarge"),
        "instances_per_fuzzer": defaults.get("instances_per_fuzzer", 4),
        "timeout_hours": defaults.get("timeout_hours", 1),
        "preliminary_interval_minutes": defaults.get("preliminary_interval_minutes", 60),
        "fuzzers": [base, variant_key],
        "fuzzer_variants": [
            {
                "key": variant_key,
                "base": base,
                descriptor["variant_field"]: dict(pr_build),
            }
        ],
        descriptor["repo_field"]: f"https://github.com/{repo_config['repo']}",
        "git_token_ssm_parameter_name": defaults.get(
            "git_token_ssm_parameter_name", ""
        ),
        "properties_path": defaults.get("properties_path", ""),
    }
    for field, request_key in descriptor["run_level"].items():
        payload[request_key] = base_build[field]
    # Carry the reviewed extras this build mode needs (tokens, toolchain pins).
    for key in descriptor["extra_defaults"]:
        if defaults.get(key):
            payload[key] = defaults[key]

    for key, value in (overrides or {}).items():
        if key not in OVERRIDE_PATTERNS:
            raise CommandError(f"unsupported option: {key!r}")
        payload[key] = value

    commit_field = "commit" if "commit" in pr_build else "git_commit"
    if pr_build[commit_field] == base_build[commit_field]:
        raise CommandError(
            "the pull request build matches the baseline build; there is "
            "nothing to compare"
        )
    return payload


def variant_key_for_pull_request(base: str, pull_number: int) -> str:
    """Variant keys must stay prefixed with their base fuzzer."""
    if not isinstance(pull_number, int) or pull_number < 1:
        raise CommandError("pull request number must be a positive integer")
    return f"{base}-pr-{pull_number}"


def dedupe_key(repo: str, comment_id: int) -> str:
    """Stable marker so a comment is only ever acted on once."""
    return f"{PROVENANCE_MARKER} {repo}#comment-{int(comment_id)}"


def render_issue_body(
    payload: dict[str, Any],
    *,
    repo: str,
    pull_number: int,
    comment_id: int,
    requester: str,
    reason: str,
) -> str:
    """Render the request issue, carrying provenance for re-verification."""
    provenance = {
        "source_repo": repo,
        "source_pull_request": pull_number,
        "source_comment_id": comment_id,
        "requested_by": requester,
    }
    return "\n".join(
        [
            f"<!-- {REQUEST_MARKER} -->",
            f"<!-- {dedupe_key(repo, comment_id)} -->",
            "",
            # A plain cross-repository reference: GitHub links this request
            # from the pull request itself, so results are visible there
            # without this repository holding any write access to it.
            f"Tracking {repo}#{pull_number}, requested by @{requester} ({reason}).",
            "",
            "This issue only proposes a benchmark. A maintainer must still apply",
            "`benchmark/03-approved` before anything is provisioned.",
            "",
            "```json",
            json.dumps(payload, indent=2, sort_keys=True),
            "```",
            "",
            f"<!-- scfuzzbench-external-provenance: {json.dumps(provenance, sort_keys=True)} -->",
        ]
    )


def parse_provenance(body: str) -> Optional[dict[str, Any]]:
    """Read the provenance block back out of a request issue."""
    match = re.search(
        r"<!--\s*scfuzzbench-external-provenance:\s*(\{.*?\})\s*-->", body or "", re.S
    )
    if not match:
        return None
    try:
        provenance = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(provenance, dict):
        return None
    return provenance


# --- build resolution -------------------------------------------------------
#
# `fetch` is injected so the resolution rules can be tested without network
# access, and so the workflow can supply an authenticated client.


def resolve_ci_build(
    fetch: Any,
    *,
    repo: str,
    commit: str,
    artifact_name: str,
) -> dict[str, str]:
    """Find the successful Actions run for `commit` publishing `artifact_name`."""
    if not COMMIT_RE.fullmatch(commit):
        raise CommandError(f"expected a full commit SHA, got {commit!r}")
    runs = fetch(f"/repos/{repo}/actions/runs?head_sha={commit}&per_page=100")
    candidates = [
        run
        for run in runs.get("workflow_runs", [])
        if run.get("status") == "completed" and run.get("conclusion") == "success"
    ]
    for run in candidates:
        run_id = run.get("id")
        artifacts = fetch(
            f"/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"
        )
        for artifact in artifacts.get("artifacts", []):
            if artifact.get("name") != artifact_name or artifact.get("expired"):
                continue
            digest = str(artifact.get("digest") or "").removeprefix("sha256:")
            if not re.fullmatch(r"[A-Fa-f0-9]{64}", digest):
                raise CommandError(
                    f"artifact {artifact_name!r} for {commit} has no usable digest"
                )
            return {
                "run_id": str(run_id),
                "artifact_name": artifact_name,
                "artifact_sha256": digest.lower(),
                "commit": commit.lower(),
            }
    raise CommandError(
        f"no successful run of {repo} for {commit[:12]} publishes an unexpired "
        f"{artifact_name!r}"
    )


def resolve_source_build(fetch: Any, *, repo: str, ref: str) -> dict[str, str]:
    """Resolve a git ref in the watched repository to a full commit."""
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", ref or ""):
        raise CommandError(f"unsupported git ref: {ref!r}")
    commit = fetch(f"/repos/{repo}/commits/{ref}")
    sha = str(commit.get("sha") or "")
    if not COMMIT_RE.fullmatch(sha):
        raise CommandError(f"{repo} ref {ref!r} did not resolve to a commit")
    return {"git_ref": ref, "git_commit": sha.lower()}


def resolve_build(
    fetch: Any,
    repo_config: dict[str, Any],
    *,
    ref: str = "",
    commit: str = "",
) -> dict[str, str]:
    """Resolve one side of the comparison for a watched repository."""
    descriptor = build_mode_for(repo_config)
    repo = str(repo_config["repo"])
    if descriptor["variant_field"] == "ci":
        head = commit
        if not head:
            head = resolve_source_build(fetch, repo=repo, ref=ref)["git_commit"]
        return resolve_ci_build(
            fetch,
            repo=repo,
            commit=head,
            artifact_name=str(repo_config["artifact_name"]),
        )
    return resolve_source_build(fetch, repo=repo, ref=ref or commit)


def resolve_comparison(
    fetch: Any,
    repo_config: dict[str, Any],
    *,
    pull_request_ref: str,
    pull_request_commit: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve the baseline and pull request builds for a watched repository."""
    baseline_ref = str(repo_config.get("baseline_ref", "master"))
    baseline = resolve_build(fetch, repo_config, ref=baseline_ref)
    pull_request = resolve_build(
        fetch, repo_config, ref=pull_request_ref, commit=pull_request_commit
    )
    return baseline, pull_request


# --- entry point ------------------------------------------------------------


def _github_client(token: str) -> Any:
    """Return a `fetch(path)` calling the GitHub API with `token`."""
    import urllib.error
    import urllib.request

    def fetch(path: str) -> Any:
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "scfuzzbench-benchmark-comment-watch",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise CommandError(f"GitHub request failed for {path}: {exc}") from exc

    return fetch


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config", help="Validate the watch configuration.")
    scan = subparsers.add_parser("scan", help="Scan watched pull requests.")
    scan.add_argument("--lookback-minutes", type=int, default=60)
    scan.add_argument("--request-repository", default="")
    scan.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = load_watch_config()
    except ConfigError as exc:
        parser.exit(1, f"error: {exc}\n")

    if args.command == "validate-config":
        repos = ", ".join(entry["repo"] for entry in config["repositories"])
        print(f"watch configuration is valid: {repos}")
        return 0

    # DRAFT: the scan loop below is deliberately read-only until the request
    # repository, token scopes and dedupe store are settled in review.
    fetch = _github_client(os.environ.get("GITHUB_TOKEN", ""))
    proposals = []
    for entry in config["repositories"]:
        proposals.extend(
            scan_repository(
                fetch,
                entry,
                command=config["command"],
                lookback_minutes=args.lookback_minutes,
            )
        )
    print(json.dumps(proposals, indent=2, sort_keys=True))
    if args.dry_run:
        print(f"dry run: {len(proposals)} proposal(s) not filed")
        return 0
    raise SystemExit(
        "filing request issues is not wired up yet; run with --dry-run"
    )


def scan_repository(
    fetch: Any,
    repo_config: dict[str, Any],
    *,
    command: str,
    lookback_minutes: int,
) -> list[dict[str, Any]]:
    """Return the authorized, resolvable proposals in a watched repository."""
    import datetime as dt

    since = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=max(lookback_minutes, 1))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = str(repo_config["repo"])
    comments = fetch(f"/repos/{repo}/issues/comments?since={since}&per_page=100")
    proposals: list[dict[str, Any]] = []
    for comment in comments if isinstance(comments, list) else []:
        try:
            overrides = parse_command(str(comment.get("body", "")), command)
        except CommandError as exc:
            proposals.append({"comment_id": comment.get("id"), "skipped": str(exc)})
            continue
        if overrides is None:
            continue
        login = str((comment.get("user") or {}).get("login", ""))
        allowed, reason = authorize_requester(login, repo_config)
        if not allowed:
            proposals.append({"comment_id": comment.get("id"), "skipped": reason})
            continue
        issue_url = str(comment.get("issue_url", ""))
        pull_number = int(issue_url.rsplit("/", 1)[-1] or 0)
        issue = fetch(f"/repos/{repo}/issues/{pull_number}")
        if not issue.get("pull_request"):
            proposals.append(
                {"comment_id": comment.get("id"), "skipped": "not a pull request"}
            )
            continue
        pull = fetch(f"/repos/{repo}/pulls/{pull_number}")
        head_sha = str((pull.get("head") or {}).get("sha", ""))
        baseline, pull_build = resolve_comparison(
            fetch,
            repo_config,
            pull_request_ref=f"refs/pull/{pull_number}/head",
            pull_request_commit=head_sha,
        )
        payload = build_request_payload(
            repo_config,
            variant_key=variant_key_for_pull_request(
                str(repo_config["base"]), pull_number
            ),
            pull_request_build=pull_build,
            baseline_build=baseline,
            overrides=overrides,
        )
        proposals.append(
            {
                "repo": repo,
                "pull_request": pull_number,
                "comment_id": comment.get("id"),
                "requested_by": login,
                "reason": reason,
                "issue_body": render_issue_body(
                    payload,
                    repo=repo,
                    pull_number=pull_number,
                    comment_id=int(comment.get("id", 0)),
                    requester=login,
                    reason=reason,
                ),
            }
        )
    return proposals


if __name__ == "__main__":
    raise SystemExit(main())

