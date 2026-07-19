#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPO_ROOT / "benchmarks" / "known_bugs.json"
DEFAULT_TARGETS_MANIFEST = REPO_ROOT / "benchmarks" / "targets.json"

TARGET_FIELDS = {
    "target_id",
    "target_commit",
    "notes",
    "canaries",
    "known_bugs",
}
CANARY_FIELDS = {"id", "title", "evidence", "aliases"}
KNOWN_BUG_FIELDS = CANARY_FIELDS | {"kind"}
EVIDENCE_FIELDS = {"url", "description"}
ALIAS_FIELDS = {"event", "fuzzers"}

FUZZERS = {"echidna", "foundry", "medusa", "recon-fuzzer"}
KNOWN_BUG_KINDS = {"documented-known-issue", "historical-regression"}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EVENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ASSERTION_SUFFIX_RE = re.compile(r"_ASSERTION_[A-Za-z0-9_]+$")
FOUNDRY_ASSERTION_WRAPPER_PREFIX = "invariant_assertion_failure_"
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
        and (
            not parsed.fragment
            or re.fullmatch(r"L[0-9]+(?:-L[0-9]+)?", parsed.fragment) is not None
        )
    )


def _normalized_event(value: object) -> bool:
    if not _string(value):
        return False
    event = str(value)
    return (
        EVENT_RE.fullmatch(event) is not None
        and not event.startswith(FOUNDRY_ASSERTION_WRAPPER_PREFIX)
        and ASSERTION_SUFFIX_RE.search(event) is None
    )


def _validate_entry(
    entry: object,
    *,
    prefix: str,
    expected_fields: set[str],
    target_commit: object,
    seen_entry_ids: dict[str, str],
    seen_aliases: dict[tuple[str, str], str],
) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return [f"{prefix} must be an object"]

    missing = expected_fields - set(entry)
    unknown = set(entry) - expected_fields
    if missing:
        errors.append(f"{prefix} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        errors.append(f"{prefix} has unknown fields: {', '.join(sorted(unknown))}")

    entry_id = entry.get("id")
    if not _string(entry_id) or not SLUG_RE.fullmatch(str(entry_id)):
        errors.append(f"{prefix}.id must be a lowercase kebab-case identifier")
    elif str(entry_id) in seen_entry_ids:
        errors.append(f"{prefix}.id duplicates {seen_entry_ids[str(entry_id)]}.id")
    else:
        seen_entry_ids[str(entry_id)] = prefix

    if not _string(entry.get("title")):
        errors.append(f"{prefix}.title must be a non-empty string")

    if "kind" in expected_fields and entry.get("kind") not in KNOWN_BUG_KINDS:
        errors.append(
            f"{prefix}.kind must be one of: {', '.join(sorted(KNOWN_BUG_KINDS))}"
        )

    evidence = entry.get("evidence")
    has_pinned_evidence = False
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{prefix}.evidence must be a non-empty array")
    else:
        for evidence_index, item in enumerate(evidence):
            evidence_prefix = f"{prefix}.evidence[{evidence_index}]"
            if not isinstance(item, dict):
                errors.append(f"{evidence_prefix} must be an object")
                continue
            missing_evidence = EVIDENCE_FIELDS - set(item)
            unknown_evidence = set(item) - EVIDENCE_FIELDS
            if missing_evidence:
                errors.append(
                    f"{evidence_prefix} is missing fields: "
                    f"{', '.join(sorted(missing_evidence))}"
                )
            if unknown_evidence:
                errors.append(
                    f"{evidence_prefix} has unknown fields: "
                    f"{', '.join(sorted(unknown_evidence))}"
                )
            url = item.get("url")
            if not _https_url(url):
                errors.append(
                    f"{evidence_prefix}.url must be an HTTPS URL without "
                    "credentials (only GitHub-style line fragments are allowed)"
                )
            elif _string(target_commit) and str(target_commit) in str(url):
                has_pinned_evidence = True
            if not _string(item.get("description")):
                errors.append(f"{evidence_prefix}.description must be a non-empty string")
    if (
        isinstance(evidence, list)
        and evidence
        and _string(target_commit)
        and not has_pinned_evidence
    ):
        errors.append(
            f"{prefix}.evidence must include a URL pinned to target_commit "
            f"{target_commit}"
        )

    aliases = entry.get("aliases")
    if not isinstance(aliases, list) or not aliases:
        errors.append(f"{prefix}.aliases must be a non-empty array")
    else:
        for alias_index, alias in enumerate(aliases):
            alias_prefix = f"{prefix}.aliases[{alias_index}]"
            if not isinstance(alias, dict):
                errors.append(f"{alias_prefix} must be an object")
                continue
            missing_alias = ALIAS_FIELDS - set(alias)
            unknown_alias = set(alias) - ALIAS_FIELDS
            if missing_alias:
                errors.append(
                    f"{alias_prefix} is missing fields: {', '.join(sorted(missing_alias))}"
                )
            if unknown_alias:
                errors.append(
                    f"{alias_prefix} has unknown fields: {', '.join(sorted(unknown_alias))}"
                )

            event = alias.get("event")
            event_valid = _normalized_event(event)
            if not event_valid:
                errors.append(
                    f"{alias_prefix}.event must be a normalized Solidity function identifier"
                )

            fuzzers = alias.get("fuzzers")
            if not isinstance(fuzzers, list) or not fuzzers:
                errors.append(f"{alias_prefix}.fuzzers must be a non-empty array")
                continue
            seen_fuzzers: set[str] = set()
            for fuzzer_index, fuzzer in enumerate(fuzzers):
                fuzzer_prefix = f"{alias_prefix}.fuzzers[{fuzzer_index}]"
                if fuzzer not in FUZZERS:
                    errors.append(
                        f"{fuzzer_prefix} must be one of: {', '.join(sorted(FUZZERS))}"
                    )
                    continue
                if str(fuzzer) in seen_fuzzers:
                    errors.append(f"{fuzzer_prefix} duplicates an earlier fuzzer")
                    continue
                seen_fuzzers.add(str(fuzzer))
                if event_valid:
                    alias_key = (str(fuzzer), str(event))
                    if alias_key in seen_aliases:
                        errors.append(
                            f"{alias_prefix} duplicates the {fuzzer}/{event} alias "
                            f"from {seen_aliases[alias_key]}"
                        )
                    else:
                        seen_aliases[alias_key] = prefix

    return errors


def validate_catalog(catalog: object, targets_manifest: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(catalog, dict):
        return ["catalog must be a JSON object"]

    unknown_top_level = set(catalog) - {"schema_version", "targets"}
    if unknown_top_level:
        errors.append(
            f"catalog has unknown fields: {', '.join(sorted(unknown_top_level))}"
        )

    if type(catalog.get("schema_version")) is not int or catalog.get("schema_version") != 1:
        errors.append("schema_version must be the integer 1")

    target_refs: dict[str, str] = {}
    if not isinstance(targets_manifest, dict) or not isinstance(
        targets_manifest.get("targets"), list
    ):
        errors.append("targets manifest must contain a targets array")
    else:
        for target in targets_manifest["targets"]:
            if not isinstance(target, dict):
                continue
            target_id = target.get("id")
            commit = target.get("commit")
            if _string(target_id) and _string(commit):
                target_refs[str(target_id)] = str(commit)

    targets = catalog.get("targets")
    if not isinstance(targets, list) or not targets:
        errors.append("targets must be a non-empty array")
        return errors

    seen_targets: dict[str, int] = {}
    for target_index, target in enumerate(targets):
        prefix = f"targets[{target_index}]"
        if not isinstance(target, dict):
            errors.append(f"{prefix} must be an object")
            continue

        missing = TARGET_FIELDS - set(target)
        unknown = set(target) - TARGET_FIELDS
        if missing:
            errors.append(f"{prefix} is missing fields: {', '.join(sorted(missing))}")
        if unknown:
            errors.append(f"{prefix} has unknown fields: {', '.join(sorted(unknown))}")

        target_id = target.get("target_id")
        if not _string(target_id) or not SLUG_RE.fullmatch(str(target_id)):
            errors.append(f"{prefix}.target_id must be a lowercase kebab-case identifier")
        elif str(target_id) in seen_targets:
            errors.append(
                f"{prefix}.target_id duplicates "
                f"targets[{seen_targets[str(target_id)]}].target_id"
            )
        else:
            seen_targets[str(target_id)] = target_index

        target_commit = target.get("target_commit")
        if not _string(target_commit) or not COMMIT_RE.fullmatch(str(target_commit)):
            errors.append(
                f"{prefix}.target_commit must be a lowercase 40-character Git SHA"
            )
        if _string(target_id):
            expected_commit = target_refs.get(str(target_id))
            if expected_commit is None:
                errors.append(
                    f"{prefix}.target_id does not exist in the target manifest: {target_id}"
                )
            elif target_commit != expected_commit:
                errors.append(
                    f"{prefix}.target_commit must match the target manifest commit "
                    f"{expected_commit}"
                )

        if not _string(target.get("notes")):
            errors.append(f"{prefix}.notes must be a non-empty string")

        seen_entry_ids: dict[str, str] = {}
        seen_aliases: dict[tuple[str, str], str] = {}
        for collection_name, expected_fields in (
            ("canaries", CANARY_FIELDS),
            ("known_bugs", KNOWN_BUG_FIELDS),
        ):
            entries = target.get(collection_name)
            if not isinstance(entries, list):
                errors.append(f"{prefix}.{collection_name} must be an array")
                continue
            for entry_index, entry in enumerate(entries):
                errors.extend(
                    _validate_entry(
                        entry,
                        prefix=f"{prefix}.{collection_name}[{entry_index}]",
                        expected_fields=expected_fields,
                        target_commit=target_commit,
                        seen_entry_ids=seen_entry_ids,
                        seen_aliases=seen_aliases,
                    )
                )

    missing_targets = sorted(set(target_refs) - set(seen_targets))
    if missing_targets:
        errors.append(
            "catalog is missing target manifest IDs: " + ", ".join(missing_targets)
        )

    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the scfuzzbench ground-truth known-bug catalog."
    )
    parser.add_argument(
        "catalog",
        nargs="?",
        type=Path,
        default=DEFAULT_CATALOG,
        help=f"Catalog to validate (default: {DEFAULT_CATALOG.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--targets-manifest",
        type=Path,
        default=DEFAULT_TARGETS_MANIFEST,
        help=(
            "Authoritative target manifest "
            f"(default: {DEFAULT_TARGETS_MANIFEST.relative_to(REPO_ROOT)})"
        ),
    )
    return parser.parse_args(argv)


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        catalog = _load_json(args.catalog)
        targets_manifest = _load_json(args.targets_manifest)
    except (OSError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1

    errors = validate_catalog(catalog, targets_manifest)
    if errors:
        for error in errors:
            print(f"{args.catalog}: {error}", file=sys.stderr)
        return 1

    known_bug_count = sum(len(target["known_bugs"]) for target in catalog["targets"])
    canary_count = sum(len(target["canaries"]) for target in catalog["targets"])
    print(
        f"{args.catalog}: valid ({len(catalog['targets'])} targets, "
        f"{known_bug_count} known bugs, {canary_count} canaries)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
