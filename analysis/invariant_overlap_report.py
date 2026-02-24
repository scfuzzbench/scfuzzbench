#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import textwrap
from typing import Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
import numpy as np
import pandas as pd

REQUIRED_COLS = {"fuzzer", "event", "elapsed_seconds"}
OPTIONAL_ID_COLS = ("run_id", "instance_id")
QUALIFIED_EVENT_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_$]*\.)+(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?)$"
)
TRAILING_PARAMS_RE = re.compile(r"\([^()]*\)$")
ASSERTION_SUFFIX_RE = re.compile(r"_ASSERTION_[A-Za-z0-9_]+$")
FOUNDRY_ASSERTION_WRAPPER_PREFIX = "invariant_assertion_failure_"


def die(message: str) -> None:
    raise SystemExit(f"error: {message}")


def normalize_invariant_name(value: str) -> str:
    name = str(value).strip()
    if not name:
        return ""
    # Medusa commonly emits qualified names (e.g. "CryticTester.property_x(...)")
    # while other fuzzers emit just "property_x(...)". Canonicalize to function id
    # and drop Solidity-style parameter signatures for grouping/readability.
    match = QUALIFIED_EVENT_RE.match(name)
    if match:
        name = match.group("name")
    name = TRAILING_PARAMS_RE.sub("", name)
    # Assertion handlers are named "..._ASSERTION_<ID>" while Foundry wrappers
    # use "invariant_assertion_failure_<handler>"; collapse both to the
    # canonical cross-fuzzer assertion identifier (target function name).
    if name.startswith(FOUNDRY_ASSERTION_WRAPPER_PREFIX):
        name = name[len(FOUNDRY_ASSERTION_WRAPPER_PREFIX) :]
    name = ASSERTION_SUFFIX_RE.sub("", name)
    return name.strip()


@dataclass(frozen=True)
class InvariantSummary:
    fuzzers: Tuple[str, ...]
    first_seen_seconds: Dict[str, float]
    runs_hit: Dict[str, int]


@dataclass(frozen=True)
class OverlapResult:
    fuzzers: List[str]
    total_events: int
    filtered_events: int
    invariants: Dict[str, InvariantSummary]
    intersections: Dict[Tuple[str, ...], List[str]]
    set_sizes: Dict[str, int]


def load_events(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = sorted(REQUIRED_COLS - set(df.columns))
    if missing:
        die(f"events CSV missing columns: {missing}")

    for col in OPTIONAL_ID_COLS:
        if col not in df.columns:
            df[col] = "unknown"

    df["fuzzer"] = df["fuzzer"].astype(str).str.strip()
    df["event"] = df["event"].astype(str).map(normalize_invariant_name)
    df["run_id"] = df["run_id"].astype(str).str.strip()
    df["instance_id"] = df["instance_id"].astype(str).str.strip()
    df["elapsed_seconds"] = pd.to_numeric(df["elapsed_seconds"], errors="coerce")

    df = df[df["fuzzer"] != ""]
    df = df[df["event"] != ""]
    df = df[df["elapsed_seconds"].notna()]
    return df.reset_index(drop=True)


def filter_budget(df: pd.DataFrame, budget_hours: Optional[float]) -> pd.DataFrame:
    if budget_hours is None:
        return df
    if budget_hours < 0:
        die("budget-hours must be >= 0")
    budget_seconds = budget_hours * 3600.0
    return df[df["elapsed_seconds"] <= budget_seconds].reset_index(drop=True)


def build_overlap(df: pd.DataFrame, *, total_events: int) -> OverlapResult:
    first_seen: Dict[str, Dict[str, float]] = defaultdict(dict)
    runs_hit: Dict[str, Dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    set_membership: Dict[str, set[str]] = defaultdict(set)

    for row in df.itertuples(index=False):
        fuzzer = str(row.fuzzer)
        invariant = str(row.event)
        elapsed = float(row.elapsed_seconds)
        run_key = f"{row.run_id}:{row.instance_id}"

        set_membership[fuzzer].add(invariant)
        prev = first_seen[invariant].get(fuzzer)
        if prev is None or elapsed < prev:
            first_seen[invariant][fuzzer] = elapsed
        runs_hit[invariant][fuzzer].add(run_key)

    fuzzers = sorted(set_membership.keys())
    set_sizes = {fuzzer: len(set_membership[fuzzer]) for fuzzer in fuzzers}

    invariants: Dict[str, InvariantSummary] = {}
    intersections: Dict[Tuple[str, ...], List[str]] = defaultdict(list)
    for invariant in sorted(first_seen.keys()):
        inv_fuzzers = tuple(sorted(first_seen[invariant].keys()))
        first = {fuzzer: first_seen[invariant][fuzzer] for fuzzer in inv_fuzzers}
        hits = {fuzzer: len(runs_hit[invariant][fuzzer]) for fuzzer in inv_fuzzers}
        summary = InvariantSummary(
            fuzzers=inv_fuzzers,
            first_seen_seconds=first,
            runs_hit=hits,
        )
        invariants[invariant] = summary
        intersections[inv_fuzzers].append(invariant)

    sorted_intersections: Dict[Tuple[str, ...], List[str]] = {}
    for combo in sorted(intersections.keys()):
        sorted_intersections[combo] = sorted(intersections[combo])

    return OverlapResult(
        fuzzers=fuzzers,
        total_events=total_events,
        filtered_events=len(df),
        invariants=invariants,
        intersections=sorted_intersections,
        set_sizes=set_sizes,
    )


def write_csv_report(result: OverlapResult, out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fuzzers = result.fuzzers
    header = (
        ["invariant", "fuzzers", "fuzzers_count"]
        + [f"{fuzzer}_first_seen_s" for fuzzer in fuzzers]
        + [f"{fuzzer}_runs_hit" for fuzzer in fuzzers]
    )

    with out_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)

        rows = sorted(
            result.invariants.items(),
            key=lambda item: (-len(item[1].fuzzers), item[0]),
        )
        for invariant, summary in rows:
            row: List[str] = [invariant, ",".join(summary.fuzzers), str(len(summary.fuzzers))]
            for fuzzer in fuzzers:
                value = summary.first_seen_seconds.get(fuzzer)
                row.append("" if value is None else f"{value:.3f}")
            for fuzzer in fuzzers:
                hits = summary.runs_hit.get(fuzzer)
                row.append("" if hits is None else str(hits))
            writer.writerow(row)


def render_invariant_list(
    lines: List[str], invariants: List[str], *, max_items: int
) -> None:
    if not invariants:
        lines.append("_None._")
        lines.append("")
        return

    shown = invariants[:max_items]
    for invariant in shown:
        lines.append(f"- `{invariant}`")
    if len(invariants) > max_items:
        lines.append(
            f"- _...and {len(invariants) - max_items} more (see `broken_invariants.csv`)._"
        )
    lines.append("")


def write_md_report(
    result: OverlapResult,
    out_md: Path,
    *,
    budget_hours: Optional[float],
    top_k: int,
    max_items_per_group: int = 200,
) -> None:
    out_md.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append("# Broken invariants")
    lines.append("")
    if budget_hours is None:
        lines.append("- Budget filter: **disabled**")
    else:
        lines.append(f"- Budget filter: **{budget_hours:.2f}h**")
    lines.append(f"- Events considered: **{result.filtered_events} / {result.total_events}**")
    lines.append(f"- Unique invariants: **{len(result.invariants)}**")
    lines.append("")

    if not result.invariants:
        lines.append("No broken invariants were found in the filtered event stream.")
        lines.append("")
        out_md.write_text("\n".join(lines), encoding="utf-8")
        return

    lines.append("## Per-fuzzer totals")
    lines.append("")
    lines.append("| Fuzzer | Invariants |")
    lines.append("|---|---:|")
    for fuzzer in result.fuzzers:
        lines.append(f"| {fuzzer} | {result.set_sizes.get(fuzzer, 0)} |")
    lines.append("")

    all_combo = tuple(result.fuzzers)
    shared_all = (
        len(result.intersections.get(all_combo, []))
        if len(result.fuzzers) > 1
        else len(next(iter(result.intersections.values())))
    )
    lines.append("## High-level overlap")
    lines.append("")
    lines.append(f"- Shared by all fuzzers: **{shared_all}**")
    for fuzzer in result.fuzzers:
        count = len(result.intersections.get((fuzzer,), []))
        lines.append(f"- Exclusive to `{fuzzer}`: **{count}**")
    lines.append("")

    lines.append("## Grouped invariants")
    lines.append("")

    for fuzzer in result.fuzzers:
        invariants = result.intersections.get((fuzzer,), [])
        lines.append("<details>")
        lines.append(f"<summary>Exclusive to <code>{fuzzer}</code> ({len(invariants)})</summary>")
        lines.append("")
        render_invariant_list(lines, invariants, max_items=max_items_per_group)
        lines.append("</details>")
        lines.append("")

    if len(result.fuzzers) > 1:
        invariants = result.intersections.get(all_combo, [])
        lines.append("<details>")
        lines.append(
            f"<summary>Shared by all fuzzers ({len(invariants)})</summary>"
        )
        lines.append("")
        render_invariant_list(lines, invariants, max_items=max_items_per_group)
        lines.append("</details>")
        lines.append("")

    subset_entries: List[Tuple[Tuple[str, ...], List[str]]] = []
    for combo, invariants in result.intersections.items():
        if len(combo) <= 1 or len(combo) == len(result.fuzzers):
            continue
        subset_entries.append((combo, invariants))

    subset_entries.sort(key=lambda item: (-len(item[1]), item[0]))
    subset_entries = subset_entries[: max(top_k, 1)]

    if subset_entries:
        lines.append(f"Top shared subsets (top {len(subset_entries)} by size):")
        lines.append("")
        for combo, invariants in subset_entries:
            combo_label = ", ".join(combo)
            lines.append("<details>")
            lines.append(
                f"<summary><code>{combo_label}</code> ({len(invariants)})</summary>"
            )
            lines.append("")
            render_invariant_list(lines, invariants, max_items=max_items_per_group)
            lines.append("</details>")
            lines.append("")

    out_md.write_text("\n".join(lines), encoding="utf-8")


def write_placeholder_plot(title: str, outpath: Path, message: str) -> None:
    plt.figure(figsize=(10, 5))
    plt.title(title)
    plt.axis("off")
    plt.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
    plt.tight_layout()
    outpath.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(outpath, dpi=200)
    plt.close()


def combo_label(combo: Tuple[str, ...]) -> str:
    return " + ".join(combo)


def _wrapped_lines(text: str, *, width: int) -> List[str]:
    wrapped = textwrap.wrap(
        text,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapped if wrapped else [text]


def _detail_lines(
    entries: List[Tuple[str, List[str]]],
    *,
    width: int,
    max_invariants_per_entry: Optional[int],
) -> List[str]:
    lines: List[str] = []
    for label, invariants in entries:
        lines.extend(_wrapped_lines(label, width=width))
        if max_invariants_per_entry is None:
            shown = invariants
        else:
            shown = invariants[:max_invariants_per_entry]
        if not shown:
            lines.append("  - (none)")
        else:
            for invariant in shown:
                lines.extend(_wrapped_lines(f"  - {invariant}", width=width))
        remaining = len(invariants) - len(shown)
        if remaining > 0 and max_invariants_per_entry is not None:
            lines.append(f"  - ... (+{remaining} more)")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def draw_detail_panel(
    ax: plt.Axes,
    *,
    title: str,
    entries: List[Tuple[str, List[str]]],
    width: int = 44,
    max_invariants_per_entry: Optional[int] = 8,
    font_size: int = 10,
) -> int:
    ax.axis("off")
    if not entries:
        ax.text(
            0.0,
            1.0,
            f"{title}\n\nNo intersections available.",
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=font_size,
            family="monospace",
        )
        return 3

    body = _detail_lines(
        entries,
        width=width,
        max_invariants_per_entry=max_invariants_per_entry,
    )
    text = "\n".join([title, "", *body])
    ax.text(
        0.0,
        1.0,
        text,
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=font_size,
        family="monospace",
    )
    return 2 + len(body)


def plot_upset(result: OverlapResult, out_png: Path, *, top_k: int) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if not result.invariants:
        write_placeholder_plot(
            "Invariant overlap (UpSet)",
            out_png,
            "No broken invariants found in the filtered event stream.",
        )
        return

    intersections = sorted(
        result.intersections.items(),
        key=lambda item: (-len(item[1]), item[0]),
    )
    intersections = intersections[: max(top_k, 1)]
    if not intersections:
        write_placeholder_plot(
            "Invariant overlap (UpSet)",
            out_png,
            "No intersections available after filtering.",
        )
        return

    fuzzers = sorted(result.fuzzers, key=lambda fuzzer: (-result.set_sizes[fuzzer], fuzzer))
    y_pos = {fuzzer: idx for idx, fuzzer in enumerate(fuzzers)}

    x = np.arange(len(intersections), dtype=float)
    heights = np.array([len(invariants) for _, invariants in intersections], dtype=float)
    max_height = max(float(np.max(heights)), 1.0)
    top_pad = max(0.5, max_height * 0.08)

    detail_entries_all = [
        (f"[{idx}] {combo_label(combo)} ({len(invariants)})", invariants)
        for idx, (combo, invariants) in enumerate(intersections, start=1)
    ]
    # Keep details readable in the top-left panel above "Set size" without
    # squeezing the main charts on the right.
    detail_entries = detail_entries_all
    detail_width = 62
    detail_line_count = 2 + len(
        _detail_lines(detail_entries, width=detail_width, max_invariants_per_entry=None)
    )
    fig_width = max(13.5, 8.5 + len(intersections) * 0.5)
    fig_height = max(6.5, 4.0 + len(fuzzers) * 0.5 + detail_line_count * 0.08)
    fig = plt.figure(figsize=(fig_width, fig_height), constrained_layout=True)
    gs = fig.add_gridspec(
        2,
        2,
        width_ratios=[2.6, 4.5],
        height_ratios=[3.2, 2.0],
        wspace=0.18,
        hspace=0.05,
    )

    ax_details = fig.add_subplot(gs[0, 0])
    ax_bars = fig.add_subplot(gs[0, 1])
    ax_matrix = fig.add_subplot(gs[1, 1], sharex=ax_bars)
    ax_sets = fig.add_subplot(gs[1, 0], sharey=ax_matrix)
    draw_detail_panel(
        ax_details,
        title="Invariants",
        entries=detail_entries,
        width=detail_width,
        max_invariants_per_entry=None,
        font_size=12,
    )

    ax_bars.bar(x, heights, color="#1f77b4")
    for idx, height in enumerate(heights):
        ax_bars.text(
            idx,
            height + top_pad * 0.25,
            str(int(height)),
            ha="center",
            va="bottom",
            fontsize=8,
        )
    ax_bars.set_ylabel("Intersection size")
    ax_bars.set_ylim(0.0, max_height + top_pad)
    ax_bars.set_xticks(x)
    ax_bars.tick_params(axis="x", labelbottom=False)
    ax_bars.set_xlim(-0.6, len(intersections) - 0.4)
    ax_bars.set_title(
        f"Invariant overlap across fuzzers (top {len(intersections)} exact intersections)"
    )

    y_ticks = np.arange(len(fuzzers), dtype=float)
    for y in y_ticks:
        ax_matrix.scatter(x, np.full_like(x, y), color="#d0d0d0", s=24, zorder=1)

    for xi, (combo, _) in enumerate(intersections):
        ys = sorted(y_pos[fuzzer] for fuzzer in combo)
        ax_matrix.scatter(
            np.full(len(ys), xi, dtype=float),
            np.array(ys, dtype=float),
            color="#222222",
            s=40,
            zorder=3,
        )
        if len(ys) > 1:
            ax_matrix.plot([xi, xi], [ys[0], ys[-1]], color="#222222", linewidth=1.4, zorder=2)

    ax_matrix.set_yticks(y_ticks)
    ax_matrix.set_yticklabels(fuzzers)
    ax_matrix.set_xticks(x)
    ax_matrix.set_xticklabels([str(i) for i in range(1, len(intersections) + 1)], fontsize=8)
    ax_matrix.set_xlabel("Intersection ID (dot matrix; see top-left panel)")
    ax_matrix.grid(axis="x", alpha=0.2)
    ax_matrix.set_xlim(-0.6, len(intersections) - 0.4)
    ax_matrix.invert_yaxis()

    set_sizes = [result.set_sizes[fuzzer] for fuzzer in fuzzers]
    ax_sets.barh(y_ticks, set_sizes, color="#7daedb")
    max_set_size = max(max(set_sizes), 1)
    for y, size in zip(y_ticks, set_sizes):
        ax_sets.text(size + max_set_size * 0.03, y, str(size), va="center", ha="left", fontsize=8)
    ax_sets.set_xlabel("Set size")
    ax_sets.set_yticks(y_ticks)
    ax_sets.set_yticklabels([])
    ax_sets.invert_yaxis()
    ax_sets.set_xlim(0, max_set_size * 1.25)

    fig.savefig(out_png, dpi=200)
    plt.close(fig)


def intersection_size(result: OverlapResult, combo: Tuple[str, ...]) -> int:
    return len(result.intersections.get(tuple(sorted(combo)), []))


def plot_venn_like(result: OverlapResult, out_png: Path) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if not result.invariants:
        write_placeholder_plot(
            "Invariant overlap (Venn-style)",
            out_png,
            "No broken invariants found in the filtered event stream.",
        )
        return

    fuzzers = sorted(result.fuzzers)
    n = len(fuzzers)

    if n == 1:
        fuzzer = fuzzers[0]
        fig = plt.figure(figsize=(11, 5), constrained_layout=True)
        gs = fig.add_gridspec(1, 2, width_ratios=[1.4, 1.2], wspace=0.15)
        ax = fig.add_subplot(gs[0, 0])
        ax_details = fig.add_subplot(gs[0, 1])
        ax.add_patch(Circle((0.5, 0.5), 0.3, alpha=0.25, color="#1f77b4", lw=2))
        ax.text(
            0.5,
            0.5,
            str(intersection_size(result, (fuzzer,))),
            ha="center",
            va="center",
            fontsize=18,
        )
        ax.text(
            0.5,
            0.15,
            f"{fuzzer} (total={result.set_sizes[fuzzer]})",
            ha="center",
            va="center",
            fontsize=11,
        )
        ax.set_title("Invariant overlap (Venn-style)")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        draw_detail_panel(
            ax_details,
            title="Region invariant strings",
            entries=[
                (
                    f"[1] {fuzzer} only ({intersection_size(result, (fuzzer,))})",
                    result.intersections.get((fuzzer,), []),
                )
            ],
            width=42,
            max_invariants_per_entry=12,
        )
        fig.savefig(out_png, dpi=200)
        plt.close(fig)
        return

    if n == 2:
        a, b = fuzzers
        a_only = intersection_size(result, (a,))
        b_only = intersection_size(result, (b,))
        ab = intersection_size(result, (a, b))

        fig = plt.figure(figsize=(12, 5), constrained_layout=True)
        gs = fig.add_gridspec(1, 2, width_ratios=[1.5, 1.1], wspace=0.15)
        ax = fig.add_subplot(gs[0, 0])
        ax_details = fig.add_subplot(gs[0, 1])
        ax.add_patch(Circle((0.42, 0.5), 0.28, alpha=0.28, color="#1f77b4", lw=2))
        ax.add_patch(Circle((0.58, 0.5), 0.28, alpha=0.28, color="#ff7f0e", lw=2))
        ax.text(0.33, 0.5, str(a_only), ha="center", va="center", fontsize=15)
        ax.text(0.67, 0.5, str(b_only), ha="center", va="center", fontsize=15)
        ax.text(0.5, 0.5, str(ab), ha="center", va="center", fontsize=15, fontweight="bold")
        ax.text(0.28, 0.17, f"{a} (total={result.set_sizes[a]})", ha="center", va="center", fontsize=10)
        ax.text(0.72, 0.17, f"{b} (total={result.set_sizes[b]})", ha="center", va="center", fontsize=10)
        ax.set_title("Invariant overlap (Venn-style)")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        draw_detail_panel(
            ax_details,
            title="Region invariant strings",
            entries=[
                (f"[1] {a} only ({a_only})", result.intersections.get((a,), [])),
                (f"[2] {a} + {b} ({ab})", result.intersections.get(tuple(sorted((a, b))), [])),
                (f"[3] {b} only ({b_only})", result.intersections.get((b,), [])),
            ],
            width=42,
            max_invariants_per_entry=12,
        )
        fig.savefig(out_png, dpi=200)
        plt.close(fig)
        return

    if n == 3:
        a, b, c = fuzzers
        a_only = intersection_size(result, (a,))
        b_only = intersection_size(result, (b,))
        c_only = intersection_size(result, (c,))
        ab = intersection_size(result, (a, b))
        ac = intersection_size(result, (a, c))
        bc = intersection_size(result, (b, c))
        abc = intersection_size(result, (a, b, c))

        fig = plt.figure(figsize=(13, 6), constrained_layout=True)
        gs = fig.add_gridspec(1, 2, width_ratios=[1.6, 1.1], wspace=0.15)
        ax = fig.add_subplot(gs[0, 0])
        ax_details = fig.add_subplot(gs[0, 1])
        ax.add_patch(Circle((0.43, 0.58), 0.24, alpha=0.28, color="#1f77b4", lw=2))
        ax.add_patch(Circle((0.57, 0.58), 0.24, alpha=0.28, color="#ff7f0e", lw=2))
        ax.add_patch(Circle((0.50, 0.42), 0.24, alpha=0.28, color="#2ca02c", lw=2))
        ax.text(0.34, 0.60, str(a_only), ha="center", va="center", fontsize=13)
        ax.text(0.66, 0.60, str(b_only), ha="center", va="center", fontsize=13)
        ax.text(0.50, 0.30, str(c_only), ha="center", va="center", fontsize=13)
        ax.text(0.50, 0.63, str(ab), ha="center", va="center", fontsize=13)
        ax.text(0.43, 0.47, str(ac), ha="center", va="center", fontsize=13)
        ax.text(0.57, 0.47, str(bc), ha="center", va="center", fontsize=13)
        ax.text(0.50, 0.52, str(abc), ha="center", va="center", fontsize=13, fontweight="bold")
        ax.text(0.27, 0.80, f"{a} (total={result.set_sizes[a]})", fontsize=10)
        ax.text(0.61, 0.80, f"{b} (total={result.set_sizes[b]})", fontsize=10)
        ax.text(0.42, 0.12, f"{c} (total={result.set_sizes[c]})", fontsize=10)
        ax.set_title("Invariant overlap (Venn-style)")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        draw_detail_panel(
            ax_details,
            title="Region invariant strings",
            entries=[
                (f"[1] {a} only ({a_only})", result.intersections.get((a,), [])),
                (f"[2] {b} only ({b_only})", result.intersections.get((b,), [])),
                (f"[3] {c} only ({c_only})", result.intersections.get((c,), [])),
                (f"[4] {a} + {b} ({ab})", result.intersections.get(tuple(sorted((a, b))), [])),
                (f"[5] {a} + {c} ({ac})", result.intersections.get(tuple(sorted((a, c))), [])),
                (f"[6] {b} + {c} ({bc})", result.intersections.get(tuple(sorted((b, c))), [])),
                (
                    f"[7] {a} + {b} + {c} ({abc})",
                    result.intersections.get(tuple(sorted((a, b, c))), []),
                ),
            ],
            width=42,
            max_invariants_per_entry=12,
        )
        fig.savefig(out_png, dpi=200)
        plt.close(fig)
        return

    write_placeholder_plot(
        "Invariant overlap (Venn-style)",
        out_png,
        "Venn-style chart supports up to 3 fuzzers.\nUse UpSet chart for higher set counts.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build broken-invariant overlap artifacts (CSV + Markdown + UpSet chart)."
    )
    parser.add_argument("--events-csv", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--out-csv", type=Path, required=True)
    parser.add_argument("--out-png", type=Path, required=True)
    parser.add_argument("--budget-hours", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.top_k <= 0:
        die("top-k must be > 0")

    events = load_events(args.events_csv)
    total_events = len(events)
    filtered = filter_budget(events, args.budget_hours)

    result = build_overlap(filtered, total_events=total_events)
    write_csv_report(result, args.out_csv)
    write_md_report(
        result,
        args.out_md,
        budget_hours=args.budget_hours,
        top_k=args.top_k,
    )
    plot_upset(result, args.out_png, top_k=args.top_k)

    print(f"wrote: {args.out_csv}")
    print(f"wrote: {args.out_md}")
    print(f"wrote: {args.out_png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
