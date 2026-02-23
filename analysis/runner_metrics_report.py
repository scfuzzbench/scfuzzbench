#!/usr/bin/env python3
import argparse
import math
from pathlib import Path
import sys
from typing import List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from analysis import analyze


REQUIRED_COLS = [
    "timestamp",
    "uptime_seconds",
    "cpu_user_pct",
    "cpu_system_pct",
    "cpu_iowait_pct",
    "mem_total_kb",
    "mem_used_kb",
]

TIMESERIES_COLS = [
    "run_id",
    "instance_id",
    "instance_label",
    "fuzzer",
    "fuzzer_label",
    "elapsed_seconds",
    "cpu_user_pct",
    "cpu_system_pct",
    "cpu_iowait_pct",
    "cpu_active_pct",
    "mem_total_kb",
    "mem_used_kb",
    "mem_used_pct",
    "mem_used_gib",
    "metrics_path",
]


def infer_elapsed_seconds(df: pd.DataFrame) -> pd.Series:
    ts = pd.to_datetime(df.get("timestamp"), utc=True, errors="coerce")
    if ts.notna().any():
        base = ts.dropna().iloc[0]
        elapsed = (ts - base).dt.total_seconds()
        if elapsed.notna().any():
            elapsed = elapsed.ffill().fillna(0.0)
            return elapsed.clip(lower=0.0)

    uptime = pd.to_numeric(df.get("uptime_seconds"), errors="coerce")
    if uptime.notna().any():
        base = float(uptime.dropna().iloc[0])
        elapsed = uptime - base
        elapsed = elapsed.ffill().fillna(0.0)
        return elapsed.clip(lower=0.0)

    return pd.Series(np.arange(len(df), dtype=float))


def load_metrics_for_instance(
    *,
    metrics_path: Path,
    logs_dir: Path,
    run_id: str,
    instance_label: str,
    budget_hours: Optional[float],
) -> pd.DataFrame:
    try:
        raw = pd.read_csv(metrics_path)
    except Exception:
        return pd.DataFrame(columns=TIMESERIES_COLS)

    if raw.empty:
        return pd.DataFrame(columns=TIMESERIES_COLS)

    missing = [col for col in REQUIRED_COLS if col not in raw.columns]
    if missing:
        return pd.DataFrame(columns=TIMESERIES_COLS)

    instance_id, fuzzer_label = analyze.split_instance_label(instance_label)
    fuzzer = analyze.normalize_fuzzer(fuzzer_label)

    work = raw.copy()
    work["run_id"] = run_id
    work["instance_id"] = instance_id
    work["instance_label"] = instance_label
    work["fuzzer_label"] = fuzzer_label
    work["fuzzer"] = fuzzer
    work["metrics_path"] = str(metrics_path.relative_to(logs_dir))
    work["elapsed_seconds"] = infer_elapsed_seconds(work)

    for col in [
        "cpu_user_pct",
        "cpu_system_pct",
        "cpu_iowait_pct",
        "mem_total_kb",
        "mem_used_kb",
        "elapsed_seconds",
    ]:
        work[col] = pd.to_numeric(work[col], errors="coerce")

    work["cpu_active_pct"] = (
        work["cpu_user_pct"].fillna(0.0)
        + work["cpu_system_pct"].fillna(0.0)
        + work["cpu_iowait_pct"].fillna(0.0)
    )
    work["cpu_active_pct"] = work["cpu_active_pct"].clip(lower=0.0, upper=100.0)
    work["mem_total_kb"] = work["mem_total_kb"].fillna(0.0)
    work["mem_used_kb"] = work["mem_used_kb"].fillna(0.0).clip(lower=0.0)
    work["mem_used_pct"] = np.where(
        work["mem_total_kb"] > 0.0,
        (work["mem_used_kb"] / work["mem_total_kb"]) * 100.0,
        0.0,
    )
    work["mem_used_pct"] = pd.to_numeric(work["mem_used_pct"], errors="coerce").fillna(0.0)
    work["mem_used_pct"] = work["mem_used_pct"].clip(lower=0.0, upper=100.0)
    work["mem_used_gib"] = work["mem_used_kb"] / (1024.0 * 1024.0)

    work = work.dropna(subset=["elapsed_seconds"]).sort_values("elapsed_seconds")
    if budget_hours is not None:
        budget_seconds = float(budget_hours) * 3600.0
        within_budget = work[work["elapsed_seconds"] <= budget_seconds + 1e-9]
        if not within_budget.empty:
            work = within_budget
        elif not work.empty:
            work = work.nsmallest(1, "elapsed_seconds")

    if work.empty:
        return pd.DataFrame(columns=TIMESERIES_COLS)

    return work[TIMESERIES_COLS].reset_index(drop=True)


def collect_metrics(logs_dir: Path, run_id: str, budget_hours: Optional[float]) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for instance_dir in sorted(path for path in logs_dir.iterdir() if path.is_dir()):
        metrics_files = sorted(
            path for path in instance_dir.rglob("*.csv") if path.name.startswith("runner_metrics")
        )
        for metrics_path in metrics_files:
            frame = load_metrics_for_instance(
                metrics_path=metrics_path,
                logs_dir=logs_dir,
                run_id=run_id,
                instance_label=instance_dir.name,
                budget_hours=budget_hours,
            )
            if not frame.empty:
                frames.append(frame)

    if not frames:
        return pd.DataFrame(columns=TIMESERIES_COLS)
    return pd.concat(frames, ignore_index=True)


def write_timeseries_csv(df: pd.DataFrame, out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if df.empty:
        pd.DataFrame(columns=TIMESERIES_COLS).to_csv(out_csv, index=False)
        return
    df.to_csv(out_csv, index=False)


def summarize_instances(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "run_id",
        "instance_id",
        "instance_label",
        "fuzzer",
        "fuzzer_label",
        "samples",
        "duration_hours",
        "cpu_active_avg_pct",
        "cpu_active_peak_pct",
        "mem_used_avg_gib",
        "mem_used_peak_gib",
        "mem_used_avg_pct",
        "mem_used_peak_pct",
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)

    grouped = df.groupby(["run_id", "instance_id", "instance_label", "fuzzer", "fuzzer_label"], sort=True)
    rows = []
    for (run_id, instance_id, instance_label, fuzzer, fuzzer_label), group in grouped:
        rows.append(
            {
                "run_id": run_id,
                "instance_id": instance_id,
                "instance_label": instance_label,
                "fuzzer": fuzzer,
                "fuzzer_label": fuzzer_label,
                "samples": int(len(group)),
                "duration_hours": float(group["elapsed_seconds"].max() / 3600.0),
                "cpu_active_avg_pct": float(group["cpu_active_pct"].mean()),
                "cpu_active_peak_pct": float(group["cpu_active_pct"].max()),
                "mem_used_avg_gib": float(group["mem_used_gib"].mean()),
                "mem_used_peak_gib": float(group["mem_used_gib"].max()),
                "mem_used_avg_pct": float(group["mem_used_pct"].mean()),
                "mem_used_peak_pct": float(group["mem_used_pct"].max()),
            }
        )

    out = pd.DataFrame(rows)
    return out.sort_values(["fuzzer", "instance_label"]).reset_index(drop=True)


def write_summary_csv(summary: pd.DataFrame, out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if summary.empty:
        summary.to_csv(out_csv, index=False)
        return
    summary.to_csv(out_csv, index=False)


def write_md_report(
    *,
    summary: pd.DataFrame,
    timeseries: pd.DataFrame,
    out_md: Path,
    budget_hours: Optional[float],
) -> None:
    lines: List[str] = []
    lines.append("# Runner resource usage")
    lines.append("")
    if budget_hours is None:
        lines.append("- Budget filter: **none**")
    else:
        lines.append(f"- Budget filter: **{budget_hours:.2f}h**")
    lines.append(f"- Instances with metrics: **{summary['instance_label'].nunique() if not summary.empty else 0}**")
    lines.append(f"- Total samples: **{len(timeseries)}**")
    lines.append("")

    if summary.empty:
        lines.append("No `runner_metrics*.csv` files were found in the prepared logs directory.")
        lines.append("")
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text("\n".join(lines), encoding="utf-8")
        return

    lines.append("## Per-fuzzer medians (across instances)")
    lines.append("")
    lines.append(
        "| Fuzzer | Instances | CPU active avg (%) | CPU active peak (%) | Memory used avg (GiB) | Memory used peak (GiB) | Memory used avg (%) | Memory used peak (%) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for fuzzer, group in summary.groupby("fuzzer", sort=True):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(fuzzer),
                    str(group["instance_label"].nunique()),
                    f"{group['cpu_active_avg_pct'].median():.2f}",
                    f"{group['cpu_active_peak_pct'].median():.2f}",
                    f"{group['mem_used_avg_gib'].median():.2f}",
                    f"{group['mem_used_peak_gib'].median():.2f}",
                    f"{group['mem_used_avg_pct'].median():.2f}",
                    f"{group['mem_used_peak_pct'].median():.2f}",
                ]
            )
            + " |"
        )
    lines.append("")

    lines.append("## Instance stats")
    lines.append("")
    lines.append(
        "| Instance | Fuzzer | Samples | Duration (h) | CPU active avg (%) | CPU active peak (%) | Memory avg (GiB) | Memory peak (GiB) | Memory avg (%) | Memory peak (%) |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for _, row in summary.iterrows():
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["instance_label"]),
                    str(row["fuzzer"]),
                    str(int(row["samples"])),
                    f"{float(row['duration_hours']):.2f}",
                    f"{float(row['cpu_active_avg_pct']):.2f}",
                    f"{float(row['cpu_active_peak_pct']):.2f}",
                    f"{float(row['mem_used_avg_gib']):.2f}",
                    f"{float(row['mem_used_peak_gib']):.2f}",
                    f"{float(row['mem_used_avg_pct']):.2f}",
                    f"{float(row['mem_used_peak_pct']):.2f}",
                ]
            )
            + " |"
        )
    lines.append("")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines), encoding="utf-8")


def write_placeholder_plot(title: str, out_png: Path, message: str) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(9, 5))
    plt.title(title)
    plt.axis("off")
    plt.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
    plt.tight_layout()
    plt.savefig(out_png, dpi=200)
    plt.close()


def _binned_distribution(df: pd.DataFrame, value_col: str, bin_seconds: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["fuzzer", "elapsed_hours", "p25", "p50", "p75"])
    work = df[["fuzzer", "instance_label", "elapsed_seconds", value_col]].copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    work = work.dropna(subset=["elapsed_seconds", value_col])
    if work.empty:
        return pd.DataFrame(columns=["fuzzer", "elapsed_hours", "p25", "p50", "p75"])

    work["bin"] = np.floor(work["elapsed_seconds"] / float(bin_seconds)).astype(int)
    per_instance = (
        work.groupby(["fuzzer", "instance_label", "bin"], as_index=False)[value_col].mean()
    )
    rows = []
    for (fuzzer, bin_idx), group in per_instance.groupby(["fuzzer", "bin"], sort=True):
        values = group[value_col].to_numpy(dtype=float)
        rows.append(
            {
                "fuzzer": fuzzer,
                "elapsed_hours": (float(bin_idx) * float(bin_seconds)) / 3600.0,
                "p25": float(np.percentile(values, 25)),
                "p50": float(np.percentile(values, 50)),
                "p75": float(np.percentile(values, 75)),
            }
        )
    return pd.DataFrame(rows).sort_values(["fuzzer", "elapsed_hours"]).reset_index(drop=True)


def plot_usage(
    *,
    timeseries: pd.DataFrame,
    value_col: str,
    out_png: Path,
    title: str,
    y_label: str,
    y_max: Optional[float] = None,
    bin_seconds: int = 60,
) -> None:
    if timeseries.empty:
        write_placeholder_plot(title, out_png, "No runner metrics found.")
        return

    dist = _binned_distribution(timeseries, value_col=value_col, bin_seconds=bin_seconds)
    if dist.empty:
        write_placeholder_plot(title, out_png, "No valid runner metrics found.")
        return

    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.figure(figsize=(9, 5))
    for fuzzer, group in dist.groupby("fuzzer", sort=True):
        x = group["elapsed_hours"].to_numpy(dtype=float)
        p25 = group["p25"].to_numpy(dtype=float)
        p50 = group["p50"].to_numpy(dtype=float)
        p75 = group["p75"].to_numpy(dtype=float)
        plt.fill_between(x, p25, p75, step="post", alpha=0.15)
        plt.step(x, p50, where="post", linewidth=2.5, label=f"{fuzzer} (median)")

    plt.title(title)
    plt.xlabel("Elapsed time (hours)")
    plt.ylabel(y_label)
    if y_max is not None and math.isfinite(y_max):
        plt.ylim(bottom=0.0, top=max(1.0, y_max))
    else:
        plt.ylim(bottom=0.0)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_png, dpi=200)
    plt.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build CPU/memory usage artifacts from runner_metrics.csv files."
    )
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--out-summary-csv", type=Path, required=True)
    parser.add_argument("--out-timeseries-csv", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--out-cpu-png", type=Path, required=True)
    parser.add_argument("--out-memory-png", type=Path, required=True)
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument("--budget-hours", type=float, default=None)
    parser.add_argument("--bin-seconds", type=int, default=60)
    args = parser.parse_args()

    if not args.logs_dir.exists():
        raise SystemExit(f"error: missing logs dir: {args.logs_dir}")
    if args.bin_seconds <= 0:
        raise SystemExit("error: --bin-seconds must be > 0")

    run_id = args.run_id or analyze.infer_run_id(args.logs_dir) or "unknown"
    timeseries = collect_metrics(args.logs_dir, run_id=run_id, budget_hours=args.budget_hours)
    summary = summarize_instances(timeseries)

    write_timeseries_csv(timeseries, args.out_timeseries_csv)
    write_summary_csv(summary, args.out_summary_csv)
    write_md_report(
        summary=summary,
        timeseries=timeseries,
        out_md=args.out_md,
        budget_hours=args.budget_hours,
    )
    plot_usage(
        timeseries=timeseries,
        value_col="cpu_active_pct",
        out_png=args.out_cpu_png,
        title="CPU usage over time (median + IQR)",
        y_label="CPU active (%)",
        y_max=100.0,
        bin_seconds=args.bin_seconds,
    )
    plot_usage(
        timeseries=timeseries,
        value_col="mem_used_pct",
        out_png=args.out_memory_png,
        title="Memory usage over time (median + IQR)",
        y_label="Memory used (%)",
        y_max=100.0,
        bin_seconds=args.bin_seconds,
    )

    print(f"wrote: {args.out_summary_csv}")
    print(f"wrote: {args.out_timeseries_csv}")
    print(f"wrote: {args.out_md}")
    print(f"plots: {args.out_cpu_png.name}, {args.out_memory_png.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
