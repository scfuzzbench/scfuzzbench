#!/usr/bin/env python3
import argparse
from pathlib import Path
import shutil
import sys


def is_runner_metrics_csv(path: Path) -> bool:
    name = path.name.lower()
    if not name.endswith(".csv"):
        return False
    return "runner_metrics" in name or "runner-metrics" in name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect .log files and runner metrics CSVs for analysis."
    )
    parser.add_argument("--unzipped-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    if not args.unzipped_dir.exists():
        print(f"Missing unzipped dir: {args.unzipped_dir}")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    copied_logs = 0
    copied_metrics = 0
    for instance_dir in sorted(p for p in args.unzipped_dir.iterdir() if p.is_dir()):
        log_files = list(instance_dir.rglob("*.log"))
        metric_files = [path for path in instance_dir.rglob("*.csv") if is_runner_metrics_csv(path)]
        if not log_files and not metric_files:
            continue
        dest_instance = args.out_dir / instance_dir.name
        dest_instance.mkdir(parents=True, exist_ok=True)
        for log_file in log_files:
            shutil.copy2(log_file, dest_instance / log_file.name)
            copied_logs += 1
        used_metric_names: set[str] = set()
        for metric_file in metric_files:
            # Keep the canonical name when unique; add a path prefix only on collision.
            out_name = metric_file.name
            if out_name in used_metric_names:
                rel = metric_file.relative_to(instance_dir)
                prefix = "__".join(rel.parts[:-1])
                if prefix:
                    out_name = f"{prefix}__{metric_file.name}"
            if out_name in used_metric_names:
                stem = Path(metric_file.name).stem
                suffix = Path(metric_file.name).suffix
                idx = 2
                while True:
                    candidate = f"{stem}__{idx}{suffix}"
                    if candidate not in used_metric_names:
                        out_name = candidate
                        break
                    idx += 1
            shutil.copy2(metric_file, dest_instance / out_name)
            used_metric_names.add(out_name)
            copied_metrics += 1
    print(
        f"Copied {copied_logs} log file(s) and {copied_metrics} runner metrics file(s) to {args.out_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
