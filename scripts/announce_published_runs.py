#!/usr/bin/env python3
"""Announce benchmark results once the docs site actually serves them.

A benchmark request used to be closed as soon as the run was provisioned, but
the analysis and the run page are published later: until the docs site is
regenerated, the link in that closing comment returns 404. So the request now
stays open in `benchmark/04-running`, and this runs after a docs deployment to
close the loop for every request whose run page has become reachable.

Requests proposed from a watched fuzzer's pull request mention that pull
request, so GitHub cross-links the two and the update is visible from the pull
request without this repository needing any write access to it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Optional

BOT_MARKER = "<!-- scfuzzbench-benchmark-request-bot -->"
LABEL_RUNNING = "benchmark/04-running"
LABEL_PUBLISHED = "benchmark/05-published"
DOCS_BASE_URL = "https://scfuzzbench.com"

RUN_ID_RE = re.compile(r"^- Run ID: `([0-9]{1,20})`$", re.MULTILINE)
BENCHMARK_UUID_RE = re.compile(r"^- Benchmark UUID: `([0-9a-f]{32})`$", re.MULTILINE)


def run_page_url(run_id: str, benchmark_uuid: str) -> str:
    return f"{DOCS_BASE_URL}/runs/{run_id}/{benchmark_uuid}/"


def parse_run_reference(body: str) -> Optional[dict[str, str]]:
    """Read the run identity out of the request bot's comment."""
    if not isinstance(body, str) or BOT_MARKER not in body:
        return None
    run_id = RUN_ID_RE.search(body)
    benchmark_uuid = BENCHMARK_UUID_RE.search(body)
    if not run_id or not benchmark_uuid:
        return None
    return {"run_id": run_id.group(1), "benchmark_uuid": benchmark_uuid.group(1)}


def find_run_reference(comments: Iterable[dict[str, Any]]) -> Optional[dict[str, str]]:
    """Return the run identity from the most recent bot comment carrying one."""
    for comment in reversed(list(comments)):
        reference = parse_run_reference(str(comment.get("body", "")))
        if reference:
            return reference
    return None


def render_announcement(reference: dict[str, str]) -> str:
    url = run_page_url(reference["run_id"], reference["benchmark_uuid"])
    return "\n".join(
        [
            BOT_MARKER,
            "## Benchmark results published",
            "",
            f"The run page is live: {url}",
            "",
            "Run metadata:",
            f"- Run ID: `{reference['run_id']}`",
            f"- Benchmark UUID: `{reference['benchmark_uuid']}`",
        ]
    )


def select_publishable(
    issues: Iterable[dict[str, Any]],
    *,
    get_comments: Callable[[int], Iterable[dict[str, Any]]],
    page_is_live: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """Pick the open requests whose run page the docs site now serves.

    A request without a resolvable run reference, or whose page is still
    missing, is left open for the next docs deployment rather than being
    closed with a link that does not work.
    """
    ready: list[dict[str, Any]] = []
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        reference = find_run_reference(get_comments(number))
        if reference is None:
            continue
        url = run_page_url(reference["run_id"], reference["benchmark_uuid"])
        if not page_is_live(url):
            continue
        ready.append({"issue": number, "url": url, **reference})
    return ready


def _http_client(token: str) -> tuple[Callable[[str], Any], Callable[[str], bool]]:
    import urllib.error
    import urllib.request

    def api(path: str, method: str = "GET", payload: Optional[dict] = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "scfuzzbench-announce-published-runs",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {}

    def page_is_live(url: str) -> bool:
        request = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError:
            return False
        except OSError:
            return False

    return api, page_is_live


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if "/" not in args.repository:
        parser.exit(1, "error: --repository must be owner/name\n")

    api, page_is_live = _http_client(os.environ.get("GITHUB_TOKEN", ""))
    issues = api(
        f"/repos/{args.repository}/issues"
        f"?state=open&labels={LABEL_RUNNING}&per_page=100"
    )
    ready = select_publishable(
        issues if isinstance(issues, list) else [],
        get_comments=lambda number: api(
            f"/repos/{args.repository}/issues/{number}/comments?per_page=100"
        ),
        page_is_live=page_is_live,
    )
    print(json.dumps(ready, indent=2, sort_keys=True))
    if args.dry_run:
        print(f"dry run: {len(ready)} request(s) not updated")
        return 0

    for item in ready:
        number = item["issue"]
        api(
            f"/repos/{args.repository}/issues/{number}/comments",
            method="POST",
            payload={"body": render_announcement(item)},
        )
        api(
            f"/repos/{args.repository}/issues/{number}/labels",
            method="POST",
            payload={"labels": [LABEL_PUBLISHED]},
        )
        api(
            f"/repos/{args.repository}/issues/{number}",
            method="PATCH",
            payload={"state": "closed"},
        )
        print(f"announced results for #{number}: {item['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
