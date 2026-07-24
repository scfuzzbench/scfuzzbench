#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin
from urllib.parse import quote
from dataclasses import dataclass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from analysis.trial_run import (  # noqa: E402
    MIN_BUDGET_HOURS,
    MIN_RUNS_PER_FUZZER,
    format_trial_run_warning,
)
from scripts.benchmark_run_state import (  # noqa: E402
    SUPERSEDED_KEY_RE,
    classify_superseded_marker,
    run_started_at_epoch,
)
from scripts.preliminary_results import (  # noqa: E402
    ANALYSIS_META_RE,
    FINALIZED_KEY_RE,
    MAX_JSON_BYTES,
    MAX_LIST_KEYS,
    MAX_LIST_PAGES,
    MAX_RUN_MANIFESTS,
    MAX_SELECTED_RUNS,
    RESULT_SCHEMA,
    RUN_KEY_RE as PRELIMINARY_RUN_KEY_RE,
    format_duration,
    s3_object_identity_args,
    validate_finalized_marker,
    validate_result_metadata,
    validate_run_manifest,
)


RUN_MANIFEST_RE = re.compile(
    r"^runs/([A-Za-z0-9][A-Za-z0-9._-]{0,79})/([0-9a-f]{32})/manifest\.json$"
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
PRICING_API_REGION = "us-east-1"
SITE_ORIGIN = "https://scfuzzbench.com"
DEFAULT_SOCIAL_DESCRIPTION = "Benchmark suite for smart-contract fuzzers."
DEFAULT_DOCS_EC2_PRICES_USD_PER_HOUR = {
    "c6a.4xlarge": 0.612,
    "c6a.8xlarge": 1.224,
}
AWS_REGION_TO_PRICING_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "sa-east-1": "South America (Sao Paulo)",
}


def aws_env(profile: str | None) -> dict:
    env = os.environ.copy()
    if profile:
        env["AWS_PROFILE"] = profile
    return env


def aws_json(args: list[str], *, profile: str | None, cli_region: str | None = None) -> dict:
    cmd = ["aws"]
    if cli_region:
        cmd += ["--region", cli_region]
    cmd += [*args, "--output", "json"]
    out = subprocess.check_output(cmd, text=True, env=aws_env(profile))
    return json.loads(out) if out.strip() else {}


def aws_text(args: list[str], *, profile: str | None) -> str:
    return subprocess.check_output(["aws", *args], text=True, env=aws_env(profile))


def list_keys(bucket: str, prefix: str, *, profile: str | None) -> list[str]:
    keys: list[str] = []
    token = ""
    pages = 0
    while True:
        pages += 1
        if pages > MAX_LIST_PAGES:
            raise ValueError(
                f"S3 listing exceeded {MAX_LIST_PAGES} pages under {prefix}"
            )
        cmd = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix]
        if token:
            cmd += ["--continuation-token", token]
        data = aws_json(cmd, profile=profile)
        contents = data.get("Contents", [])
        if not isinstance(contents, list):
            raise ValueError("S3 listing Contents must be a list")
        page_keys = [
            str(item["Key"])
            for item in contents
            if isinstance(item, dict) and "Key" in item
        ]
        if len(page_keys) != len(contents):
            raise ValueError("S3 listing contained an invalid object entry")
        if len(keys) + len(page_keys) > MAX_LIST_KEYS:
            raise ValueError(
                f"S3 listing exceeded {MAX_LIST_KEYS} keys under {prefix}"
            )
        keys.extend(page_keys)
        if not data.get("IsTruncated"):
            return keys
        token = str(data.get("NextContinuationToken", ""))
        if not token:
            raise RuntimeError(
                "S3 pagination was truncated without a continuation token"
            )


def head_exists(bucket: str, key: str, *, profile: str | None) -> bool:
    try:
        subprocess.check_call(
            ["aws", "s3api", "head-object", "--bucket", bucket, "--key", key],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=aws_env(profile),
        )
        return True
    except subprocess.CalledProcessError:
        return False


def s3_url(bucket: str, region: str, key: str) -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def utc_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def safe_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except Exception:
        return default


def rewrite_headings(md: str, *, add: int) -> str:
    out: list[str] = []
    for line in md.splitlines():
        m = re.match(r"^(#+)(\s+.*)$", line)
        if not m:
            out.append(line)
            continue
        hashes, rest = m.group(1), m.group(2)
        out.append("#" * (len(hashes) + add) + rest)
    return "\n".join(out).rstrip() + "\n"


def yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def truncate_meta_text(value: str, *, max_len: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_len:
        return text
    trimmed = text[: max_len - 3].rstrip(" .,;:-")
    return f"{trimmed}..."


def first_markdown_image(lines: list[str]) -> tuple[str, str] | None:
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = MARKDOWN_IMAGE_RE.search(line)
        if not m:
            continue
        url = m.group("url").strip()
        if not url:
            continue
        alt = m.group("alt").strip() or "Preview image"
        return url, alt
    return None


def first_heading_text(lines: list[str]) -> str:
    for line in lines:
        if not line.startswith("# "):
            continue
        heading = line[2:].strip()
        # Replace inline code spans `code` with their inner text, then remove any remaining backticks.
        heading = re.sub(r"`([^`]*)`", r"\1", heading)
        heading = heading.replace("`", "")
        return heading
    return ""


def run_social_title(run_id: str, benchmark_uuid: str, target_repo_url: str) -> str:
    target_label = compact_repo_label(target_repo_url)
    if target_label:
        return f"scfuzzbench run {run_id} - {target_label}"
    return f"scfuzzbench run {run_id} - {short_uuid(benchmark_uuid)}"


def run_social_description(run: Run) -> str:
    m = run.manifest
    parts: list[str] = [
        f"Benchmark {run.benchmark_uuid}",
        f"Date {utc_ts(run.run_started_at_epoch)}",
        f"Timeout {run.timeout_hours:g}h",
    ]

    target_repo = str(m.get("target_repo_url", "")).strip()
    if target_repo:
        parts.append(f"Target {compact_repo_label(target_repo)}")

    target_commit = str(m.get("target_commit", "")).strip()
    if target_commit:
        parts.append(f"Commit {shortish(target_commit, max_len=10)}")

    fuzzer_keys = m.get("fuzzer_keys")
    if isinstance(fuzzer_keys, list):
        fuzzers = ", ".join(str(x).strip() for x in fuzzer_keys if str(x).strip())
        if fuzzers:
            parts.append(f"Fuzzers {fuzzers}")

    return truncate_meta_text(". ".join(parts) + ".", max_len=240)


def github_repo_base(repo_url: str) -> str:
    """Return a clean https://github.com/org/repo base, or "" if not GitHub."""
    s = str(repo_url).strip().rstrip("/")
    if s.endswith(".git"):
        s = s[: -len(".git")]
    for prefix in ("https://github.com/", "http://github.com/"):
        if s.startswith(prefix) and len(s[len(prefix) :].split("/")) >= 2:
            return s
    return ""


def run_metadata_lines(manifest: dict) -> list[str]:
    """Bullet lines describing what a run tested, from its manifest.

    Every line is conditional on the field being present so legacy manifests
    render without gaps or errors.
    """
    m = manifest if isinstance(manifest, dict) else {}
    lines: list[str] = []

    target_repo = str(m.get("target_repo_url", "")).strip()
    target_commit = str(m.get("target_commit", "")).strip()
    if target_repo:
        label = compact_repo_label(target_repo) or target_repo
        entry = f"- Target: [`{label}`]({target_repo})"
        if target_commit:
            base = github_repo_base(target_repo)
            short = shortish(target_commit, max_len=10)
            if base:
                entry += f" @ [`{short}`]({base}/commit/{target_commit})"
            else:
                entry += f" @ `{short}`"
        lines.append(entry)
    elif target_commit:
        lines.append(f"- Target commit: `{shortish(target_commit, max_len=10)}`")

    benchmark_type = str(m.get("benchmark_type", "")).strip()
    if benchmark_type:
        lines.append(f"- Benchmark type: `{benchmark_type}`")

    fuzzer_keys = m.get("fuzzers")
    if isinstance(fuzzer_keys, list):
        fuzzers = ", ".join(
            f"`{str(x).strip()}`" for x in fuzzer_keys if str(x).strip()
        )
        if fuzzers:
            lines.append(f"- Fuzzers: {fuzzers}")

    inputs = m.get("nonsecret_optional_inputs")
    if isinstance(inputs, dict):
        versions = []
        for tool, key in (
            ("echidna", "echidna_version"),
            ("medusa", "medusa_version"),
            ("recon", "recon_version"),
            ("foundry", "foundry_version"),
        ):
            value = str(inputs.get(key, "") or "").strip()
            if value:
                versions.append(f"`{tool} {value}`")
        if versions:
            lines.append(f"- Tool versions: {', '.join(versions)}")

    instance_type = str(m.get("instance_type", "")).strip()
    instances = m.get("instances_per_fuzzer")
    instances_text = str(instances).strip() if instances is not None else ""
    if instance_type and instances_text:
        lines.append(
            f"- Instances: `{instances_text}` × `{instance_type}` per fuzzer"
        )
    elif instance_type:
        lines.append(f"- Instance type: `{instance_type}`")

    harness_commit = str(m.get("scfuzzbench_commit", "")).strip()
    if harness_commit:
        harness_repo = str(
            m.get("github_repository", "") or "scfuzzbench/scfuzzbench"
        ).strip()
        short = shortish(harness_commit, max_len=10)
        lines.append(
            f"- Harness: [`{harness_repo}@{short}`]"
            f"(https://github.com/{harness_repo}/commit/{harness_commit})"
        )

    return lines


def with_social_preview_head(
    lines: list[str],
    *,
    page_path: str,
    title: str | None = None,
    description: str | None = None,
) -> list[str]:
    image = first_markdown_image(lines)
    if image is None:
        return lines

    image_url, image_alt = image
    page_url = urljoin(f"{SITE_ORIGIN}/", page_path.lstrip("/"))
    absolute_image_url = urljoin(page_url, image_url)
    meta_title = truncate_meta_text(
        (title or "").strip() or first_heading_text(lines) or "scfuzzbench",
        max_len=120,
    )
    meta_description = truncate_meta_text(
        (description or "").strip() or DEFAULT_SOCIAL_DESCRIPTION,
        max_len=240,
    )

    return [
        "---",
        "head:",
        "  - - meta",
        "    - name: description",
        f"      content: {yaml_quote(meta_description)}",
        "  - - meta",
        "    - property: og:title",
        f"      content: {yaml_quote(meta_title)}",
        "  - - meta",
        "    - property: og:description",
        f"      content: {yaml_quote(meta_description)}",
        "  - - meta",
        "    - property: og:type",
        "      content: website",
        "  - - meta",
        "    - property: og:url",
        f"      content: {yaml_quote(page_url)}",
        "  - - meta",
        "    - property: og:image",
        f"      content: {yaml_quote(absolute_image_url)}",
        "  - - meta",
        "    - property: og:image:secure_url",
        f"      content: {yaml_quote(absolute_image_url)}",
        "  - - meta",
        "    - property: og:image:alt",
        f"      content: {yaml_quote(image_alt)}",
        "  - - meta",
        "    - name: twitter:card",
        "      content: summary_large_image",
        "  - - meta",
        "    - name: twitter:title",
        f"      content: {yaml_quote(meta_title)}",
        "  - - meta",
        "    - name: twitter:description",
        f"      content: {yaml_quote(meta_description)}",
        "  - - meta",
        "    - name: twitter:image",
        f"      content: {yaml_quote(absolute_image_url)}",
        "  - - meta",
        "    - name: twitter:image:alt",
        f"      content: {yaml_quote(image_alt)}",
        "---",
        "",
        *lines,
    ]


def shortish(s: str, *, max_len: int = 10) -> str:
    s = str(s).strip()
    if not s:
        return ""
    return s if len(s) <= max_len else s[:max_len]


def short_uuid(s: str) -> str:
    s = str(s).strip()
    if len(s) <= 20:
        return s
    return f"{s[:10]}...{s[-6:]}"


def compact_repo_label(repo_url: str) -> str:
    s = str(repo_url).strip()
    if not s:
        return ""

    # Prefer `org/repo` for GitHub URLs to keep tables readable.
    for prefix in ("https://github.com/", "http://github.com/"):
        if s.startswith(prefix):
            rest = s[len(prefix) :].strip("/")
            if rest.endswith(".git"):
                rest = rest[: -len(".git")]
            parts = rest.split("/")
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
            return rest or s

    return s


def pricing_location_for_region(region: str) -> str:
    return AWS_REGION_TO_PRICING_LOCATION.get(region, AWS_REGION_TO_PRICING_LOCATION["us-east-1"])


def extract_ondemand_linux_usd_per_hour(pricing_data: dict) -> float | None:
    candidates: list[float] = []
    for entry in pricing_data.get("PriceList", []):
        product = json.loads(entry) if isinstance(entry, str) else entry
        if not isinstance(product, dict):
            continue
        terms = product.get("terms", {}).get("OnDemand", {})
        if not isinstance(terms, dict):
            continue
        for term in terms.values():
            if not isinstance(term, dict):
                continue
            dims = term.get("priceDimensions", {})
            if not isinstance(dims, dict):
                continue
            for dim in dims.values():
                if not isinstance(dim, dict):
                    continue
                usd = dim.get("pricePerUnit", {}).get("USD")
                try:
                    value = float(usd)
                except Exception:
                    continue
                if value > 0:
                    candidates.append(value)
    if not candidates:
        return None
    return min(candidates)


def fetch_ec2_pricing_table(instance_types: set[str], *, profile: str | None, region: str) -> dict[str, float]:
    location = pricing_location_for_region(region)
    results: dict[str, float] = {}
    for instance_type in sorted(instance_types):
        if not instance_type:
            continue
        try:
            data = aws_json(
                [
                    "pricing",
                    "get-products",
                    "--service-code",
                    "AmazonEC2",
                    "--filters",
                    f"Type=TERM_MATCH,Field=location,Value={location}",
                    "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
                    "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
                    "Type=TERM_MATCH,Field=tenancy,Value=Shared",
                    "Type=TERM_MATCH,Field=capacitystatus,Value=Used",
                    "Type=TERM_MATCH,Field=licenseModel,Value=No License required",
                    f"Type=TERM_MATCH,Field=instanceType,Value={instance_type}",
                    "--max-results",
                    "100",
                ],
                profile=profile,
                cli_region=PRICING_API_REGION,
            )
            price = extract_ondemand_linux_usd_per_hour(data)
            if price is not None:
                results[instance_type] = round(price, 6)
        except Exception as exc:
            print(f"WARNING: pricing lookup failed for {instance_type}: {exc}", file=sys.stderr)
    return results


# Manifest version keys don't always match fuzzer_keys entries verbatim:
# the manifest carries `recon_version` while the fuzzer key is `recon-fuzzer`.
FUZZER_VERSION_KEY_ALIASES = {"recon-fuzzer": "recon"}


def format_fuzzer_lines(manifest: dict) -> list[str]:
    ordered_fuzzers: list[str] = []
    if isinstance(manifest.get("fuzzer_keys"), list):
        for item in manifest.get("fuzzer_keys", []):
            name = str(item).strip()
            if name and name not in ordered_fuzzers:
                ordered_fuzzers.append(name)

    versions: dict[str, str] = {}
    for raw_key, raw_value in manifest.items():
        key = str(raw_key).strip()
        if not key.endswith("_version"):
            continue
        version = str(raw_value).strip()
        if not version:
            continue
        fuzzer_name = key.removesuffix("_version")
        if fuzzer_name:
            versions[fuzzer_name] = version

    # Git-pinned foundry runs have an empty foundry_version; older manifests
    # predate the runner-side `forge --version` resolution, so fall back to the
    # short git ref rather than showing no version at all.
    if "foundry" not in versions:
        git_ref = str(manifest.get("foundry_git_ref", "") or "").strip()
        if git_ref:
            versions["foundry"] = f"git:{git_ref[:7]}"
    if "echidna" not in versions:
        ci_commit = str(manifest.get("echidna_ci_commit", "") or "").strip()
        if ci_commit:
            versions["echidna"] = f"ci:{ci_commit[:7]}"
    if "medusa" not in versions:
        git_commit = str(manifest.get("medusa_git_commit", "") or "").strip()
        if git_commit:
            versions["medusa"] = f"git:{git_commit[:7]}"

    lines: list[str] = []
    for fuzzer in ordered_fuzzers:
        version = versions.get(fuzzer, "").strip()
        if not version:
            alias = FUZZER_VERSION_KEY_ALIASES.get(fuzzer)
            if alias:
                version = versions.get(alias, "").strip()
        line = f"{fuzzer} ({version})" if version else fuzzer
        lines.append(f"`{line}`")
    return lines


def format_seed_corpus_lines(manifest: dict) -> list[str]:
    seed_corpus = manifest.get("seed_corpus")
    if not isinstance(seed_corpus, dict):
        return []

    lines: list[str] = []
    fields = (
        ("seed_corpus_source", "source"),
        ("seed_corpus_source_type", "source_type"),
        ("seed_corpus_file_count", "file_count"),
        ("seed_corpus_size_bytes", "size_bytes"),
        ("seed_corpus_sha256", "sha256"),
        ("seed_corpus_digest_algorithm", "digest_algorithm"),
        ("seed_corpus_copy_semantics", "copy_semantics"),
        ("seed_corpus_source_immutability", "source_immutability"),
        ("seed_corpus_s3_listing_sha256", "s3_listing_sha256"),
    )
    for label, key in fields:
        value = seed_corpus.get(key)
        if value is None:
            continue
        rendered = str(value).strip()
        if rendered:
            lines.append(f"- {label}: `{rendered}`")
    return lines


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render_redirect_page(to: str, *, heading: str) -> str:
    lines: list[str] = []
    lines.extend(
        [
            "---",
            "aside: false",
            "head:",
            "  - - meta",
            "    - http-equiv: refresh",
            f"      content: \"0; url={to}\"",
            "  - - script",
            "    - {}",
            "    - |",
            f"      window.location.replace(\"{to}\");",
            "---",
            "",
            f"# {heading}",
            "",
            f"Opening: [{to}]({to})",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def rm_tree_children(dir_path: Path, *, keep_files: set[str], dir_name_re: re.Pattern[str] | None) -> None:
    if not dir_path.exists():
        return
    for child in dir_path.iterdir():
        if child.is_file() and child.name in keep_files:
            continue
        if child.is_dir():
            if dir_name_re and not dir_name_re.match(child.name):
                continue
            shutil.rmtree(child)


def preliminary_warning_lines(metadata: dict) -> list[str]:
    expected = int(metadata.get("expected_snapshots", 0))
    present = int(metadata.get("present_snapshots", 0))
    missing = max(0, expected - present)
    return [
        "::: danger PRELIMINARY — DO NOT COMPARE OR STOP",
        "This view is incomplete and non-terminal. Wait for the canonical release.",
        "",
        f"- As of: `{metadata.get('as_of_utc', 'not available')}`",
        f"- Elapsed: `{format_duration(int(metadata.get('elapsed_seconds', 0)))}` "
        f"of `{format_duration(int(metadata.get('planned_timeout_seconds', 0)))}`",
        f"- Snapshot coverage: `{present}/{expected}` replicates (`{missing}` missing)",
        "",
        "Do not use this page for rankings, pass/fail decisions, or optional stopping.",
        "Saved-corpus selector distributions are unavailable in preliminary "
        "log-only checkpoints; wait for the canonical release.",
        ":::",
        "",
    ]


def preliminary_object_head(
    bucket: str,
    key: str,
    *,
    profile: str | None,
    max_bytes: int,
    expected_sha256: str = "",
) -> dict:
    head = aws_json(
        ["s3api", "head-object", "--bucket", bucket, "--key", key],
        profile=profile,
    )
    try:
        size = int(head.get("ContentLength"))
    except (TypeError, ValueError):
        raise ValueError(f"{key} has invalid ContentLength") from None
    digest = str(head.get("Metadata", {}).get("sha256", ""))
    if size < 0 or size > max_bytes:
        raise ValueError(f"{key} exceeds its docs byte cap")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{key} is missing immutable SHA-256 metadata")
    if expected_sha256 and digest != expected_sha256:
        raise ValueError(f"{key} SHA-256 metadata disagrees with its analysis manifest")
    return head


def preliminary_text(
    bucket: str,
    key: str,
    *,
    profile: str | None,
    max_bytes: int,
    expected_sha256: str = "",
) -> str:
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes < 1
    ):
        raise ValueError("docs byte cap must be a positive integer")
    head = preliminary_object_head(
        bucket,
        key,
        profile=profile,
        max_bytes=max_bytes,
        expected_sha256=expected_sha256,
    )
    identity_args = s3_object_identity_args(head, key)
    with tempfile.TemporaryDirectory(prefix="scfuzzbench-docs-s3-") as tmp:
        destination = Path(tmp) / "object"
        subprocess.check_call(
            [
                "aws",
                "s3api",
                "get-object",
                "--bucket",
                bucket,
                "--key",
                key,
                "--range",
                f"bytes=0-{max_bytes}",
                *identity_args,
                str(destination),
            ],
            env=aws_env(profile),
            stdout=subprocess.DEVNULL,
        )
        downloaded_size = destination.stat().st_size
        if downloaded_size > max_bytes:
            raise ValueError(f"{key} downloaded object exceeds its docs byte cap")
        if downloaded_size != int(head["ContentLength"]):
            raise ValueError(f"{key} downloaded size disagrees with S3 metadata")
        encoded = destination.read_bytes()
    if len(encoded) != int(head["ContentLength"]):
        raise ValueError(f"{key} downloaded size disagrees with S3 metadata")
    actual = hashlib.sha256(encoded).hexdigest()
    expected = str(head["Metadata"]["sha256"])
    if actual != expected:
        raise ValueError(f"{key} downloaded SHA-256 disagrees with S3 metadata")
    return encoded.decode("utf-8")


def sanitize_preliminary_markdown(value: str) -> str:
    if len(value.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("preliminary report exceeds its docs byte cap")
    if "PRELIMINARY — INCOMPLETE — DO NOT COMPARE OR STOP" not in value:
        raise ValueError("preliminary report is missing its required warning")
    # Reports contain target-controlled invariant names. Preserve Markdown
    # structure while neutralizing raw HTML/Vue templates and active URL schemes.
    sanitized = html.escape(value.replace("\x00", ""), quote=False)
    sanitized = sanitized.replace("{", "&#123;").replace("}", "&#125;")
    sanitized = re.sub(
        r"(?i)(!?\[[^\]\r\n]*\]\()\s*(?:javascript|data|vbscript):",
        r"\1blocked:",
        sanitized,
    )
    return sanitized


def preliminary_s3_url(bucket: str, region: str, key: str) -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/{quote(key, safe='/')}"


def render_preliminary_page(
    *,
    manifest: dict,
    metadata: dict | None,
    report_markdown: str,
    chart_urls: list[tuple[str, str]],
    generated_at: str,
) -> str:
    run_id = str(manifest["run_id"])
    uuid = str(manifest["benchmark_uuid"])
    lines = [
        "---",
        "aside: false",
        "---",
        "",
        f"# Preliminary run `{run_id}`",
        "",
    ]
    if metadata is None:
        lines.extend(
            [
                "::: danger PRELIMINARY — NO CHECKPOINT YET",
                "The run is active, but no settled checkpoint has been published.",
                "Wait for the next preliminary update. Do not make benchmark decisions.",
                ":::",
                "",
            ]
        )
    else:
        lines.extend(preliminary_warning_lines(metadata))
    lines.extend(
        [
            f"- Benchmark: `{uuid}`",
            f"- Started: `{utc_ts(int(manifest['run_started_at_epoch']))}`",
            f"- Planned timeout: `{float(manifest['timeout_hours']):g}h`",
            f"- Page generated: `{generated_at}`",
            "",
        ]
    )
    if chart_urls:
        lines.extend(["## Watermarked charts", ""])
        for label, url in chart_urls:
            lines.append(f"![PRELIMINARY — {label}]({url})")
            lines.append("")
    if report_markdown.strip():
        lines.extend(["## Watermarked report", ""])
        lines.extend(rewrite_headings(report_markdown, add=2).splitlines())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_preliminary_pages(
    *,
    bucket: str,
    region: str,
    profile: str | None,
    docs_dir: Path,
    now: int,
    generated_at: str,
    superseded_runs: set[tuple[str, str]] | None = None,
) -> None:
    keys = list_keys(bucket, "preliminary/", profile=profile)
    run_keys = sorted(key for key in keys if PRELIMINARY_RUN_KEY_RE.fullmatch(key))
    if len(run_keys) > MAX_RUN_MANIFESTS:
        raise ValueError(
            f"preliminary docs exceeded {MAX_RUN_MANIFESTS} run manifests"
        )
    finalized: set[tuple[str, str]] = set()
    for key in sorted(item for item in keys if FINALIZED_KEY_RE.fullmatch(item)):
        match = FINALIZED_KEY_RE.fullmatch(key)
        if match is None:
            raise AssertionError("finalized key filter disagrees with parser")
        try:
            marker = json.loads(
                preliminary_text(
                    bucket,
                    key,
                    profile=profile,
                    max_bytes=MAX_JSON_BYTES,
                )
            )
            validate_finalized_marker(
                marker,
                run_id=match["run"],
                benchmark_uuid=match["uuid"],
            )
        except Exception as exc:
            print(
                f"WARNING: ignoring invalid preliminary finalization marker "
                f"{key}: {exc}",
                file=sys.stderr,
            )
            continue
        finalized.add((match["run"], match["uuid"]))
    analysis_by_run: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for key in keys:
        match = ANALYSIS_META_RE.fullmatch(key)
        if match:
            analysis_by_run.setdefault((match["run"], match["uuid"]), []).append(
                (int(match["checkpoint"]), key)
            )

    active: list[tuple[dict, dict | None, str, list[tuple[str, str]]]] = []
    for key in run_keys:
        match = PRELIMINARY_RUN_KEY_RE.fullmatch(key)
        run_id, uuid = match["run"], match["uuid"]
        try:
            manifest = json.loads(
                preliminary_text(
                    bucket,
                    key,
                    profile=profile,
                    max_bytes=1024 * 1024,
                )
            )
            manifest = validate_run_manifest(
                manifest, run_id=run_id, benchmark_uuid=uuid
            )
        except Exception as exc:
            print(f"WARNING: skipping malformed preliminary manifest {key}: {exc}", file=sys.stderr)
            continue
        if not manifest["preliminary"]["enabled"] or (run_id, uuid) in finalized:
            continue
        if superseded_runs and (run_id, uuid) in superseded_runs:
            continue
        deadline = manifest["run_started_at_epoch"] + int(
            manifest["timeout_hours"] * 3600
        )
        if now < manifest["run_started_at_epoch"] or now >= deadline:
            continue

        metadata = None
        report_markdown = ""
        chart_urls: list[tuple[str, str]] = []
        checkpoints = analysis_by_run.get((run_id, uuid), [])
        if checkpoints:
            checkpoint, metadata_key = max(checkpoints)
            try:
                metadata = json.loads(
                    preliminary_text(
                        bucket,
                        metadata_key,
                        profile=profile,
                        max_bytes=2 * 1024 * 1024,
                    )
                )
                metadata = validate_result_metadata(
                    metadata,
                    run_id=run_id,
                    benchmark_uuid=uuid,
                    checkpoint=checkpoint,
                )
                prefix = metadata_key.removesuffix("/preliminary.json")
                report_key = f"{prefix}/data/REPORT.md"
                published_hashes = metadata["published_file_sha256"]
                report_digest = str(published_hashes.get("data/REPORT.md", ""))
                if report_digest:
                    report_markdown = sanitize_preliminary_markdown(
                        preliminary_text(
                            bucket,
                            report_key,
                            profile=profile,
                            max_bytes=2 * 1024 * 1024,
                            expected_sha256=report_digest,
                        )
                    )
                chart_prefix = f"{prefix}/images/"
                for chart_key in sorted(
                    item
                    for item in keys
                    if item.startswith(chart_prefix) and item.endswith(".png")
                ):
                    relative = chart_key.removeprefix(f"{prefix}/")
                    if not re.fullmatch(
                        r"images/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.png",
                        relative,
                    ):
                        continue
                    expected_digest = str(published_hashes.get(relative, ""))
                    if not expected_digest:
                        continue
                    preliminary_object_head(
                        bucket,
                        chart_key,
                        profile=profile,
                        max_bytes=64 * 1024 * 1024,
                        expected_sha256=expected_digest,
                    )
                    label = Path(chart_key).stem.replace("_", " ").title()
                    chart_urls.append(
                        (label, preliminary_s3_url(bucket, region, chart_key))
                    )
            except Exception as exc:
                print(
                    f"WARNING: skipping malformed preliminary analysis {metadata_key}: {exc}",
                    file=sys.stderr,
                )
                metadata = None
                report_markdown = ""
                chart_urls = []
        active.append((manifest, metadata, report_markdown, chart_urls))
        if len(active) > MAX_SELECTED_RUNS:
            raise ValueError(
                f"preliminary docs exceeded {MAX_SELECTED_RUNS} active runs"
            )

    active.sort(
        key=lambda item: (
            int(item[0]["run_started_at_epoch"]),
            str(item[0]["run_id"]),
        ),
        reverse=True,
    )
    rm_tree_children(
        docs_dir / "preliminary",
        keep_files={"index.md"},
        dir_name_re=None,
    )
    index_lines = [
        "---",
        "aside: false",
        "---",
        "",
        "# Active preliminary results",
        "",
        "::: danger Preliminary views are not benchmark results",
        "These pages are incomplete. Do not compare fuzzers, declare success or failure, or stop a run early.",
        "Saved-corpus selector distributions are unavailable in these log-only checkpoints; wait for the canonical release.",
        ":::",
        "",
        f"_Generated at: **{generated_at}** (UTC)_",
        "",
    ]
    if not active:
        index_lines.extend(["_No active preliminary runs._", ""])
    else:
        index_lines.extend(
            [
                "| Run | Started (UTC) | Benchmark | Latest checkpoint | Snapshot coverage |",
                "|---|---|---|---:|---:|",
            ]
        )
        for manifest, metadata, _, _ in active:
            run_id = str(manifest["run_id"])
            uuid = str(manifest["benchmark_uuid"])
            checkpoint = str(metadata["checkpoint"]) if metadata else "waiting"
            coverage = (
                f"{metadata['present_snapshots']}/{metadata['expected_snapshots']}"
                if metadata
                else "0/0"
            )
            index_lines.append(
                f"| [`{run_id}`](./{run_id}/{uuid}/) | "
                f"`{utc_ts(int(manifest['run_started_at_epoch']))}` | "
                f"`{uuid}` | {checkpoint} | {coverage} |"
            )
        index_lines.append("")
    write_text(
        docs_dir / "preliminary" / "index.md",
        "\n".join(index_lines).rstrip() + "\n",
    )
    for manifest, metadata, report, charts in active:
        write_text(
            docs_dir
            / "preliminary"
            / str(manifest["run_id"])
            / str(manifest["benchmark_uuid"])
            / "index.md",
            render_preliminary_page(
                manifest=manifest,
                metadata=metadata,
                report_markdown=report,
                chart_urls=charts,
                generated_at=generated_at,
            ),
        )


def collect_superseded_runs(
    bucket: str, keys: list[str], *, profile: str | None
) -> tuple[dict[tuple[str, str], str], list[str]]:
    """Map (run_id, benchmark_uuid) to the reason each run is excluded.

    Malformed markers fail closed: the run is excluded either way, so a
    corrupted marker can never resurface a superseded run in the docs.
    """

    excluded: dict[tuple[str, str], str] = {}
    warnings: list[str] = []
    for key in sorted(keys):
        match = SUPERSEDED_KEY_RE.fullmatch(key)
        if not match:
            continue
        run_id, benchmark_uuid = match.group(1), match.group(2)
        try:
            raw = aws_text(
                ["s3", "cp", f"s3://{bucket}/{key}", "-"], profile=profile
            )
            status, detail = classify_superseded_marker(
                raw, run_id=run_id, benchmark_uuid=benchmark_uuid
            )
        except Exception as exc:
            status, detail = "malformed", f"marker could not be read: {exc}"
        excluded[(run_id, benchmark_uuid)] = detail
        if status != "superseded":
            warnings.append(
                f"WARNING: supersession marker {key} is invalid; excluding the "
                f"run anyway (fail closed): {detail}"
            )
    return excluded, warnings


@dataclass(frozen=True)
class Run:
    run_id: str
    run_started_at_epoch: int
    benchmark_uuid: str
    manifest_key: str
    manifest: dict
    timeout_hours: float
    analyzed: bool
    analysis_kind: str  # "analysis", "reports", or "missing"
    analysis_prefix: str  # key prefix containing report/charts (no leading slash)


def analysis_status(r: Run) -> str:
    if not r.analyzed:
        return "**Missing analysis**"
    if r.analysis_kind == "reports":
        return "Analyzed (legacy)"
    return "Analyzed"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate scfuzzbench VitePress pages from S3.")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "").strip() or "us-east-1")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--docs-dir", type=Path, default=Path("docs"))
    parser.add_argument("--grace-seconds", type=int, default=3600)
    parser.add_argument("--recent", type=int, default=20)
    args = parser.parse_args()

    bucket: str = args.bucket
    region: str = args.region
    profile: str | None = args.profile
    docs_dir: Path = args.docs_dir

    now = int(time.time())
    generated_at = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    # Discover manifests via the run-identity-first index.
    keys = list_keys(bucket, "runs/", profile=profile)
    print(f"Discovered {len(keys)} S3 keys under runs/")
    candidates: list[tuple[str, str, str]] = []
    for key in keys:
        m = RUN_MANIFEST_RE.match(key)
        if not m:
            continue
        run_id = m.group(1)
        benchmark_uuid = m.group(2)
        candidates.append((run_id, benchmark_uuid, key))

    if keys and not candidates:
        print(
            "WARNING: Found S3 keys under runs/ but none matched the manifest pattern. "
            "This usually means the manifest regex is wrong.",
            file=sys.stderr,
        )
    print(f"Matched {len(candidates)} run manifest keys")

    superseded_runs, superseded_warnings = collect_superseded_runs(
        bucket, keys, profile=profile
    )
    for warning in superseded_warnings:
        print(warning, file=sys.stderr)

    # Load manifests + filter complete runs.
    complete_runs: list[Run] = []
    for run_id, benchmark_uuid, manifest_key in sorted(candidates, reverse=True):
        if (run_id, benchmark_uuid) in superseded_runs:
            print(
                f"Excluding superseded run {run_id}/{benchmark_uuid}: "
                f"{superseded_runs[(run_id, benchmark_uuid)]}"
            )
            continue
        try:
            raw = aws_text(["s3", "cp", f"s3://{bucket}/{manifest_key}", "-"], profile=profile)
            manifest = json.loads(raw)
        except Exception:
            # Skip malformed or missing manifests.
            continue

        timeout_hours = safe_float(manifest.get("timeout_hours", 24), 24.0)
        try:
            started_at_epoch = run_started_at_epoch(run_id, manifest)
        except ValueError:
            continue
        deadline = started_at_epoch + int(timeout_hours * 3600) + int(args.grace_seconds)
        if now < deadline:
            continue

        analysis_prefix = f"analysis/{benchmark_uuid}/{run_id}"
        legacy_prefix = f"reports/{benchmark_uuid}/{run_id}"
        report_key = f"{analysis_prefix}/REPORT.md"
        legacy_report_key = f"{legacy_prefix}/REPORT.md"

        analyzed = False
        analysis_kind = "missing"
        report_prefix = analysis_prefix
        if head_exists(bucket, report_key, profile=profile):
            analyzed = True
            analysis_kind = "analysis"
        elif head_exists(bucket, legacy_report_key, profile=profile):
            analyzed = True
            analysis_kind = "reports"
            report_prefix = legacy_prefix
        complete_runs.append(
            Run(
                run_id=run_id,
                run_started_at_epoch=started_at_epoch,
                benchmark_uuid=benchmark_uuid,
                manifest_key=manifest_key,
                manifest=manifest,
                timeout_hours=timeout_hours,
                analyzed=analyzed,
                analysis_kind=analysis_kind,
                analysis_prefix=report_prefix,
            )
        )

    complete_runs.sort(
        key=lambda r: (r.run_started_at_epoch, r.run_id, r.benchmark_uuid),
        reverse=True,
    )
    print(f"Found {len(complete_runs)} complete runs (timeout + grace)")

    generate_preliminary_pages(
        bucket=bucket,
        region=region,
        profile=profile,
        docs_dir=docs_dir,
        now=now,
        generated_at=generated_at,
        superseded_runs=set(superseded_runs),
    )

    # Build compile-time EC2 pricing table for the Start Benchmark page.
    pricing_instance_types = {
        str(r.manifest.get("instance_type", "")).strip()
        for r in complete_runs
        if str(r.manifest.get("instance_type", "")).strip()
    }
    pricing_instance_types.update(DEFAULT_DOCS_EC2_PRICES_USD_PER_HOUR.keys())
    pricing_table = fetch_ec2_pricing_table(pricing_instance_types, profile=profile, region=region)
    if not pricing_table:
        pricing_table = dict(DEFAULT_DOCS_EC2_PRICES_USD_PER_HOUR)
        print("WARNING: using fallback EC2 pricing table for docs.", file=sys.stderr)
    pricing_payload = {
        "generated_at_utc": generated_at,
        "pricing_api_region": PRICING_API_REGION,
        "requested_region": region,
        "pricing_location": pricing_location_for_region(region),
        "currency": "USD",
        "prices_usd_per_hour": pricing_table,
    }
    write_text(
        docs_dir / ".vitepress" / "generated" / "ec2-pricing.json",
        json.dumps(pricing_payload, indent=2).rstrip() + "\n",
    )

    # Clean previously generated run/benchmark subpages.
    rm_tree_children(
        docs_dir / "runs",
        keep_files={"index.md"},
        dir_name_re=re.compile(
            r"^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,79}|latest)$"
        ),
    )
    rm_tree_children(
        docs_dir / "benchmarks",
        keep_files={"index.md"},
        dir_name_re=re.compile(r"^[0-9a-f]{32}$"),
    )

    # Landing page: always open Introduction.
    write_text(
        docs_dir / "index.md",
        render_redirect_page("/introduction", heading="Redirecting to introduction..."),
    )

    # /runs/latest should always resolve to the newest complete run.
    if complete_runs:
        latest_run = complete_runs[0]
        latest_to = f"/runs/{latest_run.run_id}/{latest_run.benchmark_uuid}/"
        latest_heading = (
            f"Redirecting to latest run `{latest_run.run_id}` "
            f"(`{latest_run.benchmark_uuid}`)..."
        )
    else:
        latest_to = "/runs/"
        latest_heading = "Redirecting to runs index..."
    write_text(
        docs_dir / "runs" / "latest" / "index.md",
        render_redirect_page(latest_to, heading=latest_heading),
    )

    # Runs index page.
    runs_lines: list[str] = []
    runs_lines.append("---")
    runs_lines.append("aside: false")
    runs_lines.append("---")
    runs_lines.append("")
    runs_lines.append("# Runs")
    runs_lines.append("")
    runs_lines.append(f"_Generated at: **{generated_at}** (UTC)_")
    runs_lines.append("")
    if not complete_runs:
        runs_lines.append("_No complete runs found in the S3 run index._")
        runs_lines.append("")
    else:
        runs_lines.append("| Run ID | Date (UTC) | Benchmark | Target | Commit | Timeout |")
        runs_lines.append("|---|---|---|---|---|---:|")
        for r in complete_runs:
            m = r.manifest
            repo = str(m.get("target_repo_url", "")).strip()
            commit = str(m.get("target_commit", "")).strip()
            commit_short = commit[:10] if commit else ""
            target_cell = f"[`{repo}`]({repo})" if repo.startswith("http") else f"`{repo}`"
            runs_lines.append(
                "| "
                + " | ".join(
                    [
                        f"[`{r.run_id}`](./{r.run_id}/{r.benchmark_uuid}/)",
                        f"`{utc_ts(r.run_started_at_epoch)}`",
                        f"[`{r.benchmark_uuid}`](../benchmarks/{r.benchmark_uuid}/)",
                        target_cell,
                        f"`{commit_short}`" if commit_short else "",
                        f"{r.timeout_hours:g}h",
                    ]
                )
                + " |"
            )
        runs_lines.append("")
    write_text(docs_dir / "runs" / "index.md", "\n".join(runs_lines).rstrip() + "\n")

    # Per-run-id pages (normally one benchmark per isolated Actions run).
    by_run_id: dict[str, list[Run]] = {}
    for r in complete_runs:
        by_run_id.setdefault(r.run_id, []).append(r)

    for run_id, runs in by_run_id.items():
        runs.sort(key=lambda rr: rr.benchmark_uuid)

        lines: list[str] = []
        lines.append("---")
        lines.append("aside: false")
        lines.append("---")
        lines.append("")
        lines.append(f"# Run `{run_id}`")
        lines.append("")
        lines.append(f"- Date (UTC): `{utc_ts(runs[0].run_started_at_epoch)}`")
        lines.append(f"- Benchmarks: `{len(runs)}`")
        lines.append("")
        lines.append("| Benchmark | Details | Target | Commit | Timeout |")
        lines.append("|---|---|---|---|---:|")
        for rr in runs:
            m = rr.manifest
            repo = str(m.get("target_repo_url", "")).strip()
            commit = str(m.get("target_commit", "")).strip()
            commit_short = shortish(commit, max_len=10) if commit else ""
            target_cell = f"[`{repo}`]({repo})" if repo.startswith("http") else f"`{repo}`"
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"[`{rr.benchmark_uuid}`](/benchmarks/{rr.benchmark_uuid}/)",
                        f"[Open](./{rr.benchmark_uuid}/)",
                        target_cell,
                        f"`{commit_short}`" if commit_short else "",
                        f"{rr.timeout_hours:g}h",
                    ]
                )
                + " |"
            )
        lines.append("")
        write_text(docs_dir / "runs" / str(run_id) / "index.md", "\n".join(lines).rstrip() + "\n")

    # Benchmarks index page.
    by_benchmark: dict[str, list[Run]] = {}
    for r in complete_runs:
        by_benchmark.setdefault(r.benchmark_uuid, []).append(r)
    for uuid, runs in by_benchmark.items():
        runs.sort(
            key=lambda rr: (rr.run_started_at_epoch, rr.run_id),
            reverse=True,
        )

    bench_lines: list[str] = []
    bench_lines.append("---")
    bench_lines.append("aside: false")
    bench_lines.append("---")
    bench_lines.append("")
    bench_lines.append("# Benchmarks")
    bench_lines.append("")
    bench_lines.append(f"_Generated at: **{generated_at}** (UTC)_")
    bench_lines.append("")
    if not by_benchmark:
        bench_lines.append("_No complete runs found in the S3 run index._")
        bench_lines.append("")
    else:
        bench_lines.append(
            "| Benchmark | Latest Run | Date (UTC) | Target | Commit | Type | Instance | Instances | Fuzzers | scfuzzbench | Timeout |"
        )
        bench_lines.append("|---|---|---|---|---|---|---|---:|---|---|---:|")

        # Sort by latest run time so the index is useful at a glance.
        bench_entries: list[tuple[int, str, Run]] = []
        for uuid, runs in by_benchmark.items():
            bench_entries.append((runs[0].run_started_at_epoch, uuid, runs[0]))
        bench_entries.sort(key=lambda x: (x[0], x[1]), reverse=True)

        for _, uuid, latest_run in bench_entries:
            m = latest_run.manifest
            repo = str(m.get("target_repo_url", "")).strip()
            commit = str(m.get("target_commit", "")).strip()
            commit_short = shortish(commit, max_len=10) if commit else ""
            target_label = compact_repo_label(repo)
            target_cell = (
                f"[`{target_label}`]({repo})" if target_label and repo.startswith("http") else (f"`{target_label}`" if target_label else "")
            )
            bench_type = str(m.get("benchmark_type", "")).strip()
            inst_type = str(m.get("instance_type", "")).strip()
            insts = m.get("instances_per_fuzzer", "")

            fuzzers_cell = "<br>".join(format_fuzzer_lines(m))

            sc_commit = str(m.get("scfuzzbench_commit", "")).strip()
            sc_commit_short = shortish(sc_commit, max_len=10) if sc_commit else ""
            bench_lines.append(
                "| "
                + " | ".join(
                    [
                        f"[`{short_uuid(uuid)}`](./{uuid}/)",
                        f"[`{latest_run.run_id}`](../runs/{latest_run.run_id}/{uuid}/)",
                        f"`{utc_ts(latest_run.run_started_at_epoch)}`",
                        target_cell,
                        f"`{commit_short}`" if commit_short else "",
                        f"`{bench_type}`" if bench_type else "",
                        f"`{inst_type}`" if inst_type else "",
                        f"{insts}" if insts != "" else "",
                        fuzzers_cell,
                        f"`{sc_commit_short}`" if sc_commit_short else "",
                        f"{latest_run.timeout_hours:g}h",
                    ]
                )
                + " |"
            )
        bench_lines.append("")
    write_text(docs_dir / "benchmarks" / "index.md", "\n".join(bench_lines).rstrip() + "\n")

    # Per-benchmark pages.
    for uuid, runs in by_benchmark.items():
        lines: list[str] = []
        lines.append(f"# Benchmark `{uuid}`")
        lines.append("")
        lines.append(f"_Generated at: **{generated_at}** (UTC)_")
        lines.append("")
        latest_run = runs[0]
        m = latest_run.manifest
        repo = str(m.get("target_repo_url", "")).strip()
        commit = str(m.get("target_commit", "")).strip()
        bench_type = str(m.get("benchmark_type", "")).strip()
        inst_type = str(m.get("instance_type", "")).strip()
        insts = m.get("instances_per_fuzzer", "")
        lines.append("## Latest")
        lines.append("")
        lines.append(f"- Run: [`{latest_run.run_id}`](../../runs/{latest_run.run_id}/{uuid}/)")
        lines.append(f"- Date (UTC): `{utc_ts(latest_run.run_started_at_epoch)}`")
        if repo:
            lines.append(f"- Target: [{repo}]({repo})" if repo.startswith("http") else f"- Target: `{repo}`")
        if commit:
            lines.append(f"- Commit: `{commit}`")
        if bench_type:
            lines.append(f"- Type: `{bench_type}`")
        if inst_type:
            lines.append(f"- Instance type: `{inst_type}`")
        if insts != "":
            lines.append(f"- Instances per fuzzer: `{insts}`")
        sc_commit = str(m.get("scfuzzbench_commit", "")).strip()
        if sc_commit:
            lines.append(f"- scfuzzbench commit: `{sc_commit}`")
        if isinstance(m.get("fuzzer_keys"), list) and m.get("fuzzer_keys"):
            keys = ", ".join([str(x) for x in m.get("fuzzer_keys", [])])
            lines.append(f"- fuzzers: `{keys}`")
        versions: list[str] = []
        for k in ["foundry_version", "echidna_version", "medusa_version", "recon_version"]:
            v = str(m.get(k, "")).strip()
            if v:
                versions.append(f"{k.removesuffix('_version')}@{v}")
        if versions:
            lines.append(f"- versions: `{', '.join(versions)}`")
        lines.append("")

        lines.append("## Runs")
        lines.append("")
        lines.append("| Run ID | Date (UTC) | Status |")
        lines.append("|---|---|---|")
        for r in runs:
            status = analysis_status(r)
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"[`{r.run_id}`](../../runs/{r.run_id}/{uuid}/)",
                        f"`{utc_ts(r.run_started_at_epoch)}`",
                        status,
                    ]
                )
                + " |"
            )
        lines.append("")
        write_text(docs_dir / "benchmarks" / uuid / "index.md", "\n".join(lines).rstrip() + "\n")

    # Per-run pages.
    for r in complete_runs:
        m = r.manifest
        run_dir = docs_dir / "runs" / str(r.run_id) / r.benchmark_uuid

        lines: list[str] = []
        lines.append(f"# Run `{r.run_id}`")
        lines.append("")
        lines.append(f"- Date (UTC): `{utc_ts(r.run_started_at_epoch)}`")
        lines.append(f"- Benchmark: [`{r.benchmark_uuid}`](../../../benchmarks/{r.benchmark_uuid}/)")
        lines.append(f"- Timeout: `{r.timeout_hours:g}h`")
        lines.extend(run_metadata_lines(m))
        lines.append("")

        if not r.analyzed:
            lines.append("::: warning Missing analysis")
            lines.append("This run is **complete** by time rule but is missing published analysis artifacts.")
            lines.append("It likely needs a manual **Benchmark Release** re-run or manual analysis + upload.")
            lines.append("See [Methodology](/methodology).")
            lines.append(":::")
            lines.append("")
        elif r.analysis_kind == "reports":
            lines.append("::: tip Legacy analysis")
            lines.append("This run's analysis artifacts are stored under the legacy `reports/` prefix.")
            lines.append(":::")
            lines.append("")

        instances = m.get("instances_per_fuzzer")
        is_trial = False
        if r.timeout_hours < MIN_BUDGET_HOURS:
            is_trial = True
        if instances is not None:
            try:
                if int(instances) < MIN_RUNS_PER_FUZZER:
                    is_trial = True
            except (TypeError, ValueError):
                pass
        if is_trial:
            lines.append("::: warning Trial run")
            lines.append(format_trial_run_warning())
            lines.append(":::")
            lines.append("")

        base_url = f"https://{bucket}.s3.{region}.amazonaws.com"
        analysis_base = f"{base_url}/{r.analysis_prefix}"
        logs_base = f"{base_url}/logs/{r.run_id}/{r.benchmark_uuid}"
        corpus_base = f"{base_url}/corpus/{r.run_id}/{r.benchmark_uuid}"
        invariant_chart_key = f"{r.analysis_prefix}/invariant_overlap_upset.png"
        cpu_chart_key = f"{r.analysis_prefix}/cpu_usage_over_time.png"
        memory_chart_key = f"{r.analysis_prefix}/memory_usage_over_time.png"
        broken_md_key = f"{r.analysis_prefix}/broken_invariants.md"
        broken_csv_key = f"{r.analysis_prefix}/broken_invariants.csv"
        known_bug_md_key = f"{r.analysis_prefix}/known_bug_report.md"
        known_bug_summary_csv_key = f"{r.analysis_prefix}/known_bug_summary.csv"
        known_bug_findings_csv_key = f"{r.analysis_prefix}/known_bug_findings.csv"
        throughput_summary_csv_key = f"{r.analysis_prefix}/throughput_summary.csv"
        progress_metrics_summary_csv_key = (
            f"{r.analysis_prefix}/progress_metrics_summary.csv"
        )
        selector_distribution_csv_key = (
            f"{r.analysis_prefix}/selector_distribution.csv"
        )
        selector_summary_json_key = f"{r.analysis_prefix}/selector_summary.json"
        txps_over_time_chart_key = f"{r.analysis_prefix}/tx_per_second_over_time.png"
        gasps_over_time_chart_key = f"{r.analysis_prefix}/gas_per_second_over_time.png"
        seqps_over_time_chart_key = f"{r.analysis_prefix}/seq_per_second_over_time.png"
        coverage_over_time_chart_name = "coverage_over_time.png"
        coverage_over_time_chart_key = (
            f"{r.analysis_prefix}/{coverage_over_time_chart_name}"
        )
        legacy_coverage_over_time_chart_name = "coverage_proxy_over_time.png"
        legacy_coverage_over_time_chart_key = (
            f"{r.analysis_prefix}/{legacy_coverage_over_time_chart_name}"
        )
        corpus_over_time_chart_key = f"{r.analysis_prefix}/corpus_size_over_time.png"
        runner_md_key = f"{r.analysis_prefix}/runner_resource_usage.md"
        runner_summary_csv_key = f"{r.analysis_prefix}/runner_resource_summary.csv"
        runner_timeseries_csv_key = f"{r.analysis_prefix}/runner_resource_timeseries.csv"
        has_invariant_chart = (
            r.analysis_kind == "analysis" and head_exists(bucket, invariant_chart_key, profile=profile)
        )
        has_cpu_chart = (
            r.analysis_kind == "analysis" and head_exists(bucket, cpu_chart_key, profile=profile)
        )
        has_memory_chart = (
            r.analysis_kind == "analysis" and head_exists(bucket, memory_chart_key, profile=profile)
        )
        has_broken_md = (
            r.analysis_kind == "analysis" and head_exists(bucket, broken_md_key, profile=profile)
        )
        has_broken_csv = (
            r.analysis_kind == "analysis" and head_exists(bucket, broken_csv_key, profile=profile)
        )
        has_known_bug_md = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, known_bug_md_key, profile=profile)
        )
        has_known_bug_summary_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, known_bug_summary_csv_key, profile=profile)
        )
        has_known_bug_findings_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, known_bug_findings_csv_key, profile=profile)
        )
        has_throughput_summary_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, throughput_summary_csv_key, profile=profile)
        )
        has_progress_metrics_summary_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, progress_metrics_summary_csv_key, profile=profile)
        )
        has_selector_distribution_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, selector_distribution_csv_key, profile=profile)
        )
        has_selector_summary_json = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, selector_summary_json_key, profile=profile)
        )
        has_txps_over_time_chart = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, txps_over_time_chart_key, profile=profile)
        )
        has_gasps_over_time_chart = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, gasps_over_time_chart_key, profile=profile)
        )
        has_seqps_over_time_chart = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, seqps_over_time_chart_key, profile=profile)
        )
        has_coverage_over_time_chart = False
        if r.analysis_kind == "analysis":
            if head_exists(bucket, coverage_over_time_chart_key, profile=profile):
                has_coverage_over_time_chart = True
            elif head_exists(
                bucket, legacy_coverage_over_time_chart_key, profile=profile
            ):
                has_coverage_over_time_chart = True
                coverage_over_time_chart_name = (
                    legacy_coverage_over_time_chart_name
                )
        has_corpus_over_time_chart = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, corpus_over_time_chart_key, profile=profile)
        )
        has_runner_md = (
            r.analysis_kind == "analysis" and head_exists(bucket, runner_md_key, profile=profile)
        )
        has_runner_summary_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, runner_summary_csv_key, profile=profile)
        )
        has_runner_timeseries_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, runner_timeseries_csv_key, profile=profile)
        )

        if r.analyzed:
            lines.append("## Charts")
            lines.append("")
            if r.analysis_kind == "analysis":
                lines.append(f"![Bugs Over Time]({analysis_base}/bugs_over_time.png)")
                lines.append(f"![Time To K]({analysis_base}/time_to_k.png)")
                lines.append(f"![Final Distribution]({analysis_base}/final_distribution.png)")
                lines.append(f"![Plateau And Late Share]({analysis_base}/plateau_and_late_share.png)")
                if has_invariant_chart:
                    lines.append(f"![Invariant Overlap (UpSet)]({analysis_base}/invariant_overlap_upset.png)")
                if has_coverage_over_time_chart:
                    lines.append(
                        f"![Coverage Over Time]({analysis_base}/{coverage_over_time_chart_name})"
                    )
                if has_corpus_over_time_chart:
                    lines.append(f"![Corpus Size Over Time]({analysis_base}/corpus_size_over_time.png)")
                if has_seqps_over_time_chart:
                    lines.append(f"![Seq/s Over Time]({analysis_base}/seq_per_second_over_time.png)")
                if has_txps_over_time_chart:
                    lines.append(f"![Tx/s Over Time]({analysis_base}/tx_per_second_over_time.png)")
                if has_gasps_over_time_chart:
                    lines.append(f"![Gas/s Over Time]({analysis_base}/gas_per_second_over_time.png)")
                if has_cpu_chart:
                    lines.append(f"![CPU Usage Over Time]({analysis_base}/cpu_usage_over_time.png)")
                if has_memory_chart:
                    lines.append(f"![Memory Usage Over Time]({analysis_base}/memory_usage_over_time.png)")
            else:
                # Legacy reports prefix may not contain all charts/bundles.
                lines.append(f"![Bugs Over Time]({analysis_base}/bugs_over_time.png)")
                lines.append(f"![Time To K]({analysis_base}/time_to_k.png)")
                lines.append(f"![Final Distribution]({analysis_base}/final_distribution.png)")
                lines.append(f"![Plateau And Late Share]({analysis_base}/plateau_and_late_share.png)")
            lines.append("")

            lines.append("## Report")
            lines.append("")
            try:
                report_raw = aws_text(
                    ["s3", "cp", f"s3://{bucket}/{r.analysis_prefix}/REPORT.md", "-"],
                    profile=profile,
                )
                lines.append(rewrite_headings(report_raw, add=2).rstrip())
                lines.append("")
            except Exception:
                lines.append("_Failed to fetch REPORT.md from S3._")
                lines.append("")

            if has_broken_md:
                try:
                    broken_raw = aws_text(
                        ["s3", "cp", f"s3://{bucket}/{broken_md_key}", "-"],
                        profile=profile,
                    )
                    lines.append(rewrite_headings(broken_raw, add=2).rstrip())
                    lines.append("")
                except Exception:
                    lines.append("_Failed to fetch broken_invariants.md from S3._")
                    lines.append("")
            if has_known_bug_md:
                try:
                    known_bug_raw = aws_text(
                        ["s3", "cp", f"s3://{bucket}/{known_bug_md_key}", "-"],
                        profile=profile,
                    )
                    lines.append(rewrite_headings(known_bug_raw, add=2).rstrip())
                    lines.append("")
                except Exception:
                    lines.append("_Failed to fetch known_bug_report.md from S3._")
                    lines.append("")
            if has_runner_md:
                try:
                    runner_raw = aws_text(
                        ["s3", "cp", f"s3://{bucket}/{runner_md_key}", "-"],
                        profile=profile,
                    )
                    lines.append(rewrite_headings(runner_raw, add=2).rstrip())
                    lines.append("")
                except Exception:
                    lines.append("_Failed to fetch runner_resource_usage.md from S3._")
                    lines.append("")

        # Manifest summary.
        lines.append("## Manifest")
        lines.append("")
        def add_kv(label: str, value: object) -> None:
            if value is None:
                return
            s = str(value).strip()
            if not s:
                return
            if label.lower().endswith("url") and s.startswith("http"):
                lines.append(f"- {label}: [{s}]({s})")
            else:
                lines.append(f"- {label}: `{s}`")

        add_kv("scfuzzbench_commit", m.get("scfuzzbench_commit"))
        add_kv("target_repo_url", m.get("target_repo_url"))
        add_kv("target_commit", m.get("target_commit"))
        add_kv("benchmark_type", m.get("benchmark_type"))
        add_kv("instance_type", m.get("instance_type"))
        add_kv("instances_per_fuzzer", m.get("instances_per_fuzzer"))
        add_kv("timeout_hours", m.get("timeout_hours"))
        add_kv("aws_region", m.get("aws_region"))
        add_kv("ubuntu_ami_id", m.get("ubuntu_ami_id"))
        add_kv("foundry_version", m.get("foundry_version"))
        add_kv("foundry_git_repo", m.get("foundry_git_repo"))
        add_kv("foundry_git_ref", m.get("foundry_git_ref"))
        add_kv("foundry_source_patch", m.get("foundry_source_patch"))
        add_kv("echidna_version", m.get("echidna_version"))
        add_kv("echidna_ci_repo", m.get("echidna_ci_repo"))
        add_kv("echidna_ci_run_id", m.get("echidna_ci_run_id"))
        add_kv("echidna_ci_artifact", m.get("echidna_ci_artifact"))
        add_kv("echidna_ci_sha256", m.get("echidna_ci_sha256"))
        add_kv("echidna_ci_commit", m.get("echidna_ci_commit"))
        add_kv("echidna_ci_token_kms_key_arn", m.get("echidna_ci_token_kms_key_arn"))
        add_kv("medusa_version", m.get("medusa_version"))
        add_kv("medusa_git_repo", m.get("medusa_git_repo"))
        add_kv("medusa_git_ref", m.get("medusa_git_ref"))
        add_kv("medusa_git_commit", m.get("medusa_git_commit"))
        add_kv("medusa_go_version", m.get("medusa_go_version"))
        add_kv("medusa_go_sha256", m.get("medusa_go_sha256"))
        add_kv("recon_version", m.get("recon_version"))
        if isinstance(m.get("fuzzer_keys"), list):
            lines.append(f"- fuzzer_keys: `{', '.join([str(x) for x in m.get('fuzzer_keys', [])])}`")
        lines.extend(format_seed_corpus_lines(m))
        lines.append("")

        # Artifact links.
        lines.append("## Artifacts")
        lines.append("")
        runs_manifest_url = s3_url(bucket, region, r.manifest_key)
        lines.append(f"- Manifest (index): {runs_manifest_url}")
        lines.append("")
        if r.analyzed:
            lines.append("- Report prefix: " + f"{analysis_base}/")
            if has_broken_md:
                lines.append("- Broken invariants (Markdown): " + f"{analysis_base}/broken_invariants.md")
            if has_broken_csv:
                lines.append("- Broken invariants (CSV): " + f"{analysis_base}/broken_invariants.csv")
            if has_known_bug_md:
                lines.append(
                    "- Ground-truth known bugs (Markdown): "
                    + f"{analysis_base}/known_bug_report.md"
                )
            if has_known_bug_summary_csv:
                lines.append(
                    "- Ground-truth summary (CSV): "
                    + f"{analysis_base}/known_bug_summary.csv"
                )
            if has_known_bug_findings_csv:
                lines.append(
                    "- Ground-truth findings (CSV): "
                    + f"{analysis_base}/known_bug_findings.csv"
                )
            if has_throughput_summary_csv:
                lines.append("- Throughput summary (CSV): " + f"{analysis_base}/throughput_summary.csv")
            if has_progress_metrics_summary_csv:
                lines.append(
                    "- Progress metrics summary (CSV): "
                    + f"{analysis_base}/progress_metrics_summary.csv"
                )
            if has_selector_distribution_csv:
                lines.append(
                    "- Function selector distribution (CSV): "
                    + f"{analysis_base}/selector_distribution.csv"
                )
            if has_selector_summary_json:
                lines.append(
                    "- Function selector summary and health (JSON): "
                    + f"{analysis_base}/selector_summary.json"
                )
            if has_runner_md:
                lines.append("- Runner resource usage (Markdown): " + f"{analysis_base}/runner_resource_usage.md")
            if has_runner_summary_csv:
                lines.append(
                    "- Runner resource summary (CSV): " + f"{analysis_base}/runner_resource_summary.csv"
                )
            if has_runner_timeseries_csv:
                lines.append(
                    "- Runner resource timeseries (CSV): "
                    + f"{analysis_base}/runner_resource_timeseries.csv"
                )
        if r.analysis_kind == "analysis":
            bundles_base = f"{analysis_base}/bundles"
            lines.append("- Analysis bundle: " + f"{bundles_base}/analysis.zip")
            lines.append("- Logs bundle: " + f"{bundles_base}/logs.zip")
            lines.append("- Corpus bundle: " + f"{bundles_base}/corpus.zip")
        lines.append("- Raw logs prefix: " + f"{logs_base}/")
        lines.append("- Raw corpus prefix: " + f"{corpus_base}/")
        lines.append("")
        if not r.analyzed:
            # For missing-analysis runs, list raw logs/corpus objects to help triage.
            def list_and_render(prefix: str, title: str) -> None:
                keys = list_keys(bucket, prefix, profile=profile)
                if not keys:
                    return
                # Keep the list focused on downloadable objects.
                zips = [k for k in keys if k.endswith(".zip")]
                if not zips:
                    return
                lines.append(f"<details>")
                lines.append(f"<summary>{title} ({len(zips)})</summary>")
                lines.append("")
                for k in sorted(zips):
                    name = k.split("/")[-1]
                    lines.append(f"- [{name}]({s3_url(bucket, region, k)})")
                lines.append("")
                lines.append(f"</details>")
                lines.append("")

            list_and_render(f"logs/{r.run_id}/{r.benchmark_uuid}/", "Raw logs (.zip)")
            list_and_render(f"corpus/{r.run_id}/{r.benchmark_uuid}/", "Raw corpus (.zip)")

        page_lines = with_social_preview_head(
            lines,
            page_path=f"/runs/{r.run_id}/{r.benchmark_uuid}/",
            title=run_social_title(
                run_id=r.run_id,
                benchmark_uuid=r.benchmark_uuid,
                target_repo_url=str(m.get("target_repo_url", "")).strip(),
            ),
            description=run_social_description(r),
        )
        write_text(run_dir / "index.md", "\n".join(page_lines).rstrip() + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
