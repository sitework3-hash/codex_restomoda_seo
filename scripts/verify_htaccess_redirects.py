#!/usr/bin/env python3
"""Statically inspect and optionally verify legacy brand redirects."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx


CONDITION = re.compile(r"RewriteCond\s+%\{REQUEST_URI\}\s+\^(.+?)\$")
RULE = re.compile(
    r"RewriteRule\s+\S+\s+(?:https://%\{SERVER_NAME\})?(\S+)\s+\[([^\]]+)\]"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--htaccess", type=Path, default=Path("data_for_audit/site_code/.htaccess")
    )
    parser.add_argument("--base-url", default="https://restomoda.ru")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--requests-per-second", type=float, default=5.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/generated/htaccess_redirect_analysis.json"),
    )
    return parser.parse_args()


def parse_redirects(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    redirects = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = CONDITION.search(line)
        if not match:
            continue
        source = match.group(1).replace(r"\/", "/").replace(r"\.", ".")
        for rule_index in range(index + 1, min(index + 5, len(lines))):
            if lines[rule_index].lstrip().startswith("#"):
                continue
            rule = RULE.search(lines[rule_index])
            if rule:
                redirects.append(
                    {
                        "line": index + 1,
                        "source": source,
                        "target": rule.group(1),
                        "flags": rule.group(2),
                    }
                )
                break
    return redirects


def expected_brand_target(source: str) -> str | None:
    match = re.search(r"/brand_([^/]+)/$", source)
    if not match:
        return None
    return source[: match.start()] + "/" + match.group(1).replace("_", "-") + "/"


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1 / requests_per_second
        self.lock = asyncio.Lock()
        self.next_request = 0.0

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_request - now)
            if delay:
                await asyncio.sleep(delay)
            self.next_request = max(now, self.next_request) + self.interval


async def request_url(
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    semaphore: asyncio.Semaphore,
    url: str,
) -> dict[str, Any]:
    async with semaphore:
        await limiter.wait()
        try:
            response = await client.request("HEAD", url, follow_redirects=False)
            if response.status_code == 405:
                await limiter.wait()
                response = await client.get(
                    url, follow_redirects=False, headers={"Range": "bytes=0-0"}
                )
            return {
                "status": response.status_code,
                "location": response.headers.get("location"),
                "error": None,
            }
        except httpx.HTTPError as error:
            return {"status": None, "location": None, "error": str(error)}


async def verify_urls(
    urls: set[str], concurrency: int, requests_per_second: float
) -> dict[str, dict[str, Any]]:
    limiter = RateLimiter(requests_per_second)
    semaphore = asyncio.Semaphore(concurrency)
    timeout = httpx.Timeout(20.0)
    headers = {"User-Agent": "RestomodaTechnicalAudit/1.0"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        values = await asyncio.gather(
            *(request_url(client, limiter, semaphore, url) for url in sorted(urls))
        )
    return dict(zip(sorted(urls), values))


def main() -> None:
    args = parse_args()
    redirects = parse_redirects(args.htaccess)
    sources = Counter(item["source"] for item in redirects)
    targets = Counter(item["target"] for item in redirects)
    suspicious = []
    for item in redirects:
        expected = expected_brand_target(item["source"])
        if expected is None or item["target"] == expected:
            continue
        suspicious.append({**item, "expected_identity_target": expected})

    verification: dict[str, dict[str, Any]] = {}
    if args.verify:
        urls = {
            urljoin(args.base_url, value)
            for item in suspicious
            for value in (
                item["source"],
                item["target"],
                item["expected_identity_target"],
            )
        }
        verification = asyncio.run(
            verify_urls(urls, args.concurrency, args.requests_per_second)
        )
        for item in suspicious:
            item["verification"] = {
                name: verification[urljoin(args.base_url, item[key])]
                for name, key in (
                    ("source", "source"),
                    ("configured_target", "target"),
                    ("identity_target", "expected_identity_target"),
                )
            }

    summary = {
        "source": str(args.htaccess),
        "bytes": args.htaccess.stat().st_size,
        "lines": len(args.htaccess.read_text(errors="replace").splitlines()),
        "active_conditional_redirects": len(redirects),
        "duplicate_sources": sum(value > 1 for value in sources.values()),
        "duplicate_targets": sum(value > 1 for value in targets.values()),
        "brand_target_mismatch_candidates": len(suspicious),
        "verified": args.verify,
        "verification_unique_urls": len(verification),
        "mismatch_candidates": suspicious,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved: {args.output}")
    print(
        f"Redirects: {len(redirects)}, suspicious brand targets: {len(suspicious)}, "
        f"verified URLs: {len(verification)}"
    )


if __name__ == "__main__":
    main()
