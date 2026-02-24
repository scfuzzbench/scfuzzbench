#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from analysis import analyze  # noqa: E402


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
    args = parser.parse_args()

    exclude = {item.strip().lower() for item in args.exclude_fuzzers.split(",") if item.strip()}
    events = analyze.parse_logs(args.logs_dir, args.run_id)
    throughput_samples = analyze.parse_throughput_logs(args.logs_dir, args.run_id)
    additional_metrics_samples = analyze.parse_additional_metrics_logs(
        args.logs_dir, args.run_id
    )
    if exclude:
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
        additional_metrics_samples = [
            sample
            for sample in additional_metrics_samples
            if sample.fuzzer.lower() not in exclude and sample.fuzzer_label.lower() not in exclude
        ]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    analyze.write_events_csv(events, args.out_dir / "events.csv")
    analyze.write_summary_csv(events, args.out_dir / "summary.csv")
    analyze.write_overlap_csv(events, args.out_dir / "overlap.csv")
    analyze.write_exclusive_csv(events, args.out_dir / "exclusive.csv")
    analyze.write_throughput_samples_csv(throughput_samples, args.out_dir / "throughput_samples.csv")
    analyze.write_throughput_summary_csv(throughput_samples, args.out_dir / "throughput_summary.csv")
    analyze.write_additional_metrics_samples_csv(
        additional_metrics_samples, args.out_dir / "additional_metrics_samples.csv"
    )
    analyze.write_additional_metrics_summary_csv(
        additional_metrics_samples, args.out_dir / "additional_metrics_summary.csv"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
