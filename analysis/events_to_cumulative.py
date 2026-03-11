#!/usr/bin/env python3
import argparse
import csv
import re
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


REQUIRED_EVENT_COLS = {
    "run_id",
    "instance_id",
    "fuzzer",
    "elapsed_seconds",
}

INSTANCE_PREFIX_RE = re.compile(r"^(i-[0-9a-f]+)-(.*)$")


def die(msg: str) -> None:
    raise SystemExit(f"error: {msg}")


def load_events_csv(path: Path) -> List[dict]:
    with path.open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            die("events CSV has no header")
        missing = REQUIRED_EVENT_COLS - set(reader.fieldnames)
        if missing:
            die(f"events CSV missing columns: {sorted(missing)}")
        events = []
        for row in reader:
            events.append(row)
    return events


def infer_run_id(path: Path) -> Optional[str]:
    for part in path.parts:
        if part.isdigit() and len(part) >= 8:
            return part
    return None


def split_instance_label(label: str) -> Tuple[str, str]:
    match = INSTANCE_PREFIX_RE.match(label)
    if match:
        return match.group(1), match.group(2)
    return "unknown", label


def normalize_fuzzer(fuzzer_label: str) -> str:
    lower = fuzzer_label.lower()
    if "echidna" in lower and "symexec" in lower:
        return "echidna-symexec"
    if lower.startswith("echidna"):
        return "echidna"
    if "medusa" in lower:
        return "medusa"
    if "foundry" in lower:
        return "foundry"
    return fuzzer_label


def inventory_runs_from_logs(
    *,
    logs_dir: Path,
    run_id: Optional[str],
    exclude_fuzzers: Optional[set[str]] = None,
    raw_labels: bool = False,
) -> List[Tuple[str, str]]:
    run_id_value = run_id or infer_run_id(logs_dir) or "unknown"
    runs: List[Tuple[str, str]] = []
    for instance_dir in sorted([p for p in logs_dir.iterdir() if p.is_dir()]):
        instance_id, fuzzer_label = split_instance_label(instance_dir.name)
        fuzzer = fuzzer_label if raw_labels else normalize_fuzzer(fuzzer_label)
        if exclude_fuzzers:
            if str(fuzzer).lower() in exclude_fuzzers or fuzzer_label.lower() in exclude_fuzzers:
                continue
        runs.append((fuzzer, f"{run_id_value}:{instance_id}"))
    return runs


def build_cumulative_rows(
    events: Iterable[dict],
    include_zero: bool,
    *,
    logs_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
    exclude_fuzzers: Optional[set[str]] = None,
    raw_labels: bool = False,
) -> List[Tuple[str, str, float, int]]:
    grouped: dict[Tuple[str, str], List[float]] = {}
    if logs_dir is not None:
        for fuzzer, run_key in inventory_runs_from_logs(
            logs_dir=logs_dir, run_id=run_id, exclude_fuzzers=exclude_fuzzers,
            raw_labels=raw_labels,
        ):
            grouped.setdefault((fuzzer, run_key), [])

    for event in events:
        fuzzer = str(event["fuzzer"])
        fuzzer_label = str(event.get("fuzzer_label", ""))
        if exclude_fuzzers and (fuzzer.lower() in exclude_fuzzers or fuzzer_label.lower() in exclude_fuzzers):
            continue
        run_id_value = run_id or str(event["run_id"])
        run_key = f"{run_id_value}:{event['instance_id']}"
        try:
            elapsed = float(event["elapsed_seconds"])
        except (TypeError, ValueError):
            continue
        grouped.setdefault((fuzzer, run_key), []).append(elapsed)

    rows: List[Tuple[str, str, float, int]] = []
    for (fuzzer, run_key), times in sorted(grouped.items()):
        times_sorted = sorted(times)
        count = 0
        if include_zero:
            rows.append((fuzzer, run_key, 0.0, 0))
        for t in times_sorted:
            count += 1
            rows.append((fuzzer, run_key, t / 3600.0, count))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert events CSV to cumulative bug-count CSV.")
    parser.add_argument("--events-csv", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=None,
        help="Optional prepared logs dir. If provided, runs with 0 events still emit a time=0 row.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional run id override to ensure stable run_id:instance_id keys even when events.csv is empty.",
    )
    parser.add_argument(
        "--exclude-fuzzers",
        default="",
        help="Comma-separated list of fuzzer names to exclude (normalized name).",
    )
    parser.add_argument("--no-zero", action="store_true", help="Do not emit an initial time=0 row.")
    parser.add_argument(
        "--raw-labels",
        action="store_true",
        help="Use raw directory names as fuzzer labels instead of normalizing.",
    )
    args = parser.parse_args()

    events = load_events_csv(args.events_csv)
    exclude = {item.strip().lower() for item in args.exclude_fuzzers.split(",") if item.strip()}
    rows = build_cumulative_rows(
        events,
        include_zero=not args.no_zero,
        logs_dir=args.logs_dir,
        run_id=args.run_id,
        exclude_fuzzers=exclude,
        raw_labels=args.raw_labels,
    )

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.out_csv.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["fuzzer", "run_id", "time_hours", "bugs_found"])
        for fuzzer, run_id, time_hours, bugs_found in rows:
            writer.writerow([fuzzer, run_id, f"{time_hours:.6f}", bugs_found])

    print(f"wrote {args.out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
