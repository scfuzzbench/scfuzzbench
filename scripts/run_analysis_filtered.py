#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys
import time
from contextlib import contextmanager
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from analysis import analyze  # noqa: E402


@contextmanager
def timed_step(name: str, timings: Dict[str, float]):
    start = time.perf_counter()
    print(f"[analysis-timing] start {name}", file=sys.stderr)
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        timings[name] = elapsed
        print(f"[analysis-timing] done {name}: {elapsed:.3f}s", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run analysis with optional fuzzer filters.")
    parser.add_argument("--logs-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--exclude-fuzzers",
        default="",
        help="Comma-separated list of fuzzer names to exclude (normalized name or label).",
    )
    parser.add_argument(
        "--raw-labels",
        action="store_true",
        help="Use raw directory names as fuzzer labels instead of normalizing.",
    )
    parser.add_argument(
        "--pairing-mode",
        choices=["unpaired", "paired"],
        default="unpaired",
        help="Differential coverage test mode.",
    )
    parser.add_argument(
        "--confidence-level",
        type=float,
        default=0.95,
        help="Confidence level for differential coverage intervals and tests.",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=analyze.DEFAULT_MIN_VERDICT_SAMPLES,
        help="Minimum per-arm samples required before a statistical verdict can be conclusive.",
    )
    parser.add_argument(
        "--noninferiority-delta",
        type=float,
        default=analyze.DEFAULT_RELCOV_NONINFERIORITY_DELTA,
        help="Allowed absolute relcov loss versus baseline reliability before coverage is a regression.",
    )
    parser.add_argument(
        "--verdict-by-test",
        dest="verdict_by_test",
        action="store_true",
        default=None,
        help=(
            "Compute full bootstrap verdicts for per-test (by_test/...) campaigns. "
            "Off by default (drill-down only); these verdicts feed no consumer and "
            "dominate analysis time. Overrides SCFUZZBENCH_DIFFCOV_VERDICT_BY_TEST."
        ),
    )
    args = parser.parse_args()

    timings: Dict[str, float] = {}
    exclude = {item.strip().lower() for item in args.exclude_fuzzers.split(",") if item.strip()}
    with timed_step("discover_log_files", timings):
        log_files = analyze.discover_log_files(args.logs_dir)
    with timed_step("parse_events", timings):
        events = analyze.parse_logs(args.logs_dir, args.run_id, log_files)
    with timed_step("parse_throughput", timings):
        throughput_samples = analyze.parse_throughput_logs(
            args.logs_dir, args.run_id, log_files
        )
    with timed_step("parse_progress_metrics", timings):
        progress_metrics_samples = analyze.parse_progress_metrics_logs(
            args.logs_dir, args.run_id, log_files
        )
    if args.raw_labels:
        with timed_step("apply_raw_labels", timings):
            events = analyze._apply_raw_labels_events(events)
            throughput_samples = analyze._apply_raw_labels_throughput(throughput_samples)
            progress_metrics_samples = analyze._apply_raw_labels_progress(
                progress_metrics_samples
            )
    if exclude:
        with timed_step("filter_fuzzers", timings):
            events = [
                event
                for event in events
                if event.fuzzer.lower() not in exclude and event.fuzzer_label.lower() not in exclude
            ]
            throughput_samples = [
                sample
                for sample in throughput_samples
                if sample.fuzzer.lower() not in exclude and sample.fuzzer_label.lower() not in exclude
            ]
            progress_metrics_samples = [
                sample
                for sample in progress_metrics_samples
                if sample.fuzzer.lower() not in exclude and sample.fuzzer_label.lower() not in exclude
            ]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    with timed_step("write_event_outputs", timings):
        analyze.write_events_csv(events, args.out_dir / "events.csv")
        analyze.write_summary_csv(events, args.out_dir / "summary.csv")
        analyze.write_overlap_csv(events, args.out_dir / "overlap.csv")
        analyze.write_exclusive_csv(events, args.out_dir / "exclusive.csv")
    with timed_step("write_throughput_outputs", timings):
        analyze.write_throughput_samples_csv(throughput_samples, args.out_dir / "throughput_samples.csv")
        analyze.write_throughput_summary_csv(throughput_samples, args.out_dir / "throughput_summary.csv")
    with timed_step("write_progress_metrics_outputs", timings):
        analyze.write_progress_metrics_samples_csv(
            progress_metrics_samples, args.out_dir / "progress_metrics_samples.csv"
        )
        analyze.write_progress_metrics_summary_csv(
            progress_metrics_samples, args.out_dir / "progress_metrics_summary.csv"
        )
    with timed_step("write_differential_coverage_outputs", timings):
        analyze.write_differential_coverage_outputs(
            args.logs_dir,
            args.out_dir,
            exclude,
            pairing_mode=args.pairing_mode,
            confidence_level=args.confidence_level,
            min_samples=args.min_samples,
            noninferiority_delta=args.noninferiority_delta,
            verdict_by_test=args.verdict_by_test,
        )
    with (args.out_dir / "analysis_timing.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "log_files": len(log_files),
                "events": len(events),
                "throughput_samples": len(throughput_samples),
                "progress_metrics_samples": len(progress_metrics_samples),
                "timings_seconds": timings,
            },
            handle,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
