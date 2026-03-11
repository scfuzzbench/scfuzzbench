#!/usr/bin/env python3
import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOG_FILE_RE = re.compile(r".+\.log$")
INSTANCE_PREFIX_RE = re.compile(r"^(i-[0-9a-f]+)-(.*)$")
IGNORED_LOG_FILENAMES = {"runner_commands.log"}
ABS_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} [0-9:.]+)\]")
MEDUSA_ELAPSED_RE = re.compile(r"elapsed:\s*([0-9hms]+)")
FOUNDATION_JSON_RE = re.compile(r"^\s*\{.*\}\s*$")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
FALSIFIED_RE = re.compile(r"Test\s+([^\s]+)\s+falsified!")
ECHIDNA_FAILED_RE = re.compile(r"^([A-Za-z0-9_]+)\([^)]*\):\s+failed!")
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
        if part.isdigit() and len(part) >= 8:
            return part
    return None


def split_instance_label(label: str) -> Tuple[str, str]:
    match = INSTANCE_PREFIX_RE.match(label)
    if match:
        return match.group(1), match.group(2)
    return "unknown", label


def normalize_fuzzer(fuzzer_label: str) -> str:
    lower = fuzzer_label.lower()
    if "echidna" in lower and "symexec" in lower:
        return "echidna-symexec"
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
    candidate = after.strip()
    for sep in ("»", "\"", ")"):
        if sep in candidate:
            candidate = candidate.split(sep, 1)[0].strip()
    candidate = candidate.strip()
    if not candidate:
        return None
    return candidate


def parse_foundry_log(
    path: Path, run_id: str, instance_id: str, fuzzer_label: str
) -> List[Event]:
    events: List[Event] = []
    seen = set()
    first_ts: Optional[float] = None
    with path.open("r", errors="ignore") as handle:
        for line in handle:
            clean_line = ANSI_ESCAPE_RE.sub("", line)
            if FOUNDATION_JSON_RE.match(clean_line):
                try:
                    payload = json.loads(clean_line)
                except json.JSONDecodeError:
                    payload = None
                if payload is not None:
                    invariant = payload.get("invariant")
                    ts = payload.get("timestamp")
                    if invariant and ts is not None:
                        try:
                            ts_value = float(ts)
                        except (TypeError, ValueError):
                            ts_value = None
                        if ts_value is not None:
                            if first_ts is None:
                                # Foundry emits epoch timestamps; use the first seen
                                # timestamp as the baseline so elapsed_seconds measures
                                # time since the run began, not time since the first
                                # failure.
                                first_ts = ts_value

                            if payload.get("type") == "invariant_failure" and invariant not in seen:
                                seen.add(invariant)
                                events.append(
                                    Event(
                                        run_id=run_id,
                                        instance_id=instance_id,
                                        fuzzer=normalize_fuzzer(fuzzer_label),
                                        fuzzer_label=fuzzer_label,
                                        event=invariant,
                                        elapsed_seconds=ts_value - first_ts,
                                        source="foundry-invariant-failure",
                                        log_path=str(path),
                                    )
                                )
                    continue
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


def parse_logs(logs_dir: Path, run_id: Optional[str]) -> List[Event]:
    events: List[Event] = []
    run_id_value = run_id or infer_run_id(logs_dir) or "unknown"
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
        fuzzer = normalize_fuzzer(fuzzer_label)
        if fuzzer == "foundry":
            events.extend(parse_foundry_log(path, run_id_value, instance_id, fuzzer_label))
        elif fuzzer == "medusa":
            events.extend(parse_medusa_log(path, run_id_value, instance_id, fuzzer_label))
        elif fuzzer in ("echidna", "echidna-symexec"):
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
        else:
            events.extend(parse_generic_log(path, run_id_value, instance_id, fuzzer_label))
    return events


def parse_throughput_logs(logs_dir: Path, run_id: Optional[str]) -> List[ThroughputSample]:
    samples: List[ThroughputSample] = []
    run_id_value = run_id or infer_run_id(logs_dir) or "unknown"
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
        samples.extend(parse_throughput_log(path, run_id_value, instance_id, fuzzer_label))
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
    logs_dir: Path, run_id: Optional[str]
) -> List[ProgressMetricsSample]:
    samples: List[ProgressMetricsSample] = []
    run_id_value = run_id or infer_run_id(logs_dir) or "unknown"
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
        samples.extend(
            parse_progress_metrics_log(path, run_id_value, instance_id, fuzzer_label)
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
        events = parse_logs(args.logs_dir, args.run_id)
        if raw_labels:
            events = _apply_raw_labels_events(events)
        write_events_csv(events, args.out_csv)
        return 0
    if args.command == "run":
        out_dir: Path = args.out_dir
        events = parse_logs(args.logs_dir, args.run_id)
        throughput_samples = parse_throughput_logs(args.logs_dir, args.run_id)
        progress_metrics_samples = parse_progress_metrics_logs(
            args.logs_dir, args.run_id
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
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
