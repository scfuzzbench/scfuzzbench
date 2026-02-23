#!/usr/bin/env python3
import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REQUIRED_COLS = ["fuzzer", "run_id", "time_hours", "bugs_found"]


def die(msg: str) -> None:
    raise SystemExit(f"error: {msg}")


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        die(f"missing columns {missing}. Expected columns: {REQUIRED_COLS}")
    df["fuzzer"] = df["fuzzer"].astype(str)
    df["run_id"] = df["run_id"].astype(str)
    df["time_hours"] = pd.to_numeric(df["time_hours"], errors="coerce")
    df["bugs_found"] = pd.to_numeric(df["bugs_found"], errors="coerce").astype("Int64")
    if df["time_hours"].isna().any():
        die("time_hours has NaNs after parsing")
    if df["bugs_found"].isna().any():
        die("bugs_found has NaNs after parsing")
    return df


def validate_monotonic(df: pd.DataFrame) -> None:
    bad = []
    for (fuzzer, run_id), group in df.groupby(["fuzzer", "run_id"], sort=False):
        g = group.sort_values("time_hours")
        times = g["time_hours"].to_numpy(dtype=float)
        bugs = g["bugs_found"].to_numpy(dtype=float)
        if np.any(np.diff(times) < 0):
            bad.append((fuzzer, run_id, "time not non-decreasing"))
        if np.any(np.diff(bugs) < 0):
            bad.append((fuzzer, run_id, "bugs_found decreased"))
        if np.any(bugs < 0):
            bad.append((fuzzer, run_id, "bugs_found negative"))
        if not np.all(np.equal(np.mod(bugs, 1), 0)):
            bad.append((fuzzer, run_id, "bugs_found not integer"))
    if bad:
        lines = "\n".join([f"  - {fz}/{rid}: {reason}" for fz, rid, reason in bad[:20]])
        die(f"validation failed for some runs:\n{lines}\n(only first 20 shown)")


def resample_to_grid(df: pd.DataFrame, grid: np.ndarray) -> pd.DataFrame:
    out = []
    for (fuzzer, run_id), group in df.groupby(["fuzzer", "run_id"], sort=False):
        g = group.sort_values("time_hours")
        g = g.groupby("time_hours", as_index=False)["bugs_found"].max()
        series = pd.Series(g["bugs_found"].to_numpy(), index=g["time_hours"].to_numpy())
        reindexed = series.reindex(grid, method="ffill")
        reindexed = reindexed.fillna(0).astype(int)
        out.append(
            pd.DataFrame(
                {
                    "fuzzer": fuzzer,
                    "run_id": run_id,
                    "time_hours": grid,
                    "bugs_found": reindexed.to_numpy(),
                }
            )
        )
    return pd.concat(out, ignore_index=True)


def time_to_k(run_df: pd.DataFrame, k: int, budget: float) -> float:
    g = run_df.sort_values("time_hours")
    hit = g[g["bugs_found"] >= k]
    if hit.empty:
        return float("inf")
    t = float(hit.iloc[0]["time_hours"])
    return t if t <= budget else float("inf")


def auc_step(time: np.ndarray, y: np.ndarray) -> float:
    dt = np.diff(time)
    return float(np.sum(y[:-1] * dt))


def first_plateau_time(time: np.ndarray, y: np.ndarray) -> float:
    max_suffix = np.maximum.accumulate(y[::-1])[::-1]
    final = max_suffix[0]
    idx = np.where(y == final)[0]
    if len(idx) == 0:
        return float(time[-1])
    return float(time[idx[0]])


@dataclass
class FuzzerMetrics:
    fuzzer: str
    runs: int
    bugs_p50_t: Dict[float, int]
    bugs_p25_t: Dict[float, int]
    bugs_p75_t: Dict[float, int]
    auc_norm: float
    plateau_time: float
    late_share: float
    time_to_k_p50: Dict[int, float]
    success_rate_k: Dict[int, float]
    final_p50: int
    final_iqr: float


def compute_metrics(
    df_grid: pd.DataFrame, budget: float, checkpoints: List[float], ks: List[int]
) -> List[FuzzerMetrics]:
    metrics: List[FuzzerMetrics] = []
    max_bugs = int(df_grid["bugs_found"].max())
    if max_bugs <= 0:
        max_bugs = 1

    for fuzzer, group in df_grid.groupby("fuzzer", sort=False):
        runs = group["run_id"].nunique()
        pivot = (
            group.pivot_table(
                index="time_hours", columns="run_id", values="bugs_found", aggfunc="max"
            )
            .sort_index()
            .astype(float)
        )
        time = pivot.index.to_numpy(dtype=float)
        arr = pivot.to_numpy(dtype=float)

        p25 = np.percentile(arr, 25, axis=1)
        p50 = np.percentile(arr, 50, axis=1)
        p75 = np.percentile(arr, 75, axis=1)

        bugs_p50_t: Dict[float, int] = {}
        bugs_p25_t: Dict[float, int] = {}
        bugs_p75_t: Dict[float, int] = {}
        for t in checkpoints:
            idx = int(np.argmin(np.abs(time - t)))
            bugs_p50_t[t] = int(round(p50[idx]))
            bugs_p25_t[t] = int(round(p25[idx]))
            bugs_p75_t[t] = int(round(p75[idx]))

        auc = auc_step(time, p50)
        auc_norm = auc / (budget * max_bugs)

        plateau_time = first_plateau_time(time, p50)

        mid = budget / 2.0
        idx_mid = int(np.argmin(np.abs(time - mid)))
        final = p50[-1]
        early = p50[idx_mid]
        late_share = float((final - early) / final) if final > 0 else 0.0

        final_values = pivot.iloc[-1].to_numpy(dtype=float)
        final_p50 = int(round(np.median(final_values)))
        final_iqr = float(
            np.percentile(final_values, 75) - np.percentile(final_values, 25)
        )

        time_to_k_p50: Dict[int, float] = {}
        success_rate_k: Dict[int, float] = {}
        for k in ks:
            times = []
            successes = 0
            for run_id, run in group.groupby("run_id", sort=False):
                t_hit = time_to_k(run, k, budget)
                times.append(t_hit)
                if math.isfinite(t_hit):
                    successes += 1
            times_arr = np.array(times, dtype=float)
            finite = times_arr[np.isfinite(times_arr)]
            time_to_k_p50[k] = float(np.median(finite)) if finite.size else float("inf")
            success_rate_k[k] = successes / runs if runs else 0.0

        metrics.append(
            FuzzerMetrics(
                fuzzer=str(fuzzer),
                runs=int(runs),
                bugs_p50_t=bugs_p50_t,
                bugs_p25_t=bugs_p25_t,
                bugs_p75_t=bugs_p75_t,
                auc_norm=float(auc_norm),
                plateau_time=float(plateau_time),
                late_share=float(late_share),
                time_to_k_p50=time_to_k_p50,
                success_rate_k=success_rate_k,
                final_p50=final_p50,
                final_iqr=final_iqr,
            )
        )
    return metrics


def plot_bugs_over_time(
    df_grid: pd.DataFrame, outpath: Path, label_map: dict[str, str] | None
) -> None:
    plt.figure(figsize=(9, 5))
    for fuzzer, group in df_grid.groupby("fuzzer", sort=False):
        label = label_map.get(str(fuzzer), str(fuzzer)) if label_map else str(fuzzer)
        pivot = (
            group.pivot_table(
                index="time_hours", columns="run_id", values="bugs_found", aggfunc="max"
            )
            .sort_index()
            .astype(float)
        )
        time = pivot.index.to_numpy(dtype=float)
        arr = pivot.to_numpy(dtype=float)
        p25 = np.percentile(arr, 25, axis=1)
        p50 = np.percentile(arr, 50, axis=1)
        p75 = np.percentile(arr, 75, axis=1)

        plt.fill_between(time, p25, p75, step="post", alpha=0.15)
        plt.step(time, np.rint(p50), where="post", linewidth=2.5, label=f"{label} (median)")

    plt.title("Bugs found over time (median + IQR)")
    plt.xlabel("Elapsed time (hours)")
    plt.ylabel("Bugs found (cumulative count)")
    plt.yticks(range(0, int(df_grid["bugs_found"].max()) + 2))
    plt.legend()
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()


def plot_bugs_over_time_runs(
    df_grid: pd.DataFrame, outpath: Path, label_map: dict[str, str] | None
) -> None:
    plt.figure(figsize=(9, 5))
    ax = plt.gca()
    for fuzzer, group in df_grid.groupby("fuzzer", sort=False):
        fuzzer_label = label_map.get(str(fuzzer), str(fuzzer)) if label_map else str(fuzzer)
        pivot = (
            group.pivot_table(
                index="time_hours", columns="run_id", values="bugs_found", aggfunc="max"
            )
            .sort_index()
            .astype(float)
        )
        time = pivot.index.to_numpy(dtype=float)
        arr = pivot.to_numpy(dtype=float)
        p50 = np.percentile(arr, 50, axis=1)

        color = ax._get_lines.get_next_color()
        run_labels = [str(run_id).split(":", 1)[-1] for run_id in pivot.columns]
        for col, run_label in enumerate(run_labels):
            plt.step(
                time,
                np.rint(arr[:, col]),
                where="post",
                linewidth=1.0,
                alpha=0.35,
                color=color,
                linestyle=":",
                label=f"{fuzzer_label} {run_label}",
            )
        plt.step(
            time,
            np.rint(p50),
            where="post",
            linewidth=3.5,
            alpha=1.0,
            color=color,
            label=f"{fuzzer_label} (median)",
        )

    plt.title("Bugs found over time (all runs + median)")
    plt.xlabel("Elapsed time (hours)")
    plt.ylabel("Bugs found (cumulative count)")
    plt.yticks(range(0, int(df_grid["bugs_found"].max()) + 2))
    plt.legend()
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()


def plot_time_to_k(
    metrics: List[FuzzerMetrics],
    ks: List[int],
    outpath: Path,
    label_map: dict[str, str] | None,
) -> None:
    plt.figure(figsize=(9, 5))
    fuzzers = [label_map.get(m.fuzzer, m.fuzzer) if label_map else m.fuzzer for m in metrics]
    x = np.arange(len(fuzzers))
    width = 0.8 / max(1, len(ks))
    cmap = plt.get_cmap("Blues")
    sorted_ks = sorted(ks)
    k_rank = {k: idx for idx, k in enumerate(sorted_ks)}
    min_shade = 0.45
    max_shade = 0.9

    for j, k in enumerate(ks):
        vals = []
        for metric in metrics:
            t = metric.time_to_k_p50[k]
            vals.append(np.nan if not math.isfinite(t) else t)
        if len(ks) == 1:
            shade = max_shade
        else:
            shade = min_shade + (max_shade - min_shade) * (k_rank[k] / (len(ks) - 1))
        plt.bar(
            x + (j - (len(ks) - 1) / 2) * width,
            vals,
            width=width,
            label=f"k={k}",
            color=cmap(shade),
        )

    plt.xticks(x, fuzzers)
    plt.ylabel("Median time-to-k (hours)")
    plt.title("Median time-to-k (lower is better; NaN means never reached)")
    plt.legend()
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()


def plot_final_distribution(
    df_grid: pd.DataFrame, outpath: Path, label_map: dict[str, str] | None
) -> None:
    plt.figure(figsize=(9, 5))
    data = []
    labels = []
    for fuzzer, group in df_grid.groupby("fuzzer", sort=False):
        pivot = (
            group.pivot_table(
                index="time_hours", columns="run_id", values="bugs_found", aggfunc="max"
            )
            .sort_index()
            .astype(float)
        )
        data.append(pivot.iloc[-1].to_numpy(dtype=float))
        labels.append(label_map.get(str(fuzzer), str(fuzzer)) if label_map else str(fuzzer))

    plt.boxplot(data, labels=labels, showfliers=False)
    plt.ylim(bottom=0)
    plt.ylabel("Bugs found at end of budget")
    plt.title("End-of-budget bug count distribution (per run)")
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()


def plot_plateau_and_late_share(
    metrics: List[FuzzerMetrics], outpath: Path, label_map: dict[str, str] | None
) -> None:
    plt.figure(figsize=(9, 5))
    fuzzers = [label_map.get(m.fuzzer, m.fuzzer) if label_map else m.fuzzer for m in metrics]
    plateau = [m.plateau_time for m in metrics]
    late = [m.late_share for m in metrics]

    x = np.arange(len(fuzzers))
    width = 0.35

    plt.bar(x - width / 2, plateau, width=width, label="Plateau time (h)")
    plt.bar(x + width / 2, late, width=width, label="Late discovery share")

    plt.xticks(x, fuzzers)
    plt.title("Plateau time and late discovery share")
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()


def fmt_time(value: float) -> str:
    if not math.isfinite(value):
        return "inf"
    return f"{value:.2f}h"


def write_report(
    metrics: List[FuzzerMetrics],
    budget: float,
    checkpoints: List[float],
    ks: List[int],
    outpath: Path,
) -> None:
    lines: List[str] = []
    lines.append("# Fuzzer Benchmark Report (from bug-count CSV)")
    lines.append("")
    lines.append(f"- Time budget: **{budget:.2f}h**")
    lines.append("")
    lines.append("## Executive summary")
    lines.append(
        "This report is derived solely from cumulative bugs-found over time across repeated runs per fuzzer. "
        "It emphasizes robust, distribution-based metrics (median/IQR, success rates, time-to-k) and shape-based behavior "
        "(plateau time, late discovery share) instead of single-run time-to-first-bug."
    )
    lines.append("")

    lines.append("## Bugs found at fixed time budgets (median [IQR])")
    header = ["Fuzzer", "Runs"] + [f"{t:g}h" for t in checkpoints]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for metric in metrics:
        row = [metric.fuzzer, str(metric.runs)]
        for t in checkpoints:
            med = metric.bugs_p50_t[t]
            lo = metric.bugs_p25_t[t]
            hi = metric.bugs_p75_t[t]
            row.append(f"{med} [{lo},{hi}]")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## Overall metrics")
    header = [
        "Fuzzer",
        "AUC (norm)",
        "Plateau time",
        "Late discovery share",
        "Final median",
        "Final IQR",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for metric in metrics:
        lines.append(
            "| "
            + " | ".join(
                [
                    metric.fuzzer,
                    f"{metric.auc_norm:.3f}",
                    f"{metric.plateau_time:.2f}h",
                    f"{metric.late_share:.3f}",
                    str(metric.final_p50),
                    f"{metric.final_iqr:.2f}",
                ]
            )
            + " |"
        )
    lines.append("")

    lines.append("## Milestones: time-to-k and success rates")
    header = ["Fuzzer"] + [f"time-to-{k} (p50)" for k in ks] + [f"reach-{k} rate" for k in ks]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for metric in metrics:
        row = [metric.fuzzer]
        row += [fmt_time(metric.time_to_k_p50[k]) for k in ks]
        row += [f"{100 * metric.success_rate_k[k]:.1f}%" for k in ks]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## Shape-based interpretation (rules of thumb)")
    lines.append(
        "- **Fast-start / early-plateau**: high early checkpoint median + early plateau time + low late discovery share."
    )
    lines.append(
        "- **Steady**: moderate AUC, later plateau, consistent improvements across checkpoints, moderate variance."
    )
    lines.append(
        "- **Slow-burn / late-surge**: low early checkpoints but high late discovery share and later plateau time; often higher final median."
    )
    lines.append("")

    lines.append("## Limitations")
    lines.append(
        "- Core metrics in this section are count-based; use `broken_invariants.md` / `broken_invariants.csv` for invariant identities."
    )
    lines.append(
        "- Severity, exploitability, and root-cause uniqueness cannot be measured directly without richer per-bug metadata."
    )
    lines.append(
        "- Harness design still affects results; mitigate by keeping harness identical across fuzzers and reporting many runs."
    )
    lines.append("")

    outpath.write_text("\n".join(lines), encoding="utf-8")


def write_no_data_report(
    *,
    budget: float,
    checkpoints: List[float],
    ks: List[int],
    outpath: Path,
    csv_path: Path,
) -> None:
    lines: List[str] = []
    lines.append("# Fuzzer Benchmark Report (from bug-count CSV)")
    lines.append("")
    lines.append(f"- Time budget: **{budget:.2f}h**")
    lines.append(f"- Source CSV: `{csv_path}`")
    lines.append("")
    lines.append("## No data")
    lines.append("")
    lines.append(
        "The input CSV contained **no rows**, so there is nothing to plot or compare."
    )
    lines.append("")
    lines.append("Common causes:")
    lines.append("- No bugs were found in any run (so the event list is empty).")
    lines.append("- Log parsing failed to detect events for this benchmark.")
    lines.append("")
    lines.append("Report parameters:")
    lines.append(f"- Checkpoints: {', '.join([f'{t:g}h' for t in checkpoints])}")
    lines.append(f"- ks: {', '.join([str(k) for k in ks])}")
    lines.append("")
    outpath.write_text("\n".join(lines), encoding="utf-8")


def write_placeholder_plot(title: str, outpath: Path, message: str) -> None:
    plt.figure(figsize=(9, 5))
    plt.title(title)
    plt.axis("off")
    plt.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, required=True)
    # Backwards-compatible output selection:
    # - If --outdir is provided, it is used for both the report and images.
    # - Otherwise, callers can split outputs via --report-outdir and --images-outdir.
    parser.add_argument("--outdir", type=Path, default=None)
    parser.add_argument("--report-outdir", type=Path, default=None)
    parser.add_argument("--images-outdir", type=Path, default=None)
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--grid_step_min", type=float, default=6.0)
    parser.add_argument("--checkpoints", type=str, default="1,4,8,24")
    parser.add_argument("--ks", type=str, default="1,3,5")
    parser.add_argument("--anonymize", action="store_true", help="Use generic fuzzer labels in plots.")
    args = parser.parse_args()

    report_outdir = args.report_outdir or args.outdir
    images_outdir = args.images_outdir or args.outdir
    if report_outdir is None or images_outdir is None:
        print("error: provide --outdir or both --report-outdir and --images-outdir", file=sys.stderr)
        return 2

    df = load_csv(args.csv)
    validate_monotonic(df)

    if args.budget is None:
        if df.empty:
            budget = 0.0
        else:
            max_time = float(df["time_hours"].max())
            budget = float(round(max_time))
            if budget <= 0:
                budget = max_time
    else:
        budget = float(args.budget)
    raw_checkpoints = [float(x) for x in args.checkpoints.split(",") if x.strip()]
    checkpoints = []
    for t in raw_checkpoints:
        if t > budget + 1e-9:
            continue
        if t not in checkpoints:
            checkpoints.append(t)
    if not checkpoints:
        checkpoints = [budget]
    ks = [int(x) for x in args.ks.split(",") if x.strip()]

    step_h = float(args.grid_step_min) / 60.0
    grid = np.arange(0.0, budget + 1e-9, step_h)

    report_outdir.mkdir(parents=True, exist_ok=True)
    images_outdir.mkdir(parents=True, exist_ok=True)

    if df.empty:
        write_no_data_report(
            budget=budget,
            checkpoints=checkpoints,
            ks=ks,
            outpath=report_outdir / "REPORT.md",
            csv_path=args.csv,
        )
        msg = "No rows in input CSV. This usually means no bugs were found (or parsing produced no events)."
        write_placeholder_plot(
            "Bugs found over time (median + IQR)", images_outdir / "bugs_over_time.png", msg
        )
        write_placeholder_plot(
            "Bugs found over time (all runs + median)", images_outdir / "bugs_over_time_runs.png", msg
        )
        write_placeholder_plot("Median time-to-k", images_outdir / "time_to_k.png", msg)
        write_placeholder_plot(
            "End-of-budget bug count distribution (per run)",
            images_outdir / "final_distribution.png",
            msg,
        )
        write_placeholder_plot(
            "Plateau time and late discovery share",
            images_outdir / "plateau_and_late_share.png",
            msg,
        )
        print(f"wrote: {report_outdir / 'REPORT.md'} (no data)")
        return 0

    df_grid = resample_to_grid(df, grid)
    metrics = compute_metrics(df_grid, budget=budget, checkpoints=checkpoints, ks=ks)
    metrics = sorted(metrics, key=lambda m: (m.final_p50, m.auc_norm), reverse=True)

    label_map = None
    if args.anonymize:
        fuzzers = sorted({str(f) for f in df_grid["fuzzer"].unique()})
        label_map = {fz: f"Fuzzer {chr(65 + idx)}" for idx, fz in enumerate(fuzzers)}

    plot_bugs_over_time(df_grid, images_outdir / "bugs_over_time.png", label_map)
    plot_bugs_over_time_runs(df_grid, images_outdir / "bugs_over_time_runs.png", label_map)
    plot_time_to_k(metrics, ks=ks, outpath=images_outdir / "time_to_k.png", label_map=label_map)
    plot_final_distribution(df_grid, images_outdir / "final_distribution.png", label_map)
    plot_plateau_and_late_share(metrics, images_outdir / "plateau_and_late_share.png", label_map)
    write_report(metrics, budget=budget, checkpoints=checkpoints, ks=ks, outpath=report_outdir / "REPORT.md")

    print(f"wrote: {report_outdir / 'REPORT.md'}")
    print(
        "plots: bugs_over_time.png, bugs_over_time_runs.png, time_to_k.png, "
        "final_distribution.png, plateau_and_late_share.png"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
