#!/usr/bin/env python3
import argparse
import csv
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from differential_coverage import ApproachData, DifferentialCoverage
from scipy import stats


@dataclass(frozen=True)
class LogFile:
    path: Path
    instance_id: str
    fuzzer_label: str

LOG_FILE_RE = re.compile(r".+\.log$")
INSTANCE_PREFIX_RE = re.compile(r"^(i-[0-9a-f]+)-(.*)$")
IGNORED_LOG_FILENAMES = {"runner_commands.log"}
ABS_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} [0-9:.]+)\]")
MEDUSA_ELAPSED_RE = re.compile(r"elapsed:\s*([0-9hms]+)")
FOUNDATION_JSON_RE = re.compile(r"^\s*\{.*\}\s*$")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
FALSIFIED_RE = re.compile(r"Test\s+([^\s]+)\s+falsified!")
ECHIDNA_FAILED_RE = re.compile(r"^([A-Za-z0-9_]+)\([^)]*\):\s+failed!")
FOUNDRY_FAIL_LINE_RE = re.compile(r"\[FAIL(?:[^\]]*)\]\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
FOUNDRY_TEST_RESULT_RE = re.compile(
    r"(?:\[[^\]]+\]\s+)?(?:test|invariant)[A-Za-z0-9_]*\s*\(\)\s*:\s*(?:FAIL|failed)\b",
    re.IGNORECASE,
)
FOUNDRY_RESULT_NAME_RE = re.compile(r"(?:\[[^\]]+\]\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# End-of-run handler-failure summary entries, printed on graceful exit (the runner passes
# --show-progress so SIGINT at the benchmark timeout triggers this path):
#   [FAIL: assertion failed] src/path.sol:Contract::function
# Note the artifact prefix, the double colon, and the absence of trailing parentheses —
# FOUNDRY_FAIL_LINE_RE cannot match these.
FOUNDRY_HANDLER_SUMMARY_RE = re.compile(
    r"\[FAIL[^\]]*\]\s+\S+:([A-Za-z_][A-Za-z0-9_]*)::([A-Za-z_][A-Za-z0-9_]*)"
)
# Invariant entries in the end-of-run summary are bare names with no parens or
# path prefix: "[FAIL: ] invariant_canary", "[FAIL: panic: ... (0x12)] invariant_x".
# FOUNDRY_FAIL_LINE_RE requires a "(" and the handler regex requires "::", so
# without this pattern those failures are silently dropped (observed on the
# superform leg of run 1783612049).
FOUNDRY_INVARIANT_SUMMARY_RE = re.compile(
    r"\[FAIL[^\]]*\]\s+([A-Za-z_][A-Za-z0-9_]*)\s*$"
)
TX_RATE_PATTERNS = [
    re.compile(r"(?i)(?:tx|txn|transactions?|calls?)\s*(?:/|per)\s*s(?:ec(?:ond)?)?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)"),
    re.compile(r"(?i)([0-9]+(?:\.[0-9]+)?)\s*(?:tx|txn|transactions?|calls?)\s*/\s*s(?:ec(?:ond)?)?\b"),
    re.compile(r"(?i)calls?\s*:\s*[0-9]+(?:\.[0-9]+)?\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*s(?:ec(?:ond)?)?\s*\)"),
]
GAS_RATE_PATTERNS = [
    re.compile(r"(?i)gas\s*(?:/|per)\s*s(?:ec(?:ond)?)?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)"),
    re.compile(r"(?i)([0-9]+(?:\.[0-9]+)?)\s*gas\s*/\s*s(?:ec(?:ond)?)?\b"),
]
TEXT_TX_COUNT_PATTERNS = [
    re.compile(r"(?i)\bfuzzing:\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*[0-9]+"),
]
TEXT_GAS_COUNT_PATTERNS = [
    re.compile(r"(?i)\bgas\s*:\s*([0-9]+(?:\.[0-9]+)?)"),
]

DEFAULT_SHOWMAP_MAX_WORK_ITEMS = 50_000_000
DEFAULT_MIN_VERDICT_SAMPLES = 6
# Per-test (`by_test/...`) campaigns are emitted for human drill-down only: their
# verdicts feed neither the aggregate verdict (see `_is_target_family_campaign`)
# nor any downstream report consumer. Running the full per-campaign statistics
# (BCa bootstrap CIs + non-inferiority tests) for every invariant across every
# target/round/arm dominates analysis time, so the verdict/bootstrap is skipped
# for `by_test` campaigns by default. Their point relscore/relcov values are still
# emitted in the relscores/relcov CSVs and as cheap inconclusive summary rows.
DEFAULT_DIFFCOV_VERDICT_BY_TEST = False
DEFAULT_AUTOTUNE_VALIDATION = "leave-one-target-out"
DEFAULT_RELCOV_NONINFERIORITY_DELTA = 0.05
A12_MEANINGFUL_HIGH = 0.56
A12_MEANINGFUL_LOW = 0.44

TX_RATE_KEYS = (
    "tx_per_second",
    "tx_per_sec",
    "txps",
    "tps",
    "transactions_per_second",
    "transactions_per_sec",
    "calls_per_second",
    "calls_per_sec",
)
GAS_RATE_KEYS = (
    "gas_per_second",
    "gas_per_sec",
    "gasps",
    "gps",
    "gas_used_per_second",
    "gas_spent_per_second",
)
TX_COUNT_KEYS = (
    "cumulative_tx_count",
    "cumulative_txs",
    "cumulative_transactions",
    "total_tx_count",
    "total_transactions",
    "total_txs",
    "tx_count",
    "transaction_count",
    "transactions",
    "txs",
    "total_calls",
    "cumulative_calls",
    "calls",
)
GAS_COUNT_KEYS = (
    "cumulative_gas_spent",
    "cumulative_gas_used",
    "cumulative_gas",
    "total_gas_spent",
    "total_gas_used",
    "total_gas",
    "gas_spent",
    "gas_used",
)
SEQ_RATE_KEYS = (
    "seq_per_second",
    "seq_per_sec",
    "seqps",
    "sequences_per_second",
    "sequences_per_sec",
)
COVERAGE_KEYS = (
    "cumulative_edges_seen",
    "edges_seen",
    "branches_hit",
    "cov",
    "coverage",
)
CORPUS_KEYS = (
    "corpus_count",
    "corpus_size",
    "corpus",
)
FAVORED_KEYS = (
    "favored_items",
)
FAILED_CURRENT_KEYS = (
    "failed_current",
)
FAILED_TOTAL_KEYS = (
    "failed_total",
    "failures_total",
)

SEQ_RATE_PATTERNS = [
    re.compile(r"(?i)\bseq/s:\s*([0-9]+(?:\.[0-9]+)?)"),
    re.compile(r"(?i)\bseq(?:uences?)?\s*(?:/|per)\s*s(?:ec(?:ond)?)?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)"),
]
COVERAGE_PATTERNS = [
    re.compile(r"(?i)\bbranches hit:\s*([0-9]+(?:\.[0-9]+)?)"),
    re.compile(r"(?i)\bcov:\s*([0-9]+(?:\.[0-9]+)?)"),
    re.compile(r"(?i)\bUnique instructions:\s*([0-9]+(?:\.[0-9]+)?)"),
]
CORPUS_PATTERNS = [
    re.compile(r"(?i)\bcorpus:\s*([0-9]+(?:\.[0-9]+)?)"),
    re.compile(r"(?i)\bCorpus size:\s*([0-9]+(?:\.[0-9]+)?)"),
]
FAILURE_RATE_PATTERNS = [
    re.compile(
        r"(?i)\bfailures:\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*([0-9]+(?:\.[0-9]+)?)"
    ),
]


@dataclass(frozen=True)
class Event:
    run_id: str
    instance_id: str
    fuzzer: str
    fuzzer_label: str
    event: str
    elapsed_seconds: float
    source: str
    log_path: str


@dataclass(frozen=True)
class ThroughputSample:
    run_id: str
    instance_id: str
    fuzzer: str
    fuzzer_label: str
    elapsed_seconds: float
    tx_per_second: Optional[float]
    gas_per_second: Optional[float]
    source: str
    log_path: str


@dataclass(frozen=True)
class ProgressMetricsSample:
    run_id: str
    instance_id: str
    fuzzer: str
    fuzzer_label: str
    elapsed_seconds: float
    seq_per_second: Optional[float]
    coverage_proxy: Optional[float]
    corpus_size: Optional[float]
    favored_items: Optional[float]
    failure_rate: Optional[float]
    source: str
    log_path: str


@dataclass(frozen=True)
class ShowmapTrial:
    instance_label: str
    instance_id: str
    fuzzer_label: str
    target_label: Optional[str]
    seed_label: Optional[str]
    approach: str
    suite_test: Optional[str]
    trial_id: str
    raw_path: str
    edges: Set[str]


def parse_duration(text: str) -> Optional[int]:
    matches = re.findall(r"(\d+)([hms])", text)
    if not matches:
        return None
    total = 0
    for value, unit in matches:
        value_i = int(value)
        if unit == "h":
            total += value_i * 3600
        elif unit == "m":
            total += value_i * 60
        elif unit == "s":
            total += value_i
    return total


def parse_timestamp(line: str) -> Optional[float]:
    match = ABS_TS_RE.match(line)
    if not match:
        return None
    ts = match.group(1)
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def infer_run_id(path: Path) -> Optional[str]:
    for part in path.parts:
        if (part.isdigit() and len(part) >= 8) or re.fullmatch(
            r"gh-[0-9]+-[0-9]+", part
        ):
            return part
    return None


def split_instance_label(label: str) -> Tuple[str, str]:
    match = INSTANCE_PREFIX_RE.match(label)
    if match:
        return match.group(1), match.group(2)
    return "unknown", label


def normalize_fuzzer(fuzzer_label: str) -> str:
    lower = fuzzer_label.lower()
    if "recon" in lower:
        return "recon-fuzzer"
    if lower.startswith("echidna"):
        return "echidna"
    if "medusa" in lower:
        return "medusa"
    if "foundry" in lower:
        return "foundry"
    return fuzzer_label


def should_parse_log_file(path: Path) -> bool:
    if not LOG_FILE_RE.match(path.name):
        return False
    return path.name.lower() not in IGNORED_LOG_FILENAMES


def parse_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def normalize_metric_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def flatten_numeric_values(payload: Any, prefix: str = "") -> Dict[str, float]:
    values: Dict[str, float] = {}
    if isinstance(payload, dict):
        for raw_key, raw_value in payload.items():
            key = normalize_metric_key(str(raw_key))
            if not key:
                continue
            nested = f"{prefix}_{key}" if prefix else key
            values.update(flatten_numeric_values(raw_value, nested))
        return values
    value = parse_optional_float(payload)
    if value is not None and prefix:
        values[prefix] = value
    return values


def pick_metric_value(metric_values: Dict[str, float], keys: Tuple[str, ...]) -> Optional[float]:
    # Prefer exact/suffix key matches (e.g. "metrics_tx_per_second"), then substring matches.
    for key in keys:
        for metric_key, value in metric_values.items():
            if metric_key == key or metric_key.endswith(f"_{key}"):
                return value
    for key in keys:
        for metric_key, value in metric_values.items():
            if key in metric_key:
                return value
    return None


def parse_rate_from_text(line: str, patterns: List[re.Pattern[str]]) -> Optional[float]:
    for pattern in patterns:
        match = pattern.search(line)
        if not match:
            continue
        value = parse_optional_float(match.group(1))
        if value is not None:
            return value
    return None


def parse_count_from_text(line: str, patterns: List[re.Pattern[str]]) -> Optional[float]:
    for pattern in patterns:
        match = pattern.search(line)
        if not match:
            continue
        value = parse_optional_float(match.group(1))
        if value is not None:
            return value
    return None


def parse_failure_rate_from_text(line: str) -> Optional[float]:
    for pattern in FAILURE_RATE_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue
        failures = parse_optional_float(match.group(1))
        total = parse_optional_float(match.group(2))
        if failures is None or total is None or total <= 0.0:
            return None
        return failures / total
    return None


def parse_throughput_from_payload(
    payload: Dict[str, Any], elapsed_seconds: Optional[float]
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    metric_values = flatten_numeric_values(payload)
    tx_rate = pick_metric_value(metric_values, TX_RATE_KEYS)
    gas_rate = pick_metric_value(metric_values, GAS_RATE_KEYS)

    source = None
    if tx_rate is not None or gas_rate is not None:
        source = "json-rate"

    tx_count = pick_metric_value(metric_values, TX_COUNT_KEYS)
    gas_count = pick_metric_value(metric_values, GAS_COUNT_KEYS)
    needs_count_derivation = (tx_rate is None and tx_count is not None) or (
        gas_rate is None and gas_count is not None
    )
    if (
        elapsed_seconds is not None
        and elapsed_seconds > 0.0
        and needs_count_derivation
    ):
        if tx_rate is None and tx_count is not None:
            tx_rate = tx_count / elapsed_seconds
        if gas_rate is None and gas_count is not None:
            gas_rate = gas_count / elapsed_seconds
        if tx_rate is not None or gas_rate is not None:
            source = "json-cumulative" if source is None else source

    if tx_rate is None and gas_rate is None:
        return None, None, None
    return tx_rate, gas_rate, source


def parse_progress_metrics_from_payload(
    payload: Dict[str, Any],
) -> Tuple[
    Optional[float],
    Optional[float],
    Optional[float],
    Optional[float],
    Optional[float],
    Optional[str],
]:
    metric_values = flatten_numeric_values(payload)
    seq_per_second = pick_metric_value(metric_values, SEQ_RATE_KEYS)
    coverage_proxy = pick_metric_value(metric_values, COVERAGE_KEYS)
    corpus_size = pick_metric_value(metric_values, CORPUS_KEYS)
    favored_items = pick_metric_value(metric_values, FAVORED_KEYS)
    failure_rate = pick_metric_value(metric_values, ("failure_rate", "fail_rate"))

    if failure_rate is None:
        failed_current = pick_metric_value(metric_values, FAILED_CURRENT_KEYS)
        failed_total = pick_metric_value(metric_values, FAILED_TOTAL_KEYS)
        if (
            failed_current is not None
            and failed_total is not None
            and failed_total > 0.0
        ):
            failure_rate = failed_current / failed_total

    if (
        seq_per_second is None
        and coverage_proxy is None
        and corpus_size is None
        and favored_items is None
        and failure_rate is None
    ):
        return None, None, None, None, None, None
    return (
        seq_per_second,
        coverage_proxy,
        corpus_size,
        favored_items,
        failure_rate,
        "json-metrics",
    )


def percentile(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100.0)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def extract_bang_event(line: str) -> Optional[str]:
    if "!!!" not in line:
        return None
    _, after = line.split("!!!", 1)
    feature = after.strip()
    for sep in ("»", "\"", ")"):
        if sep in feature:
            feature = feature.split(sep, 1)[0].strip()
    feature = feature.strip()
    if not feature:
        return None
    return feature


def normalize_foundry_failure_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    name = str(value).strip()
    if not name:
        return None
    # Foundry handler assertion failures use "Contract:function"; keep only function
    # name for cross-fuzzer normalization. Ignore unexpected multi-colon values.
    if name.count(":") == 1:
        contract_name, function_name = name.split(":", 1)
        if contract_name and function_name:
            name = function_name.strip()
    return name or None


def extract_foundry_text_failure(line: str) -> Optional[str]:
    fail_match = FOUNDRY_FAIL_LINE_RE.search(line)
    if fail_match:
        return normalize_foundry_failure_name(fail_match.group(1))

    invariant_match = FOUNDRY_INVARIANT_SUMMARY_RE.search(line)
    if invariant_match:
        return normalize_foundry_failure_name(invariant_match.group(1))

    if FOUNDRY_TEST_RESULT_RE.search(line):
        name_match = FOUNDRY_RESULT_NAME_RE.search(line)
        if name_match:
            return normalize_foundry_failure_name(name_match.group(1))

    return None


def extract_foundry_failure(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    ts_value = parse_optional_float(payload.get("timestamp"))
    # We require timestamps to compute elapsed seconds in benchmark reports.
    if ts_value is None:
        return None, None, None

    if str(payload.get("event") or "").strip() == "failure":
        # Handler-assertion failure events (foundry-rs/foundry#15689) carry no
        # function name — only the harness contract *address* in `target` and a
        # 4-byte `selector`. Naming by address would collapse every handler bug
        # into one identity and double-count against the end-of-run summary
        # (which names them properly), so leave these unnamed here: identity
        # still comes from the summary plus the broken_assertions pulse counts.
        if str(payload.get("failure_type") or "").strip() == "handler_assertion":
            return None, ts_value, None
        # Prefer the per-invariant identity so distinct invariant failures are
        # counted as distinct bugs. The `target` field is the harness contract
        # (e.g. "CryticToFoundry"), which is identical across every invariant and
        # would otherwise collapse all failures into a single bug. Fall back to
        # `target` only when no invariant name is present.
        failure_name = (
            normalize_foundry_failure_name(payload.get("invariant"))
            or normalize_foundry_failure_name(payload.get("target"))
        )
        if failure_name:
            return failure_name, ts_value, "foundry-failure-event"

    if str(payload.get("type") or "").strip() == "invariant_failure":
        invariant_name = normalize_foundry_failure_name(payload.get("invariant"))
        if invariant_name:
            return invariant_name, ts_value, "foundry-invariant-failure"

    return None, ts_value, None


def extract_foundry_failure_totals(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[int], Optional[int]]:
    ts_value = parse_optional_float(payload.get("timestamp"))
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return ts_value, None, None

    unique_failures = parse_optional_float(metrics.get("unique_failures"))
    if unique_failures is None:
        unique_failures = parse_optional_float(metrics.get("broken_invariants"))
    broken_handlers = parse_optional_float(metrics.get("broken_assertions"))
    if broken_handlers is None:
        broken_handlers = parse_optional_float(metrics.get("broken_handlers"))
    return (
        ts_value,
        None if unique_failures is None else int(unique_failures),
        None if broken_handlers is None else int(broken_handlers),
    )


def parse_foundry_log(
    path: Path, run_id: str, instance_id: str, fuzzer_label: str
) -> List[Event]:
    events: List[Event] = []
    seen = set()
    synthetic_handler_count = 0
    # Indices (into `events`) of synthetic handler-bug events not yet matched to a
    # name from the end-of-run summary, oldest first. Replaced in place when a named
    # summary entry arrives so the discovery-time elapsed from the pulse delta is
    # preserved.
    unnamed_synthetic_indices: List[int] = []
    first_ts: Optional[float] = None
    last_json_ts: Optional[float] = None
    last_elapsed: Optional[float] = None

    def current_elapsed() -> float:
        # Best-known elapsed time for events that carry no timestamp of their own
        # (text lines): prefer the JSON pulse clock, fall back to text elapsed.
        if last_json_ts is not None and first_ts is not None:
            return last_json_ts - first_ts
        return last_elapsed or 0.0

    with path.open("r", errors="ignore") as handle:
        for line in handle:
            clean_line = ANSI_ESCAPE_RE.sub("", line)
            elapsed_match = MEDUSA_ELAPSED_RE.search(clean_line)
            if elapsed_match:
                parsed_elapsed = parse_duration(elapsed_match.group(1))
                if parsed_elapsed is not None:
                    last_elapsed = float(parsed_elapsed)

            if FOUNDATION_JSON_RE.match(clean_line):
                try:
                    payload = json.loads(clean_line)
                except json.JSONDecodeError:
                    payload = None
                if payload is not None:
                    payload_ts = parse_optional_float(payload.get("timestamp"))
                    if payload_ts is not None:
                        if first_ts is None:
                            # Foundry emits epoch timestamps. Anchor elapsed time to the
                            # first JSON event so failures are measured since the run
                            # began, not since the first failure.
                            first_ts = payload_ts
                        last_json_ts = payload_ts

                    event_name, ts_value, source = extract_foundry_failure(payload)
                    if event_name and ts_value is not None and source:
                        if event_name not in seen:
                            seen.add(event_name)
                            events.append(
                                Event(
                                    run_id=run_id,
                                    instance_id=instance_id,
                                    fuzzer=normalize_fuzzer(fuzzer_label),
                                    fuzzer_label=fuzzer_label,
                                    event=event_name,
                                    elapsed_seconds=ts_value - (first_ts or ts_value),
                                    source=source,
                                    log_path=str(path),
                                )
                            )
                    totals_ts, unique_failures, broken_handlers = extract_foundry_failure_totals(payload)
                    if (
                        totals_ts is not None
                        and unique_failures is not None
                        and broken_handlers is not None
                    ):
                        expected_events = unique_failures + broken_handlers
                        missing_events = expected_events - len(seen)
                        for _ in range(max(0, missing_events)):
                            synthetic_handler_count += 1
                            synthetic_name = f"foundry_handler_bug_{synthetic_handler_count}"
                            if synthetic_name in seen:
                                continue
                            seen.add(synthetic_name)
                            events.append(
                                Event(
                                    run_id=run_id,
                                    instance_id=instance_id,
                                    fuzzer=normalize_fuzzer(fuzzer_label),
                                    fuzzer_label=fuzzer_label,
                                    event=synthetic_name,
                                    elapsed_seconds=totals_ts - (first_ts or totals_ts),
                                    source="foundry-broken-handler-metric",
                                    log_path=str(path),
                                )
                            )
                            unnamed_synthetic_indices.append(len(events) - 1)
                    continue

            handler_match = FOUNDRY_HANDLER_SUMMARY_RE.search(clean_line)
            if handler_match:
                handler_name = handler_match.group(2)
                if handler_name and handler_name not in seen:
                    seen.add(handler_name)
                    if unnamed_synthetic_indices:
                        # A pulse-count delta already produced an anonymous event for
                        # this bug with the right discovery time — give it its name.
                        # Summary order may differ from discovery order when several
                        # handler bugs exist; names and times both stay exact as sets.
                        idx = unnamed_synthetic_indices.pop(0)
                        events[idx] = replace(
                            events[idx],
                            event=handler_name,
                            source="foundry-handler-summary",
                        )
                    else:
                        events.append(
                            Event(
                                run_id=run_id,
                                instance_id=instance_id,
                                fuzzer=normalize_fuzzer(fuzzer_label),
                                fuzzer_label=fuzzer_label,
                                event=handler_name,
                                elapsed_seconds=current_elapsed(),
                                source="foundry-handler-summary",
                                log_path=str(path),
                            )
                        )
                continue

            event_name = extract_foundry_text_failure(clean_line)
            if event_name and event_name not in seen:
                seen.add(event_name)
                events.append(
                    Event(
                        run_id=run_id,
                        instance_id=instance_id,
                        fuzzer=normalize_fuzzer(fuzzer_label),
                        fuzzer_label=fuzzer_label,
                        event=event_name,
                        elapsed_seconds=current_elapsed(),
                        source="foundry-text-failure",
                        log_path=str(path),
                    )
                )
    return events


def parse_medusa_log(
    path: Path, run_id: str, instance_id: str, fuzzer_label: str
) -> List[Event]:
    events: List[Event] = []
    seen = set()
    last_elapsed: Optional[int] = None
    last_failed: Optional[str] = None
    with path.open("r", errors="ignore") as handle:
        for line in handle:
            clean_line = ANSI_ESCAPE_RE.sub("", line)
            elapsed_match = MEDUSA_ELAPSED_RE.search(clean_line)
            if elapsed_match:
                last_elapsed = parse_duration(elapsed_match.group(1))

            failed_match = re.search(r"(Assertion|Property) Test:\s*(.+)$", clean_line)
            if "[FAILED]" in clean_line and failed_match:
                last_failed = failed_match.group(2).strip()
                if last_failed not in seen and last_elapsed is not None:
                    seen.add(last_failed)
                    events.append(
                        Event(
                            run_id=run_id,
                            instance_id=instance_id,
                            fuzzer=normalize_fuzzer(fuzzer_label),
                            fuzzer_label=fuzzer_label,
                            event=last_failed,
                            elapsed_seconds=float(last_elapsed),
                            source="medusa-failed",
                            log_path=str(path),
                        )
                    )
                continue

            bang_event = extract_bang_event(clean_line)
            if bang_event and last_elapsed is not None:
                event_name = last_failed or bang_event
                if event_name not in seen:
                    seen.add(event_name)
                    events.append(
                        Event(
                            run_id=run_id,
                            instance_id=instance_id,
                            fuzzer=normalize_fuzzer(fuzzer_label),
                            fuzzer_label=fuzzer_label,
                            event=event_name,
                            elapsed_seconds=float(last_elapsed),
                            source="medusa-bang",
                            log_path=str(path),
                        )
                    )
                continue

            if "panic: assertion failed" in clean_line and last_failed and last_failed not in seen:
                if last_elapsed is not None:
                    seen.add(last_failed)
                    events.append(
                        Event(
                            run_id=run_id,
                            instance_id=instance_id,
                            fuzzer=normalize_fuzzer(fuzzer_label),
                            fuzzer_label=fuzzer_label,
                            event=last_failed,
                            elapsed_seconds=float(last_elapsed),
                            source="medusa-panic",
                            log_path=str(path),
                        )
                    )
    return events


def parse_generic_log(
    path: Path,
    run_id: str,
    instance_id: str,
    fuzzer_label: str,
    *,
    allow_bang: bool = True,
    allow_falsified: bool = True,
    allow_failed: bool = False,
) -> List[Event]:
    events: List[Event] = []
    seen = set()
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    with path.open("r", errors="ignore") as handle:
        for line in handle:
            clean_line = ANSI_ESCAPE_RE.sub("", line)
            ts = parse_timestamp(clean_line)
            if ts is not None:
                last_ts = ts
                if first_ts is None:
                    first_ts = ts
            if allow_bang:
                bang_event = extract_bang_event(clean_line)
                if bang_event:
                    if bang_event in seen:
                        continue
                    if last_ts is None or first_ts is None:
                        continue
                    seen.add(bang_event)
                    events.append(
                        Event(
                            run_id=run_id,
                            instance_id=instance_id,
                            fuzzer=normalize_fuzzer(fuzzer_label),
                            fuzzer_label=fuzzer_label,
                            event=bang_event,
                            elapsed_seconds=last_ts - first_ts,
                            source="bang",
                            log_path=str(path),
                        )
                    )
                    continue
            if allow_failed:
                failed_match = ECHIDNA_FAILED_RE.search(clean_line)
                if failed_match:
                    event_name = failed_match.group(1)
                    if event_name in seen:
                        continue
                    if last_ts is None or first_ts is None:
                        continue
                    seen.add(event_name)
                    events.append(
                        Event(
                            run_id=run_id,
                            instance_id=instance_id,
                            fuzzer=normalize_fuzzer(fuzzer_label),
                            fuzzer_label=fuzzer_label,
                            event=event_name,
                            elapsed_seconds=last_ts - first_ts,
                            source="failed",
                            log_path=str(path),
                        )
                    )
                    continue
            if allow_falsified:
                falsified_match = FALSIFIED_RE.search(clean_line)
                if falsified_match:
                    event_name = falsified_match.group(1)
                    if event_name in seen:
                        continue
                    if last_ts is None or first_ts is None:
                        continue
                    seen.add(event_name)
                    events.append(
                        Event(
                            run_id=run_id,
                            instance_id=instance_id,
                            fuzzer=normalize_fuzzer(fuzzer_label),
                            fuzzer_label=fuzzer_label,
                            event=event_name,
                            elapsed_seconds=last_ts - first_ts,
                            source="falsified",
                            log_path=str(path),
                        )
                    )
                    continue
            if "panic: assertion failed" in clean_line or "FAILURE" in clean_line:
                if last_ts is None or first_ts is None:
                    continue
                event_name = "assertion_failed"
                if event_name in seen:
                    continue
                seen.add(event_name)
                events.append(
                    Event(
                        run_id=run_id,
                        instance_id=instance_id,
                        fuzzer=normalize_fuzzer(fuzzer_label),
                        fuzzer_label=fuzzer_label,
                        event=event_name,
                        elapsed_seconds=last_ts - first_ts,
                        source="panic",
                        log_path=str(path),
                    )
                )
    return events


def parse_throughput_log(
    path: Path, run_id: str, instance_id: str, fuzzer_label: str
) -> List[ThroughputSample]:
    samples: List[ThroughputSample] = []
    first_ts: Optional[float] = None
    first_abs_ts: Optional[float] = None
    last_elapsed: Optional[float] = None
    previous_key: Optional[Tuple[float, Optional[float], Optional[float]]] = None

    with path.open("r", errors="ignore") as handle:
        for line in handle:
            clean_line = ANSI_ESCAPE_RE.sub("", line)

            absolute_ts = parse_timestamp(clean_line)
            if absolute_ts is not None:
                if first_abs_ts is None:
                    first_abs_ts = absolute_ts
                last_elapsed = max(0.0, absolute_ts - first_abs_ts)

            elapsed_match = MEDUSA_ELAPSED_RE.search(clean_line)
            if elapsed_match:
                elapsed_value = parse_duration(elapsed_match.group(1))
                if elapsed_value is not None:
                    last_elapsed = float(elapsed_value)

            payload: Optional[Dict[str, Any]] = None
            if FOUNDATION_JSON_RE.match(clean_line):
                try:
                    parsed = json.loads(clean_line)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    payload = parsed

            source: Optional[str] = None
            tx_rate: Optional[float] = None
            gas_rate: Optional[float] = None
            elapsed_seconds: Optional[float] = last_elapsed

            if payload is not None:
                ts_value = parse_optional_float(payload.get("timestamp"))
                if ts_value is not None:
                    if first_ts is None:
                        first_ts = ts_value
                    elapsed_seconds = max(0.0, ts_value - first_ts)
                    last_elapsed = elapsed_seconds
                tx_rate, gas_rate, source = parse_throughput_from_payload(payload, elapsed_seconds)
            else:
                tx_rate = parse_rate_from_text(clean_line, TX_RATE_PATTERNS)
                gas_rate = parse_rate_from_text(clean_line, GAS_RATE_PATTERNS)
                if elapsed_seconds is not None and elapsed_seconds > 0.0:
                    tx_count = parse_count_from_text(clean_line, TEXT_TX_COUNT_PATTERNS)
                    gas_count = parse_count_from_text(clean_line, TEXT_GAS_COUNT_PATTERNS)
                    if tx_rate is None and tx_count is not None:
                        tx_rate = tx_count / elapsed_seconds
                        source = "text-cumulative"
                    if gas_rate is None and gas_count is not None:
                        gas_rate = gas_count / elapsed_seconds
                        source = "text-cumulative"
                if tx_rate is not None or gas_rate is not None:
                    source = source or "text-rate"

            if tx_rate is None and gas_rate is None:
                continue
            if elapsed_seconds is None:
                continue

            key = (
                round(float(elapsed_seconds), 3),
                None if tx_rate is None else round(tx_rate, 12),
                None if gas_rate is None else round(gas_rate, 12),
            )
            if key == previous_key:
                continue
            previous_key = key

            samples.append(
                ThroughputSample(
                    run_id=run_id,
                    instance_id=instance_id,
                    fuzzer=normalize_fuzzer(fuzzer_label),
                    fuzzer_label=fuzzer_label,
                    elapsed_seconds=float(elapsed_seconds),
                    tx_per_second=None if tx_rate is None else float(tx_rate),
                    gas_per_second=None if gas_rate is None else float(gas_rate),
                    source=source or "unknown",
                    log_path=str(path),
                )
            )
    return samples


def discover_log_files(logs_dir: Path) -> Tuple[LogFile, ...]:
    files: List[LogFile] = []
    for path in logs_dir.rglob("*"):
        if not path.is_file():
            continue
        if not should_parse_log_file(path):
            continue
        rel = path.relative_to(logs_dir)
        if len(rel.parts) < 2:
            continue
        instance_label = rel.parts[0]
        instance_id, fuzzer_label = split_instance_label(instance_label)
        files.append(LogFile(path, instance_id, fuzzer_label))
    return tuple(files)


def parse_logs(
    logs_dir: Path,
    run_id: Optional[str],
    log_files: Sequence[LogFile],
) -> List[Event]:
    events: List[Event] = []
    run_id_value = run_id or infer_run_id(logs_dir) or "unknown"
    for log_file in log_files:
        path = log_file.path
        instance_id = log_file.instance_id
        fuzzer_label = log_file.fuzzer_label
        fuzzer = normalize_fuzzer(fuzzer_label)
        if fuzzer == "foundry":
            events.extend(parse_foundry_log(path, run_id_value, instance_id, fuzzer_label))
        elif fuzzer == "medusa":
            events.extend(parse_medusa_log(path, run_id_value, instance_id, fuzzer_label))
        elif fuzzer == "echidna":
            events.extend(
                parse_generic_log(
                    path,
                    run_id_value,
                    instance_id,
                    fuzzer_label,
                    allow_bang=False,
                    allow_falsified=True,
                    allow_failed=False,
                )
            )
        elif fuzzer == "recon-fuzzer":
            events.extend(
                parse_generic_log(
                    path,
                    run_id_value,
                    instance_id,
                    fuzzer_label,
                    allow_bang=False,
                    allow_falsified=True,
                    allow_failed=True,
                )
            )
        else:
            events.extend(parse_generic_log(path, run_id_value, instance_id, fuzzer_label))
    return events


def parse_throughput_logs(
    logs_dir: Path,
    run_id: Optional[str],
    log_files: Sequence[LogFile],
) -> List[ThroughputSample]:
    samples: List[ThroughputSample] = []
    run_id_value = run_id or infer_run_id(logs_dir) or "unknown"
    for log_file in log_files:
        samples.extend(
            parse_throughput_log(
                log_file.path,
                run_id_value,
                log_file.instance_id,
                log_file.fuzzer_label,
            )
        )
    return samples


def parse_progress_metrics_log(
    path: Path, run_id: str, instance_id: str, fuzzer_label: str
) -> List[ProgressMetricsSample]:
    samples: List[ProgressMetricsSample] = []
    first_ts: Optional[float] = None
    first_abs_ts: Optional[float] = None
    last_elapsed: Optional[float] = None
    previous_key: Optional[
        Tuple[
            float,
            Optional[float],
            Optional[float],
            Optional[float],
            Optional[float],
            Optional[float],
        ]
    ] = None

    with path.open("r", errors="ignore") as handle:
        for line in handle:
            clean_line = ANSI_ESCAPE_RE.sub("", line)

            absolute_ts = parse_timestamp(clean_line)
            if absolute_ts is not None:
                if first_abs_ts is None:
                    first_abs_ts = absolute_ts
                last_elapsed = max(0.0, absolute_ts - first_abs_ts)

            elapsed_match = MEDUSA_ELAPSED_RE.search(clean_line)
            if elapsed_match:
                elapsed_value = parse_duration(elapsed_match.group(1))
                if elapsed_value is not None:
                    last_elapsed = float(elapsed_value)

            payload: Optional[Dict[str, Any]] = None
            if FOUNDATION_JSON_RE.match(clean_line):
                try:
                    parsed = json.loads(clean_line)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    payload = parsed

            elapsed_seconds: Optional[float] = last_elapsed
            source: Optional[str] = None
            seq_per_second: Optional[float] = None
            coverage_proxy: Optional[float] = None
            corpus_size: Optional[float] = None
            favored_items: Optional[float] = None
            failure_rate: Optional[float] = None

            if payload is not None:
                ts_value = parse_optional_float(payload.get("timestamp"))
                if ts_value is not None:
                    if first_ts is None:
                        first_ts = ts_value
                    elapsed_seconds = max(0.0, ts_value - first_ts)
                    last_elapsed = elapsed_seconds
                (
                    seq_per_second,
                    coverage_proxy,
                    corpus_size,
                    favored_items,
                    failure_rate,
                    source,
                ) = parse_progress_metrics_from_payload(payload)
            else:
                seq_per_second = parse_rate_from_text(clean_line, SEQ_RATE_PATTERNS)
                coverage_proxy = parse_count_from_text(clean_line, COVERAGE_PATTERNS)
                corpus_size = parse_count_from_text(clean_line, CORPUS_PATTERNS)
                failure_rate = parse_failure_rate_from_text(clean_line)
                if (
                    seq_per_second is not None
                    or coverage_proxy is not None
                    or corpus_size is not None
                    or failure_rate is not None
                ):
                    source = "text-metrics"

            if (
                seq_per_second is None
                and coverage_proxy is None
                and corpus_size is None
                and favored_items is None
                and failure_rate is None
            ):
                continue
            if elapsed_seconds is None:
                continue

            key = (
                round(float(elapsed_seconds), 3),
                None if seq_per_second is None else round(seq_per_second, 12),
                None if coverage_proxy is None else round(coverage_proxy, 12),
                None if corpus_size is None else round(corpus_size, 12),
                None if favored_items is None else round(favored_items, 12),
                None if failure_rate is None else round(failure_rate, 12),
            )
            if key == previous_key:
                continue
            previous_key = key

            samples.append(
                ProgressMetricsSample(
                    run_id=run_id,
                    instance_id=instance_id,
                    fuzzer=normalize_fuzzer(fuzzer_label),
                    fuzzer_label=fuzzer_label,
                    elapsed_seconds=float(elapsed_seconds),
                    seq_per_second=seq_per_second,
                    coverage_proxy=coverage_proxy,
                    corpus_size=corpus_size,
                    favored_items=favored_items,
                    failure_rate=failure_rate,
                    source=source or "unknown",
                    log_path=str(path),
                )
            )
    return samples


def parse_progress_metrics_logs(
    logs_dir: Path,
    run_id: Optional[str],
    log_files: Sequence[LogFile],
) -> List[ProgressMetricsSample]:
    samples: List[ProgressMetricsSample] = []
    run_id_value = run_id or infer_run_id(logs_dir) or "unknown"
    for log_file in log_files:
        samples.extend(
            parse_progress_metrics_log(
                log_file.path,
                run_id_value,
                log_file.instance_id,
                log_file.fuzzer_label,
            )
        )
    return samples


def write_events_csv(events: Iterable[Event], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id",
                "instance_id",
                "fuzzer",
                "fuzzer_label",
                "event",
                "elapsed_seconds",
                "source",
                "log_path",
            ]
        )
        for event in events:
            writer.writerow(
                [
                    event.run_id,
                    event.instance_id,
                    event.fuzzer,
                    event.fuzzer_label,
                    event.event,
                    f"{event.elapsed_seconds:.3f}",
                    event.source,
                    event.log_path,
                ]
            )


def load_events_csv(path: Path) -> List[Event]:
    events: List[Event] = []
    with path.open("r", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                elapsed = float(row["elapsed_seconds"])
            except (KeyError, ValueError):
                continue
            events.append(
                Event(
                    run_id=row.get("run_id", "unknown"),
                    instance_id=row.get("instance_id", "unknown"),
                    fuzzer=row.get("fuzzer", "unknown"),
                    fuzzer_label=row.get("fuzzer_label", row.get("fuzzer", "unknown")),
                    event=row.get("event", "unknown"),
                    elapsed_seconds=elapsed,
                    source=row.get("source", ""),
                    log_path=row.get("log_path", ""),
                )
            )
    return events


def write_throughput_samples_csv(samples: Iterable[ThroughputSample], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id",
                "instance_id",
                "fuzzer",
                "fuzzer_label",
                "elapsed_seconds",
                "tx_per_second",
                "gas_per_second",
                "source",
                "log_path",
            ]
        )
        for sample in samples:
            writer.writerow(
                [
                    sample.run_id,
                    sample.instance_id,
                    sample.fuzzer,
                    sample.fuzzer_label,
                    f"{sample.elapsed_seconds:.3f}",
                    "" if sample.tx_per_second is None else f"{sample.tx_per_second:.6f}",
                    "" if sample.gas_per_second is None else f"{sample.gas_per_second:.6f}",
                    sample.source,
                    sample.log_path,
                ]
            )


def write_throughput_summary_csv(samples: Iterable[ThroughputSample], out_path: Path) -> None:
    per_fuzzer_runs: Dict[str, Dict[str, Dict[str, Optional[float]]]] = defaultdict(dict)

    for sample in samples:
        run_key = f"{sample.run_id}:{sample.instance_id}:{sample.fuzzer_label}"
        run_state = per_fuzzer_runs[sample.fuzzer].setdefault(
            run_key,
            {
                "tx_per_second": None,
                "tx_elapsed": None,
                "gas_per_second": None,
                "gas_elapsed": None,
            },
        )

        tx_elapsed = run_state["tx_elapsed"]
        if sample.tx_per_second is not None and (
            tx_elapsed is None or sample.elapsed_seconds >= tx_elapsed
        ):
            run_state["tx_per_second"] = sample.tx_per_second
            run_state["tx_elapsed"] = sample.elapsed_seconds

        gas_elapsed = run_state["gas_elapsed"]
        if sample.gas_per_second is not None and (
            gas_elapsed is None or sample.elapsed_seconds >= gas_elapsed
        ):
            run_state["gas_per_second"] = sample.gas_per_second
            run_state["gas_elapsed"] = sample.elapsed_seconds

    def fmt(value: float) -> str:
        return f"{value:.6f}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "fuzzer",
                "runs",
                "txps_runs",
                "gasps_runs",
                "txps_p50",
                "txps_p25",
                "txps_p75",
                "gasps_p50",
                "gasps_p25",
                "gasps_p75",
            ]
        )
        for fuzzer in sorted(per_fuzzer_runs):
            run_values = per_fuzzer_runs[fuzzer]
            tx_values = [
                float(run_state["tx_per_second"])
                for run_state in run_values.values()
                if run_state["tx_per_second"] is not None
            ]
            gas_values = [
                float(run_state["gas_per_second"])
                for run_state in run_values.values()
                if run_state["gas_per_second"] is not None
            ]

            writer.writerow(
                [
                    fuzzer,
                    len(run_values),
                    len(tx_values),
                    len(gas_values),
                    "" if not tx_values else fmt(percentile(tx_values, 50)),
                    "" if not tx_values else fmt(percentile(tx_values, 25)),
                    "" if not tx_values else fmt(percentile(tx_values, 75)),
                    "" if not gas_values else fmt(percentile(gas_values, 50)),
                    "" if not gas_values else fmt(percentile(gas_values, 25)),
                    "" if not gas_values else fmt(percentile(gas_values, 75)),
                ]
            )


def write_progress_metrics_samples_csv(
    samples: Iterable[ProgressMetricsSample], out_path: Path
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id",
                "instance_id",
                "fuzzer",
                "fuzzer_label",
                "elapsed_seconds",
                "seq_per_second",
                "coverage_proxy",
                "corpus_size",
                "favored_items",
                "failure_rate",
                "source",
                "log_path",
            ]
        )
        for sample in samples:
            writer.writerow(
                [
                    sample.run_id,
                    sample.instance_id,
                    sample.fuzzer,
                    sample.fuzzer_label,
                    f"{sample.elapsed_seconds:.3f}",
                    ""
                    if sample.seq_per_second is None
                    else f"{sample.seq_per_second:.6f}",
                    ""
                    if sample.coverage_proxy is None
                    else f"{sample.coverage_proxy:.6f}",
                    "" if sample.corpus_size is None else f"{sample.corpus_size:.6f}",
                    "" if sample.favored_items is None else f"{sample.favored_items:.6f}",
                    "" if sample.failure_rate is None else f"{sample.failure_rate:.6f}",
                    sample.source,
                    sample.log_path,
                ]
            )


def write_progress_metrics_summary_csv(
    samples: Iterable[ProgressMetricsSample], out_path: Path
) -> None:
    per_fuzzer_runs: Dict[str, Dict[str, Dict[str, Optional[float]]]] = defaultdict(dict)

    for sample in samples:
        run_key = f"{sample.run_id}:{sample.instance_id}:{sample.fuzzer_label}"
        run_state = per_fuzzer_runs[sample.fuzzer].setdefault(
            run_key,
            {
                "seq_per_second": None,
                "seq_elapsed": None,
                "coverage_proxy": None,
                "coverage_elapsed": None,
                "corpus_size": None,
                "corpus_elapsed": None,
                "favored_items": None,
                "favored_elapsed": None,
                "failure_rate": None,
                "failure_elapsed": None,
            },
        )

        seq_elapsed = run_state["seq_elapsed"]
        if sample.seq_per_second is not None and (
            seq_elapsed is None or sample.elapsed_seconds >= seq_elapsed
        ):
            run_state["seq_per_second"] = sample.seq_per_second
            run_state["seq_elapsed"] = sample.elapsed_seconds

        coverage_elapsed = run_state["coverage_elapsed"]
        if sample.coverage_proxy is not None and (
            coverage_elapsed is None or sample.elapsed_seconds >= coverage_elapsed
        ):
            run_state["coverage_proxy"] = sample.coverage_proxy
            run_state["coverage_elapsed"] = sample.elapsed_seconds

        corpus_elapsed = run_state["corpus_elapsed"]
        if sample.corpus_size is not None and (
            corpus_elapsed is None or sample.elapsed_seconds >= corpus_elapsed
        ):
            run_state["corpus_size"] = sample.corpus_size
            run_state["corpus_elapsed"] = sample.elapsed_seconds

        favored_elapsed = run_state["favored_elapsed"]
        if sample.favored_items is not None and (
            favored_elapsed is None or sample.elapsed_seconds >= favored_elapsed
        ):
            run_state["favored_items"] = sample.favored_items
            run_state["favored_elapsed"] = sample.elapsed_seconds

        failure_elapsed = run_state["failure_elapsed"]
        if sample.failure_rate is not None and (
            failure_elapsed is None or sample.elapsed_seconds >= failure_elapsed
        ):
            run_state["failure_rate"] = sample.failure_rate
            run_state["failure_elapsed"] = sample.elapsed_seconds

    def fmt(value: float) -> str:
        return f"{value:.6f}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "fuzzer",
                "runs",
                "seqps_runs",
                "coverage_runs",
                "corpus_runs",
                "favored_runs",
                "failure_rate_runs",
                "seqps_p50",
                "seqps_p25",
                "seqps_p75",
                "coverage_p50",
                "coverage_p25",
                "coverage_p75",
                "corpus_p50",
                "corpus_p25",
                "corpus_p75",
                "favored_p50",
                "favored_p25",
                "favored_p75",
                "failure_rate_p50",
                "failure_rate_p25",
                "failure_rate_p75",
            ]
        )
        for fuzzer in sorted(per_fuzzer_runs):
            run_values = per_fuzzer_runs[fuzzer]
            seq_values = [
                float(run_state["seq_per_second"])
                for run_state in run_values.values()
                if run_state["seq_per_second"] is not None
            ]
            coverage_values = [
                float(run_state["coverage_proxy"])
                for run_state in run_values.values()
                if run_state["coverage_proxy"] is not None
            ]
            corpus_values = [
                float(run_state["corpus_size"])
                for run_state in run_values.values()
                if run_state["corpus_size"] is not None
            ]
            favored_values = [
                float(run_state["favored_items"])
                for run_state in run_values.values()
                if run_state["favored_items"] is not None
            ]
            failure_values = [
                float(run_state["failure_rate"])
                for run_state in run_values.values()
                if run_state["failure_rate"] is not None
            ]
            writer.writerow(
                [
                    fuzzer,
                    len(run_values),
                    len(seq_values),
                    len(coverage_values),
                    len(corpus_values),
                    len(favored_values),
                    len(failure_values),
                    "" if not seq_values else fmt(percentile(seq_values, 50)),
                    "" if not seq_values else fmt(percentile(seq_values, 25)),
                    "" if not seq_values else fmt(percentile(seq_values, 75)),
                    "" if not coverage_values else fmt(percentile(coverage_values, 50)),
                    "" if not coverage_values else fmt(percentile(coverage_values, 25)),
                    "" if not coverage_values else fmt(percentile(coverage_values, 75)),
                    "" if not corpus_values else fmt(percentile(corpus_values, 50)),
                    "" if not corpus_values else fmt(percentile(corpus_values, 25)),
                    "" if not corpus_values else fmt(percentile(corpus_values, 75)),
                    "" if not favored_values else fmt(percentile(favored_values, 50)),
                    "" if not favored_values else fmt(percentile(favored_values, 25)),
                    "" if not favored_values else fmt(percentile(favored_values, 75)),
                    "" if not failure_values else fmt(percentile(failure_values, 50)),
                    "" if not failure_values else fmt(percentile(failure_values, 25)),
                    "" if not failure_values else fmt(percentile(failure_values, 75)),
                ]
            )


def build_runs(events: Iterable[Event]) -> Dict[str, Dict[str, List[float]]]:
    runs: Dict[str, Dict[str, List[float]]] = {}
    for event in events:
        run_key = f"{event.run_id}:{event.instance_id}:{event.fuzzer_label}"
        runs.setdefault(event.fuzzer, {}).setdefault(run_key, []).append(event.elapsed_seconds)
    for fuzzer_runs in runs.values():
        for run_key, times in fuzzer_runs.items():
            fuzzer_runs[run_key] = sorted(set(times))
    return runs


def build_event_sets(events: Iterable[Event]) -> Dict[str, set]:
    event_sets: Dict[str, set] = defaultdict(set)
    for event in events:
        event_sets[event.fuzzer].add(event.event)
    return event_sets


def compute_exclusive_events(event_sets: Dict[str, set]) -> Tuple[Dict[str, set], Dict[str, set]]:
    event_to_fuzzers: Dict[str, set] = defaultdict(set)
    for fuzzer, events in event_sets.items():
        for event in events:
            event_to_fuzzers[event].add(fuzzer)
    exclusive: Dict[str, set] = {fuzzer: set() for fuzzer in event_sets}
    for event, fuzzers in event_to_fuzzers.items():
        if len(fuzzers) == 1:
            fuzzer = next(iter(fuzzers))
            exclusive[fuzzer].add(event)
    return exclusive, event_to_fuzzers


def write_summary_csv(events: Iterable[Event], out_path: Path) -> None:
    runs = build_runs(events)
    event_sets = build_event_sets(events)
    exclusive, _ = compute_exclusive_events(event_sets)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "fuzzer",
                "runs",
                "unique_bugs",
                "exclusive_bugs",
                "shared_bugs",
                "mean_bugs_per_run",
                "median_bugs_per_run",
                "stdev_bugs_per_run",
                "min_bugs_per_run",
                "max_bugs_per_run",
                "mean_ttfb_seconds",
                "median_ttfb_seconds",
            ]
        )
        for fuzzer in sorted(event_sets.keys() | runs.keys()):
            run_map = runs.get(fuzzer, {})
            run_counts = [len(times) for times in run_map.values()]
            ttfb_values = [min(times) for times in run_map.values() if times]
            unique_bugs = len(event_sets.get(fuzzer, set()))
            exclusive_bugs = len(exclusive.get(fuzzer, set()))
            shared_bugs = unique_bugs - exclusive_bugs
            mean_count = statistics.mean(run_counts) if run_counts else 0.0
            median_count = statistics.median(run_counts) if run_counts else 0.0
            stdev_count = statistics.stdev(run_counts) if len(run_counts) > 1 else 0.0
            min_count = min(run_counts) if run_counts else 0
            max_count = max(run_counts) if run_counts else 0
            mean_ttfb = statistics.mean(ttfb_values) if ttfb_values else 0.0
            median_ttfb = statistics.median(ttfb_values) if ttfb_values else 0.0
            writer.writerow(
                [
                    fuzzer,
                    len(run_map),
                    unique_bugs,
                    exclusive_bugs,
                    shared_bugs,
                    f"{mean_count:.3f}",
                    f"{median_count:.3f}",
                    f"{stdev_count:.3f}",
                    min_count,
                    max_count,
                    f"{mean_ttfb:.3f}",
                    f"{median_ttfb:.3f}",
                ]
            )


def write_overlap_csv(events: Iterable[Event], out_path: Path) -> None:
    event_sets = build_event_sets(events)
    fuzzers = sorted(event_sets.keys())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["fuzzer", *fuzzers])
        for fuzzer in fuzzers:
            row = [fuzzer]
            set_a = event_sets[fuzzer]
            for other in fuzzers:
                set_b = event_sets[other]
                union = set_a | set_b
                jaccard = (len(set_a & set_b) / len(union)) if union else 0.0
                row.append(f"{jaccard:.3f}")
            writer.writerow(row)


def write_exclusive_csv(events: Iterable[Event], out_path: Path) -> None:
    event_sets = build_event_sets(events)
    exclusive, _ = compute_exclusive_events(event_sets)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["fuzzer", "event"])
        for fuzzer in sorted(exclusive.keys()):
            for event in sorted(exclusive[fuzzer]):
                writer.writerow([fuzzer, event])


def sanitize_showmap_component(value: str) -> str:
    value = value.strip()
    if not value:
        return "unknown"
    sanitized = re.sub(r"[^A-Za-z0-9_.=-]+", "_", value)
    if sanitized in {".", ".."}:
        return "unknown"
    return sanitized


def parse_showmap_approach_dir(name: str) -> Tuple[str, Optional[str]]:
    parts = [part for part in name.split("__") if part]
    if len(parts) >= 2:
        return parts[0], "__".join(parts[1:])
    return name, None


def parse_showmap_instance_label(
    instance_label: str,
) -> Tuple[str, str, Optional[str], Optional[str]]:
    marker = re.search(r"__(?:target|seed)-", instance_label)
    base_label = instance_label[: marker.start()] if marker else instance_label
    target_label = None
    seed_label = None

    if marker:
        for part in instance_label[marker.start() + 2 :].split("__"):
            if part.startswith("target-"):
                target_label = sanitize_showmap_component(part.removeprefix("target-"))
            elif part.startswith("seed-"):
                seed_label = sanitize_showmap_component(part.removeprefix("seed-"))

    instance_id, fuzzer_label = split_instance_label(base_label)
    return instance_id, fuzzer_label, target_label, seed_label


def read_afl_showmap(path: Path) -> Set[str]:
    edges: Set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(errors="ignore").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line:
            continue
        edge_id, sep, count_text = line.partition(":")
        if not sep:
            raise ValueError(f"invalid AFL showmap line {path}:{line_number}: {line}")
        try:
            count = int(count_text.strip())
        except ValueError as exc:
            raise ValueError(f"invalid AFL showmap count {path}:{line_number}: {line}") from exc
        if count > 0:
            edges.add(edge_id.strip())
    return edges


def load_showmap_trials(
    logs_dir: Path,
    excluded_fuzzers: Optional[Set[str]] = None,
) -> Tuple[List[ShowmapTrial], List[Dict[str, str]]]:
    trials: List[ShowmapTrial] = []
    skipped: List[Dict[str, str]] = []
    excluded_fuzzers = excluded_fuzzers or set()

    for showmap_dir in sorted(path for path in logs_dir.rglob("showmap") if path.is_dir()):
        rel_showmap_dir = showmap_dir.relative_to(logs_dir)
        if not rel_showmap_dir.parts:
            continue
        instance_label = rel_showmap_dir.parts[0]
        instance_id, fuzzer_label, target_label, seed_label = parse_showmap_instance_label(
            instance_label
        )

        for trial_file in sorted(showmap_dir.rglob("*.txt")):
            rel_trial = trial_file.relative_to(showmap_dir)
            if len(rel_trial.parts) < 2:
                skipped.append({"path": str(trial_file), "reason": "missing approach directory"})
                continue

            approach, suite_test = parse_showmap_approach_dir(rel_trial.parts[0])
            if (
                approach.lower() in excluded_fuzzers
                or normalize_fuzzer(approach).lower() in excluded_fuzzers
                or fuzzer_label.lower() in excluded_fuzzers
                or normalize_fuzzer(fuzzer_label).lower() in excluded_fuzzers
            ):
                continue

            try:
                edges = read_afl_showmap(trial_file)
            except ValueError as exc:
                skipped.append({"path": str(trial_file), "reason": str(exc)})
                continue
            if not edges:
                skipped.append({"path": str(trial_file), "reason": "empty coverage"})
                continue

            trial_rel = Path(*rel_trial.parts[1:]).with_suffix("")
            trial_name = "__".join(trial_rel.parts)
            trial_id = (
                sanitize_showmap_component(f"seed-{seed_label}")
                if seed_label is not None
                else sanitize_showmap_component(f"{instance_label}__{trial_name}")
            )
            trials.append(
                ShowmapTrial(
                    instance_label=instance_label,
                    instance_id=instance_id,
                    fuzzer_label=fuzzer_label,
                    target_label=target_label,
                    seed_label=seed_label,
                    approach=sanitize_showmap_component(approach),
                    suite_test=(
                        sanitize_showmap_component(suite_test)
                        if suite_test is not None
                        else None
                    ),
                    trial_id=trial_id,
                    raw_path=str(trial_file),
                    edges=edges,
                )
            )
    return trials, skipped


def merge_edges(
    target: Dict[str, Dict[str, Set[str]]],
    approach: str,
    trial_id: str,
    edges: Set[str],
) -> None:
    target.setdefault(approach, {}).setdefault(trial_id, set()).update(edges)


def build_showmap_campaigns(
    trials: Iterable[ShowmapTrial],
) -> Dict[str, Dict[str, Dict[str, Set[str]]]]:
    campaigns: Dict[str, Dict[str, Dict[str, Set[str]]]] = {"combined": {}}
    for trial in trials:
        merge_edges(campaigns["combined"], trial.approach, trial.trial_id, trial.edges)
        if trial.target_label is not None:
            target_campaign = f"by_target/{trial.target_label}"
            merge_edges(
                campaigns.setdefault(target_campaign, {}),
                trial.approach,
                trial.trial_id,
                trial.edges,
            )
        if trial.suite_test is not None:
            campaign_name = f"by_test/{trial.suite_test}"
            merge_edges(
                campaigns.setdefault(campaign_name, {}),
                trial.approach,
                trial.trial_id,
                trial.edges,
            )
            if trial.target_label is not None:
                target_test_campaign = f"by_target/{trial.target_label}/by_test/{trial.suite_test}"
                merge_edges(
                    campaigns.setdefault(target_test_campaign, {}),
                    trial.approach,
                    trial.trial_id,
                    trial.edges,
                )
    return campaigns


def write_showmap_campaign_dir(
    campaign: Dict[str, Dict[str, Set[str]]],
    out_dir: Path,
) -> None:
    for approach, trials in sorted(campaign.items()):
        approach_dir = out_dir / approach
        approach_dir.mkdir(parents=True, exist_ok=True)
        for trial_id, edges in sorted(trials.items()):
            trial_path = approach_dir / f"{trial_id}.txt"
            with trial_path.open("w", encoding="utf-8") as handle:
                for edge in sorted(edges):
                    handle.write(f"{edge}:1\n")


def showmap_campaign_work_items(campaign: Dict[str, Dict[str, Set[str]]]) -> int:
    unique_edges: Set[str] = set()
    trial_count = 0
    for trials in campaign.values():
        trial_count += len(trials)
        for edges in trials.values():
            unique_edges.update(edges)
    return len(unique_edges) * max(trial_count, 1)


def calculate_relscores(
    campaign: Dict[str, Dict[str, Set[str]]],
) -> Dict[str, float]:
    """Compute relscores for a campaign.

    Fast, allocation-light reimplementation of
    ``differential_coverage.DifferentialCoverage.relscores``. For valid
    scfuzzbench campaigns it computes the same mathematical relscore (see
    ``test_differential_coverage`` for parity checks against the upstream
    library). It may differ from the library by ordinary floating-point
    roundoff: the library divides and accumulates a float per edge, while this
    sums exact integer products and divides once.

    The upstream implementation is ``O(|all_edges| * |trials|^2)`` per approach
    because ``ApproachData.edges_by_trial`` rebuilds every trial's frozenset on
    each property access, and that access happens once per edge inside
    ``_calculate_relscore``. For the ``combined`` and ``by_target`` campaigns
    (hundreds of trials, tens of thousands of edges) this dominated the entire
    analysis step, taking >300s for ``combined`` alone. The implementation
    below is ``O(sum of trial sizes)`` per approach.
    """
    # Per-approach edge union, plus a count of how many approaches' unions
    # contain each edge (used to derive how many approaches never hit an edge).
    unions: Dict[str, Set[str]] = {}
    approaches_containing_edge: Dict[str, int] = defaultdict(int)
    for approach, trials in campaign.items():
        union: Set[str] = set()
        for edges in trials.values():
            union.update(edges)
        unions[approach] = union
        for edge in union:
            approaches_containing_edge[edge] += 1

    num_approaches = len(campaign)
    scores: Dict[str, float] = {}
    for approach, trials in campaign.items():
        trials_with_non_empty_cov = sum(1 for edges in trials.values() if edges)
        if trials_with_non_empty_cov == 0:
            scores[approach] = 0.0
            continue
        # hit_count[edge] = number of this approach's trials that hit `edge`.
        hit_count: Dict[str, int] = defaultdict(int)
        for edges in trials.values():
            for edge in edges:
                hit_count[edge] += 1
        # score = sum over edges of (#approaches that never hit edge) *
        #         (#trials of this approach that hit edge) / trials_non_empty.
        # Edges this approach never hits contribute 0, so iterating its own
        # hit_count is equivalent to iterating all_edges.
        total = 0
        for edge, count in hit_count.items():
            never_hit = num_approaches - approaches_containing_edge[edge]
            total += never_hit * count
        scores[approach] = float(total) / trials_with_non_empty_cov
    return scores


def calculate_relcovs(
    campaign: Dict[str, Dict[str, Set[str]]],
) -> Dict[str, Dict[str, float]]:
    """Compute pairwise relcov for a campaign.

    Equivalent to calling ``ApproachData.relcov`` (median value reducer, union
    collection reducer) for every (approach, reference) pair, but without
    rebuilding the upstream ``edges_by_trial`` frozensets.
    """
    unions: Dict[str, Set[str]] = {
        approach: set().union(*trials.values()) if trials else set()
        for approach, trials in campaign.items()
    }
    result: Dict[str, Dict[str, float]] = {}
    for approach, trials in campaign.items():
        trial_edge_sets = list(trials.values())
        row: Dict[str, float] = {}
        for reference in campaign:
            reference_union = unions[reference]
            reference_size = len(reference_union)
            if reference_size == 0:
                row[reference] = 0.0
                continue
            ratios = [
                len(edges & reference_union) / reference_size
                for edges in trial_edge_sets
            ]
            row[reference] = float(statistics.median(ratios)) if ratios else 0.0
        result[approach] = row
    return result


def find_baseline_feature(approaches: Iterable[str]) -> Optional[Tuple[str, str]]:
    approaches = sorted(approaches)
    if len(approaches) != 2:
        return None

    baseline_names = {"master", "main", "stable"}

    def is_baseline(name: str) -> bool:
        lower = name.lower()
        return lower in baseline_names or lower.endswith("-master") or lower.endswith("-main")

    baselines = [approach for approach in approaches if is_baseline(approach)]
    if len(baselines) != 1:
        return None

    baseline = baselines[0]
    feature = next(approach for approach in approaches if approach != baseline)
    return baseline, feature


def _mean(values: Sequence[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _sample_variance(values: Sequence[float]) -> float:
    return statistics.variance(values) if len(values) >= 2 else 0.0


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = min(max(q, 0.0), 1.0) * (len(sorted_values) - 1)
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return sorted_values[int(pos)]
    weight = pos - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _bca_mean_ci(
    values: Sequence[float],
    confidence_level: float,
    min_samples: int,
    iterations: int = 2000,
) -> Tuple[Optional[float], Optional[float], str]:
    finite_values = [float(value) for value in values if math.isfinite(float(value))]
    if len(finite_values) < min_samples:
        return None, None, "insufficient n"
    theta_hat = _mean(finite_values)
    if len(set(finite_values)) == 1:
        return theta_hat, theta_hat, "ok"

    rng = random.Random(8675309)
    n = len(finite_values)
    boot = sorted(
        _mean([finite_values[rng.randrange(n)] for _ in range(n)])
        for _ in range(iterations)
    )

    less = sum(1 for value in boot if value < theta_hat)
    equal = sum(1 for value in boot if value == theta_hat)
    prop_less = min(max((less + 0.5 * equal) / iterations, 1e-9), 1.0 - 1e-9)
    z0 = float(stats.norm.ppf(prop_less))

    jack = [
        _mean([value for idx, value in enumerate(finite_values) if idx != leave_out])
        for leave_out in range(n)
    ]
    jack_mean = _mean(jack)
    numerator = sum((jack_mean - value) ** 3 for value in jack)
    denominator = 6.0 * (
        sum((jack_mean - value) ** 2 for value in jack) ** 1.5
    )
    acceleration = numerator / denominator if denominator else 0.0

    alpha = (1.0 - confidence_level) / 2.0

    def adjusted_quantile(raw_alpha: float) -> float:
        z_alpha = float(stats.norm.ppf(raw_alpha))
        denom = 1.0 - acceleration * (z0 + z_alpha)
        if denom == 0.0:
            return raw_alpha
        return float(stats.norm.cdf(z0 + (z0 + z_alpha) / denom))

    low_q = adjusted_quantile(alpha)
    high_q = adjusted_quantile(1.0 - alpha)
    return _percentile(boot, low_q), _percentile(boot, high_q), "ok"


def _a12(xs: Sequence[float], ys: Sequence[float]) -> float:
    if not xs or not ys:
        return 0.5
    wins = 0.0
    for x in xs:
        for y in ys:
            if x > y:
                wins += 1.0
            elif x == y:
                wins += 0.5
    return wins / (len(xs) * len(ys))


def _mann_whitney_u(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float]:
    if not xs or not ys:
        return 0.0, 1.0
    if _mean(xs) > _mean(ys):
        alternative = "greater"
    elif _mean(xs) < _mean(ys):
        alternative = "less"
    else:
        return 0.0, 1.0
    result = stats.mannwhitneyu(xs, ys, alternative=alternative, method="auto")
    return float(result.statistic), float(result.pvalue)


def _wilcoxon_signed_rank(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float]:
    if not xs or not ys:
        return 0.0, 1.0
    pairs = [(x, y) for x, y in zip(xs, ys) if x != y]
    if not pairs:
        return 0.0, 1.0
    x_values = [x for x, _ in pairs]
    y_values = [y for _, y in pairs]
    if _mean(x_values) > _mean(y_values):
        alternative = "greater"
    elif _mean(x_values) < _mean(y_values):
        alternative = "less"
    else:
        return 0.0, 1.0
    result = stats.wilcoxon(
        x_values,
        y_values,
        alternative=alternative,
        zero_method="wilcox",
        method="auto",
    )
    return float(result.statistic), float(result.pvalue)


def _wilcoxon_noninferiority(
    deltas: Sequence[float],
    noninferiority_delta: float,
) -> Tuple[float, float]:
    shifted = [float(delta) + noninferiority_delta for delta in deltas]
    shifted = [value for value in shifted if value != 0.0]
    if not shifted:
        return 0.0, 1.0
    result = stats.wilcoxon(
        shifted,
        alternative="greater",
        zero_method="wilcox",
        method="auto",
    )
    return float(result.statistic), float(result.pvalue)


def _mann_whitney_noninferiority(
    xs: Sequence[float],
    ys: Sequence[float],
    noninferiority_delta: float,
) -> Tuple[float, float]:
    if not xs or not ys:
        return 0.0, 1.0
    shifted_xs = [float(value) + noninferiority_delta for value in xs]
    result = stats.mannwhitneyu(
        shifted_xs,
        [float(value) for value in ys],
        alternative="greater",
        method="auto",
    )
    return float(result.statistic), float(result.pvalue)


def _holm_adjust(p_values: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [1.0] * len(p_values)
    running = 0.0
    m = len(p_values)
    for rank, (idx, p_value) in enumerate(indexed):
        running = max(running, min(1.0, (m - rank) * p_value))
        adjusted[idx] = running
    return adjusted


def _is_target_family_campaign(campaign_name: str) -> bool:
    return campaign_name.startswith("by_target/") and "/by_test/" not in campaign_name


def _is_by_test_campaign(campaign_name: str) -> bool:
    return campaign_name.startswith("by_test/") or "/by_test/" in campaign_name


def per_sample_relscores(
    campaign: Dict[str, Dict[str, Set[str]]],
) -> Dict[str, Dict[str, float]]:
    approach_count = len(campaign)
    union_by_approach = {
        approach: set().union(*trials.values()) if trials else set()
        for approach, trials in campaign.items()
    }
    approaches_hitting_edge: "Counter[str]" = Counter()
    for union in union_by_approach.values():
        approaches_hitting_edge.update(union)
    scores: Dict[str, Dict[str, float]] = {}
    for approach, trials in campaign.items():
        scores[approach] = {
            trial_id: float(
                sum(approach_count - approaches_hitting_edge[edge] for edge in edges)
            )
            for trial_id, edges in trials.items()
        }
    return scores


def trial_scores(
    campaign: Dict[str, Dict[str, Set[str]]],
) -> Dict[str, Dict[str, float]]:
    return per_sample_relscores(campaign)


def _relcov_samples(
    dc: DifferentialCoverage[str, str, str],
    approach: str,
    reference: str,
    sample_ids: Optional[Sequence[str]] = None,
) -> List[float]:
    if approach not in dc.approaches or reference not in dc.approaches:
        return []
    reference_data = dc.approaches[reference]
    if not reference_data.edges_union:
        return []
    trials = dc.approaches[approach].edges_by_trial
    ids = list(sample_ids) if sample_ids is not None else list(trials.keys())

    samples: List[float] = []
    for trial_id in ids:
        if trial_id not in trials:
            continue
        edges = set(trials[trial_id])
        if not edges:
            samples.append(0.0)
            continue
        samples.append(float(ApproachData({trial_id: edges}).relcov(reference_data)))
    return samples


def _paired_deltas(xs: Sequence[float], ys: Sequence[float]) -> List[float]:
    return [float(x) - float(y) for x, y in zip(xs, ys)]


def _bca_unpaired_mean_diff_ci(
    xs: Sequence[float],
    ys: Sequence[float],
    confidence_level: float,
    min_samples: int,
    iterations: int = 2000,
) -> Tuple[Optional[float], Optional[float], str]:
    finite_xs = [float(value) for value in xs if math.isfinite(float(value))]
    finite_ys = [float(value) for value in ys if math.isfinite(float(value))]
    if len(finite_xs) < min_samples or len(finite_ys) < min_samples:
        return None, None, "insufficient n"
    theta_hat = _mean(finite_xs) - _mean(finite_ys)
    if len(set(finite_xs)) == 1 and len(set(finite_ys)) == 1:
        return theta_hat, theta_hat, "ok"

    rng = random.Random(8675309)
    nx = len(finite_xs)
    ny = len(finite_ys)
    boot = sorted(
        _mean([finite_xs[rng.randrange(nx)] for _ in range(nx)])
        - _mean([finite_ys[rng.randrange(ny)] for _ in range(ny)])
        for _ in range(iterations)
    )

    less = sum(1 for value in boot if value < theta_hat)
    equal = sum(1 for value in boot if value == theta_hat)
    prop_less = min(max((less + 0.5 * equal) / iterations, 1e-9), 1.0 - 1e-9)
    z0 = float(stats.norm.ppf(prop_less))

    jack: List[float] = []
    if nx > 1:
        jack.extend(
            _mean([value for idx, value in enumerate(finite_xs) if idx != leave_out])
            - _mean(finite_ys)
            for leave_out in range(nx)
        )
    if ny > 1:
        jack.extend(
            _mean(finite_xs)
            - _mean([value for idx, value in enumerate(finite_ys) if idx != leave_out])
            for leave_out in range(ny)
        )
    jack_mean = _mean(jack)
    numerator = sum((jack_mean - value) ** 3 for value in jack)
    denominator = 6.0 * (
        sum((jack_mean - value) ** 2 for value in jack) ** 1.5
    )
    acceleration = numerator / denominator if denominator else 0.0

    alpha = (1.0 - confidence_level) / 2.0

    def adjusted_quantile(raw_alpha: float) -> float:
        z_alpha = float(stats.norm.ppf(raw_alpha))
        denom = 1.0 - acceleration * (z0 + z_alpha)
        if denom == 0.0:
            return raw_alpha
        return float(stats.norm.cdf(z0 + (z0 + z_alpha) / denom))

    return (
        _percentile(boot, adjusted_quantile(alpha)),
        _percentile(boot, adjusted_quantile(1.0 - alpha)),
        "ok",
    )


def _statistical_verdict(row: Dict[str, Any], confidence_level: float) -> str:
    if row["too_few_samples"]:
        return "inconclusive"
    if row["pairing_mode"] == "paired" and not row["paired"]:
        return "inconclusive"

    alpha = 1.0 - confidence_level
    p_adjusted = float(row["p_value_adjusted"])
    feature_mean = float(row["feature_sample_mean"])
    baseline_mean = float(row["baseline_sample_mean"])
    a12 = float(row["effect_size_a12"])

    significant = p_adjusted <= alpha
    relscore_up = significant and feature_mean > baseline_mean and a12 >= A12_MEANINGFUL_HIGH
    relscore_down = significant and feature_mean < baseline_mean and a12 <= A12_MEANINGFUL_LOW
    relcov_status = row.get("relcov_status", "inconclusive")

    if relscore_down or relcov_status == "failed":
        return "regression"
    if relscore_up and relcov_status == "held":
        return "improvement"
    if relscore_up:
        return "needs-review"
    return "inconclusive"


def _verdict_reason(row: Dict[str, Any]) -> str:
    if row["pairing_mode"] == "paired" and not row["paired"]:
        return "paired mode requires matched seed labels; unpaired fallback disabled"
    if row["too_few_samples"]:
        return "too few runs"
    relcov_status = row.get("relcov_status", "inconclusive")
    relcov_ci_status = row.get("relcov_delta_ci_status", "ok")
    verdict = row["verdict"]
    if verdict == "improvement":
        return "significant relscore improvement and relcov non-inferiority held"
    if verdict == "regression":
        if relcov_status == "failed":
            return "relcov non-inferiority failed"
        return "significant relscore regression"
    if verdict == "needs-review":
        if relcov_ci_status != "ok":
            return f"significant relscore improvement with relcov {relcov_ci_status}"
        return "significant relscore improvement with relcov non-inferiority inconclusive"
    return "not significant after correction"


def build_differential_coverage_verdict_rows(
    summary_rows: List[Tuple[str, str, str, float, float]],
    campaigns: Dict[str, Dict[str, Dict[str, Set[str]]]],
    confidence_level: float = 0.95,
    noninferiority_delta: float = DEFAULT_RELCOV_NONINFERIORITY_DELTA,
    pairing_mode: str = "unpaired",
    min_samples: int = DEFAULT_MIN_VERDICT_SAMPLES,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if pairing_mode not in {"unpaired", "paired"}:
        raise ValueError(f"unsupported differential coverage pairing mode: {pairing_mode}")
    rows: List[Dict[str, Any]] = []
    for (
        campaign_name,
        baseline,
        feature,
        baseline_relscore,
        feature_relscore,
    ) in summary_rows:
        campaign = campaigns.get(campaign_name, {})
        if not campaign or baseline not in campaign or feature not in campaign:
            continue
        dc = DifferentialCoverage(campaign)
        feature_performance = float(dc.approaches[feature].relcov(dc.approaches[baseline]))
        baseline_reliability = float(dc.approaches[baseline].relcov(dc.approaches[baseline]))
        relscores_by_sample = per_sample_relscores(campaign)
        baseline_scores_by_sample = relscores_by_sample.get(baseline, {})
        feature_scores_by_sample = relscores_by_sample.get(feature, {})
        shared_ids = sorted(set(baseline_scores_by_sample) & set(feature_scores_by_sample))
        baseline_seed_ids = {
            sample_id for sample_id in baseline_scores_by_sample if sample_id.startswith("seed-")
        }
        feature_seed_ids = {
            sample_id for sample_id in feature_scores_by_sample if sample_id.startswith("seed-")
        }
        seed_pairing_expected = pairing_mode == "paired"
        expected_pairs = (
            min(len(baseline_seed_ids), len(feature_seed_ids))
            if seed_pairing_expected
            else 0
        )
        pairing_rate = len(shared_ids) / expected_pairs if expected_pairs else 0.0
        paired = seed_pairing_expected and expected_pairs > 0 and bool(shared_ids)
        if paired:
            sample_ids = shared_ids
            baseline_scores = [baseline_scores_by_sample[sample_id] for sample_id in sample_ids]
            feature_scores = [feature_scores_by_sample[sample_id] for sample_id in sample_ids]
        elif seed_pairing_expected:
            sample_ids = []
            baseline_scores = []
            feature_scores = []
        else:
            sample_ids = sorted(feature_scores_by_sample)
            baseline_scores = list(baseline_scores_by_sample.values())
            feature_scores = list(feature_scores_by_sample.values())
        statistic, p_value = (
            _wilcoxon_signed_rank(feature_scores, baseline_scores)
            if paired
            else (0.0, 1.0)
            if seed_pairing_expected
            else _mann_whitney_u(feature_scores, baseline_scores)
        )
        feature_retention_samples = _relcov_samples(
            dc,
            feature,
            baseline,
            sample_ids if paired else None,
        )
        baseline_reliability_samples = _relcov_samples(
            dc,
            baseline,
            baseline,
            sample_ids if paired else None,
        )
        if paired:
            relcov_deltas = _paired_deltas(
                feature_retention_samples,
                baseline_reliability_samples,
            )
            relcov_statistic, relcov_p_value = _wilcoxon_noninferiority(
                relcov_deltas,
                noninferiority_delta,
            )
            relcov_delta_ci = _bca_mean_ci(
                relcov_deltas,
                confidence_level,
                min_samples,
            )
        elif seed_pairing_expected:
            relcov_deltas = []
            relcov_statistic, relcov_p_value = 0.0, 1.0
            relcov_delta_ci = (None, None, "insufficient n")
        else:
            relcov_deltas = []
            relcov_statistic, relcov_p_value = _mann_whitney_noninferiority(
                feature_retention_samples,
                baseline_reliability_samples,
                noninferiority_delta,
            )
            relcov_delta_ci = _bca_unpaired_mean_diff_ci(
                feature_retention_samples,
                baseline_reliability_samples,
                confidence_level,
                min_samples,
            )
        n_samples = (
            len(shared_ids)
            if paired
            else 0
            if seed_pairing_expected
            else min(len(feature_scores), len(baseline_scores))
        )
        seed_ids = baseline_seed_ids | feature_seed_ids
        n_seeds = len(shared_ids) if paired else len(seed_ids)
        too_few_samples = n_samples < min_samples
        if (
            seed_pairing_expected and not paired
        ) or too_few_samples or relcov_delta_ci[2] != "ok":
            relcov_status = "inconclusive"
        elif relcov_delta_ci[1] is not None and relcov_delta_ci[1] < -noninferiority_delta:
            relcov_status = "failed"
        elif relcov_delta_ci[0] is not None and relcov_delta_ci[0] >= -noninferiority_delta:
            relcov_status = "held"
        else:
            relcov_status = "inconclusive"
        if seed_pairing_expected and not paired:
            reason = "paired mode requires matched seed labels; unpaired fallback disabled"
        elif seed_pairing_expected and pairing_rate < 0.8:
            reason = f"low matched seed rate {len(shared_ids)}/{expected_pairs}; use verdict with caution"
        elif too_few_samples:
            reason = "too few runs"
        else:
            reason = "not significant after correction"
        test_name = (
            "wilcoxon-signed-rank"
            if paired
            else "paired-required"
            if seed_pairing_expected
            else "mann-whitney-u"
        )
        base_row = {
            "campaign": campaign_name,
            "baseline": baseline,
            "feature": feature,
            "baseline_relscore": baseline_relscore,
            "feature_relscore": feature_relscore,
            "pairing_mode": pairing_mode,
            "n_trials": n_samples,
            "n_samples": n_samples,
            "n_seeds": n_seeds,
            "paired": paired,
            "pairing_rate": pairing_rate,
            "too_few_samples": too_few_samples,
            "test_name": test_name,
            "statistic": statistic,
            "p_value": p_value,
            "effect_size_a12": _a12(feature_scores, baseline_scores),
            "baseline_sample_mean": _mean(baseline_scores),
            "feature_sample_mean": _mean(feature_scores),
            "baseline_reliability": baseline_reliability,
            "feature_performance": feature_performance,
            "noninferiority_delta": noninferiority_delta,
            "relcov_delta_ci_low": relcov_delta_ci[0],
            "relcov_delta_ci_high": relcov_delta_ci[1],
            "relcov_delta_ci_status": relcov_delta_ci[2],
            "relcov_status": relcov_status,
            "missing_count": 0,
            "reason": reason,
        }
        rows.append({**base_row, "metric": "relscore"})
        rows.append(
            {
                **base_row,
                "metric": "relcov",
                "test_name": (
                    "wilcoxon-noninferiority"
                    if paired
                    else "paired-required"
                    if seed_pairing_expected
                    else "mann-whitney-u-noninferiority"
                ),
                "statistic": relcov_statistic,
                "p_value": relcov_p_value,
                "effect_size_a12": _a12(
                    feature_retention_samples,
                    baseline_reliability_samples,
                ),
                "baseline_sample_mean": _mean(baseline_reliability_samples),
                "feature_sample_mean": _mean(feature_retention_samples),
            }
        )
    for row in rows:
        row["p_value_adjusted"] = row["p_value"]
    target_indices = [
        idx
        for idx, row in enumerate(rows)
        if row["metric"] == "relscore"
        and _is_target_family_campaign(str(row["campaign"]))
    ]
    target_adjusted = _holm_adjust([float(rows[idx]["p_value"]) for idx in target_indices])
    for idx, p_adjusted in zip(target_indices, target_adjusted):
        rows[idx]["p_value_adjusted"] = p_adjusted
    target_relcov_indices = [
        idx
        for idx, row in enumerate(rows)
        if row["metric"] == "relcov"
        and _is_target_family_campaign(str(row["campaign"]))
    ]
    target_relcov_adjusted = _holm_adjust(
        [float(rows[idx]["p_value"]) for idx in target_relcov_indices]
    )
    for idx, p_adjusted in zip(target_relcov_indices, target_relcov_adjusted):
        rows[idx]["p_value_adjusted"] = p_adjusted

    verdict_by_campaign: Dict[Tuple[str, str], str] = {}
    reason_by_campaign: Dict[Tuple[str, str], str] = {}
    for row in rows:
        if row["metric"] != "relscore":
            continue
        key = (str(row["campaign"]), str(row["feature"]))
        verdict = _statistical_verdict(row, confidence_level)
        row["verdict"] = verdict
        row["reason"] = _verdict_reason(row)
        verdict_by_campaign[key] = verdict
        reason_by_campaign[key] = row["reason"]
    for row in rows:
        key = (str(row["campaign"]), str(row["feature"]))
        row["verdict"] = verdict_by_campaign.get(key, "inconclusive")
        row["reason"] = reason_by_campaign.get(key, row["reason"])

    target_verdict_rows = [
        row
        for row in rows
        if row["metric"] == "relscore" and _is_target_family_campaign(str(row["campaign"]))
    ]
    target_count = len(target_verdict_rows)
    improvement_count = sum(1 for row in target_verdict_rows if row["verdict"] == "improvement")
    relcov_held_for_all_targets = target_count > 0 and all(
        row["relcov_status"] == "held"
        for row in target_verdict_rows
    )
    aggregate_verdict = "inconclusive"
    if any(row["verdict"] == "regression" for row in target_verdict_rows):
        aggregate_verdict = "regression"
    elif any(row["verdict"] == "needs-review" for row in target_verdict_rows):
        aggregate_verdict = "needs-review"
    elif (
        target_count > 0
        and improvement_count > target_count / 2
        and relcov_held_for_all_targets
    ):
        aggregate_verdict = "improvement"
    campaign_rows = [row for row in rows if row["metric"] == "relscore"]

    verdict_state = {
        "version": 1,
        "confidence_level": confidence_level,
        "relcov_noninferiority_delta": noninferiority_delta,
        "min_samples": min_samples,
        "aggregate": {
            "winner": "",
            "verdict": aggregate_verdict,
            "eligible_count": len(campaign_rows),
            "total_samples": sum(int(row["n_samples"]) for row in campaign_rows),
        },
    }
    return rows, verdict_state


def build_differential_coverage_summary_rows(
    campaign_name: str,
    relscores: Dict[str, float],
    relcovs: Dict[str, Dict[str, float]],
) -> List[Tuple[str, str, str, float, float]]:
    pair = find_baseline_feature(relcovs.keys())
    if pair is None:
        return []
    baseline, feature = pair
    if baseline not in relscores or feature not in relscores:
        return []

    baseline_relscore = relscores[baseline]
    feature_relscore = relscores[feature]
    return [
        (
            campaign_name,
            baseline,
            feature,
            baseline_relscore,
            feature_relscore,
        )
    ]


def showmap_campaign_summary(
    campaign: Dict[str, Dict[str, Set[str]]],
) -> Dict[str, Dict[str, int]]:
    summary: Dict[str, Dict[str, int]] = {}
    for approach, trials in campaign.items():
        covered_edges: Set[str] = set()
        for edges in trials.values():
            covered_edges.update(edges)
        summary[approach] = {
            "trials": len(trials),
            "covered_edges": len(covered_edges),
        }
    return summary


def showmap_max_work_items_from_env() -> int:
    raw = os.environ.get("SCFUZZBENCH_DIFFCOV_MAX_WORK_ITEMS")
    if raw is None or raw == "":
        return DEFAULT_SHOWMAP_MAX_WORK_ITEMS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SHOWMAP_MAX_WORK_ITEMS
    return max(value, 0)


def diffcov_verdict_by_test_from_env() -> bool:
    raw = os.environ.get("SCFUZZBENCH_DIFFCOV_VERDICT_BY_TEST")
    if raw is None or raw == "":
        return DEFAULT_DIFFCOV_VERDICT_BY_TEST
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _format_csv_float(value: Any) -> str:
    if value is None:
        return "insufficient n"
    return f"{float(value):.6f}"


def write_differential_coverage_outputs(
    logs_dir: Path,
    out_dir: Path,
    excluded_fuzzers: Optional[Set[str]] = None,
    max_work_items: Optional[int] = None,
    pairing_mode: str = "unpaired",
    confidence_level: float = 0.95,
    min_samples: int = DEFAULT_MIN_VERDICT_SAMPLES,
    noninferiority_delta: float = DEFAULT_RELCOV_NONINFERIORITY_DELTA,
    verdict_by_test: Optional[bool] = None,
) -> None:
    if verdict_by_test is None:
        verdict_by_test = diffcov_verdict_by_test_from_env()
    trials, skipped = load_showmap_trials(logs_dir, excluded_fuzzers)
    campaigns = build_showmap_campaigns(trials)
    campaign_root = out_dir / "showmap_campaigns"
    if campaign_root.exists():
        shutil.rmtree(campaign_root)

    relscore_rows: List[Tuple[str, str, float, int, int]] = []
    relcov_rows: List[Tuple[str, str, str, float]] = []
    summary_rows: List[Tuple[str, str, str, float, float]] = []
    manifest: Dict[str, Any] = {
        "raw_trials": len(trials),
        "skipped": skipped,
        "campaigns": {},
    }
    if max_work_items is None:
        max_work_items = showmap_max_work_items_from_env()
    manifest["max_work_items"] = max_work_items
    manifest["verdict_by_test"] = verdict_by_test

    for campaign_name, campaign in sorted(campaigns.items()):
        if not campaign:
            continue
        start = time.perf_counter()
        campaign_dir = campaign_root / campaign_name
        write_showmap_campaign_dir(campaign, campaign_dir)
        summary = showmap_campaign_summary(campaign)
        work_items = showmap_campaign_work_items(campaign)
        manifest["campaigns"][campaign_name] = {
            "approaches": summary,
            "work_items": work_items,
        }
        if campaign_name != "combined" and max_work_items and work_items > max_work_items:
            manifest["campaigns"][campaign_name]["skipped_analysis"] = (
                f"work_items {work_items} exceeds max_work_items {max_work_items}"
            )
            continue
        relscores = calculate_relscores(campaign)
        for approach, score in sorted(
            relscores.items(), key=lambda item: (-item[1], item[0])
        ):
            relscore_rows.append(
                (
                    campaign_name,
                    approach,
                    score,
                    summary[approach]["trials"],
                    summary[approach]["covered_edges"],
                )
            )
        relcovs = calculate_relcovs(campaign)
        for approach, references in sorted(relcovs.items()):
            for reference, relcov in sorted(references.items()):
                if approach == reference:
                    continue
                relcov_rows.append((campaign_name, approach, reference, relcov))
        summary_rows.extend(
            build_differential_coverage_summary_rows(campaign_name, relscores, relcovs)
        )
        manifest["campaigns"][campaign_name]["analysis_seconds"] = round(
            time.perf_counter() - start, 6
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    relscore_csv = out_dir / "differential_coverage_relscores.csv"
    with relscore_csv.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["campaign", "approach", "relscore", "trials", "covered_edges"])
        for campaign_name, approach, score, trials_count, covered_edges in relscore_rows:
            writer.writerow(
                [campaign_name, approach, f"{score:.6f}", trials_count, covered_edges]
            )

    relcov_csv = out_dir / "differential_coverage_relcov.csv"
    with relcov_csv.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["campaign", "approach", "reference_approach", "relcov"])
        for campaign_name, approach, reference, relcov in relcov_rows:
            writer.writerow([campaign_name, approach, reference, f"{relcov:.6f}"])

    summary_csv = out_dir / "differential_coverage_summary.csv"
    # Only the per-target and combined campaigns drive verdicts; `by_test`
    # campaigns are drill-down only. Running the bootstrap statistics for each
    # invariant across every target/round/arm is the dominant analysis cost, so
    # exclude them from the verdict path by default. Their point relscore/relcov
    # values still land in the dedicated CSVs and as cheap inconclusive summary
    # rows below (verdict_by_key has no entry, so they default to inconclusive).
    verdict_summary_rows = (
        summary_rows
        if verdict_by_test
        else [row for row in summary_rows if not _is_by_test_campaign(row[0])]
    )
    verdict_rows, verdict_state = build_differential_coverage_verdict_rows(
        verdict_summary_rows,
        campaigns,
        confidence_level=confidence_level,
        pairing_mode=pairing_mode,
        min_samples=min_samples,
        noninferiority_delta=noninferiority_delta,
    )
    verdict_by_key = {
        (row["campaign"], row["feature"], row["metric"]): row for row in verdict_rows
    }
    with summary_csv.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "campaign",
                "metric",
                "baseline",
                "feature",
                "verdict",
                "verdict_reason",
                "baseline_relscore",
                "feature_relscore",
                "pairing_mode",
                "n_trials",
                "n_samples",
                "n_seeds",
                "paired",
                "pairing_rate",
                "too_few_samples",
                "test_name",
                "statistic",
                "p_value",
                "p_value_adjusted",
                "effect_size_a12",
                "baseline_sample_mean",
                "feature_sample_mean",
                "baseline_reliability",
                "feature_performance",
                "noninferiority_delta",
                "relcov_delta_ci_low",
                "relcov_delta_ci_high",
                "relcov_delta_ci_status",
                "relcov_status",
                "missing_count",
            ]
        )
        for source_row in summary_rows:
            (
                campaign_name,
                baseline,
                feature,
                baseline_relscore,
                feature_relscore,
            ) = source_row
            for metric in ("relscore", "relcov"):
                verdict_row = verdict_by_key.get((campaign_name, feature, metric), {})
                writer.writerow(
                    [
                        campaign_name,
                        metric,
                        baseline,
                        feature,
                        verdict_row.get("verdict", "inconclusive"),
                        verdict_row.get("reason", ""),
                        f"{baseline_relscore:.6f}",
                        f"{feature_relscore:.6f}",
                        verdict_row.get("pairing_mode", pairing_mode),
                        verdict_row.get("n_trials", ""),
                        verdict_row.get("n_samples", ""),
                        verdict_row.get("n_seeds", ""),
                        str(verdict_row.get("paired", "")).lower(),
                        f"{float(verdict_row.get('pairing_rate', 0.0)):.6f}" if verdict_row else "",
                        str(verdict_row.get("too_few_samples", "")).lower(),
                        verdict_row.get("test_name", ""),
                        _format_csv_float(verdict_row.get("statistic", 0.0)) if verdict_row else "",
                        _format_csv_float(verdict_row.get("p_value", 1.0)) if verdict_row else "",
                        _format_csv_float(verdict_row.get("p_value_adjusted", 1.0)) if verdict_row else "",
                        _format_csv_float(verdict_row.get("effect_size_a12", 0.5)) if verdict_row else "",
                        _format_csv_float(verdict_row.get("baseline_sample_mean", 0.0)) if verdict_row else "",
                        _format_csv_float(verdict_row.get("feature_sample_mean", 0.0)) if verdict_row else "",
                        _format_csv_float(verdict_row.get("baseline_reliability")) if verdict_row else "",
                        _format_csv_float(verdict_row.get("feature_performance")) if verdict_row else "",
                        _format_csv_float(verdict_row.get("noninferiority_delta")) if verdict_row else "",
                        _format_csv_float(verdict_row.get("relcov_delta_ci_low")) if verdict_row else "",
                        _format_csv_float(verdict_row.get("relcov_delta_ci_high")) if verdict_row else "",
                        verdict_row.get("relcov_delta_ci_status", ""),
                        verdict_row.get("relcov_status", ""),
                        verdict_row.get("missing_count", ""),
                    ]
                )

    statistics_path = out_dir / "differential_coverage_statistics.json"
    with statistics_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema": "scfuzzbench.differential_coverage.statistics.v1",
                "pairing_mode": pairing_mode,
                "rows": verdict_rows,
                "state": verdict_state,
            },
            handle,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")

    manifest_path = out_dir / "showmap_campaign_manifest.json"
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze scfuzzbench logs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_parser = subparsers.add_parser("parse", help="Parse logs to CSV.")
    parse_parser.add_argument("--logs-dir", required=True, type=Path)
    parse_parser.add_argument("--run-id", default=None)
    parse_parser.add_argument("--out-csv", required=True, type=Path)
    parse_parser.add_argument(
        "--raw-labels",
        action="store_true",
        help="Use raw directory names as fuzzer labels instead of normalizing.",
    )

    run_parser = subparsers.add_parser("run", help="Parse logs and write CSVs.")
    run_parser.add_argument("--logs-dir", required=True, type=Path)
    run_parser.add_argument("--run-id", default=None)
    run_parser.add_argument("--out-dir", required=True, type=Path)
    run_parser.add_argument(
        "--raw-labels",
        action="store_true",
        help="Use raw directory names as fuzzer labels instead of normalizing.",
    )
    run_parser.add_argument(
        "--pairing-mode",
        choices=["unpaired", "paired"],
        default="unpaired",
        help="Differential coverage test mode: independent rounds or explicit paired runs.",
    )
    run_parser.add_argument(
        "--confidence-level",
        type=float,
        default=0.95,
        help="Confidence level for differential coverage intervals and tests.",
    )
    run_parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_VERDICT_SAMPLES,
        help="Minimum per-arm samples required before a statistical verdict can be conclusive.",
    )
    run_parser.add_argument(
        "--noninferiority-delta",
        type=float,
        default=DEFAULT_RELCOV_NONINFERIORITY_DELTA,
        help="Allowed absolute relcov loss versus baseline reliability before coverage is a regression.",
    )
    run_parser.add_argument(
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
    return parser.parse_args()


def _apply_raw_labels_events(events: List[Event]) -> List[Event]:
    """Replace normalized fuzzer with the raw fuzzer_label."""
    return [replace(e, fuzzer=e.fuzzer_label) for e in events]


def _apply_raw_labels_throughput(
    samples: List[ThroughputSample],
) -> List[ThroughputSample]:
    return [replace(s, fuzzer=s.fuzzer_label) for s in samples]


def _apply_raw_labels_progress(
    samples: List[ProgressMetricsSample],
) -> List[ProgressMetricsSample]:
    return [replace(s, fuzzer=s.fuzzer_label) for s in samples]


def main() -> int:
    args = parse_args()
    raw_labels = getattr(args, "raw_labels", False)
    if args.command == "parse":
        log_files = discover_log_files(args.logs_dir)
        events = parse_logs(args.logs_dir, args.run_id, log_files)
        if raw_labels:
            events = _apply_raw_labels_events(events)
        write_events_csv(events, args.out_csv)
        return 0
    if args.command == "run":
        out_dir: Path = args.out_dir
        log_files = discover_log_files(args.logs_dir)
        events = parse_logs(args.logs_dir, args.run_id, log_files)
        throughput_samples = parse_throughput_logs(
            args.logs_dir, args.run_id, log_files
        )
        progress_metrics_samples = parse_progress_metrics_logs(
            args.logs_dir, args.run_id, log_files
        )
        if raw_labels:
            events = _apply_raw_labels_events(events)
            throughput_samples = _apply_raw_labels_throughput(throughput_samples)
            progress_metrics_samples = _apply_raw_labels_progress(
                progress_metrics_samples
            )
        events_csv = out_dir / "events.csv"
        summary_csv = out_dir / "summary.csv"
        overlap_csv = out_dir / "overlap.csv"
        exclusive_csv = out_dir / "exclusive.csv"
        throughput_samples_csv = out_dir / "throughput_samples.csv"
        throughput_summary_csv = out_dir / "throughput_summary.csv"
        progress_metrics_samples_csv = out_dir / "progress_metrics_samples.csv"
        progress_metrics_summary_csv = out_dir / "progress_metrics_summary.csv"
        write_events_csv(events, events_csv)
        write_summary_csv(events, summary_csv)
        write_overlap_csv(events, overlap_csv)
        write_exclusive_csv(events, exclusive_csv)
        write_throughput_samples_csv(throughput_samples, throughput_samples_csv)
        write_throughput_summary_csv(throughput_samples, throughput_summary_csv)
        write_progress_metrics_samples_csv(
            progress_metrics_samples, progress_metrics_samples_csv
        )
        write_progress_metrics_summary_csv(
            progress_metrics_samples, progress_metrics_summary_csv
        )
        write_differential_coverage_outputs(
            args.logs_dir,
            out_dir,
            pairing_mode=args.pairing_mode,
            confidence_level=args.confidence_level,
            min_samples=args.min_samples,
            noninferiority_delta=args.noninferiority_delta,
            verdict_by_test=args.verdict_by_test,
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
