#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
GENERATED_JSON_PATH = DOCS_DIR / ".vitepress" / "generated" / "aws-costs.json"
PUBLIC_JSON_PATH = DOCS_DIR / "public" / "data" / "aws-costs.json"

COST_EXPLORER_REGION = "us-east-1"
METRIC = "UnblendedCost"
PUBLIC_DATA_PATH = "/data/aws-costs.json"
DEFAULT_HISTORY_MONTHS = 12
MAX_STACKED_SERVICES = 5


@dataclass(frozen=True)
class ServiceCost:
    service: str
    cost: Decimal


def aws_env(profile: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if profile:
        env["AWS_PROFILE"] = profile
    return env


def aws_json(
    args: list[str],
    *,
    profile: str | None,
    cli_region: str | None = None,
) -> dict:
    cmd = ["aws"]
    if cli_region:
        cmd += ["--region", cli_region]
    cmd += [*args, "--output", "json"]
    out = subprocess.check_output(cmd, text=True, env=aws_env(profile))
    return json.loads(out) if out.strip() else {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2).rstrip() + "\n")


def add_months(value: dt.date, months: int) -> dt.date:
    month_index = (value.year * 12 + (value.month - 1)) + months
    year = month_index // 12
    month = month_index % 12 + 1
    return dt.date(year, month, 1)


def month_key(value: dt.date) -> str:
    return value.strftime("%Y-%m")


def month_label(value: dt.date) -> str:
    return value.strftime("%b %Y")


def iso_date(value: dt.date) -> str:
    return value.isoformat()


def money(value: Decimal) -> float:
    rounded = round(float(value), 6)
    if abs(rounded) < 0.0000005:
        return 0.0
    return rounded


def sum_groups(groups: list[dict]) -> Decimal:
    total = Decimal("0")
    for group in groups:
        total += Decimal(group["Metrics"][METRIC]["Amount"])
    return total


def normalize_service_groups(groups: list[dict], *, total: Decimal) -> list[dict]:
    items: list[ServiceCost] = []
    for group in groups:
        cost = Decimal(group["Metrics"][METRIC]["Amount"])
        if cost == 0:
            continue
        service = str(group["Keys"][0]).strip() or "Unknown"
        items.append(ServiceCost(service=service, cost=cost))
    items.sort(key=lambda item: item.cost, reverse=True)

    normalized: list[dict] = []
    for item in items:
        share = Decimal("0")
        if total > 0:
            share = (item.cost / total) * Decimal("100")
        normalized.append(
            {
                "service": item.service,
                "cost_usd": money(item.cost),
                "share_of_total_pct": round(float(share), 4),
            }
        )
    return normalized


def make_unavailable_payload(generated_at: str, message: str, *, history_months: int) -> dict:
    return {
        "available": False,
        "generated_at_utc": generated_at,
        "metric": METRIC,
        "currency": "USD",
        "cost_explorer_region": COST_EXPLORER_REGION,
        "history_months": history_months,
        "max_stacked_services": MAX_STACKED_SERVICES,
        "public_data_path": PUBLIC_DATA_PATH,
        "error": message,
        "history": {"months": [], "top_services": []},
        "previous_month": None,
        "current_month": {
            "key": "",
            "label": "",
            "start": "",
            "end_exclusive": "",
            "total_usd": 0,
            "estimated": True,
            "tax_usd": 0,
            "by_service": [],
            "daily": [],
        },
    }


def build_payload(*, profile: str | None, history_months: int, today: dt.date) -> dict:
    current_month_start = today.replace(day=1)
    next_month_start = add_months(current_month_start, 1)
    history_start = add_months(current_month_start, -(history_months - 1))
    tomorrow = today + dt.timedelta(days=1)
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    monthly = aws_json(
        [
            "ce",
            "get-cost-and-usage",
            "--time-period",
            f"Start={iso_date(history_start)},End={iso_date(next_month_start)}",
            "--granularity",
            "MONTHLY",
            "--metrics",
            METRIC,
            "--group-by",
            "Type=DIMENSION,Key=SERVICE",
        ],
        profile=profile,
        cli_region=COST_EXPLORER_REGION,
    )
    current_daily = aws_json(
        [
            "ce",
            "get-cost-and-usage",
            "--time-period",
            f"Start={iso_date(current_month_start)},End={iso_date(tomorrow)}",
            "--granularity",
            "DAILY",
            "--metrics",
            METRIC,
        ],
        profile=profile,
        cli_region=COST_EXPLORER_REGION,
    )

    months: list[dict] = []
    service_rollup: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for bucket in monthly.get("ResultsByTime", []):
        start = dt.date.fromisoformat(bucket["TimePeriod"]["Start"])
        end = dt.date.fromisoformat(bucket["TimePeriod"]["End"])
        groups = bucket.get("Groups", [])
        total = sum_groups(groups)
        by_service = normalize_service_groups(groups, total=total)

        for item in by_service:
            service_rollup[item["service"]] += Decimal(str(item["cost_usd"]))

        tax_usd = 0.0
        for item in by_service:
            if item["service"] == "Tax":
                tax_usd = item["cost_usd"]
                break

        months.append(
            {
                "key": month_key(start),
                "label": month_label(start),
                "start": iso_date(start),
                "end_exclusive": iso_date(end),
                "total_usd": money(total),
                "estimated": bool(bucket.get("Estimated", False)) and start >= current_month_start,
                "tax_usd": tax_usd,
                "by_service": by_service,
            }
        )

    months.sort(key=lambda item: item["start"])

    daily = []
    for bucket in current_daily.get("ResultsByTime", []):
        total = Decimal(bucket["Total"][METRIC]["Amount"])
        daily.append(
            {
                "date": bucket["TimePeriod"]["Start"],
                "end_exclusive": bucket["TimePeriod"]["End"],
                "total_usd": money(total),
                "estimated": bool(bucket.get("Estimated", False)),
            }
        )

    top_services = [
        {"service": service, "cost_usd": money(cost)}
        for service, cost in sorted(service_rollup.items(), key=lambda item: item[1], reverse=True)
        if cost > 0
    ]

    previous_month_key = month_key(add_months(current_month_start, -1))
    previous_month = next((bucket for bucket in months if bucket["key"] == previous_month_key), None)
    current_month = next((bucket for bucket in months if bucket["key"] == month_key(current_month_start)), None)
    if current_month is None:
        current_month = {
            "key": month_key(current_month_start),
            "label": month_label(current_month_start),
            "start": iso_date(current_month_start),
            "end_exclusive": iso_date(next_month_start),
            "total_usd": 0,
            "estimated": True,
            "tax_usd": 0,
            "by_service": [],
        }
    current_month = {
        **current_month,
        "daily": daily,
    }

    return {
        "available": True,
        "generated_at_utc": generated_at,
        "metric": METRIC,
        "currency": "USD",
        "cost_explorer_region": COST_EXPLORER_REGION,
        "history_months": history_months,
        "max_stacked_services": MAX_STACKED_SERVICES,
        "public_data_path": PUBLIC_DATA_PATH,
        "history": {
            "months": months,
            "top_services": top_services,
        },
        "previous_month": previous_month,
        "current_month": current_month,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate static AWS cost payload for docs.")
    parser.add_argument("--profile", help="AWS named profile for local/dev usage.")
    parser.add_argument(
        "--history-months",
        type=int,
        default=DEFAULT_HISTORY_MONTHS,
        help="How many monthly buckets to include, counting the current month.",
    )
    parser.add_argument(
        "--allow-unavailable",
        action="store_true",
        help="Write an unavailable placeholder instead of failing when AWS cost lookup errors.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    today = dt.datetime.now(dt.timezone.utc).date()
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    if args.history_months < 2:
        print("--history-months must be >= 2", file=sys.stderr)
        return 1

    try:
        payload = build_payload(profile=args.profile, history_months=args.history_months, today=today)
    except Exception as exc:
        if not args.allow_unavailable:
            print(f"ERROR: failed to generate AWS cost transparency payload: {exc}", file=sys.stderr)
            return 1
        print(f"WARNING: failed to generate AWS cost transparency payload: {exc}", file=sys.stderr)
        payload = make_unavailable_payload(
            generated_at,
            str(exc),
            history_months=args.history_months,
        )

    write_json(GENERATED_JSON_PATH, payload)
    write_json(PUBLIC_JSON_PATH, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
