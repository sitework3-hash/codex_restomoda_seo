#!/usr/bin/env python3
"""Verify the externally observable parts of the P0 deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from lxml import html


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("remediation/2026-07-12-p0/p0b-redirects-manifest.json"),
    )
    parser.add_argument("--base-url", default="https://restomoda.ru/")
    parser.add_argument(
        "--pagination-url",
        default="https://restomoda.ru/catalog/teplovoe-oborudovanie/?PAGEN_1=3",
    )
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--requests-per-second", type=float, default=5.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/generated/p0-deployment-verification.json"),
    )
    return parser.parse_args()


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


async def head_url(
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    semaphore: asyncio.Semaphore,
    url: str,
) -> dict[str, Any]:
    async with semaphore:
        await limiter.wait()
        try:
            response = await client.head(url, follow_redirects=False)
            return {
                "status": response.status_code,
                "location": response.headers.get("location"),
                "last_modified": response.headers.get("last-modified"),
                "date": response.headers.get("date"),
                "error": None,
            }
        except httpx.HTTPError as error:
            return {
                "status": None,
                "location": None,
                "last_modified": None,
                "date": None,
                "error": str(error),
            }


def expected_canonical(url: str) -> str:
    parts = urlsplit(url)
    pagination = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.startswith("PAGEN_") and key[6:].isdigit() and value.isdigit():
            if int(value) > 1:
                pagination.append((key, str(int(value))))
    pagination.sort(key=lambda item: [int(piece) if piece.isdigit() else piece for piece in item[0].split("_")])
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path or "/",
            urlencode(pagination),
            "",
        )
    )


def near_response_time(last_modified: str | None, date_header: str | None) -> bool:
    if not last_modified:
        return False
    try:
        modified = parsedate_to_datetime(last_modified)
        reference = (
            parsedate_to_datetime(date_header)
            if date_header
            else datetime.now(timezone.utc)
        )
        return abs((reference - modified).total_seconds()) <= 300
    except (TypeError, ValueError, OverflowError):
        return False


async def verify(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    fixes = manifest.get("verified_redirect_fixes", [])
    base_url = args.base_url.rstrip("/") + "/"
    pairs = [
        {
            **item,
            "source_url": urljoin(base_url, item["source"].lstrip("/")),
            "target_url": urljoin(base_url, item["new_target"].lstrip("/")),
        }
        for item in fixes
    ]
    urls = sorted(
        {value for item in pairs for value in (item["source_url"], item["target_url"])}
    )
    limiter = RateLimiter(args.requests_per_second)
    semaphore = asyncio.Semaphore(args.concurrency)
    headers = {"User-Agent": "RestomodaP0DeploymentVerifier/1.0"}
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, transport=transport
    ) as client:
        values = await asyncio.gather(
            *(head_url(client, limiter, semaphore, url) for url in urls)
        )
        responses = dict(zip(urls, values))

        redirect_failures = []
        for pair in pairs:
            source = responses[pair["source_url"]]
            target = responses[pair["target_url"]]
            actual_location = (
                urljoin(pair["source_url"], source["location"])
                if source["location"]
                else None
            )
            reasons = []
            if source["error"]:
                reasons.append("source_request_error")
            if source["status"] != 301:
                reasons.append("source_is_not_301")
            if actual_location != pair["target_url"]:
                reasons.append("location_mismatch")
            if target["error"]:
                reasons.append("target_request_error")
            if target["status"] != 200:
                reasons.append("target_is_not_200")
            if reasons:
                redirect_failures.append(
                    {
                        "source": pair["source"],
                        "expected_target": pair["new_target"],
                        "actual_source": source,
                        "actual_target": target,
                        "actual_location": actual_location,
                        "reasons": reasons,
                    }
                )

        await limiter.wait()
        pagination_response = await client.get(args.pagination_url)
        document = html.fromstring(pagination_response.content)
        canonical_nodes = document.xpath(
            "//link[contains(concat(' ', normalize-space(@rel), ' '), ' canonical ')]"
        )
        canonicals = [
            {
                "href": node.get("href"),
                "parent": node.getparent().tag if node.getparent() is not None else None,
            }
            for node in canonical_nodes
        ]
        expected = expected_canonical(str(pagination_response.url))
        canonical_ok = (
            pagination_response.status_code == 200
            and canonicals == [{"href": expected, "parent": "head"}]
        )

        last_modified_checks = []
        for _ in range(2):
            last_modified_checks.append(
                await head_url(client, limiter, semaphore, base_url)
            )
            if len(last_modified_checks) == 1:
                await asyncio.sleep(2)
        first, second = last_modified_checks
        dynamic_last_modified = (
            first["last_modified"]
            and second["last_modified"]
            and first["last_modified"] != second["last_modified"]
            and near_response_time(first["last_modified"], first["date"])
            and near_response_time(second["last_modified"], second["date"])
        )

    failures = len(redirect_failures) + int(not canonical_ok) + int(bool(dynamic_last_modified))
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "manifest": str(args.manifest),
        "redirects": {
            "checked": len(pairs),
            "passed": len(pairs) - len(redirect_failures),
            "failed": len(redirect_failures),
            "failures": redirect_failures,
        },
        "pagination_canonical": {
            "url": str(pagination_response.url),
            "status": pagination_response.status_code,
            "expected": expected,
            "found": canonicals,
            "passed": canonical_ok,
        },
        "last_modified": {
            "requests": last_modified_checks,
            "dynamic_near_current_time": bool(dynamic_last_modified),
            "passed": not bool(dynamic_last_modified),
        },
        "total_failures": failures,
        "passed": failures == 0,
    }


def main() -> None:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = asyncio.run(verify(args, manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved: {args.output}")
    print(
        f"Redirects: {result['redirects']['passed']}/{result['redirects']['checked']}, "
        f"canonical: {'ok' if result['pagination_canonical']['passed'] else 'failed'}, "
        f"Last-Modified: {'ok' if result['last_modified']['passed'] else 'failed'}, "
        f"total failures: {result['total_failures']}"
    )
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
