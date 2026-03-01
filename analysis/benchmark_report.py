#!/usr/bin/env python3
import argparse
import csv
import itertools
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

from analysis.trial_run import format_trial_run_warning, is_trial_run

REQUIRED_COLS = ["fuzzer", "run_id", "time_hours", "bugs_found"]
REQUIRED_SAMPLE_BASE_COLS = ["fuzzer", "run_id", "instance_id", "elapsed_seconds"]
THROUGHPUT_SAMPLE_VALUE_COLS = ["tx_per_second", "gas_per_second"]
PROGRESS_SAMPLE_VALUE_COLS = [
    "seq_per_second",
    "coverage_proxy",
    "corpus_size",
]


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
    final_values: np.ndarray = None  # type: ignore[assignment]


@dataclass
class PairwiseResult:
    fuzzer_a: str
    fuzzer_b: str
    u_stat: float
    p_value: float
    p_corrected: float
    significant: bool
    direction: str
    median_a: float
    median_b: float


@dataclass
class ThroughputSummary:
    fuzzer: str
    runs: int
    txps_runs: int
    gasps_runs: int
    txps_p50: Optional[float]
    txps_p25: Optional[float]
    txps_p75: Optional[float]
    gasps_p50: Optional[float]
    gasps_p25: Optional[float]
    gasps_p75: Optional[float]


@dataclass
class ProgressMetricsSummary:
    fuzzer: str
    runs: int
    seqps_runs: int
    coverage_runs: int
    corpus_runs: int
    favored_runs: int
    failure_rate_runs: int
    seqps_p50: Optional[float]
    seqps_p25: Optional[float]
    seqps_p75: Optional[float]
    coverage_p50: Optional[float]
    coverage_p25: Optional[float]
    coverage_p75: Optional[float]
    corpus_p50: Optional[float]
    corpus_p25: Optional[float]
    corpus_p75: Optional[float]
    favored_p50: Optional[float]
    favored_p25: Optional[float]
    favored_p75: Optional[float]
    failure_rate_p50: Optional[float]
    failure_rate_p25: Optional[float]
    failure_rate_p75: Optional[float]


def parse_optional_float(value: str | None) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def load_throughput_summary(path: Path) -> Dict[str, ThroughputSummary]:
    if not path.exists():
        return {}
    rows: Dict[str, ThroughputSummary] = {}
    with path.open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            fuzzer = str(row.get("fuzzer", "")).strip()
            if not fuzzer:
                continue
            rows[fuzzer] = ThroughputSummary(
                fuzzer=fuzzer,
                runs=parse_int(row.get("runs"), 0),
                txps_runs=parse_int(row.get("txps_runs"), 0),
                gasps_runs=parse_int(row.get("gasps_runs"), 0),
                txps_p50=parse_optional_float(row.get("txps_p50")),
                txps_p25=parse_optional_float(row.get("txps_p25")),
                txps_p75=parse_optional_float(row.get("txps_p75")),
                gasps_p50=parse_optional_float(row.get("gasps_p50")),
                gasps_p25=parse_optional_float(row.get("gasps_p25")),
                gasps_p75=parse_optional_float(row.get("gasps_p75")),
            )
    return rows


def load_progress_metrics_summary(path: Path) -> Dict[str, ProgressMetricsSummary]:
    if not path.exists():
        return {}
    rows: Dict[str, ProgressMetricsSummary] = {}
    with path.open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            fuzzer = str(row.get("fuzzer", "")).strip()
            if not fuzzer:
                continue
            rows[fuzzer] = ProgressMetricsSummary(
                fuzzer=fuzzer,
                runs=parse_int(row.get("runs"), 0),
                seqps_runs=parse_int(row.get("seqps_runs"), 0),
                coverage_runs=parse_int(row.get("coverage_runs"), 0),
                corpus_runs=parse_int(row.get("corpus_runs"), 0),
                favored_runs=parse_int(row.get("favored_runs"), 0),
                failure_rate_runs=parse_int(row.get("failure_rate_runs"), 0),
                seqps_p50=parse_optional_float(row.get("seqps_p50")),
                seqps_p25=parse_optional_float(row.get("seqps_p25")),
                seqps_p75=parse_optional_float(row.get("seqps_p75")),
                coverage_p50=parse_optional_float(row.get("coverage_p50")),
                coverage_p25=parse_optional_float(row.get("coverage_p25")),
                coverage_p75=parse_optional_float(row.get("coverage_p75")),
                corpus_p50=parse_optional_float(row.get("corpus_p50")),
                corpus_p25=parse_optional_float(row.get("corpus_p25")),
                corpus_p75=parse_optional_float(row.get("corpus_p75")),
                favored_p50=parse_optional_float(row.get("favored_p50")),
                favored_p25=parse_optional_float(row.get("favored_p25")),
                favored_p75=parse_optional_float(row.get("favored_p75")),
                failure_rate_p50=parse_optional_float(row.get("failure_rate_p50")),
                failure_rate_p25=parse_optional_float(row.get("failure_rate_p25")),
                failure_rate_p75=parse_optional_float(row.get("failure_rate_p75")),
            )
    return rows


def load_metric_samples_csv(path: Path, value_columns: List[str]) -> pd.DataFrame:
    if not path.exists():
        cols = ["fuzzer", "series_id", "time_hours", *value_columns]
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(path)
    missing = [c for c in [*REQUIRED_SAMPLE_BASE_COLS, *value_columns] if c not in df.columns]
    if missing:
        die(f"missing columns {missing} in sample CSV {path}")

    df["fuzzer"] = df["fuzzer"].astype(str)
    df["run_id"] = df["run_id"].astype(str)
    df["instance_id"] = df["instance_id"].astype(str)
    df["series_id"] = df["run_id"] + ":" + df["instance_id"]

    elapsed_seconds = pd.to_numeric(df["elapsed_seconds"], errors="coerce")
    df["time_hours"] = elapsed_seconds / 3600.0

    for col in value_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["time_hours"])
    return df[["fuzzer", "series_id", "time_hours", *value_columns]]


def resample_metric_samples_to_grid(
    samples_df: pd.DataFrame, value_column: str, grid: np.ndarray
) -> pd.DataFrame:
    if samples_df.empty:
        return pd.DataFrame(columns=["fuzzer", "series_id", "time_hours", value_column])

    out: List[pd.DataFrame] = []
    for (fuzzer, series_id), group in samples_df.groupby(["fuzzer", "series_id"], sort=False):
        g = group[["time_hours", value_column]].dropna()
        if g.empty:
            continue
        g = g.sort_values("time_hours")
        g = g.groupby("time_hours", as_index=False)[value_column].mean()
        series = pd.Series(g[value_column].to_numpy(dtype=float), index=g["time_hours"].to_numpy(dtype=float))
        reindexed = series.reindex(grid, method="ffill")
        out.append(
            pd.DataFrame(
                {
                    "fuzzer": fuzzer,
                    "series_id": series_id,
                    "time_hours": grid,
                    value_column: reindexed.to_numpy(dtype=float),
                }
            )
        )
    if not out:
        return pd.DataFrame(columns=["fuzzer", "series_id", "time_hours", value_column])
    return pd.concat(out, ignore_index=True)


def fmt_triplet(p50: Optional[float], p25: Optional[float], p75: Optional[float]) -> str:
    if p50 is None or p25 is None or p75 is None:
        return "n/a"
    return f"{p50:.2f} [{p25:.2f},{p75:.2f}]"


def fmt_pct_triplet(p50: Optional[float], p25: Optional[float], p75: Optional[float]) -> str:
    if p50 is None or p25 is None or p75 is None:
        return "n/a"
    return f"{100.0 * p50:.1f}% [{100.0 * p25:.1f}%,{100.0 * p75:.1f}%]"


def append_throughput_section(
    lines: List[str], throughput_by_fuzzer: Dict[str, ThroughputSummary], fuzzer_order: List[str]
) -> None:
    if not throughput_by_fuzzer:
        return

    lines.append("## Throughput metrics (if supported by log format)")
    lines.append(
        "Values are run-level rates aggregated per fuzzer; `n/a` indicates the parser could not recover that metric from logs."
    )
    header = [
        "Fuzzer",
        "Runs",
        "Tx/s runs",
        "Tx/s p50 [p25,p75]",
        "Gas/s runs",
        "Gas/s p50 [p25,p75]",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    ordered_fuzzers: List[str] = []
    seen = set()
    for fuzzer in fuzzer_order:
        if fuzzer in throughput_by_fuzzer and fuzzer not in seen:
            ordered_fuzzers.append(fuzzer)
            seen.add(fuzzer)
    for fuzzer in sorted(throughput_by_fuzzer):
        if fuzzer in seen:
            continue
        ordered_fuzzers.append(fuzzer)

    for fuzzer in ordered_fuzzers:
        row = throughput_by_fuzzer[fuzzer]
        lines.append(
            "| "
            + " | ".join(
                [
                    row.fuzzer,
                    str(row.runs),
                    str(row.txps_runs),
                    fmt_triplet(row.txps_p50, row.txps_p25, row.txps_p75),
                    str(row.gasps_runs),
                    fmt_triplet(row.gasps_p50, row.gasps_p25, row.gasps_p75),
                ]
            )
            + " |"
        )
    lines.append("")


def append_progress_metrics_section(
    lines: List[str],
    progress_metrics_by_fuzzer: Dict[str, ProgressMetricsSummary],
    fuzzer_order: List[str],
) -> None:
    if not progress_metrics_by_fuzzer:
        return

    lines.append("## Progress metrics from logs (fuzzer-specific proxies)")
    lines.append(
        "Coverage/corpus values are parsed from each fuzzer's native progress output and are useful for trend context, not strict cross-fuzzer equivalence."
    )
    header = [
        "Fuzzer",
        "Runs",
        "Seq/s runs",
        "Seq/s p50 [p25,p75]",
        "Coverage runs",
        "Coverage p50 [p25,p75]",
        "Corpus runs",
        "Corpus p50 [p25,p75]",
    ]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    ordered_fuzzers: List[str] = []
    seen = set()
    for fuzzer in fuzzer_order:
        if fuzzer in progress_metrics_by_fuzzer and fuzzer not in seen:
            ordered_fuzzers.append(fuzzer)
            seen.add(fuzzer)
    for fuzzer in sorted(progress_metrics_by_fuzzer):
        if fuzzer in seen:
            continue
        ordered_fuzzers.append(fuzzer)

    for fuzzer in ordered_fuzzers:
        row = progress_metrics_by_fuzzer[fuzzer]
        lines.append(
            "| "
            + " | ".join(
                [
                    row.fuzzer,
                    str(row.runs),
                    str(row.seqps_runs),
                    fmt_triplet(row.seqps_p50, row.seqps_p25, row.seqps_p75),
                    str(row.coverage_runs),
                    fmt_triplet(row.coverage_p50, row.coverage_p25, row.coverage_p75),
                    str(row.corpus_runs),
                    fmt_triplet(row.corpus_p50, row.corpus_p25, row.corpus_p75),
                ]
            )
            + " |"
        )
    lines.append("")


def nan_percentile_rows(arr: np.ndarray, percentile_value: float) -> np.ndarray:
    out = np.full(arr.shape[0], np.nan, dtype=float)
    for idx, row in enumerate(arr):
        finite = row[np.isfinite(row)]
        if finite.size == 0:
            continue
        out[idx] = float(np.percentile(finite, percentile_value))
    return out


def plot_metric_over_time(
    *,
    metric_df: pd.DataFrame,
    value_column: str,
    outpath: Path,
    title: str,
    ylabel: str,
    label_map: dict[str, str] | None,
    scale: float = 1.0,
) -> bool:
    if metric_df.empty:
        return False

    plt.figure(figsize=(9, 5))
    plotted = False

    for fuzzer, group in metric_df.groupby("fuzzer", sort=False):
        label = label_map.get(str(fuzzer), str(fuzzer)) if label_map else str(fuzzer)
        pivot = (
            group.pivot_table(
                index="time_hours", columns="series_id", values=value_column, aggfunc="mean"
            )
            .sort_index()
            .astype(float)
        )
        if pivot.empty:
            continue
        time = pivot.index.to_numpy(dtype=float)
        arr = pivot.to_numpy(dtype=float)
        p25 = nan_percentile_rows(arr, 25) * scale
        p50 = nan_percentile_rows(arr, 50) * scale
        p75 = nan_percentile_rows(arr, 75) * scale
        if not np.isfinite(p50).any():
            continue

        plt.fill_between(time, p25, p75, step="post", alpha=0.15)
        plt.step(time, p50, where="post", linewidth=2.5, label=f"{label} (median)")
        plotted = True

    if not plotted:
        plt.close()
        return False

    plt.title(title)
    plt.xlabel("Elapsed time (hours)")
    plt.ylabel(ylabel)
    plt.legend()
    plt.tight_layout()
    plt.savefig(outpath, dpi=200)
    plt.close()
    return True


def plot_sample_metric_charts(
    *,
    throughput_samples_df: pd.DataFrame,
    progress_metrics_samples_df: pd.DataFrame,
    grid: np.ndarray,
    images_outdir: Path,
    label_map: dict[str, str] | None,
) -> List[str]:
    generated: List[str] = []

    throughput_specs = [
        (
            "tx_per_second",
            "tx_per_second_over_time.png",
            "Throughput over time (tx/s)",
            "Tx/s",
            1.0,
        ),
        (
            "gas_per_second",
            "gas_per_second_over_time.png",
            "Throughput over time (gas/s)",
            "Gas/s",
            1.0,
        ),
    ]
    for value_column, filename, title, ylabel, scale in throughput_specs:
        metric_df = resample_metric_samples_to_grid(throughput_samples_df, value_column, grid)
        if plot_metric_over_time(
            metric_df=metric_df,
            value_column=value_column,
            outpath=images_outdir / filename,
            title=title,
            ylabel=ylabel,
            label_map=label_map,
            scale=scale,
        ):
            generated.append(filename)

    progress_specs = [
        (
            "seq_per_second",
            "seq_per_second_over_time.png",
            "Sequence rate over time (seq/s)",
            "Seq/s",
            1.0,
        ),
        (
            "coverage_proxy",
            "coverage_proxy_over_time.png",
            "Coverage proxy over time",
            "Coverage proxy",
            1.0,
        ),
        (
            "corpus_size",
            "corpus_size_over_time.png",
            "Corpus size over time",
            "Corpus size",
            1.0,
        ),
    ]
    for value_column, filename, title, ylabel, scale in progress_specs:
        metric_df = resample_metric_samples_to_grid(
            progress_metrics_samples_df, value_column, grid
        )
        if plot_metric_over_time(
            metric_df=metric_df,
            value_column=value_column,
            outpath=images_outdir / filename,
            title=title,
            ylabel=ylabel,
            label_map=label_map,
            scale=scale,
        ):
            generated.append(filename)

    return generated


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
                final_values=final_values,
            )
        )
    return metrics


def plot_bugs_over_time(
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
        p25 = np.percentile(arr, 25, axis=1)
        p50 = np.percentile(arr, 50, axis=1)
        p75 = np.percentile(arr, 75, axis=1)

        color = ax._get_lines.get_next_color()

        # Individual runs (faint dotted lines)
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

        # IQR shading
        plt.fill_between(time, p25, p75, step="post", alpha=0.15, color=color)

        # Median line
        plt.step(
            time,
            np.rint(p50),
            where="post",
            linewidth=3.5,
            alpha=1.0,
            color=color,
            label=f"{fuzzer_label} median",
        )

    plt.title("Bugs found over time")
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


def compute_statistical_tests(
    metrics: List[FuzzerMetrics], alpha: float = 0.05
) -> Tuple[List[PairwiseResult], List[str]]:
    results: List[PairwiseResult] = []
    warn: List[str] = []

    if len(metrics) < 2:
        warn.append("Only one fuzzer present; skipping pairwise statistical tests.")
        return results, warn

    small_sample = False
    for m in metrics:
        if m.final_values is not None and len(m.final_values) < 5:
            small_sample = True

    if small_sample:
        warn.append(
            "One or more fuzzers have fewer than 5 runs. "
            "Statistical power may be limited."
        )

    pairs = list(itertools.combinations(metrics, 2))
    n_comparisons = len(pairs)

    for m_a, m_b in pairs:
        a = m_a.final_values
        b = m_b.final_values

        if a is None or b is None or len(a) < 2 or len(b) < 2:
            warn.append(
                f"Skipping {m_a.fuzzer} vs {m_b.fuzzer}: fewer than 2 runs."
            )
            continue

        median_a = float(np.median(a))
        median_b = float(np.median(b))

        # Identical constant arrays: scipy raises ValueError
        if np.array_equal(a, b) or (np.all(a == a[0]) and np.all(b == b[0]) and a[0] == b[0]):
            results.append(
                PairwiseResult(
                    fuzzer_a=m_a.fuzzer,
                    fuzzer_b=m_b.fuzzer,
                    u_stat=float(len(a) * len(b)) / 2.0,
                    p_value=1.0,
                    p_corrected=1.0,
                    significant=False,
                    direction="=",
                    median_a=median_a,
                    median_b=median_b,
                )
            )
            continue

        u_stat, p_value = stats.mannwhitneyu(a, b, alternative="two-sided")
        p_corrected = min(p_value * n_comparisons, 1.0)
        significant = p_corrected < alpha

        if median_a > median_b:
            direction = ">"
        elif median_a < median_b:
            direction = "<"
        else:
            direction = "="

        results.append(
            PairwiseResult(
                fuzzer_a=m_a.fuzzer,
                fuzzer_b=m_b.fuzzer,
                u_stat=float(u_stat),
                p_value=float(p_value),
                p_corrected=float(p_corrected),
                significant=significant,
                direction=direction,
                median_a=median_a,
                median_b=median_b,
            )
        )

    return results, warn


def format_statistical_report(
    results: List[PairwiseResult],
    warnings_list: List[str],
    alpha: float = 0.05,
) -> List[str]:
    lines: List[str] = []
    lines.append("## Statistical comparison (Mann-Whitney U test)")
    lines.append("")

    n_comparisons = len(results)
    lines.append(
        "Pairwise Mann-Whitney U tests on end-of-budget bug counts (two-sided)."
    )
    lines.append(
        f"Bonferroni correction applied for {n_comparisons} comparison(s). "
        f"Significance level: alpha = {alpha}."
    )
    lines.append("")

    if warnings_list:
        for w in warnings_list:
            lines.append(f"**Warning:** {w}")
        lines.append("")

    if results:
        header = [
            "Fuzzer A",
            "Fuzzer B",
            "Median A",
            "Median B",
            "U statistic",
            "p-value",
            "p (corrected)",
            "Significant",
        ]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for r in results:
            med_a = (
                str(int(r.median_a))
                if r.median_a == int(r.median_a)
                else f"{r.median_a:.1f}"
            )
            med_b = (
                str(int(r.median_b))
                if r.median_b == int(r.median_b)
                else f"{r.median_b:.1f}"
            )
            sig_str = "yes" if r.significant else "no"
            lines.append(
                f"| {r.fuzzer_a} | {r.fuzzer_b} | {med_a} | {med_b} "
                f"| {r.u_stat:.1f} | {r.p_value:.4f} | {r.p_corrected:.4f} | {sig_str} |"
            )
        lines.append("")

        significant_results = [r for r in results if r.significant]
        if significant_results:
            lines.append("**Conclusions:**")
            lines.append("")
            for r in significant_results:
                med_a = (
                    str(int(r.median_a))
                    if r.median_a == int(r.median_a)
                    else f"{r.median_a:.1f}"
                )
                med_b = (
                    str(int(r.median_b))
                    if r.median_b == int(r.median_b)
                    else f"{r.median_b:.1f}"
                )
                if r.direction == ">":
                    lines.append(
                        f"- With p < {alpha}, {r.fuzzer_a} finds significantly more bugs "
                        f"than {r.fuzzer_b} (median {med_a} vs {med_b})."
                    )
                elif r.direction == "<":
                    lines.append(
                        f"- With p < {alpha}, {r.fuzzer_b} finds significantly more bugs "
                        f"than {r.fuzzer_a} (median {med_b} vs {med_a})."
                    )
                else:
                    lines.append(
                        f"- With p < {alpha}, {r.fuzzer_a} and {r.fuzzer_b} differ significantly "
                        f"(both median {med_a}), likely due to distributional differences."
                    )
            lines.append("")
        else:
            lines.append(
                "No pairwise comparison reached significance after Bonferroni correction."
            )
            lines.append("")

    lines.append(
        "> **Note:** The Mann-Whitney U test assesses whether one fuzzer tends to find"
    )
    lines.append(
        "> more bugs than another across runs. It does not measure effect size or"
    )
    lines.append(
        "> practical importance. Small sample sizes (fewer than 5 runs) reduce"
    )
    lines.append("> statistical power.")
    lines.append("")

    return lines


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
    throughput_by_fuzzer: Dict[str, ThroughputSummary] | None = None,
    progress_metrics_by_fuzzer: Dict[str, ProgressMetricsSummary] | None = None,
    stat_results: Optional[List[PairwiseResult]] = None,
    stat_warnings: Optional[List[str]] = None,
    alpha: float = 0.05,
) -> None:
    lines: List[str] = []
    lines.append("# Fuzzer Benchmark Report (from bug-count CSV)")
    lines.append("")
    lines.append(f"- Time budget: **{budget:.2f}h**")
    lines.append("")
    if is_trial_run(budget, [m.runs for m in metrics]):
        lines.append("> " + format_trial_run_warning())
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

    append_throughput_section(
        lines,
        throughput_by_fuzzer or {},
        fuzzer_order=[metric.fuzzer for metric in metrics],
    )
    append_progress_metrics_section(
        lines,
        progress_metrics_by_fuzzer or {},
        fuzzer_order=[metric.fuzzer for metric in metrics],
    )

    if stat_results is not None:
        lines.extend(
            format_statistical_report(
                stat_results, stat_warnings or [], alpha=alpha
            )
        )

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
    throughput_by_fuzzer: Dict[str, ThroughputSummary] | None = None,
    progress_metrics_by_fuzzer: Dict[str, ProgressMetricsSummary] | None = None,
) -> None:
    lines: List[str] = []
    lines.append("# Fuzzer Benchmark Report (from bug-count CSV)")
    lines.append("")
    lines.append(f"- Time budget: **{budget:.2f}h**")
    lines.append(f"- Source CSV: `{csv_path}`")
    lines.append("")
    if is_trial_run(budget, []):
        lines.append("> " + format_trial_run_warning())
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

    append_throughput_section(
        lines,
        throughput_by_fuzzer or {},
        fuzzer_order=sorted((throughput_by_fuzzer or {}).keys()),
    )
    append_progress_metrics_section(
        lines,
        progress_metrics_by_fuzzer or {},
        fuzzer_order=sorted((progress_metrics_by_fuzzer or {}).keys()),
    )

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
    parser.add_argument(
        "--throughput-summary-csv",
        type=Path,
        default=None,
        help="Optional per-fuzzer throughput summary CSV generated by analysis/analyze.py.",
    )
    parser.add_argument(
        "--throughput-samples-csv",
        type=Path,
        default=None,
        help="Optional throughput samples CSV for time-series charts (tx/s and gas/s).",
    )
    parser.add_argument(
        "--progress-metrics-summary-csv",
        type=Path,
        default=None,
        help="Optional per-fuzzer progress metrics summary CSV generated by analysis/analyze.py.",
    )
    parser.add_argument(
        "--progress-metrics-samples-csv",
        type=Path,
        default=None,
        help="Optional progress metrics samples CSV for time-series charts.",
    )
    parser.add_argument(
        "--additional-metrics-summary-csv",
        dest="progress_metrics_summary_csv",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--anonymize", action="store_true", help="Use generic fuzzer labels in plots.")
    args = parser.parse_args()

    report_outdir = args.report_outdir or args.outdir
    images_outdir = args.images_outdir or args.outdir
    if report_outdir is None or images_outdir is None:
        print("error: provide --outdir or both --report-outdir and --images-outdir", file=sys.stderr)
        return 2

    df = load_csv(args.csv)
    validate_monotonic(df)
    throughput_by_fuzzer = (
        load_throughput_summary(args.throughput_summary_csv)
        if args.throughput_summary_csv is not None
        else {}
    )
    progress_metrics_by_fuzzer = (
        load_progress_metrics_summary(args.progress_metrics_summary_csv)
        if args.progress_metrics_summary_csv is not None
        else {}
    )
    throughput_samples_df = (
        load_metric_samples_csv(args.throughput_samples_csv, THROUGHPUT_SAMPLE_VALUE_COLS)
        if args.throughput_samples_csv is not None
        else pd.DataFrame(columns=["fuzzer", "series_id", "time_hours", *THROUGHPUT_SAMPLE_VALUE_COLS])
    )
    progress_metrics_samples_df = (
        load_metric_samples_csv(
            args.progress_metrics_samples_csv, PROGRESS_SAMPLE_VALUE_COLS
        )
        if args.progress_metrics_samples_csv is not None
        else pd.DataFrame(columns=["fuzzer", "series_id", "time_hours", *PROGRESS_SAMPLE_VALUE_COLS])
    )

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
            throughput_by_fuzzer=throughput_by_fuzzer,
            progress_metrics_by_fuzzer=progress_metrics_by_fuzzer,
        )
        msg = "No rows in input CSV. This usually means no bugs were found (or parsing produced no events)."
        write_placeholder_plot(
            "Bugs found over time", images_outdir / "bugs_over_time.png", msg
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
        sample_metric_plot_files = plot_sample_metric_charts(
            throughput_samples_df=throughput_samples_df,
            progress_metrics_samples_df=progress_metrics_samples_df,
            grid=grid,
            images_outdir=images_outdir,
            label_map=None,
        )
        print(f"wrote: {report_outdir / 'REPORT.md'} (no data)")
        if sample_metric_plot_files:
            print("sample metric plots: " + ", ".join(sample_metric_plot_files))
        return 0

    df_grid = resample_to_grid(df, grid)
    metrics = compute_metrics(df_grid, budget=budget, checkpoints=checkpoints, ks=ks)
    metrics = sorted(metrics, key=lambda m: (m.final_p50, m.auc_norm), reverse=True)
    stat_results, stat_warnings = compute_statistical_tests(metrics)

    label_map = None
    if args.anonymize:
        fuzzers = sorted({str(f) for f in df_grid["fuzzer"].unique()})
        label_map = {fz: f"Fuzzer {chr(65 + idx)}" for idx, fz in enumerate(fuzzers)}

    plot_bugs_over_time(df_grid, images_outdir / "bugs_over_time.png", label_map)
    plot_time_to_k(metrics, ks=ks, outpath=images_outdir / "time_to_k.png", label_map=label_map)
    plot_final_distribution(df_grid, images_outdir / "final_distribution.png", label_map)
    plot_plateau_and_late_share(metrics, images_outdir / "plateau_and_late_share.png", label_map)
    sample_metric_plot_files = plot_sample_metric_charts(
        throughput_samples_df=throughput_samples_df,
        progress_metrics_samples_df=progress_metrics_samples_df,
        grid=grid,
        images_outdir=images_outdir,
        label_map=label_map,
    )
    write_report(
        metrics,
        budget=budget,
        checkpoints=checkpoints,
        ks=ks,
        outpath=report_outdir / "REPORT.md",
        throughput_by_fuzzer=throughput_by_fuzzer,
        progress_metrics_by_fuzzer=progress_metrics_by_fuzzer,
        stat_results=stat_results,
        stat_warnings=stat_warnings,
    )

    print(f"wrote: {report_outdir / 'REPORT.md'}")
    plot_files = [
        "bugs_over_time.png",
        "time_to_k.png",
        "final_distribution.png",
        "plateau_and_late_share.png",
    ]
    plot_files.extend(sample_metric_plot_files)
    print("plots: " + ", ".join(plot_files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
