#!/usr/bin/env python3
"""Resolve the latest upstream RELEASE version for release-installed fuzzers.

At dispatch time the benchmark tracks the newest published GitHub release of each
release-installed fuzzer (Echidna, Medusa, Recon) unless the request explicitly
pinned a version or opted into a bleeding-edge source/CI override. The resolved
version is threaded downstream and pinned into the run manifest, so each
individual run stays reproducible even though the default tracks "latest".

Resolution rules, per tool:
  * An explicitly requested (non-empty) version always wins -- operators can
    still pin a specific version.
  * When a bleeding-edge override is active (Echidna CI artifact, Medusa source
    build), the release path is skipped and the version is left empty for the
    override install path to handle.
  * Otherwise the latest published, non-prerelease, non-draft release tag is
    fetched from GitHub and normalized (a single leading ``v`` is dropped).

Foundry is intentionally excluded: the harness builds it from a pinned master
commit that is ahead of every release (see infrastructure/variables.tf), so no
release resolution applies to it.

Fails closed: a resolution error aborts the dispatch rather than silently
falling back to a stale pin.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Version strings accepted by the per-fuzzer install scripts (bare, no leading
# "v"): see fuzzers/{echidna,medusa,recon-fuzzer}/install.sh.
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

# Release source repositories. Foundry is deliberately absent (master pin).
TOOL_REPOS = {
    "echidna": "crytic/echidna",
    "medusa": "crytic/medusa",
    "recon": "Recon-Fuzz/recon-fuzzer",
}


class ResolutionError(RuntimeError):
    """A tool version could not be resolved."""


def _normalize_tag(tag: str) -> str:
    """Drop a single leading ``v`` and validate the bare version string."""
    version = tag.strip()
    if version[:1] == "v":
        version = version[1:]
    if not VERSION_RE.fullmatch(version):
        raise ResolutionError(
            f"resolved release tag {tag!r} is not a valid bare version"
        )
    return version


def _fetch_latest_release(repo: str, *, token: str | None, retries: int = 3) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "scfuzzbench-tool-version-resolver",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ResolutionError(
                    f"latest-release response for {repo} was not an object"
                )
            return value
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise ResolutionError(
        f"latest-release request failed for {repo}: {last_exc}"
    ) from last_exc


def resolve_latest_version(
    repo: str, *, token: str | None = None, fetcher=_fetch_latest_release
) -> str:
    """Return the normalized latest published release version for ``repo``."""
    release = fetcher(repo, token=token)
    if release.get("draft") or release.get("prerelease"):
        # The /releases/latest endpoint already excludes these, but never trust
        # a draft/prerelease as the canonical "latest".
        raise ResolutionError(f"latest release for {repo} is a draft/prerelease")
    tag = str(release.get("tag_name") or "").strip()
    if not tag:
        raise ResolutionError(f"latest release for {repo} has no tag_name")
    return _normalize_tag(tag)


def resolve_all(env: dict, *, fetcher=_fetch_latest_release) -> dict:
    """Compute resolved {echidna,medusa,recon}_version from the request env."""
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or None

    echidna_ci = bool((env.get("ECHIDNA_CI_REPO") or "").strip())
    medusa_source = bool((env.get("MEDUSA_GIT_REPO") or "").strip())

    plan = {
        "echidna": {
            "requested": (env.get("ECHIDNA_VERSION") or "").strip(),
            "override": echidna_ci,
        },
        "medusa": {
            "requested": (env.get("MEDUSA_VERSION") or "").strip(),
            "override": medusa_source,
        },
        "recon": {
            "requested": (env.get("RECON_VERSION") or "").strip(),
            "override": False,
        },
    }

    resolved: dict[str, str] = {}
    for tool, spec in plan.items():
        if spec["requested"]:
            # Explicit pin wins; validate it here too so downstream stays clean.
            resolved[tool] = _normalize_tag(spec["requested"])
        elif spec["override"]:
            resolved[tool] = ""
        else:
            resolved[tool] = resolve_latest_version(
                TOOL_REPOS[tool], token=token, fetcher=fetcher
            )
    return resolved


def _write_github_output(path: str, resolved: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for tool in ("echidna", "medusa", "recon"):
            handle.write(f"{tool}_version={resolved[tool]}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT", ""),
        help="Path of the GITHUB_OUTPUT file to append resolved versions to.",
    )
    args = parser.parse_args(argv)

    try:
        resolved = resolve_all(os.environ)
    except ResolutionError as exc:
        print(f"tool version resolution failed: {exc}", file=sys.stderr)
        return 1

    for tool in ("echidna", "medusa", "recon"):
        value = resolved[tool] or "(override/source path)"
        print(f"resolved {tool}_version={value}", file=sys.stderr)

    if args.github_output:
        _write_github_output(args.github_output, resolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
