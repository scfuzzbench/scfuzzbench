#!/usr/bin/env python3
"""Post-run function-selector analytics for saved fuzzer corpora.

The four runners do not expose equivalent runtime telemetry. Echidna and Recon
persist typed Solidity calls, Medusa persists ABI-labelled transactions, and
Foundry persists raw calldata only when corpus persistence is enabled. This
module keeps those provenance differences visible instead of manufacturing
data for a tool whose corpus is unavailable.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from Crypto.Hash import keccak


SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
INSTANCE_PREFIX_RE = re.compile(r"^(i-[0-9a-f]+)-(.*)$", re.IGNORECASE)
SELECTOR_RE = re.compile(r"^0x[0-9a-fA-F]{8}$")
SIGNATURE_RE = re.compile(r"^([A-Za-z_$][A-Za-z0-9_$]*)\((.*)\)$")

# Forge Std's invariant targeting API and panic-test helpers can appear in
# corpora even though they are harness/framework plumbing, not benchmark
# handlers. They remain visible in distributions but cannot define the
# peer-consensus expectation heuristic.
FRAMEWORK_FUNCTIONS = {
    "IS_TEST",
    "arithmeticError",
    "assertionError",
    "divisionError",
    "encodeStorageError",
    "enumConversionError",
    "excludeArtifacts",
    "excludeContracts",
    "excludeSenders",
    "failed",
    "indexOOBError",
    "memOverflowError",
    "popError",
    "setUp",
    "setup",
    "targetArtifactSelectors",
    "targetArtifacts",
    "targetContracts",
    "targetInterfaces",
    "targetSelectors",
    "targetSenders",
    "zeroVarError",
}
TEST_FUNCTION_PREFIXES = (
    "crytic_",
    "echidna_",
    "invariant_",
    "optimize_",
    "property_",
)


@dataclass(frozen=True)
class SelectorCall:
    selector: Optional[str]
    signature: Optional[str]
    function_name: Optional[str]
    source_format: str


@dataclass
class Observation:
    selector: Optional[str]
    signatures: set[str] = field(default_factory=set)
    function_names: set[str] = field(default_factory=set)
    source_formats: set[str] = field(default_factory=set)
    count: int = 0

    @property
    def comparison_key(self) -> str:
        if self.selector:
            return self.selector
        if len(self.function_names) == 1:
            return f"name:{next(iter(self.function_names))}"
        return "unknown"


@dataclass
class InstanceResult:
    instance_label: str
    instance_id: str
    engine: str
    fuzzer: str
    fuzzer_label: str
    corpus_path: Optional[Path]
    artifact_files: int = 0
    parsed_sequences: int = 0
    duplicate_sequences: int = 0
    malformed_artifacts: int = 0
    ignored_calls: int = 0
    selector_mismatches: int = 0
    calls: list[SelectorCall] = field(default_factory=list)
    status: str = "unavailable"
    warnings: list[str] = field(default_factory=list)


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


def split_instance_label(label: str) -> tuple[str, str]:
    match = INSTANCE_PREFIX_RE.match(label)
    if match:
        return match.group(1), match.group(2)
    return "unknown", label


def normalize_selector(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if SELECTOR_RE.fullmatch(value):
        return value
    return None


def selector_from_calldata(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]*", value) or len(value) < 10:
        return None
    return value[:10].lower()


def normalize_signature(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = SIGNATURE_RE.fullmatch(value)
    if not match:
        return None
    name, arguments = match.groups()
    arguments = re.sub(r"\baddress\s+payable\b", "address", arguments)
    arguments = re.sub(r"\s+", "", arguments)
    arguments = re.sub(r"\buint(?=(?:\[|,|\)|$))", "uint256", arguments)
    arguments = re.sub(r"\bint(?=(?:\[|,|\)|$))", "int256", arguments)
    arguments = re.sub(r"\bbyte(?=(?:\[|,|\)|$))", "bytes1", arguments)
    return f"{name}({arguments})"


def function_name_from_signature(signature: Optional[str]) -> Optional[str]:
    if not signature:
        return None
    match = SIGNATURE_RE.fullmatch(signature)
    return match.group(1) if match else None


def selector_from_signature(signature: Optional[str]) -> Optional[str]:
    normalized = normalize_signature(signature)
    if normalized is None:
        return None
    digest = keccak.new(digest_bits=256)
    digest.update(normalized.encode("utf-8"))
    return f"0x{digest.hexdigest()[:8]}"


def _tagged_contents(value: Any) -> tuple[Optional[str], Any]:
    if not isinstance(value, dict):
        return None, None
    tag = value.get("tag")
    if not isinstance(tag, str):
        return None, None
    return tag, value.get("contents")


def _echidna_abi_type(value: Any) -> Optional[str]:
    tag, contents = _tagged_contents(value)
    if tag == "AbiUInt" and isinstance(contents, list) and contents:
        return f"uint{contents[0]}"
    if tag == "AbiInt" and isinstance(contents, list) and contents:
        return f"int{contents[0]}"
    if tag == "AbiAddress":
        return "address"
    if tag == "AbiBool":
        return "bool"
    if tag == "AbiBytes" and isinstance(contents, list) and contents:
        return f"bytes{contents[0]}"
    if tag == "AbiBytesDynamic":
        return "bytes"
    if tag == "AbiString":
        return "string"
    if tag == "AbiFunction":
        return "function"
    if tag == "AbiTuple" and isinstance(contents, list):
        item_types = [_echidna_abi_type(item) for item in contents]
        if any(item is None for item in item_types):
            return None
        return f"({','.join(item_types)})"
    if tag == "AbiArrayDynamic" and isinstance(contents, list) and contents:
        item_type = _echidna_type_descriptor(contents[0])
        return f"{item_type}[]" if item_type else None
    if tag == "AbiArray" and isinstance(contents, list) and len(contents) >= 2:
        length: Any
        descriptor: Any
        if isinstance(contents[0], int):
            length, descriptor = contents[0], contents[1]
        elif isinstance(contents[1], int):
            descriptor, length = contents[0], contents[1]
        else:
            return None
        item_type = _echidna_type_descriptor(descriptor)
        return f"{item_type}[{length}]" if item_type else None
    return None


def _echidna_type_descriptor(value: Any) -> Optional[str]:
    tag, contents = _tagged_contents(value)
    if tag == "AbiUIntType":
        return f"uint{contents}"
    if tag == "AbiIntType":
        return f"int{contents}"
    if tag == "AbiAddressType":
        return "address"
    if tag == "AbiBoolType":
        return "bool"
    if tag == "AbiBytesType":
        return f"bytes{contents}"
    if tag == "AbiBytesDynamicType":
        return "bytes"
    if tag == "AbiStringType":
        return "string"
    if tag == "AbiFunctionType":
        return "function"
    if tag == "AbiArrayDynamicType":
        item_type = _echidna_type_descriptor(contents)
        return f"{item_type}[]" if item_type else None
    if tag == "AbiArrayType" and isinstance(contents, list) and len(contents) == 2:
        length, descriptor = contents
        item_type = _echidna_type_descriptor(descriptor)
        return f"{item_type}[{length}]" if item_type else None
    if tag == "AbiTupleType" and isinstance(contents, list):
        item_types = [_echidna_type_descriptor(item) for item in contents]
        if any(item is None for item in item_types):
            return None
        return f"({','.join(item_types)})"
    return _echidna_abi_type(value)


def _signature_from_typed_args(
    function_name: Any, args: Any, type_parser
) -> Optional[str]:
    if not isinstance(function_name, str) or not isinstance(args, list):
        return None
    types = [type_parser(arg) for arg in args]
    if any(value is None for value in types):
        return None
    return normalize_signature(f"{function_name}({','.join(types)})")


def _recon_abi_type(value: Any) -> Optional[str]:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    tag, contents = next(iter(value.items()))
    if tag == "Uint" and isinstance(contents, list) and len(contents) >= 2:
        return f"uint{contents[1]}"
    if tag == "Int" and isinstance(contents, list) and len(contents) >= 2:
        return f"int{contents[1]}"
    if tag == "Address":
        return "address"
    if tag == "Bool":
        return "bool"
    if tag == "Bytes":
        if isinstance(contents, list) and len(contents) >= 2:
            size = contents[1] if isinstance(contents[1], int) else contents[0]
            return f"bytes{size}" if isinstance(size, int) else None
        return None
    if tag in {"DynamicBytes", "BytesDynamic"}:
        return "bytes"
    if tag == "String":
        return "string"
    if tag == "Function":
        return "function"
    if tag in {"Array", "DynamicArray"} and isinstance(contents, list):
        item_types = {_recon_abi_type(item) for item in contents}
        item_types.discard(None)
        return f"{next(iter(item_types))}[]" if len(item_types) == 1 else None
    if tag in {"FixedArray", "ArrayFixed"} and isinstance(contents, list):
        values: Any = None
        length: Any = None
        if len(contents) == 2:
            values, length = contents
        if not isinstance(values, list) or not isinstance(length, int):
            return None
        item_types = {_recon_abi_type(item) for item in values}
        item_types.discard(None)
        return (
            f"{next(iter(item_types))}[{length}]" if len(item_types) == 1 else None
        )
    if tag == "Tuple" and isinstance(contents, list):
        item_types = [_recon_abi_type(item) for item in contents]
        if any(item is None for item in item_types):
            return None
        return f"({','.join(item_types)})"
    return None


def _call_from_echidna(item: Any) -> Optional[SelectorCall]:
    if not isinstance(item, dict):
        return None
    call = item.get("call")
    if not isinstance(call, dict):
        return None
    tag, contents = _tagged_contents(call)
    if tag == "SolCall" and isinstance(contents, list) and len(contents) == 2:
        function_name, args = contents
        signature = _signature_from_typed_args(
            function_name, args, _echidna_abi_type
        )
        return SelectorCall(
            selector=selector_from_signature(signature),
            signature=signature,
            function_name=function_name if isinstance(function_name, str) else None,
            source_format="echidna-typed-corpus",
        )
    if tag == "SolCalldata":
        return SelectorCall(
            selector=selector_from_calldata(contents),
            signature=None,
            function_name=None,
            source_format="echidna-raw-calldata",
        )
    return None


def _call_from_recon(item: Any) -> Optional[SelectorCall]:
    if not isinstance(item, dict):
        return None
    call = item.get("call")
    if not isinstance(call, dict):
        return None
    sol_call = call.get("SolCall")
    if isinstance(sol_call, dict):
        function_name = sol_call.get("name")
        signature = _signature_from_typed_args(
            function_name, sol_call.get("args"), _recon_abi_type
        )
        return SelectorCall(
            selector=selector_from_signature(signature),
            signature=signature,
            function_name=function_name if isinstance(function_name, str) else None,
            source_format="recon-typed-corpus",
        )
    raw = call.get("SolCalldata")
    if raw is not None:
        return SelectorCall(
            selector=selector_from_calldata(raw),
            signature=None,
            function_name=None,
            source_format="recon-raw-calldata",
        )
    return None


def _call_from_medusa(item: Any) -> Optional[SelectorCall]:
    if not isinstance(item, dict):
        return None
    call = item.get("call")
    if not isinstance(call, dict):
        return None
    selector = selector_from_calldata(call.get("data"))
    abi_values = call.get("dataAbiValues")
    raw_signature = (
        abi_values.get("methodSignature") if isinstance(abi_values, dict) else None
    )
    signature = normalize_signature(raw_signature)
    function_name = function_name_from_signature(signature)
    if selector is None and signature is not None:
        selector = selector_from_signature(signature)
    if selector is None and signature is None:
        return None
    return SelectorCall(
        selector=selector,
        signature=signature,
        function_name=function_name,
        source_format="medusa-call-sequence",
    )


def _call_from_foundry(item: Any) -> Optional[SelectorCall]:
    if not isinstance(item, dict):
        return None
    selector = selector_from_calldata(item.get("calldata"))
    if selector is None:
        return None
    return SelectorCall(
        selector=selector,
        signature=None,
        function_name=None,
        source_format="foundry-persisted-corpus",
    )


def _artifact_candidates(instance_dir: Path, engine: str) -> list[Path]:
    candidates: list[Path] = []
    for path in instance_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        parts = {part.lower() for part in path.parts}
        suffixes = [suffix.lower() for suffix in path.suffixes]
        if engine in {"echidna", "recon-fuzzer"}:
            if "coverage" in parts and path.suffix.lower() == ".txt":
                candidates.append(path)
        elif engine == "medusa":
            if "call_sequences" in parts and path.suffix.lower() == ".json":
                candidates.append(path)
        elif engine == "foundry":
            if "corpus" in parts and (
                path.suffix.lower() == ".json" or suffixes[-2:] == [".json", ".gz"]
            ):
                candidates.append(path)
    return sorted(candidates)


def _read_json_artifact(path: Path) -> Any:
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rb") as handle:
        payload = handle.read(MAX_ARTIFACT_BYTES + 1)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    return json.loads(payload.decode("utf-8"))


def _sequence_digest(payload: Any) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_instance(result: InstanceResult) -> None:
    if result.corpus_path is None:
        result.status = "unavailable"
        return

    artifacts = _artifact_candidates(result.corpus_path, result.engine)
    result.artifact_files = len(artifacts)
    if not artifacts:
        # Foundry deliberately disables corpus persistence in the default runner.
        # No artifact therefore means unavailable telemetry, not an observed zero.
        if result.engine == "foundry":
            result.status = "unavailable"
        else:
            result.status = "observed_zero"
            result.warnings.append("no selector-bearing corpus artifacts were saved")
        return

    parser = {
        "echidna": _call_from_echidna,
        "recon-fuzzer": _call_from_recon,
        "medusa": _call_from_medusa,
        "foundry": _call_from_foundry,
    }.get(result.engine)
    if parser is None:
        result.status = "unavailable"
        return

    seen_sequences: set[str] = set()
    for path in artifacts:
        try:
            payload = _read_json_artifact(path)
            if not isinstance(payload, list):
                raise ValueError("corpus artifact must contain a JSON array")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            result.malformed_artifacts += 1
            continue
        digest = _sequence_digest(payload)
        if digest in seen_sequences:
            result.duplicate_sequences += 1
            continue
        seen_sequences.add(digest)
        result.parsed_sequences += 1
        for item in payload:
            call = parser(item)
            if call is None or (
                call.selector is None
                and call.signature is None
                and call.function_name is None
            ):
                result.ignored_calls += 1
                continue
            if (
                call.selector
                and call.signature
                and selector_from_signature(call.signature) != call.selector
            ):
                result.selector_mismatches += 1
            result.calls.append(call)

    if result.calls:
        result.status = "partial" if result.malformed_artifacts else "available"
    elif result.parsed_sequences:
        result.status = "observed_zero"
        result.warnings.append("parsed corpus artifacts contained zero selector calls")
    else:
        result.status = "unavailable"

    if result.malformed_artifacts:
        result.warnings.append(
            f"{result.malformed_artifacts} selector corpus artifact(s) were malformed"
        )
    if result.selector_mismatches:
        result.warnings.append(
            f"{result.selector_mismatches} ABI signature(s) disagreed with raw calldata"
        )


def _discover_instance_paths(root: Optional[Path]) -> dict[str, Path]:
    if root is None or not root.exists():
        return {}
    paths: dict[str, Path] = {}
    for path in sorted(child for child in root.iterdir() if child.is_dir()):
        if INSTANCE_PREFIX_RE.match(path.name):
            paths[path.name] = path
    return paths


def _enrich_calls(instances: Sequence[InstanceResult]) -> None:
    selector_metadata: dict[str, set[tuple[str, str]]] = defaultdict(set)
    name_metadata: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for instance in instances:
        for call in instance.calls:
            if call.selector and call.signature and call.function_name:
                metadata = (call.selector, call.signature)
                selector_metadata[call.selector].add(
                    (call.function_name, call.signature)
                )
                name_metadata[call.function_name].add(metadata)

    for instance in instances:
        enriched: list[SelectorCall] = []
        for call in instance.calls:
            selector = call.selector
            signature = call.signature
            function_name = call.function_name
            if selector and (signature is None or function_name is None):
                metadata = selector_metadata.get(selector, set())
                if len(metadata) == 1:
                    function_name, signature = next(iter(metadata))
            if selector is None and function_name:
                metadata = name_metadata.get(function_name, set())
                if len(metadata) == 1:
                    selector, signature = next(iter(metadata))
            enriched.append(
                SelectorCall(
                    selector=selector,
                    signature=signature,
                    function_name=function_name,
                    source_format=call.source_format,
                )
            )
        instance.calls = enriched


def _observations(calls: Iterable[SelectorCall]) -> dict[str, Observation]:
    observations: dict[str, Observation] = {}
    for call in calls:
        key = call.selector or (
            f"name:{call.function_name}" if call.function_name else "unknown"
        )
        observation = observations.setdefault(
            key, Observation(selector=call.selector)
        )
        observation.count += 1
        observation.source_formats.add(call.source_format)
        if call.signature:
            observation.signatures.add(call.signature)
        if call.function_name:
            observation.function_names.add(call.function_name)
    return observations


def _is_expectation_candidate(observation: Observation) -> bool:
    if not observation.selector or len(observation.function_names) != 1:
        return False
    name = next(iter(observation.function_names))
    if name in FRAMEWORK_FUNCTIONS:
        return False
    return not name.startswith(TEST_FUNCTION_PREFIXES)


def _load_explicit_expected(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("selectors") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("expected-selector JSON must be a list or contain 'selectors'")
    selectors: dict[str, dict[str, Any]] = {}
    for value in values:
        if isinstance(value, str):
            selector = normalize_selector(value)
            signature = None
            function_name = None
        elif isinstance(value, dict):
            signature = normalize_signature(value.get("signature"))
            selector = normalize_selector(value.get("selector"))
            if selector is None:
                selector = selector_from_signature(signature)
            function_name = value.get("function_name") or function_name_from_signature(
                signature
            )
            if not isinstance(function_name, str):
                function_name = None
        else:
            raise ValueError("expected selectors must be strings or objects")
        if selector is None:
            raise ValueError(f"invalid expected selector entry: {value!r}")
        selectors[selector] = {
            "selector": selector,
            "function_name": function_name,
            "signature": signature,
            "supporting_fuzzers": [],
        }
    return [selectors[key] for key in sorted(selectors)]


def _derive_peer_expected(
    instances: Sequence[InstanceResult],
    observations_by_instance: dict[str, dict[str, Observation]],
) -> list[dict[str, Any]]:
    available_by_engine: dict[str, set[str]] = defaultdict(set)
    present_by_selector: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    metadata: dict[str, Observation] = {}

    for instance in instances:
        if instance.status not in {"available", "partial"}:
            continue
        available_by_engine[instance.engine].add(instance.instance_label)
        for selector, observation in observations_by_instance[
            instance.instance_label
        ].items():
            if not _is_expectation_candidate(observation):
                continue
            present_by_selector[selector][instance.engine].add(
                instance.instance_label
            )
            aggregate = metadata.setdefault(
                selector, Observation(selector=observation.selector)
            )
            aggregate.signatures.update(observation.signatures)
            aggregate.function_names.update(observation.function_names)

    expected: list[dict[str, Any]] = []
    for selector, by_engine in sorted(present_by_selector.items()):
        supporting_engines = sorted(
            engine
            for engine, instance_labels in by_engine.items()
            if available_by_engine[engine]
            and instance_labels == available_by_engine[engine]
        )
        # This is deliberately a conservative heuristic, never ground truth:
        # every available instance in at least two different fuzzer families
        # must have persisted the selector.
        if len(supporting_engines) < 2:
            continue
        observation = metadata[selector]
        expected.append(
            {
                "selector": selector,
                "function_name": (
                    next(iter(observation.function_names))
                    if len(observation.function_names) == 1
                    else None
                ),
                "signature": (
                    next(iter(observation.signatures))
                    if len(observation.signatures) == 1
                    else None
                ),
                "supporting_fuzzers": supporting_engines,
            }
        )
    return expected


def analyze_selector_artifacts(
    *,
    corpus_dir: Optional[Path],
    logs_dir: Optional[Path],
    run_id: Optional[str] = None,
    exclude_fuzzers: Optional[set[str]] = None,
    raw_labels: bool = False,
    expected_selectors_json: Optional[Path] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    exclude = {value.lower() for value in (exclude_fuzzers or set())}
    corpus_paths = _discover_instance_paths(corpus_dir)
    log_paths = _discover_instance_paths(logs_dir)
    labels = sorted(set(corpus_paths) | set(log_paths))
    instances: list[InstanceResult] = []
    for label in labels:
        instance_id, fuzzer_label = split_instance_label(label)
        engine = normalize_fuzzer(fuzzer_label)
        fuzzer = fuzzer_label if raw_labels else engine
        if engine.lower() in exclude or fuzzer_label.lower() in exclude:
            continue
        result = InstanceResult(
            instance_label=label,
            instance_id=instance_id,
            engine=engine,
            fuzzer=fuzzer,
            fuzzer_label=fuzzer_label,
            corpus_path=corpus_paths.get(label),
        )
        _parse_instance(result)
        instances.append(result)

    _enrich_calls(instances)
    observations_by_instance = {
        instance.instance_label: _observations(instance.calls)
        for instance in instances
    }

    if expected_selectors_json is not None:
        expected = _load_explicit_expected(expected_selectors_json)
        expected_source = f"explicit selector catalog: {expected_selectors_json.name}"
        expected_kind = "explicit"
    else:
        expected = _derive_peer_expected(instances, observations_by_instance)
        expected_source = (
            "peer-consensus heuristic: observed in every available corpus for at "
            "least two different fuzzer families; not benchmark ground truth"
        )
        expected_kind = "peer-consensus-heuristic"
    expected_selectors = {entry["selector"] for entry in expected}

    health_warnings: list[str] = []
    limitations: list[str] = []
    rows: list[dict[str, Any]] = []
    instance_summaries: list[dict[str, Any]] = []
    for instance in instances:
        observations = observations_by_instance[instance.instance_label]
        total_calls = sum(item.count for item in observations.values())
        observed_selectors = {
            item.selector for item in observations.values() if item.selector
        }
        missing_expected = sorted(expected_selectors - observed_selectors)
        warnings = list(instance.warnings)
        if (
            instance.status in {"available", "partial", "observed_zero"}
            and missing_expected
        ):
            warnings.append(
                f"missing {len(missing_expected)} expected selector(s): "
                + ", ".join(missing_expected)
            )

        if instance.status == "observed_zero":
            health_warnings.append(
                f"{instance.instance_label}: selector telemetry observed zero calls"
            )
        if instance.malformed_artifacts:
            health_warnings.append(
                f"{instance.instance_label}: "
                f"{instance.malformed_artifacts} malformed selector artifact(s)"
            )
        if missing_expected and instance.status in {
            "available",
            "partial",
            "observed_zero",
        }:
            health_warnings.append(
                f"{instance.instance_label}: missing expected selector(s) "
                + ", ".join(missing_expected)
            )

        if instance.status == "unavailable":
            if instance.engine == "foundry":
                limitations.append(
                    f"{instance.instance_label}: Foundry selector distribution is "
                    "unavailable because no persisted corpus was present; failure-event "
                    "selectors are intentionally not used as a distribution"
                )
            else:
                limitations.append(
                    f"{instance.instance_label}: selector telemetry was unavailable"
                )

        for key, observation in sorted(
            observations.items(), key=lambda item: (-item[1].count, item[0])
        ):
            rows.append(
                {
                    "run_id": run_id or "unknown",
                    "instance_id": instance.instance_id,
                    "fuzzer": instance.fuzzer,
                    "fuzzer_label": instance.fuzzer_label,
                    "status": instance.status,
                    "comparison_key": key,
                    "selector": observation.selector or "",
                    "function_signatures": ";".join(
                        sorted(observation.signatures)
                    ),
                    "function_names": ";".join(
                        sorted(observation.function_names)
                    ),
                    "saved_corpus_calls": observation.count,
                    "share": (
                        round(observation.count / total_calls, 12)
                        if total_calls
                        else 0.0
                    ),
                    "source_formats": ";".join(
                        sorted(observation.source_formats)
                    ),
                    "is_expected": observation.selector in expected_selectors,
                    "expected_source": (
                        expected_source
                        if observation.selector in expected_selectors
                        else ""
                    ),
                }
            )

        instance_summaries.append(
            {
                "instance_label": instance.instance_label,
                "instance_id": instance.instance_id,
                "fuzzer": instance.fuzzer,
                "fuzzer_label": instance.fuzzer_label,
                "engine": instance.engine,
                "status": instance.status,
                "artifact_files": instance.artifact_files,
                "parsed_sequences": instance.parsed_sequences,
                "duplicate_sequences": instance.duplicate_sequences,
                "malformed_artifacts": instance.malformed_artifacts,
                "ignored_calls": instance.ignored_calls,
                "saved_corpus_calls": total_calls,
                "unique_selectors": len(observed_selectors),
                "unique_call_identities": len(observations),
                "missing_expected_selectors": missing_expected,
                "warnings": warnings,
            }
        )

    fuzzer_summaries: list[dict[str, Any]] = []
    by_fuzzer: dict[str, list[InstanceResult]] = defaultdict(list)
    for instance in instances:
        by_fuzzer[instance.fuzzer].append(instance)
    for fuzzer, fuzzer_instances in sorted(by_fuzzer.items()):
        aggregate_calls: list[SelectorCall] = []
        for instance in fuzzer_instances:
            aggregate_calls.extend(instance.calls)
        aggregate = _observations(aggregate_calls)
        total_calls = sum(item.count for item in aggregate.values())
        distributions = []
        for key, observation in sorted(
            aggregate.items(), key=lambda item: (-item[1].count, item[0])
        ):
            distributions.append(
                {
                    "comparison_key": key,
                    "selector": observation.selector,
                    "function_signatures": sorted(observation.signatures),
                    "function_names": sorted(observation.function_names),
                    "saved_corpus_calls": observation.count,
                    "share": (
                        round(observation.count / total_calls, 12)
                        if total_calls
                        else 0.0
                    ),
                }
            )
        statuses = {instance.status for instance in fuzzer_instances}
        if statuses <= {"unavailable"}:
            status = "unavailable"
        elif statuses <= {"observed_zero"}:
            status = "observed_zero"
        elif "partial" in statuses or "unavailable" in statuses or "observed_zero" in statuses:
            status = "partial"
        else:
            status = "available"
        fuzzer_summaries.append(
            {
                "fuzzer": fuzzer,
                "status": status,
                "instances": len(fuzzer_instances),
                "available_instances": sum(
                    instance.status in {"available", "partial"}
                    for instance in fuzzer_instances
                ),
                "observed_zero_instances": sum(
                    instance.status == "observed_zero"
                    for instance in fuzzer_instances
                ),
                "unavailable_instances": sum(
                    instance.status == "unavailable"
                    for instance in fuzzer_instances
                ),
                "saved_corpus_calls": total_calls,
                "unique_selectors": len(
                    {
                        observation.selector
                        for observation in aggregate.values()
                        if observation.selector
                    }
                ),
                "unique_call_identities": len(aggregate),
                "distribution": distributions,
            }
        )

    if corpus_dir is None or not corpus_dir.exists():
        limitations.append(
            "No corpus artifact directory was available; selector distributions "
            "cannot be reconstructed from logs alone"
        )
    if not expected:
        limitations.append(
            "No expected selector set was available: the peer heuristic requires "
            "corroboration from every available instance in at least two fuzzer families"
        )

    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or "unknown",
        "measurement": (
            "Counts are occurrences in unique saved corpus sequences, not runtime "
            "execution frequencies"
        ),
        "expected_set": {
            "kind": expected_kind,
            "source": expected_source,
            "status": "available" if expected else "unavailable",
            "selectors": expected,
        },
        "instances": instance_summaries,
        "fuzzers": fuzzer_summaries,
        "health_warnings": sorted(set(health_warnings)),
        "limitations": sorted(set(limitations)),
    }
    return rows, summary


SELECTOR_DISTRIBUTION_FIELDS = [
    "run_id",
    "instance_id",
    "fuzzer",
    "fuzzer_label",
    "status",
    "comparison_key",
    "selector",
    "function_signatures",
    "function_names",
    "saved_corpus_calls",
    "share",
    "source_formats",
    "is_expected",
    "expected_source",
]


def write_selector_outputs(
    rows: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    distribution_csv: Path,
    summary_json: Path,
) -> None:
    distribution_csv.parent.mkdir(parents=True, exist_ok=True)
    with distribution_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SELECTOR_DISTRIBUTION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build saved-corpus function-selector distributions."
    )
    parser.add_argument("--corpus-dir", required=True, type=Path)
    parser.add_argument("--logs-dir", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--exclude-fuzzers", default="")
    parser.add_argument("--raw-labels", action="store_true")
    parser.add_argument("--expected-selectors-json", type=Path, default=None)
    parser.add_argument("--out-csv", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    args = parser.parse_args()
    exclude = {
        value.strip().lower()
        for value in args.exclude_fuzzers.split(",")
        if value.strip()
    }
    rows, summary = analyze_selector_artifacts(
        corpus_dir=args.corpus_dir,
        logs_dir=args.logs_dir,
        run_id=args.run_id,
        exclude_fuzzers=exclude,
        raw_labels=args.raw_labels,
        expected_selectors_json=args.expected_selectors_json,
    )
    write_selector_outputs(rows, summary, args.out_csv, args.out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
