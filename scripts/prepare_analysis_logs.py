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


def sanitize_copy_hint(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._=-" else "_" for ch in value)
    return cleaned.strip("_") or "copy"


def unique_copy_target(target: Path, hint: str) -> Path:
    if not target.exists():
        return target

    safe_hint = sanitize_copy_hint(hint)
    candidate = target.with_name(f"{target.stem}__{safe_hint}{target.suffix}")
    idx = 2
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}__{safe_hint}__{idx}{target.suffix}")
        idx += 1
    return candidate


def copy_showmap_tree(src: Path, dest: Path, hint: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.rglob("*")):
        rel = child.relative_to(src)
        target = dest / rel
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, unique_copy_target(target, hint))


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
    copied_showmap_dirs = 0
    for instance_dir in sorted(p for p in args.unzipped_dir.iterdir() if p.is_dir()):
        log_files = list(instance_dir.rglob("*.log"))
        metric_files = [path for path in instance_dir.rglob("*.csv") if is_runner_metrics_csv(path)]
        showmap_dirs = [path for path in instance_dir.rglob("showmap") if path.is_dir()]
        if not log_files and not metric_files and not showmap_dirs:
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
        dest_showmap = dest_instance / "showmap"
        if showmap_dirs and dest_showmap.exists():
            shutil.rmtree(dest_showmap)
        for showmap_dir in showmap_dirs:
            rel_parent = showmap_dir.relative_to(instance_dir).parent
            hint = "__".join(rel_parent.parts)
            copy_showmap_tree(showmap_dir, dest_showmap, hint)
            copied_showmap_dirs += 1
    print(
        f"Copied {copied_logs} log file(s), {copied_metrics} runner metrics file(s), "
        f"and {copied_showmap_dirs} showmap tree(s) to {args.out_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
