#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "benchmarks" / "targets.json"

TARGET_FIELDS = {
    "id",
    "label",
    "repo",
    "commit",
    "properties_path",
    "rationale",
    "related_work_refs",
    "overlap_group",
    "overlap_notes",
}
RELATED_WORK_FIELDS = {"url", "description"}

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _https_url(value: object) -> bool:
    if not _string(value):
        return False
    parsed = urlsplit(str(value))
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _validate_properties_path(value: object) -> str | None:
    if not _string(value):
        return "must be a non-empty string"
    raw = str(value)
    if "\\" in raw:
        return "must use forward slashes"
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        return "must be a repo-relative path without '..'"
    if str(path) != raw:
        return "must be a normalized repo-relative path"
    if path.suffix != ".sol":
        return "must point to a .sol file"
    return None


def validate_manifest(manifest: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]

    unknown_top_level = set(manifest) - {"schema_version", "targets"}
    if unknown_top_level:
        errors.append(f"manifest has unknown fields: {', '.join(sorted(unknown_top_level))}")

    if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
        errors.append("schema_version must be the integer 1")

    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        errors.append("targets must be a non-empty array")
        return errors

    seen_ids: dict[str, int] = {}
    seen_repos: dict[str, int] = {}

    for index, target in enumerate(targets):
        prefix = f"targets[{index}]"
        if not isinstance(target, dict):
            errors.append(f"{prefix} must be an object")
            continue

        missing = TARGET_FIELDS - set(target)
        unknown = set(target) - TARGET_FIELDS
        if missing:
            errors.append(f"{prefix} is missing fields: {', '.join(sorted(missing))}")
        if unknown:
            errors.append(f"{prefix} has unknown fields: {', '.join(sorted(unknown))}")

        target_id = target.get("id")
        if not _string(target_id) or not SLUG_RE.fullmatch(str(target_id)):
            errors.append(f"{prefix}.id must be a lowercase kebab-case identifier")
        elif str(target_id) in seen_ids:
            errors.append(f"{prefix}.id duplicates targets[{seen_ids[str(target_id)]}].id")
        else:
            seen_ids[str(target_id)] = index

        if not _string(target.get("label")):
            errors.append(f"{prefix}.label must be a non-empty string")

        repo = target.get("repo")
        if not _string(repo) or not REPO_RE.fullmatch(str(repo)):
            errors.append(f"{prefix}.repo must be a canonical https://github.com/owner/repo URL")
        elif str(repo).endswith(".git"):
            errors.append(f"{prefix}.repo must not end in .git")
        elif str(repo).lower() in seen_repos:
            errors.append(f"{prefix}.repo duplicates targets[{seen_repos[str(repo).lower()]}].repo")
        else:
            seen_repos[str(repo).lower()] = index

        commit = target.get("commit")
        if not _string(commit) or not COMMIT_RE.fullmatch(str(commit)):
            errors.append(f"{prefix}.commit must be a lowercase 40-character Git SHA")

        path_error = _validate_properties_path(target.get("properties_path"))
        if path_error:
            errors.append(f"{prefix}.properties_path {path_error}")

        if not _string(target.get("rationale")):
            errors.append(f"{prefix}.rationale must be a non-empty string")

        related_work_refs = target.get("related_work_refs")
        if not isinstance(related_work_refs, list):
            errors.append(f"{prefix}.related_work_refs must be an array")
        else:
            for ref_index, ref in enumerate(related_work_refs):
                ref_prefix = f"{prefix}.related_work_refs[{ref_index}]"
                if not isinstance(ref, dict):
                    errors.append(f"{ref_prefix} must be an object")
                    continue
                missing_ref = RELATED_WORK_FIELDS - set(ref)
                unknown_ref = set(ref) - RELATED_WORK_FIELDS
                if missing_ref:
                    errors.append(f"{ref_prefix} is missing fields: {', '.join(sorted(missing_ref))}")
                if unknown_ref:
                    errors.append(f"{ref_prefix} has unknown fields: {', '.join(sorted(unknown_ref))}")
                if not _https_url(ref.get("url")):
                    errors.append(f"{ref_prefix}.url must be an HTTPS URL without credentials or fragments")
                if not _string(ref.get("description")):
                    errors.append(f"{ref_prefix}.description must be a non-empty string")

        overlap_group = target.get("overlap_group")
        if not _string(overlap_group) or not SLUG_RE.fullmatch(str(overlap_group)):
            errors.append(f"{prefix}.overlap_group must be a lowercase kebab-case identifier")
        if not _string(target.get("overlap_notes")):
            errors.append(f"{prefix}.overlap_notes must be a non-empty string")

    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the scfuzzbench target catalog.")
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Manifest to validate (default: {DEFAULT_MANIFEST.relative_to(REPO_ROOT)})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"{args.manifest}: {error}", file=sys.stderr)
        return 1

    errors = validate_manifest(manifest)
    if errors:
        for error in errors:
            print(f"{args.manifest}: {error}", file=sys.stderr)
        return 1

    print(f"{args.manifest}: valid ({len(manifest['targets'])} targets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
