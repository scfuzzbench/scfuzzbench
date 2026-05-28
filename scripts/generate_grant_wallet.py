#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
GENERATED_JSON_PATH = DOCS_DIR / ".vitepress" / "generated" / "grant-wallet.json"
PUBLIC_JSON_PATH = DOCS_DIR / "public" / "data" / "grant-wallet.json"

GRANT_ADDRESS = "0x20f4614a502d61d119a461fb6e320f6f54953adc"
ZERION_API_URL = f"https://api.zerion.io/v1/wallets/{GRANT_ADDRESS}/portfolio"
ETHEREUM_RPC_URL = "https://ethereum-rpc.publicnode.com"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
PUBLIC_DATA_PATH = "/data/grant-wallet.json"

ROUND_NAME = "Ethereum Security QF Round"
ROUND_RESULTS_URL = "https://forum.giveth.io/t/ethereum-security-qf-round-results-april-23-may-14-2026/2201"
ROUND_DATES = "April 23-May 14, 2026"
MATCHING_POOL_ETH = 637.4274
TOTAL_DONATED_USD = 315020
ROUND_NETWORKS = ["Ethereum", "Gnosis", "Optimism", "Polygon", "Celo", "Arbitrum", "Base"]


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def money(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), 6)
    if abs(rounded) < 0.0000005:
        return 0.0
    return rounded


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2).rstrip() + "\n")


def base_payload(generated_at: str) -> dict:
    return {
        "available": False,
        "generated_at_utc": generated_at,
        "currency": "USD",
        "address": GRANT_ADDRESS,
        "public_data_path": PUBLIC_DATA_PATH,
        "source": "unavailable",
        "source_label": "Unavailable",
        "coverage": "Not generated",
        "zerion_url": f"https://app.zerion.io/{GRANT_ADDRESS}/overview",
        "etherscan_url": f"https://etherscan.io/address/{GRANT_ADDRESS}",
        "round": {
            "name": ROUND_NAME,
            "results_url": ROUND_RESULTS_URL,
            "dates": ROUND_DATES,
            "matching_pool_eth": MATCHING_POOL_ETH,
            "total_donated_usd": TOTAL_DONATED_USD,
            "networks": ROUND_NETWORKS,
        },
        "portfolio": {
            "total_usd": None,
            "absolute_1d_usd": None,
            "percent_1d": None,
            "eth_balance": None,
            "eth_price_usd": None,
            "positions_distribution_by_chain": {},
            "positions_distribution_by_type": {},
        },
    }


def make_unavailable_payload(generated_at: str, message: str) -> dict:
    payload = base_payload(generated_at)
    payload["error"] = message
    return payload


def request_json(url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "accept": "application/json",
            "user-agent": "scfuzzbench-grant-wallet-generator/1.0",
            **(headers or {}),
        },
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def fetch_zerion_portfolio(api_key: str, *, timeout: int) -> dict:
    encoded_key = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    query = urllib.parse.urlencode({"currency": "usd", "filter[positions]": "no_filter"})
    url = f"{ZERION_API_URL}?{query}"
    return request_json(url, headers={"Authorization": f"Basic {encoded_key}"}, timeout=timeout)


def portfolio_from_zerion(data: dict, generated_at: str) -> dict:
    attributes = data["data"]["attributes"]
    total = attributes.get("total", {})
    changes = attributes.get("changes", {})
    by_chain = attributes.get("positions_distribution_by_chain") or {}
    by_type = attributes.get("positions_distribution_by_type") or {}

    payload = base_payload(generated_at)
    payload.update(
        {
            "available": True,
            "source": "zerion",
            "source_label": "Zerion API",
            "coverage": "Full wallet portfolio across supported chains",
            "portfolio": {
                "total_usd": money(total.get("positions")),
                "absolute_1d_usd": money(changes.get("absolute_1d")),
                "percent_1d": money(changes.get("percent_1d")),
                "eth_balance": None,
                "eth_price_usd": None,
                "positions_distribution_by_chain": {
                    str(key): money(value) for key, value in by_chain.items() if value
                },
                "positions_distribution_by_type": {
                    str(key): money(value) for key, value in by_type.items() if value
                },
            },
        }
    )
    return payload


def fetch_eth_balance(*, timeout: int) -> Decimal:
    response = request_json(
        ETHEREUM_RPC_URL,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getBalance",
                "params": [GRANT_ADDRESS, "latest"],
            }
        ).encode("utf-8"),
        timeout=timeout,
    )
    if "result" not in response:
        raise RuntimeError(response.get("error") or "Ethereum RPC response did not include a result")
    wei = Decimal(int(str(response["result"]), 16))
    return wei / Decimal(10**18)


def fetch_eth_price_usd(*, timeout: int) -> Decimal:
    query = urllib.parse.urlencode({"ids": "ethereum", "vs_currencies": "usd"})
    data = request_json(f"{COINGECKO_PRICE_URL}?{query}", timeout=timeout)
    return Decimal(str(data["ethereum"]["usd"]))


def portfolio_from_eth_balance(generated_at: str, *, timeout: int) -> dict:
    eth_balance = fetch_eth_balance(timeout=timeout)
    eth_price = fetch_eth_price_usd(timeout=timeout)
    total_usd = eth_balance * eth_price

    payload = base_payload(generated_at)
    payload.update(
        {
            "available": True,
            "source": "ethereum_rpc_coingecko",
            "source_label": "Ethereum RPC + CoinGecko",
            "coverage": "Ethereum mainnet ETH balance only",
            "portfolio": {
                "total_usd": money(total_usd),
                "absolute_1d_usd": None,
                "percent_1d": None,
                "eth_balance": money(eth_balance),
                "eth_price_usd": money(eth_price),
                "positions_distribution_by_chain": {"ethereum": money(total_usd)},
                "positions_distribution_by_type": {"wallet": money(total_usd)},
            },
        }
    )
    return payload


def build_payload(*, api_key: str | None, allow_public_fallback: bool, timeout: int) -> dict:
    generated_at = now_utc()
    if api_key:
        return portfolio_from_zerion(fetch_zerion_portfolio(api_key, timeout=timeout), generated_at)
    if allow_public_fallback:
        return portfolio_from_eth_balance(generated_at, timeout=timeout)
    return make_unavailable_payload(
        generated_at,
        "ZERION_API_KEY is not configured. Set it as a docs build secret to generate full wallet balances.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate static grant wallet transparency payload for docs.")
    parser.add_argument(
        "--api-key-env",
        default="ZERION_API_KEY",
        help="Environment variable containing the Zerion API key.",
    )
    parser.add_argument(
        "--allow-unavailable",
        action="store_true",
        help="Write an unavailable placeholder instead of failing when live lookup errors.",
    )
    parser.add_argument(
        "--no-public-fallback",
        action="store_true",
        help="Do not use the ETH-only public RPC fallback when no Zerion API key is configured.",
    )
    parser.add_argument("--timeout", type=int, default=20, help="Network timeout in seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get(args.api_key_env, "").strip() or None

    try:
        payload = build_payload(
            api_key=api_key,
            allow_public_fallback=not args.no_public_fallback,
            timeout=args.timeout,
        )
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, RuntimeError) as exc:
        if not args.allow_unavailable:
            print(f"ERROR: failed to generate grant wallet payload: {exc}", file=sys.stderr)
            return 1
        print(f"WARNING: failed to generate grant wallet payload: {exc}", file=sys.stderr)
        payload = make_unavailable_payload(now_utc(), str(exc))

    write_json(GENERATED_JSON_PATH, payload)
    write_json(PUBLIC_JSON_PATH, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
