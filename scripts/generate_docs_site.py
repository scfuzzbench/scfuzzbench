#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass


RUN_MANIFEST_RE = re.compile(r"^runs/([0-9]+)/([0-9a-f]{32})/manifest\.json$")
PRICING_API_REGION = "us-east-1"
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
    token: str | None = None
    while True:
        cmd = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix]
        if token:
            cmd += ["--continuation-token", token]
        data = aws_json(cmd, profile=profile)
        keys.extend([obj["Key"] for obj in data.get("Contents", [])])
        if not data.get("IsTruncated"):
            break
        token = data.get("NextContinuationToken")
        if not token:
            break
    return keys


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

    lines: list[str] = []
    for fuzzer in ordered_fuzzers:
        version = versions.get(fuzzer, "").strip()
        if not version and fuzzer == "echidna-symexec":
            echidna_version = versions.get("echidna", "").strip()
            bitwuzla_version = versions.get("bitwuzla", "").strip()
            if echidna_version and bitwuzla_version:
                version = f"{echidna_version}, bitwuzla {bitwuzla_version}"
            elif echidna_version:
                version = echidna_version
            elif bitwuzla_version:
                version = f"bitwuzla {bitwuzla_version}"
        line = f"{fuzzer} ({version})" if version else fuzzer
        lines.append(f"`{line}`")
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


@dataclass(frozen=True)
class Run:
    run_id: int
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

    # Discover manifests via the timestamp-first run index.
    keys = list_keys(bucket, "runs/", profile=profile)
    print(f"Discovered {len(keys)} S3 keys under runs/")
    candidates: list[tuple[int, str, str]] = []
    for key in keys:
        m = RUN_MANIFEST_RE.match(key)
        if not m:
            continue
        run_id = int(m.group(1))
        benchmark_uuid = m.group(2)
        candidates.append((run_id, benchmark_uuid, key))

    if keys and not candidates:
        print(
            "WARNING: Found S3 keys under runs/ but none matched the manifest pattern. "
            "This usually means the manifest regex is wrong.",
            file=sys.stderr,
        )
    print(f"Matched {len(candidates)} run manifest keys")

    # Load manifests + filter complete runs.
    complete_runs: list[Run] = []
    for run_id, benchmark_uuid, manifest_key in sorted(candidates, reverse=True):
        try:
            raw = aws_text(["s3", "cp", f"s3://{bucket}/{manifest_key}", "-"], profile=profile)
            manifest = json.loads(raw)
        except Exception:
            # Skip malformed or missing manifests.
            continue

        timeout_hours = safe_float(manifest.get("timeout_hours", 24), 24.0)
        deadline = run_id + int(timeout_hours * 3600) + int(args.grace_seconds)
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
                benchmark_uuid=benchmark_uuid,
                manifest_key=manifest_key,
                manifest=manifest,
                timeout_hours=timeout_hours,
                analyzed=analyzed,
                analysis_kind=analysis_kind,
                analysis_prefix=report_prefix,
            )
        )

    complete_runs.sort(key=lambda r: (r.run_id, r.benchmark_uuid), reverse=True)
    print(f"Found {len(complete_runs)} complete runs (timeout + grace)")

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
        dir_name_re=re.compile(r"^(?:[0-9]+|latest)$"),
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
                        f"`{utc_ts(r.run_id)}`",
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

    # Per-run-id pages (group benchmarks that share a timestamp run ID).
    by_run_id: dict[int, list[Run]] = {}
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
        lines.append(f"- Date (UTC): `{utc_ts(run_id)}`")
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
        runs.sort(key=lambda rr: rr.run_id, reverse=True)

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
            bench_entries.append((runs[0].run_id, uuid, runs[0]))
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
                        f"`{utc_ts(latest_run.run_id)}`",
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
        lines.append(f"- Date (UTC): `{utc_ts(latest_run.run_id)}`")
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
        for k in ["foundry_version", "echidna_version", "medusa_version", "bitwuzla_version"]:
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
                        f"`{utc_ts(r.run_id)}`",
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
        lines.append(f"- Date (UTC): `{utc_ts(r.run_id)}`")
        lines.append(f"- Benchmark: [`{r.benchmark_uuid}`](../../../benchmarks/{r.benchmark_uuid}/)")
        lines.append(f"- Timeout: `{r.timeout_hours:g}h`")
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

        base_url = f"https://{bucket}.s3.{region}.amazonaws.com"
        analysis_base = f"{base_url}/{r.analysis_prefix}"
        logs_base = f"{base_url}/logs/{r.run_id}/{r.benchmark_uuid}"
        corpus_base = f"{base_url}/corpus/{r.run_id}/{r.benchmark_uuid}"
        invariant_chart_key = f"{r.analysis_prefix}/invariant_overlap_upset.png"
        cpu_chart_key = f"{r.analysis_prefix}/cpu_usage_over_time.png"
        memory_chart_key = f"{r.analysis_prefix}/memory_usage_over_time.png"
        broken_md_key = f"{r.analysis_prefix}/broken_invariants.md"
        broken_csv_key = f"{r.analysis_prefix}/broken_invariants.csv"
        throughput_summary_csv_key = f"{r.analysis_prefix}/throughput_summary.csv"
        additional_metrics_summary_csv_key = (
            f"{r.analysis_prefix}/additional_metrics_summary.csv"
        )
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
        has_throughput_summary_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, throughput_summary_csv_key, profile=profile)
        )
        has_additional_metrics_summary_csv = (
            r.analysis_kind == "analysis"
            and head_exists(bucket, additional_metrics_summary_csv_key, profile=profile)
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
                lines.append(f"![Bugs Over Time (All Runs)]({analysis_base}/bugs_over_time_runs.png)")
                lines.append(f"![Time To K]({analysis_base}/time_to_k.png)")
                lines.append(f"![Final Distribution]({analysis_base}/final_distribution.png)")
                lines.append(f"![Plateau And Late Share]({analysis_base}/plateau_and_late_share.png)")
                if has_invariant_chart:
                    lines.append(f"![Invariant Overlap (UpSet)]({analysis_base}/invariant_overlap_upset.png)")
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
        add_kv("echidna_version", m.get("echidna_version"))
        add_kv("medusa_version", m.get("medusa_version"))
        add_kv("bitwuzla_version", m.get("bitwuzla_version"))
        if isinstance(m.get("fuzzer_keys"), list):
            lines.append(f"- fuzzer_keys: `{', '.join([str(x) for x in m.get('fuzzer_keys', [])])}`")
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
            if has_throughput_summary_csv:
                lines.append("- Throughput summary (CSV): " + f"{analysis_base}/throughput_summary.csv")
            if has_additional_metrics_summary_csv:
                lines.append(
                    "- Additional metrics summary (CSV): "
                    + f"{analysis_base}/additional_metrics_summary.csv"
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

        write_text(run_dir / "index.md", "\n".join(lines).rstrip() + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
