#!/usr/bin/env python3
import argparse
import csv
import itertools
import json
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
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

from analysis.plot_palette import (
    build_fuzzer_color_map,
    collect_fuzzer_names,
    non_fuzzer_shades,
)
from analysis.trial_run import format_trial_run_warning, is_trial_run

REQUIRED_COLS = ["fuzzer", "run_id", "time_hours", "bugs_found"]
REQUIRED_SAMPLE_BASE_COLS = ["fuzzer", "run_id", "instance_id", "elapsed_seconds"]
THROUGHPUT_SAMPLE_VALUE_COLS = ["tx_per_second", "gas_per_second"]
PROGRESS_SAMPLE_VALUE_COLS = [
    "seq_per_second",
    "coverage_proxy",
    "corpus_size",
]


VERDICT_COLORS = {
    "improvement": "#1b9e77",
    "regression": "#d95f02",
    "needs-review": "#7570b3",
    "inconclusive": "#666666",
}


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


@dataclass
class RelativeScoreSummary:
    fuzzer: str
    relscore: Optional[float]
    relcov: Optional[float]


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


def _find_column(fieldnames: List[str], candidates: List[str]) -> Optional[str]:
    normalized = {name.strip().lower().replace("_", ""): name for name in fieldnames}
    for candidate in candidates:
        found = normalized.get(candidate.strip().lower().replace("_", ""))
        if found is not None:
            return found
    return None


def load_relative_scores(path: Path) -> Dict[str, RelativeScoreSummary]:
    if not path.exists():
        return {}
    rows: Dict[str, RelativeScoreSummary] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        data_rows = list(reader)
        # The analysis pipeline always writes differential_coverage_relscores.csv,
        # header-only when the run has no showmap campaigns. An empty CSV means
        # "no scoreboard", not a malformed input — skip instead of dying.
        if not fieldnames or not data_rows:
            return {}
        fuzzer_col = _find_column(fieldnames, ["fuzzer", "fuzzer_label", "tool", "name", "approach"])
        relscore_col = _find_column(fieldnames, ["relscore", "relscores", "relative_score"])
        relcov_col = _find_column(fieldnames, ["relcov", "relative_coverage", "coverage_score"])
        if fuzzer_col is None:
            die(f"missing fuzzer column in relative score CSV {path}")
        if relscore_col is None and relcov_col is None:
            die(f"missing relscore/relscores or relcov column in relative score CSV {path}")

        for row in data_rows:
            fuzzer = str(row.get(fuzzer_col, "")).strip()
            if not fuzzer:
                continue
            rows[fuzzer] = RelativeScoreSummary(
                fuzzer=fuzzer,
                relscore=parse_optional_float(row.get(relscore_col)) if relscore_col else None,
                relcov=parse_optional_float(row.get(relcov_col)) if relcov_col else None,
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


# A leg whose last parsed activity ends this early (as a fraction of the time
# budget) almost certainly crashed rather than ran quietly to the end.
RUN_HEALTH_EARLY_EXIT_FRACTION = 0.5


def build_run_health_warnings(
    bugs_df: pd.DataFrame,
    sample_dfs: List[pd.DataFrame],
    budget: float,
    early_exit_fraction: float = RUN_HEALTH_EARLY_EXIT_FRACTION,
) -> List[str]:
    """Flag fuzzer legs that most likely never ran or terminated early.

    Without this, a leg that crashed at startup renders as "N instances,
    0 bugs" — indistinguishable from a clean run that found nothing (observed
    on the echidna/medusa/recon legs of run 1783547385, which all died within
    ~20 minutes of a 2h budget yet reported as zero-bug legs).
    """
    warnings: List[str] = []
    if budget <= 0 or bugs_df.empty:
        return warnings
    for fuzzer in sorted(bugs_df["fuzzer"].astype(str).unique()):
        fuzzer_bugs = bugs_df[bugs_df["fuzzer"].astype(str) == fuzzer]
        final_bugs = 0
        if not fuzzer_bugs.empty:
            final_bugs = int(fuzzer_bugs.groupby("run_id")["bugs_found"].max().max())

        last_activity_by_series: Dict[str, float] = {}
        for sample_df in sample_dfs:
            if sample_df is None or sample_df.empty:
                continue
            subset = sample_df[sample_df["fuzzer"].astype(str) == fuzzer]
            if subset.empty:
                continue
            for series_id, last_hours in subset.groupby("series_id")["time_hours"].max().items():
                key = str(series_id)
                last_activity_by_series[key] = max(
                    last_activity_by_series.get(key, 0.0), float(last_hours)
                )

        if not last_activity_by_series:
            if final_bugs == 0:
                warnings.append(
                    f"**{fuzzer}**: no bugs and no runtime activity samples were parsed from any "
                    "instance — the fuzzer most likely never executed (startup/compile failure). "
                    "Inspect the instance bootstrap logs before drawing conclusions from this leg."
                )
            continue

        last_activity = max(last_activity_by_series.values())
        if last_activity < early_exit_fraction * budget:
            warnings.append(
                f"**{fuzzer}**: last parsed activity at {last_activity:.2f}h of a "
                f"{budget:.2f}h budget across {len(last_activity_by_series)} instance(s) — "
                "the leg likely terminated early. Inspect the instance logs."
            )
    return warnings


def append_run_health_section(lines: List[str], warnings: List[str]) -> None:
    if not warnings:
        return
    lines.append("## ⚠️ Run health")
    lines.append("")
    lines.append(
        "The following fuzzer legs show signs of not having run for the full time budget. "
        "Their results below are **not comparable** to healthy legs."
    )
    lines.append("")
    for warning in warnings:
        lines.append(f"- ⚠️ {warning}")
    lines.append("")


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


def _relative_to_best(value: Optional[float], best: float) -> Optional[float]:
    if value is None or not math.isfinite(value) or best <= 0:
        return None
    return value / best


def build_relative_score_summaries(
    *,
    metrics: List[FuzzerMetrics],
    relative_scores_by_fuzzer: Dict[str, RelativeScoreSummary],
) -> Dict[str, RelativeScoreSummary]:
    # Scores come exclusively from the explicit --relative-scores-csv input. There is
    # deliberately no fallback: bug counts already have their own table, and per-fuzzer
    # coverage proxies (echidna `cov:` values, foundry edge counts, medusa branches-hit)
    # are in different units, so ranking them against each other would declare a
    # meaningless "best coverage" winner.
    summaries: Dict[str, RelativeScoreSummary] = {}
    for metric in metrics:
        current = relative_scores_by_fuzzer.get(metric.fuzzer)
        summaries[metric.fuzzer] = RelativeScoreSummary(
            fuzzer=metric.fuzzer,
            relscore=current.relscore if current else None,
            relcov=current.relcov if current else None,
        )
    return summaries


def fmt_relative_score(value: Optional[float]) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.3f}"


def append_relative_scoreboard(
    lines: List[str],
    relative_scores_by_fuzzer: Dict[str, RelativeScoreSummary],
    fuzzer_order: List[str],
) -> None:
    if not relative_scores_by_fuzzer:
        return

    def sort_key(score: RelativeScoreSummary) -> Tuple[float, float, str]:
        relscore = (
            score.relscore
            if score.relscore is not None and math.isfinite(score.relscore)
            else -1.0
        )
        relcov = (
            score.relcov
            if score.relcov is not None and math.isfinite(score.relcov)
            else -1.0
        )
        return (relscore, relcov, score.fuzzer)

    ordered = sorted(relative_scores_by_fuzzer.values(), key=sort_key, reverse=True)
    best_relscore = max(
        (
            score.relscore
            for score in ordered
            if score.relscore is not None and math.isfinite(score.relscore)
        ),
        default=None,
    )
    best_relcov = max(
        (
            score.relcov
            for score in ordered
            if score.relcov is not None and math.isfinite(score.relcov)
        ),
        default=None,
    )
    relscore_leaders = [
        score.fuzzer
        for score in ordered
        if best_relscore is not None
        and score.relscore is not None
        and math.isclose(score.relscore, best_relscore)
    ]
    relcov_leaders = [
        score.fuzzer
        for score in ordered
        if best_relcov is not None
        and score.relcov is not None
        and math.isclose(score.relcov, best_relcov)
    ]

    lines.append("## Fuzzer scoreboard (higher is better)")
    lines.append(
        "`relscore` is the relative bug-finding score; `relcov` is the relative coverage score. "
        "A value of 1.000 is the best observed fuzzer for that score, and lower values trail that leader."
    )
    if relscore_leaders:
        lines.append(
            f"- Best bug-finding score: **{', '.join(relscore_leaders)}** "
            f"({fmt_relative_score(best_relscore)} relscore)"
        )
    if relcov_leaders:
        lines.append(
            f"- Best coverage score: **{', '.join(relcov_leaders)}** "
            f"({fmt_relative_score(best_relcov)} relcov)"
        )
    lines.append("")

    order_index = {fuzzer: idx for idx, fuzzer in enumerate(fuzzer_order)}
    ordered = sorted(
        ordered,
        key=lambda score: (
            -(
                score.relscore
                if score.relscore is not None and math.isfinite(score.relscore)
                else -1.0
            ),
            -(
                score.relcov
                if score.relcov is not None and math.isfinite(score.relcov)
                else -1.0
            ),
            order_index.get(score.fuzzer, len(order_index)),
            score.fuzzer,
        ),
    )
    header = ["Rank", "Fuzzer", "relscore", "relcov", "Readout"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for rank, score in enumerate(ordered, start=1):
        if score.relscore is not None and score.relcov is not None:
            readout = (
                "best overall"
                if score.fuzzer in relscore_leaders and score.fuzzer in relcov_leaders
                else "compare both scores"
            )
        elif score.relscore is not None:
            readout = "bug score only"
        elif score.relcov is not None:
            readout = "coverage score only"
        else:
            readout = "no relative scores"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(rank),
                    score.fuzzer,
                    fmt_relative_score(score.relscore),
                    fmt_relative_score(score.relcov),
                    readout,
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


def shorten_series_label(label: str) -> str:
    """Make a chart legend / tick label readable.

    Matrix analysis labels look like ``<engine>-<arm>__target-<target>`` (e.g.
    ``foundry-master__target-aave-v4``). The raw string is long and, on combined
    charts with several arms/targets, makes the legend overflow the plot. Render
    it as ``<arm> · <target>`` and drop the redundant engine prefix so series
    stay distinct but compact. Anonymized labels (``Fuzzer A``) and labels
    without the matrix shape are returned unchanged.
    """
    arm, sep, target = label.partition("__target-")
    if not sep:
        return label

    for prefix in ("foundry-", "medusa-", "echidna-", "recon-"):
        if arm.startswith(prefix):
            arm = arm[len(prefix) :]
            break
    return f"{arm} · {target}"


def plot_metric_over_time(
    *,
    metric_df: pd.DataFrame,
    value_column: str,
    outpath: Path,
    title: str,
    ylabel: str,
    label_map: dict[str, str] | None,
    fuzzer_colors: Dict[str, tuple] | None,
    scale: float = 1.0,
) -> bool:
    if metric_df.empty:
        return False

    plt.figure(figsize=(9, 5))
    ax = plt.gca()
    plotted = False

    for fuzzer, group in metric_df.groupby("fuzzer", sort=False):
        label = label_map.get(str(fuzzer), str(fuzzer)) if label_map else str(fuzzer)
        label = shorten_series_label(label)
        color = fuzzer_colors.get(str(fuzzer)) if fuzzer_colors else None
        if color is None:
            color = ax._get_lines.get_next_color()
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

        plt.fill_between(time, p25, p75, step="post", alpha=0.15, color=color)
        plt.step(
            time,
            p50,
            where="post",
            linewidth=2.5,
            label=f"{label} (median)",
            color=color,
        )
        plotted = True

    if not plotted:
        plt.close()
        return False

    plt.title(title)
    plt.xlabel("Elapsed time (hours)")
    plt.ylabel(ylabel)
    plt.legend(loc="best", fontsize=8)
    plt.tight_layout()
    plt.savefig(outpath, dpi=200, bbox_inches="tight")
    plt.close()
    return True


def plot_sample_metric_charts(
    *,
    throughput_samples_df: pd.DataFrame,
    progress_metrics_samples_df: pd.DataFrame,
    grid: np.ndarray,
    images_outdir: Path,
    label_map: dict[str, str] | None,
    fuzzer_colors: Dict[str, tuple] | None,
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
            fuzzer_colors=fuzzer_colors,
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
            fuzzer_colors=fuzzer_colors,
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
    df_grid: pd.DataFrame,
    outpath: Path,
    label_map: dict[str, str] | None,
    fuzzer_colors: Dict[str, tuple] | None,
) -> None:
    plt.figure(figsize=(9, 5))
    ax = plt.gca()
    for fuzzer, group in df_grid.groupby("fuzzer", sort=False):
        fuzzer_label = label_map.get(str(fuzzer), str(fuzzer)) if label_map else str(fuzzer)
        fuzzer_label = shorten_series_label(fuzzer_label)
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

        color = fuzzer_colors.get(str(fuzzer)) if fuzzer_colors else None
        if color is None:
            color = ax._get_lines.get_next_color()

        # Individual runs (faint dotted lines). These are not labeled
        # individually: one entry per run would flood the legend (and, for
        # matrix runs, repeat an uninformative instance id). The median line
        # below carries the legend entry for the series.
        for col in range(arr.shape[1]):
            plt.step(
                time,
                np.rint(arr[:, col]),
                where="post",
                linewidth=1.0,
                alpha=0.35,
                color=color,
                linestyle=":",
                label="_nolegend_",
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
    plt.legend(loc="best", fontsize=8)
    plt.tight_layout()
    plt.savefig(outpath, dpi=200, bbox_inches="tight")
    plt.close()


def plot_time_to_k(
    metrics: List[FuzzerMetrics],
    ks: List[int],
    outpath: Path,
    label_map: dict[str, str] | None,
) -> None:
    plt.figure(figsize=(9, 5))
    fuzzers = [
        shorten_series_label(label_map.get(m.fuzzer, m.fuzzer) if label_map else m.fuzzer)
        for m in metrics
    ]
    x = np.arange(len(fuzzers))
    width = 0.8 / max(1, len(ks))
    sorted_ks = sorted(ks)
    k_colors = {k: color for k, color in zip(sorted_ks, non_fuzzer_shades(len(sorted_ks)))}

    for j, k in enumerate(ks):
        vals = []
        for metric in metrics:
            t = metric.time_to_k_p50[k]
            vals.append(np.nan if not math.isfinite(t) else t)
        plt.bar(
            x + (j - (len(ks) - 1) / 2) * width,
            vals,
            width=width,
            label=f"k={k}",
            color=k_colors[k],
        )

    plt.xticks(x, fuzzers, rotation=20, ha="right", fontsize=8)
    plt.ylabel("Median time-to-k (hours)")
    plt.title("Median time-to-k (lower is better; NaN means never reached)")
    plt.legend(loc="best", fontsize=8)
    plt.tight_layout()
    plt.savefig(outpath, dpi=200, bbox_inches="tight")
    plt.close()


def plot_final_distribution(
    df_grid: pd.DataFrame,
    outpath: Path,
    label_map: dict[str, str] | None,
    fuzzer_colors: Dict[str, tuple] | None,
) -> None:
    plt.figure(figsize=(9, 5))
    data = []
    labels = []
    box_colors = []
    for fuzzer, group in df_grid.groupby("fuzzer", sort=False):
        pivot = (
            group.pivot_table(
                index="time_hours", columns="run_id", values="bugs_found", aggfunc="max"
            )
            .sort_index()
            .astype(float)
        )
        data.append(pivot.iloc[-1].to_numpy(dtype=float))
        labels.append(
            shorten_series_label(
                label_map.get(str(fuzzer), str(fuzzer)) if label_map else str(fuzzer)
            )
        )
        box_colors.append(
            fuzzer_colors.get(str(fuzzer), "#333333")
            if fuzzer_colors
            else "#333333"
        )

    boxplot = plt.boxplot(data, tick_labels=labels, showfliers=False, patch_artist=True)
    for idx, color in enumerate(box_colors):
        boxplot["boxes"][idx].set_facecolor(mcolors.to_rgba(color, alpha=0.25))
        boxplot["boxes"][idx].set_edgecolor(color)
        boxplot["medians"][idx].set_color(color)
        boxplot["medians"][idx].set_linewidth(2.0)
        for whisker in boxplot["whiskers"][idx * 2 : (idx + 1) * 2]:
            whisker.set_color(color)
        for cap in boxplot["caps"][idx * 2 : (idx + 1) * 2]:
            cap.set_color(color)

    plt.ylim(bottom=0)
    plt.xticks(rotation=20, ha="right", fontsize=8)
    plt.ylabel("Bugs found at end of budget")
    plt.title("End-of-budget bug count distribution (per run)")
    plt.tight_layout()
    plt.savefig(outpath, dpi=200, bbox_inches="tight")
    plt.close()


def plot_plateau_and_late_share(
    metrics: List[FuzzerMetrics], outpath: Path, label_map: dict[str, str] | None
) -> None:
    plt.figure(figsize=(9, 5))
    fuzzers = [
        shorten_series_label(label_map.get(m.fuzzer, m.fuzzer) if label_map else m.fuzzer)
        for m in metrics
    ]
    plateau = [m.plateau_time for m in metrics]
    late = [m.late_share for m in metrics]

    x = np.arange(len(fuzzers))
    width = 0.35
    purple_pair = non_fuzzer_shades(2, min_shade=0.55, max_shade=0.85)
    plateau_color = purple_pair[1]
    late_color = purple_pair[0]

    plt.bar(
        x - width / 2,
        plateau,
        width=width,
        label="Plateau time (h)",
        color=plateau_color,
    )
    plt.bar(
        x + width / 2,
        late,
        width=width,
        label="Late discovery share",
        color=late_color,
    )

    plt.xticks(x, fuzzers, rotation=20, ha="right", fontsize=8)
    plt.title("Plateau time and late discovery share")
    plt.legend(loc="best", fontsize=8)
    plt.tight_layout()
    plt.savefig(outpath, dpi=200, bbox_inches="tight")
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
        lines.append("**Warnings:**")
        for w in warnings_list:
            lines.append(f"- {w}")
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
    relative_scores_by_fuzzer: Dict[str, RelativeScoreSummary] | None = None,
    stat_results: Optional[List[PairwiseResult]] = None,
    stat_warnings: Optional[List[str]] = None,
    alpha: float = 0.05,
    run_health_warnings: Optional[List[str]] = None,
) -> None:
    lines: List[str] = []
    lines.append("# Fuzzer Benchmark Report (from bug-count CSV)")
    lines.append("")
    lines.append(f"- Time budget: **{budget:.2f}h**")
    lines.append("")
    if is_trial_run(budget, [m.runs for m in metrics]):
        lines.append("> " + format_trial_run_warning())
        lines.append("")
    append_run_health_section(lines, run_health_warnings or [])
    lines.append("## Executive summary")
    lines.append(
        "This report is derived solely from cumulative bugs-found over time across repeated runs per fuzzer. "
        "It emphasizes robust, distribution-based metrics (median/IQR, success rates, time-to-k) and shape-based behavior "
        "(plateau time, late discovery share) instead of single-run time-to-first-bug."
    )
    lines.append("")

    # Only rendered when --relative-scores-csv was explicitly provided; without it
    # there is no commensurable cross-fuzzer score to rank.
    if relative_scores_by_fuzzer:
        append_relative_scoreboard(
            lines,
            build_relative_score_summaries(
                metrics=metrics,
                relative_scores_by_fuzzer=relative_scores_by_fuzzer,
            ),
            fuzzer_order=[metric.fuzzer for metric in metrics],
        )

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
        "- Core count-based charts use normalized event identities stored under the legacy `bugs_found` column. They do not count crash inputs, but they are not confirmed root-cause bug counts."
    )
    lines.append(
        "- Use `known_bug_report.md`, `known_bug_summary.csv`, and `known_bug_findings.csv` for evidence-backed known-bug hit rates; use `broken_invariants.md` / `broken_invariants.csv` for raw invariant identities."
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


def _to_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def plot_differential_coverage_statistics(
    statistics_json: Optional[Path], images_outdir: Path
) -> Optional[str]:
    if statistics_json is None or not statistics_json.exists():
        return None
    try:
        payload = json.loads(statistics_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    rows = payload.get("rows") or []
    relscore_rows = [r for r in rows if r.get("metric") == "relscore"]
    target_rows = [r for r in relscore_rows if str(r.get("campaign", "")).startswith("by_target/")]
    relscore_rows = target_rows or [r for r in relscore_rows if r.get("campaign") == "combined"]
    if not relscore_rows:
        return None

    labels = [
        str(row.get("campaign", "")).replace("by_target/", "") or "combined"
        for row in relscore_rows
    ]
    relcov_by_campaign = {
        row.get("campaign"): row
        for row in rows
        if row.get("metric") == "relcov"
    }
    y = np.arange(len(labels), dtype=float)
    height = max(4.0, 1.0 + 0.45 * len(labels))
    fig, (ax_score, ax_relcov) = plt.subplots(
        1,
        2,
        figsize=(12, height),
        sharey=True,
        gridspec_kw={"width_ratios": [1.0, 1.2]},
    )

    baseline_scores = [_to_float(row.get("baseline_sample_mean")) for row in relscore_rows]
    feature_scores = [_to_float(row.get("feature_sample_mean")) for row in relscore_rows]
    verdicts = [str(row.get("verdict") or "inconclusive") for row in relscore_rows]

    for idx, row in enumerate(relscore_rows):
        base = baseline_scores[idx]
        feature = feature_scores[idx]
        color = VERDICT_COLORS.get(verdicts[idx], VERDICT_COLORS["inconclusive"])
        if base is not None:
            ax_score.scatter(base, y[idx] - 0.08, color="#4c78a8", label="baseline" if idx == 0 else None)
        if feature is not None:
            ax_score.scatter(feature, y[idx] + 0.08, color=color, label="feature" if idx == 0 else None)
        if base is not None and feature is not None:
            ax_score.plot([base, feature], [y[idx], y[idx]], color="#b0b0b0", linewidth=1.2, zorder=0)
        p_adj = _to_float(row.get("p_value_adjusted"))
        a12 = _to_float(row.get("effect_size_a12"))
        note_parts = []
        if p_adj is not None:
            note_parts.append(f"p={p_adj:.3g}")
        if a12 is not None:
            note_parts.append(f"A12={a12:.2f}")
        if note_parts:
            right = max([v for v in [base, feature] if v is not None], default=0.0)
            ax_score.text(right, y[idx] + 0.18, " ".join(note_parts), fontsize=7, va="center")

    relcov_points = []
    relcov_errors_low = []
    relcov_errors_high = []
    relcov_statuses = []
    noninferiority_delta = None
    for row in relscore_rows:
        relcov = relcov_by_campaign.get(row.get("campaign"), {})
        low = _to_float(relcov.get("relcov_delta_ci_low"))
        high = _to_float(relcov.get("relcov_delta_ci_high"))
        delta = _to_float(relcov.get("noninferiority_delta"))
        if noninferiority_delta is None and delta is not None:
            noninferiority_delta = delta
        if low is None or high is None:
            relcov_points.append(None)
            relcov_errors_low.append(None)
            relcov_errors_high.append(None)
        else:
            point = (low + high) / 2.0
            relcov_points.append(point)
            relcov_errors_low.append(point - low)
            relcov_errors_high.append(high - point)
        relcov_statuses.append(str(relcov.get("relcov_status") or "inconclusive"))

    for idx, point in enumerate(relcov_points):
        color = {
            "held": VERDICT_COLORS["improvement"],
            "failed": VERDICT_COLORS["regression"],
            "inconclusive": VERDICT_COLORS["inconclusive"],
        }.get(relcov_statuses[idx], VERDICT_COLORS["inconclusive"])
        if point is None:
            ax_relcov.text(0.0, y[idx], "insufficient n", fontsize=8, va="center", color=color)
            continue
        ax_relcov.errorbar(
            point,
            y[idx],
            xerr=[[relcov_errors_low[idx]], [relcov_errors_high[idx]]],
            fmt="o",
            color=color,
            ecolor=color,
            capsize=3,
            linewidth=1.4,
        )
        ax_relcov.text(point, y[idx] + 0.18, relcov_statuses[idx], fontsize=7, va="center", color=color)

    ax_score.set_title("RelScore samples")
    ax_score.set_xlabel("mean per-run relscore")
    ax_score.set_yticks(y)
    ax_score.set_yticklabels(labels)
    ax_score.grid(True, axis="x", alpha=0.25)
    ax_score.legend(loc="best", fontsize=8)

    ax_relcov.axvline(0.0, color="#808080", linewidth=1.0, linestyle=":", label="parity")
    if noninferiority_delta is not None:
        ax_relcov.axvline(
            -noninferiority_delta,
            color="#d95f02",
            linewidth=1.0,
            linestyle="--",
            label=f"allowed loss {-noninferiority_delta:.2f}",
        )
    ax_relcov.set_title("RelCov non-inferiority")
    ax_relcov.set_xlabel("feature performance - baseline reliability")
    ax_relcov.grid(True, axis="x", alpha=0.25)
    ax_relcov.legend(loc="best", fontsize=8)

    fig.suptitle("Differential coverage verdict inputs", y=0.99)
    fig.tight_layout()
    out_png = images_outdir / "differential_coverage_statistics.png"
    fig.savefig(out_png, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out_png.name


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
        "--relative-scores-csv",
        type=Path,
        default=None,
        help=(
            "Optional CSV enabling the report scoreboard (omitted = no scoreboard). "
            "Schema: one row per fuzzer with columns 'fuzzer' (or 'tool'/'name'), "
            "'relscore' (or 'relscores') and/or 'relcov' (or 'relative_coverage'/"
            "'coverage_score'); values are ratios in [0, 1] relative to the best "
            "fuzzer, computed by the caller from commensurable data."
        ),
    )
    parser.add_argument(
        "--additional-metrics-summary-csv",
        dest="progress_metrics_summary_csv",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--differential-coverage-statistics-json",
        type=Path,
        default=None,
        help="Optional differential coverage statistics JSON for verdict-input charts.",
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
    relative_scores_by_fuzzer = (
        load_relative_scores(args.relative_scores_csv)
        if args.relative_scores_csv is not None
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
    fuzzer_colors = build_fuzzer_color_map(
        collect_fuzzer_names(
            df["fuzzer"],
            throughput_samples_df["fuzzer"],
            progress_metrics_samples_df["fuzzer"],
        )
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
            fuzzer_colors=fuzzer_colors,
        )
        differential_plot = plot_differential_coverage_statistics(
            args.differential_coverage_statistics_json, images_outdir
        )
        print(f"wrote: {report_outdir / 'REPORT.md'} (no data)")
        if sample_metric_plot_files:
            print("sample metric plots: " + ", ".join(sample_metric_plot_files))
        if differential_plot:
            print("differential coverage plot: " + differential_plot)
        return 0

    df_grid = resample_to_grid(df, grid)
    metrics = compute_metrics(df_grid, budget=budget, checkpoints=checkpoints, ks=ks)
    metrics = sorted(metrics, key=lambda m: (m.final_p50, m.auc_norm), reverse=True)
    stat_results, stat_warnings = compute_statistical_tests(metrics)

    label_map = None
    if args.anonymize:
        fuzzers = sorted({str(f) for f in df_grid["fuzzer"].unique()})
        label_map = {fz: f"Fuzzer {chr(65 + idx)}" for idx, fz in enumerate(fuzzers)}

    plot_bugs_over_time(
        df_grid,
        images_outdir / "bugs_over_time.png",
        label_map,
        fuzzer_colors,
    )
    plot_time_to_k(metrics, ks=ks, outpath=images_outdir / "time_to_k.png", label_map=label_map)
    plot_final_distribution(
        df_grid,
        images_outdir / "final_distribution.png",
        label_map,
        fuzzer_colors,
    )
    plot_plateau_and_late_share(metrics, images_outdir / "plateau_and_late_share.png", label_map)
    sample_metric_plot_files = plot_sample_metric_charts(
        throughput_samples_df=throughput_samples_df,
        progress_metrics_samples_df=progress_metrics_samples_df,
        grid=grid,
        images_outdir=images_outdir,
        label_map=label_map,
        fuzzer_colors=fuzzer_colors,
    )
    differential_plot = plot_differential_coverage_statistics(
        args.differential_coverage_statistics_json, images_outdir
    )
    run_health_warnings = build_run_health_warnings(
        df,
        [throughput_samples_df, progress_metrics_samples_df],
        budget=budget,
    )
    write_report(
        metrics,
        budget=budget,
        checkpoints=checkpoints,
        ks=ks,
        outpath=report_outdir / "REPORT.md",
        throughput_by_fuzzer=throughput_by_fuzzer,
        progress_metrics_by_fuzzer=progress_metrics_by_fuzzer,
        relative_scores_by_fuzzer=relative_scores_by_fuzzer,
        stat_results=stat_results,
        stat_warnings=stat_warnings,
        run_health_warnings=run_health_warnings,
    )

    print(f"wrote: {report_outdir / 'REPORT.md'}")
    plot_files = [
        "bugs_over_time.png",
        "time_to_k.png",
        "final_distribution.png",
        "plateau_and_late_share.png",
    ]
    plot_files.extend(sample_metric_plot_files)
    if differential_plot:
        plot_files.append(differential_plot)
    print("plots: " + ", ".join(plot_files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
