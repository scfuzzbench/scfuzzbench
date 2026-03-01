"""Shared trial-run detection constants and helpers."""

from typing import List

MIN_RUNS_PER_FUZZER = 10
MIN_BUDGET_HOURS = 24.0
TRIAL_RUN_WARNING = (
    "**Warning — trial run.** "
    "This benchmark was executed with fewer than {n} instances per fuzzer and/or "
    "a time budget shorter than {t}h. "
    "Results from trial runs are meant for debugging purposes and are "
    "not valid for extracting conclusions across different fuzzers."
)


def is_trial_run(budget: float, runs_per_fuzzer: List[int]) -> bool:
    if budget < MIN_BUDGET_HOURS:
        return True
    if runs_per_fuzzer and min(runs_per_fuzzer) < MIN_RUNS_PER_FUZZER:
        return True
    return False


def format_trial_run_warning() -> str:
    return TRIAL_RUN_WARNING.format(n=MIN_RUNS_PER_FUZZER, t=int(MIN_BUDGET_HOURS))
