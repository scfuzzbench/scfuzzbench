#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.validate_known_bugs import validate_catalog  # noqa: E402


DEFAULT_CATALOG = REPO_ROOT / "benchmarks" / "known_bugs.json"
DEFAULT_TARGETS_MANIFEST = REPO_ROOT / "benchmarks" / "targets.json"

INSTANCE_PREFIX_RE = re.compile(r"^(i-[0-9a-f]+)-(.*)$")
QUALIFIED_EVENT_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_$]*\.)+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?)$"
)
TRAILING_PARAMS_RE = re.compile(r"\([^()]*\)$")
ASSERTION_SUFFIX_RE = re.compile(r"_ASSERTION_[A-Za-z0-9_]+$")
FOUNDRY_ASSERTION_WRAPPER_PREFIX = "invariant_assertion_failure_"

FINDINGS_FIELDS = (
    "target_id",
    "target_commit",
    "run_id",
    "instance_id",
    "fuzzer",
    "fuzzer_label",
    "finding_class",
    "ground_truth_id",
    "title",
    "kind",
    "event",
    "raw_event",
    "elapsed_seconds",
    "observations",
    "source",
    "log_path",
)
SUMMARY_FIELDS = (
    "target_id",
    "target_commit",
    "fuzzer",
    "runs",
    "known_bugs",
    "known_bug_run_hits",
    "known_bug_run_opportunities",
    "known_bug_hit_rate",
    "distinct_known_bugs_hit",
    "known_bug_catalog_coverage_rate",
    "canaries",
    "canary_run_hits",
    "canary_run_opportunities",
    "canary_hit_rate",
    "distinct_canaries_hit",
    "canary_catalog_coverage_rate",
    "unmapped_event_findings",
    "distinct_unmapped_events",
)
REQUIRED_EVENT_FIELDS = {
    "run_id",
    "instance_id",
    "fuzzer",
    "event",
    "elapsed_seconds",
}


@dataclass(frozen=True)
class EventRecord:
    run_id: str
    instance_id: str
    fuzzer: str
    fuzzer_label: str
    event: str
    elapsed_seconds: float
    source: str
    log_path: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    instance_id: str
    fuzzer: str
    fuzzer_label: str


def normalize_fuzzer(value: object) -> str:
    label = str(value or "").strip()
    lower = label.lower()
    if "recon" in lower:
        return "recon-fuzzer"
    if lower.startswith("echidna"):
        return "echidna"
    if "medusa" in lower:
        return "medusa"
    if "foundry" in lower:
        return "foundry"
    return label


def normalize_event(value: object) -> str:
    name = str(value or "").strip()
    if not name:
        return ""
    match = QUALIFIED_EVENT_RE.match(name)
    if match:
        name = match.group("name")
    name = TRAILING_PARAMS_RE.sub("", name)
    if name.startswith(FOUNDRY_ASSERTION_WRAPPER_PREFIX):
        name = name[len(FOUNDRY_ASSERTION_WRAPPER_PREFIX) :]
    name = ASSERTION_SUFFIX_RE.sub("", name)
    return name.strip()


def normalize_repo_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower()


def load_events_csv(path: Path) -> list[EventRecord]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = sorted(REQUIRED_EVENT_FIELDS - fields)
        if missing:
            raise ValueError(f"events CSV missing columns: {', '.join(missing)}")

        events: list[EventRecord] = []
        for row_number, row in enumerate(reader, 2):
            try:
                elapsed_seconds = float(row["elapsed_seconds"])
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"events CSV row {row_number} has invalid elapsed_seconds"
                ) from error
            events.append(
                EventRecord(
                    run_id=str(row.get("run_id") or "unknown").strip(),
                    instance_id=str(row.get("instance_id") or "unknown").strip(),
                    fuzzer=str(row.get("fuzzer") or "unknown").strip(),
                    fuzzer_label=str(
                        row.get("fuzzer_label") or row.get("fuzzer") or "unknown"
                    ).strip(),
                    event=str(row.get("event") or "").strip(),
                    elapsed_seconds=elapsed_seconds,
                    source=str(row.get("source") or "").strip(),
                    log_path=str(row.get("log_path") or "").strip(),
                )
            )
    return events


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def find_run_manifest(logs_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"run manifest not found: {explicit}")
        return explicit

    candidates = (
        logs_dir / "manifest.json",
        logs_dir / "benchmark_manifest.json",
        logs_dir.parent / "logs" / "zips" / "manifest.json",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def resolve_target(
    *,
    catalog: Mapping[str, object],
    targets_manifest: Mapping[str, object],
    run_manifest: Mapping[str, object] | None,
    explicit_target_id: str | None,
) -> tuple[Mapping[str, object] | None, str]:
    catalog_targets = {
        str(target["target_id"]): target
        for target in catalog["targets"]  # type: ignore[index]
        if isinstance(target, dict)
    }
    target_refs = {
        str(target["id"]): target
        for target in targets_manifest["targets"]  # type: ignore[index]
        if isinstance(target, dict)
    }

    if explicit_target_id:
        target = catalog_targets.get(explicit_target_id)
        if target is None:
            return None, f"target ID is not cataloged: {explicit_target_id}"
        if run_manifest is None:
            return None, (
                "run manifest was not found; --target-id cannot bypass "
                "repository and revision verification"
            )
        ref = target_refs.get(explicit_target_id, {})
        run_repo = normalize_repo_url(run_manifest.get("target_repo_url"))
        expected_repo = normalize_repo_url(ref.get("repo"))
        run_commit = str(run_manifest.get("target_commit") or "").strip()
        expected_commit = str(target.get("target_commit") or "")
        if not run_repo:
            return None, "run manifest has no target_repo_url"
        if run_repo != expected_repo:
            return None, (
                f"run manifest repository {run_repo} does not match "
                f"target {explicit_target_id}"
            )
        if not run_commit:
            return None, "run manifest has no target_commit"
        if run_commit != expected_commit:
            return None, (
                f"run commit {run_commit} is not the cataloged revision "
                f"{expected_commit}"
            )
        return target, "matched explicit target ID"

    if run_manifest is None:
        return None, "run manifest was not found and --target-id was not supplied"

    run_repo = normalize_repo_url(run_manifest.get("target_repo_url"))
    run_commit = str(run_manifest.get("target_commit") or "").strip()
    if not run_repo:
        return None, "run manifest has no target_repo_url"
    if not run_commit:
        return None, "run manifest has no target_commit"

    repo_matches = [
        target_id
        for target_id, ref in target_refs.items()
        if normalize_repo_url(ref.get("repo")) == run_repo
    ]
    if not repo_matches:
        return None, f"run repository is not in the active target manifest: {run_repo}"

    target_id = repo_matches[0]
    target = catalog_targets.get(target_id)
    if target is None:
        return None, f"target has no ground-truth catalog entry: {target_id}"
    expected_commit = str(target.get("target_commit") or "")
    if run_commit != expected_commit:
        return None, (
            f"run commit {run_commit} is not the evidence-pinned catalog revision "
            f"{expected_commit}"
        )
    return target, "matched run repository and commit"


def _event_run_ids(events: Sequence[EventRecord]) -> dict[tuple[str, str], set[str]]:
    result: dict[tuple[str, str], set[str]] = {}
    for event in events:
        fuzzer = normalize_fuzzer(event.fuzzer_label or event.fuzzer)
        result.setdefault((event.instance_id, fuzzer), set()).add(event.run_id)
    return result


def build_runs(
    events: Sequence[EventRecord],
    *,
    logs_dir: Path,
    default_run_id: str,
    run_manifest: Mapping[str, object] | None,
    excluded_fuzzers: set[str],
) -> list[RunRecord]:
    runs: dict[tuple[str, str, str], RunRecord] = {}
    event_run_ids = _event_run_ids(events)

    for event in events:
        fuzzer = normalize_fuzzer(event.fuzzer_label or event.fuzzer)
        if (
            fuzzer.lower() in excluded_fuzzers
            or event.fuzzer_label.lower() in excluded_fuzzers
        ):
            continue
        key = (event.run_id, event.instance_id, fuzzer)
        runs[key] = RunRecord(
            run_id=event.run_id,
            instance_id=event.instance_id,
            fuzzer=fuzzer,
            fuzzer_label=event.fuzzer_label,
        )

    if logs_dir.is_dir():
        for instance_dir in sorted(path for path in logs_dir.iterdir() if path.is_dir()):
            match = INSTANCE_PREFIX_RE.match(instance_dir.name)
            if not match:
                continue
            instance_id, fuzzer_label = match.groups()
            fuzzer = normalize_fuzzer(fuzzer_label)
            if (
                fuzzer.lower() in excluded_fuzzers
                or fuzzer_label.lower() in excluded_fuzzers
            ):
                continue
            matching_run_ids = event_run_ids.get((instance_id, fuzzer), set())
            run_id = (
                next(iter(matching_run_ids))
                if len(matching_run_ids) == 1
                else default_run_id
            )
            key = (run_id, instance_id, fuzzer)
            runs[key] = RunRecord(
                run_id=run_id,
                instance_id=instance_id,
                fuzzer=fuzzer,
                fuzzer_label=fuzzer_label,
            )

    if run_manifest is not None:
        try:
            instances_per_fuzzer = int(run_manifest.get("instances_per_fuzzer") or 0)
        except (TypeError, ValueError):
            instances_per_fuzzer = 0
        fuzzer_keys = run_manifest.get("fuzzer_keys")
        if instances_per_fuzzer > 0 and isinstance(fuzzer_keys, list):
            expected_by_fuzzer: dict[str, int] = {}
            for fuzzer_key in fuzzer_keys:
                fuzzer_label = str(fuzzer_key).strip()
                fuzzer = normalize_fuzzer(fuzzer_label)
                if (
                    not fuzzer
                    or fuzzer.lower() in excluded_fuzzers
                    or fuzzer_label.lower() in excluded_fuzzers
                ):
                    continue
                expected_by_fuzzer[fuzzer] = (
                    expected_by_fuzzer.get(fuzzer, 0) + instances_per_fuzzer
                )

            for fuzzer, expected in expected_by_fuzzer.items():
                actual = sum(1 for run in runs.values() if run.fuzzer == fuzzer)
                for missing_index in range(actual + 1, expected + 1):
                    instance_id = f"missing-{fuzzer}-{missing_index}"
                    key = (default_run_id, instance_id, fuzzer)
                    runs[key] = RunRecord(
                        run_id=default_run_id,
                        instance_id=instance_id,
                        fuzzer=fuzzer,
                        fuzzer_label=fuzzer,
                    )

    return sorted(
        runs.values(),
        key=lambda run: (run.fuzzer, run.run_id, run.instance_id),
    )


def build_alias_index(
    target: Mapping[str, object],
) -> dict[tuple[str, str], tuple[str, Mapping[str, object]]]:
    result: dict[tuple[str, str], tuple[str, Mapping[str, object]]] = {}
    for finding_class, collection_name in (
        ("canary", "canaries"),
        ("known_bug", "known_bugs"),
    ):
        for entry in target[collection_name]:  # type: ignore[index]
            for alias in entry["aliases"]:  # type: ignore[index]
                event = str(alias["event"])
                for fuzzer in alias["fuzzers"]:
                    result[(str(fuzzer), event)] = (finding_class, entry)
    return result


def catalog_ids_for_fuzzer(
    target: Mapping[str, object],
    *,
    collection_name: str,
    fuzzer: str,
) -> set[str]:
    target_id = str(target["target_id"])
    result: set[str] = set()
    for entry in target[collection_name]:  # type: ignore[index]
        if any(
            fuzzer in alias["fuzzers"]  # type: ignore[index]
            for alias in entry["aliases"]  # type: ignore[index]
        ):
            result.add(f"{target_id}/{entry['id']}")  # type: ignore[index]
    return result


def map_events(
    events: Sequence[EventRecord],
    *,
    target: Mapping[str, object],
    excluded_fuzzers: set[str],
) -> list[dict[str, object]]:
    target_id = str(target["target_id"])
    target_commit = str(target["target_commit"])
    aliases = build_alias_index(target)
    findings: dict[tuple[str, ...], dict[str, object]] = {}

    for event in events:
        fuzzer = normalize_fuzzer(event.fuzzer_label or event.fuzzer)
        if (
            fuzzer.lower() in excluded_fuzzers
            or event.fuzzer_label.lower() in excluded_fuzzers
        ):
            continue
        normalized_event = normalize_event(event.event)
        if not normalized_event:
            continue

        match = aliases.get((fuzzer, normalized_event))
        if match is None:
            finding_class = "unmapped"
            ground_truth_id = ""
            title = ""
            kind = "unmapped-event"
            finding_key = (
                event.run_id,
                event.instance_id,
                fuzzer,
                finding_class,
                normalized_event,
            )
        else:
            finding_class, entry = match
            ground_truth_id = f"{target_id}/{entry['id']}"
            title = str(entry["title"])
            kind = (
                "canary"
                if finding_class == "canary"
                else str(entry.get("kind") or "known-bug")
            )
            finding_key = (
                event.run_id,
                event.instance_id,
                fuzzer,
                finding_class,
                ground_truth_id,
            )

        row: dict[str, object] = {
            "target_id": target_id,
            "target_commit": target_commit,
            "run_id": event.run_id,
            "instance_id": event.instance_id,
            "fuzzer": fuzzer,
            "fuzzer_label": event.fuzzer_label,
            "finding_class": finding_class,
            "ground_truth_id": ground_truth_id,
            "title": title,
            "kind": kind,
            "event": normalized_event,
            "raw_event": event.event,
            "elapsed_seconds": event.elapsed_seconds,
            "observations": 1,
            "source": event.source,
            "log_path": event.log_path,
        }

        previous = findings.get(finding_key)
        if previous is None:
            findings[finding_key] = row
            continue
        observations = int(previous["observations"]) + 1
        if float(row["elapsed_seconds"]) < float(previous["elapsed_seconds"]):
            row["observations"] = observations
            findings[finding_key] = row
        else:
            previous["observations"] = observations

    return sorted(
        findings.values(),
        key=lambda row: (
            str(row["fuzzer"]),
            str(row["run_id"]),
            str(row["instance_id"]),
            str(row["finding_class"]),
            str(row["ground_truth_id"] or row["event"]),
        ),
    )


def _rate(numerator: int, denominator: int) -> str:
    return "" if denominator == 0 else f"{numerator / denominator:.6f}"


def summarize_findings(
    findings: Sequence[Mapping[str, object]],
    *,
    runs: Sequence[RunRecord],
    target: Mapping[str, object],
) -> list[dict[str, object]]:
    fuzzers = sorted({run.fuzzer for run in runs} | {str(row["fuzzer"]) for row in findings})
    summary: list[dict[str, object]] = []
    for fuzzer in fuzzers:
        fuzzer_runs = [run for run in runs if run.fuzzer == fuzzer]
        fuzzer_findings = [row for row in findings if row["fuzzer"] == fuzzer]
        known_rows = [
            row for row in fuzzer_findings if row["finding_class"] == "known_bug"
        ]
        canary_rows = [
            row for row in fuzzer_findings if row["finding_class"] == "canary"
        ]
        unmapped_rows = [
            row for row in fuzzer_findings if row["finding_class"] == "unmapped"
        ]

        known_ids = catalog_ids_for_fuzzer(
            target, collection_name="known_bugs", fuzzer=fuzzer
        )
        canary_ids = catalog_ids_for_fuzzer(
            target, collection_name="canaries", fuzzer=fuzzer
        )
        known_hits = {str(row["ground_truth_id"]) for row in known_rows}
        canary_hits = {str(row["ground_truth_id"]) for row in canary_rows}
        known_opportunities = len(known_ids) * len(fuzzer_runs)
        canary_opportunities = len(canary_ids) * len(fuzzer_runs)

        summary.append(
            {
                "target_id": target["target_id"],
                "target_commit": target["target_commit"],
                "fuzzer": fuzzer,
                "runs": len(fuzzer_runs),
                "known_bugs": len(known_ids),
                "known_bug_run_hits": len(known_rows),
                "known_bug_run_opportunities": known_opportunities,
                "known_bug_hit_rate": _rate(len(known_rows), known_opportunities),
                "distinct_known_bugs_hit": len(known_hits),
                "known_bug_catalog_coverage_rate": _rate(
                    len(known_hits), len(known_ids)
                ),
                "canaries": len(canary_ids),
                "canary_run_hits": len(canary_rows),
                "canary_run_opportunities": canary_opportunities,
                "canary_hit_rate": _rate(len(canary_rows), canary_opportunities),
                "distinct_canaries_hit": len(canary_hits),
                "canary_catalog_coverage_rate": _rate(
                    len(canary_hits), len(canary_ids)
                ),
                "unmapped_event_findings": len(unmapped_rows),
                "distinct_unmapped_events": len(
                    {str(row["event"]) for row in unmapped_rows}
                ),
            }
        )
    return summary


def write_csv(
    rows: Iterable[Mapping[str, object]],
    path: Path,
    *,
    fields: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            output = dict(row)
            if "elapsed_seconds" in output:
                output["elapsed_seconds"] = f"{float(output['elapsed_seconds']):.3f}"
            writer.writerow(output)


def _markdown_text(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _ratio_cell(numerator: object, denominator: object, rate: object) -> str:
    num = int(numerator)
    den = int(denominator)
    if den == 0 or not str(rate):
        return "n/a"
    return f"{num}/{den} ({float(rate):.1%})"


def write_markdown_report(
    path: Path,
    *,
    target: Mapping[str, object] | None,
    status: str,
    summary: Sequence[Mapping[str, object]],
    findings: Sequence[Mapping[str, object]],
    catalog_path: Path,
) -> None:
    lines = ["# Ground-truth known-bug mapping", ""]
    if target is None:
        lines.extend(
            [
                "Ground-truth mapping was not applied.",
                "",
                f"- Reason: {_markdown_text(status)}",
                f"- Catalog: `{catalog_path}`",
                "",
                "No event was classified as a known bug. Raw event analysis remains "
                "available, but it must not be interpreted as a confirmed bug count.",
            ]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return

    lines.extend(
        [
            f"- Target: `{target['target_id']}`",
            f"- Evidence-pinned commit: `{target['target_commit']}`",
            f"- Resolution: {_markdown_text(status)}",
            f"- Catalog: `{catalog_path}`",
            "",
            _markdown_text(target["notes"]),
            "",
            "Canaries are reported separately as harness-health checks and are never "
            "included in the known-bug hit-rate denominator.",
            "",
            "## Per-fuzzer results",
            "",
            "| Fuzzer | Runs | Known-bug run hits | Catalog coverage | Canary run hits | Unmapped event findings |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_text(row["fuzzer"]),
                    str(row["runs"]),
                    _ratio_cell(
                        row["known_bug_run_hits"],
                        row["known_bug_run_opportunities"],
                        row["known_bug_hit_rate"],
                    ),
                    _ratio_cell(
                        row["distinct_known_bugs_hit"],
                        row["known_bugs"],
                        row["known_bug_catalog_coverage_rate"],
                    ),
                    _ratio_cell(
                        row["canary_run_hits"],
                        row["canary_run_opportunities"],
                        row["canary_hit_rate"],
                    ),
                    str(row["unmapped_event_findings"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Known-bug catalog",
            "",
            "| Ground-truth ID | Kind | Title | Evidence |",
            "| --- | --- | --- | --- |",
        ]
    )
    known_bugs = target["known_bugs"]  # type: ignore[index]
    if known_bugs:
        for bug in known_bugs:
            evidence = bug["evidence"][0]
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{target['target_id']}/{bug['id']}`",
                        _markdown_text(bug["kind"]),
                        _markdown_text(bug["title"]),
                        f"[source]({evidence['url']})",
                    ]
                )
                + " |"
            )
    else:
        lines.append("| _None cataloged_ |  |  |  |")

    unmapped: dict[tuple[str, str], set[str]] = {}
    for row in findings:
        if row["finding_class"] != "unmapped":
            continue
        key = (str(row["fuzzer"]), str(row["event"]))
        unmapped.setdefault(key, set()).add(
            f"{row['run_id']}:{row['instance_id']}"
        )

    lines.extend(
        [
            "",
            "## Unmapped event identities",
            "",
        ]
    )
    if unmapped:
        lines.extend(
            [
                "| Fuzzer | Event identity | Runs observed |",
                "| --- | --- | ---: |",
            ]
        )
        for (fuzzer, event), run_keys in sorted(unmapped.items()):
            lines.append(
                f"| {_markdown_text(fuzzer)} | `{_markdown_text(event)}` | "
                f"{len(run_keys)} |"
            )
    else:
        lines.append("No unmapped event identities were observed.")

    lines.extend(
        [
            "",
            "## Counting semantics",
            "",
            "- A canonical known-bug ID counts at most once per run, even when several "
            "event aliases or counterexamples reach it.",
            "- Known-bug hit rate is `canonical bug/run hits ÷ (cataloged bugs × runs)`.",
            "- Unmapped rows are distinct normalized event identities per run. They are "
            "triage candidates, not claimed bugs.",
            "- Crash inputs and corpus files are not used as bug identities.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_unavailable_outputs(
    *,
    target_status: str,
    summary_out: Path,
    findings_out: Path,
    report_out: Path,
    catalog_path: Path,
) -> None:
    write_csv([], summary_out, fields=SUMMARY_FIELDS)
    write_csv([], findings_out, fields=FINDINGS_FIELDS)
    write_markdown_report(
        report_out,
        target=None,
        status=target_status,
        summary=[],
        findings=[],
        catalog_path=catalog_path,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Map normalized fuzzer events to evidence-backed known-bug IDs and "
            "report per-fuzzer hit rates."
        )
    )
    parser.add_argument("--events-csv", required=True, type=Path)
    parser.add_argument("--logs-dir", required=True, type=Path)
    parser.add_argument("--summary-out", required=True, type=Path)
    parser.add_argument("--findings-out", required=True, type=Path)
    parser.add_argument("--report-out", required=True, type=Path)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument(
        "--targets-manifest", type=Path, default=DEFAULT_TARGETS_MANIFEST
    )
    parser.add_argument("--run-manifest", type=Path, default=None)
    parser.add_argument("--target-id", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--exclude-fuzzers",
        default="",
        help="Comma-separated normalized fuzzer names or raw labels to exclude.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        catalog = load_json(args.catalog)
        targets_manifest = load_json(args.targets_manifest)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    errors = validate_catalog(catalog, targets_manifest)
    if errors:
        for error in errors:
            print(f"error: ground-truth catalog: {error}", file=sys.stderr)
        return 1

    try:
        manifest_path = find_run_manifest(args.logs_dir, args.run_manifest)
        run_manifest = (
            load_json(manifest_path) if manifest_path is not None else None
        )
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if run_manifest is not None and not isinstance(run_manifest, dict):
        print("error: run manifest must be a JSON object", file=sys.stderr)
        return 1

    target, status = resolve_target(
        catalog=catalog,  # type: ignore[arg-type]
        targets_manifest=targets_manifest,  # type: ignore[arg-type]
        run_manifest=run_manifest,
        explicit_target_id=args.target_id,
    )
    if target is None:
        write_unavailable_outputs(
            target_status=status,
            summary_out=args.summary_out,
            findings_out=args.findings_out,
            report_out=args.report_out,
            catalog_path=args.catalog,
        )
        print(f"Ground-truth mapping unavailable: {status}", file=sys.stderr)
        return 0

    try:
        events = load_events_csv(args.events_csv)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    excluded_fuzzers = {
        item.strip().lower()
        for item in args.exclude_fuzzers.split(",")
        if item.strip()
    }
    default_run_id = str(
        args.run_id
        or (run_manifest or {}).get("run_id")
        or next((event.run_id for event in events if event.run_id), "unknown")
    )
    runs = build_runs(
        events,
        logs_dir=args.logs_dir,
        default_run_id=default_run_id,
        run_manifest=run_manifest,
        excluded_fuzzers=excluded_fuzzers,
    )
    findings = map_events(
        events,
        target=target,
        excluded_fuzzers=excluded_fuzzers,
    )
    summary = summarize_findings(findings, runs=runs, target=target)

    write_csv(findings, args.findings_out, fields=FINDINGS_FIELDS)
    write_csv(summary, args.summary_out, fields=SUMMARY_FIELDS)
    write_markdown_report(
        args.report_out,
        target=target,
        status=status,
        summary=summary,
        findings=findings,
        catalog_path=args.catalog,
    )
    print(
        f"Mapped {len(findings)} deduplicated finding(s) across "
        f"{len(runs)} run(s) for {target['target_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
