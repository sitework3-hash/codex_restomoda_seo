#!/usr/bin/env python3
"""Rate-limited technical crawl of URLs collected from Restomoda sitemaps."""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from lxml import etree, html


USER_AGENT = "RestomodaTechnicalAudit/1.0 (site-owner SEO audit)"
SOFT_404_PATTERNS = (
    "страница не найдена",
    "товар не найден",
    "ничего не найдено",
    "ошибка 404",
)


@dataclass
class CrawlResult:
    url: str
    source_sitemap: str
    lastmod: str
    bucket: str
    status: int | None = None
    final_url: str = ""
    redirect_count: int = 0
    redirect_chain: str = ""
    content_type: str = ""
    response_bytes: int = 0
    response_ms: int = 0
    title: str = ""
    title_length: int = 0
    description: str = ""
    description_length: int = 0
    h1: str = ""
    h1_count: int = 0
    canonical: str = ""
    canonical_is_self: bool | None = None
    meta_robots: str = ""
    x_robots_tag: str = ""
    noindex: bool = False
    html_lang: str = ""
    text_length: int = 0
    internal_links: int = 0
    external_links: int = 0
    jsonld_types: str = ""
    microdata_types: str = ""
    soft_404_hint: bool = False
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path, default=Path("reports/generated/sitemap_urls.csv.gz")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("reports/generated/page_crawl.csv.gz")
    )
    parser.add_argument(
        "--summary", type=Path, default=Path("reports/generated/page_crawl_summary.json")
    )
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--requests-per-second", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=20260711)
    return parser.parse_args()


def normalize_url(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path or "/"
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, "")
    )


def read_unique_rows(path: Path) -> list[dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    by_url: dict[str, dict[str, str]] = {}
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            normalized = normalize_url(row["url"])
            if normalized not in by_url:
                by_url[normalized] = row
            elif row["source_sitemap"] not in by_url[normalized]["source_sitemap"]:
                by_url[normalized]["source_sitemap"] += "|" + row["source_sitemap"]
    return list(by_url.values())


def stratified_sample(
    rows: list[dict[str, str]], limit: int, seed: int
) -> list[dict[str, str]]:
    if limit <= 0 or limit >= len(rows):
        return rows
    rng = random.Random(seed)
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["source_sitemap"].split("|", 1)[0]].append(row)
    selected: list[dict[str, str]] = []
    remaining: list[dict[str, str]] = []
    minimum = max(1, min(25, limit // max(1, len(groups))))
    for group_rows in groups.values():
        rng.shuffle(group_rows)
        take = min(minimum, len(group_rows))
        selected.extend(group_rows[:take])
        remaining.extend(group_rows[take:])
    rng.shuffle(remaining)
    selected.extend(remaining[: max(0, limit - len(selected))])
    rng.shuffle(selected)
    return selected[:limit]


class StartRateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / max(requests_per_second, 0.1)
        self.lock = asyncio.Lock()
        self.next_start = 0.0

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            delay = self.next_start - now
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_start = max(self.next_start, time.monotonic()) + self.interval


def first_text(document: html.HtmlElement, xpath: str) -> str:
    values = document.xpath(xpath)
    if not values:
        return ""
    value = values[0]
    if isinstance(value, etree._Element):
        value = value.text_content()
    return re.sub(r"\s+", " ", str(value)).strip()


def jsonld_types(document: html.HtmlElement) -> str:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            type_value = value.get("@type")
            if isinstance(type_value, str):
                found.add(type_value)
            elif isinstance(type_value, list):
                found.update(str(item) for item in type_value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for raw in document.xpath(
        "//script[translate(@type, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')='application/ld+json']/text()"
    ):
        try:
            visit(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return "|".join(sorted(found))


def microdata_types(document: html.HtmlElement) -> str:
    found = set()
    for itemtype in document.xpath("//*[@itemtype]/@itemtype"):
        for value in str(itemtype).split():
            found.add(value.rstrip("/").rsplit("/", 1)[-1])
    return "|".join(sorted(found))


def parse_html(result: CrawlResult, response: httpx.Response) -> None:
    try:
        document = html.fromstring(response.content, base_url=str(response.url))
    except (etree.ParserError, ValueError) as exc:
        result.error = f"HTML parse error: {exc}"
        return
    title = first_text(document, "//title")
    description = first_text(
        document,
        "//meta[translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')='description']/@content",
    )
    h1_values = [
        re.sub(r"\s+", " ", node.text_content()).strip()
        for node in document.xpath("//h1")
    ]
    canonical = first_text(
        document,
        "//link[contains(concat(' ', translate(@rel, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), ' '), ' canonical ')]/@href",
    )
    if canonical:
        canonical = normalize_url(urljoin(str(response.url), canonical))
    meta_robots = first_text(
        document,
        "//meta[translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')='robots']/@content",
    ).lower()
    lang = first_text(document, "/html/@lang")
    result.jsonld_types = jsonld_types(document)
    result.microdata_types = microdata_types(document)
    for node in document.xpath("//script|//style|//noscript|//svg"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    visible_text = re.sub(r"\s+", " ", document.text_content()).strip()
    host = urlparse(result.final_url).netloc.lower()
    internal_links = 0
    external_links = 0
    for href in document.xpath("//a[@href]/@href"):
        target = urlparse(urljoin(result.final_url, href))
        if target.scheme not in ("http", "https"):
            continue
        if target.netloc.lower() == host:
            internal_links += 1
        else:
            external_links += 1
    result.title = title
    result.title_length = len(title)
    result.description = description
    result.description_length = len(description)
    result.h1 = h1_values[0] if h1_values else ""
    result.h1_count = len(h1_values)
    result.canonical = canonical
    result.canonical_is_self = (
        normalize_url(result.final_url) == canonical if canonical else None
    )
    result.meta_robots = meta_robots
    result.noindex = "noindex" in f"{meta_robots},{result.x_robots_tag}".lower()
    result.html_lang = lang
    result.text_length = len(visible_text)
    result.internal_links = internal_links
    result.external_links = external_links
    lower_text = f"{title} {visible_text[:4000]}".lower()
    result.soft_404_hint = any(pattern in lower_text for pattern in SOFT_404_PATTERNS)


async def fetch_one(
    row: dict[str, str],
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    limiter: StartRateLimiter,
) -> CrawlResult:
    result = CrawlResult(
        url=row["url"],
        source_sitemap=row["source_sitemap"],
        lastmod=row["lastmod"],
        bucket=row["bucket"],
    )
    async with semaphore:
        for attempt in range(3):
            await limiter.wait()
            started = time.monotonic()
            try:
                response = await client.get(row["url"])
                result.response_ms = round((time.monotonic() - started) * 1000)
                if response.status_code in (429, 503) and attempt < 2:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                result.status = response.status_code
                result.final_url = str(response.url)
                result.redirect_count = len(response.history)
                result.redirect_chain = " -> ".join(
                    f"{item.status_code}:{item.url}" for item in response.history
                )
                result.content_type = response.headers.get("content-type", "")
                result.response_bytes = len(response.content)
                result.x_robots_tag = response.headers.get("x-robots-tag", "")
                if "html" in result.content_type.lower() and response.content:
                    parse_html(result, response)
                return result
            except httpx.HTTPError as exc:
                result.response_ms = round((time.monotonic() - started) * 1000)
                result.error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        return result


async def crawl(args: argparse.Namespace, rows: list[dict[str, str]]) -> list[CrawlResult]:
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    timeout = httpx.Timeout(args.timeout, connect=10.0)
    semaphore = asyncio.Semaphore(args.concurrency)
    limiter = StartRateLimiter(args.requests_per_second)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        limits=limits,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
    ) as client:
        tasks = [fetch_one(row, client, semaphore, limiter) for row in rows]
        results = []
        completed = 0
        for task in asyncio.as_completed(tasks):
            results.append(await task)
            completed += 1
            if completed % 250 == 0 or completed == len(tasks):
                print(f"Crawled {completed}/{len(tasks)}", flush=True)
        return results


def build_summary(results: list[CrawlResult], args: argparse.Namespace) -> dict[str, Any]:
    statuses = Counter(str(item.status or "error") for item in results)
    return {
        "requested_urls": len(results),
        "concurrency": args.concurrency,
        "requests_per_second": args.requests_per_second,
        "statuses": dict(statuses),
        "errors": sum(bool(item.error) for item in results),
        "redirected": sum(item.redirect_count > 0 for item in results),
        "non_200": sum(item.status != 200 for item in results),
        "missing_title": sum(item.status == 200 and not item.title for item in results),
        "missing_description": sum(
            item.status == 200 and not item.description for item in results
        ),
        "missing_h1": sum(item.status == 200 and item.h1_count == 0 for item in results),
        "multiple_h1": sum(item.status == 200 and item.h1_count > 1 for item in results),
        "missing_canonical": sum(
            item.status == 200 and not item.canonical for item in results
        ),
        "non_self_canonical": sum(
            item.status == 200 and item.canonical_is_self is False for item in results
        ),
        "noindex": sum(item.noindex for item in results),
        "soft_404_hints": sum(item.soft_404_hint for item in results),
        "jsonld_types": dict(
            Counter(
                schema_type
                for item in results
                for schema_type in item.jsonld_types.split("|")
                if schema_type
            )
        ),
        "microdata_types": dict(
            Counter(
                schema_type
                for item in results
                for schema_type in item.microdata_types.split("|")
                if schema_type
            )
        ),
        "median_response_ms": sorted(item.response_ms for item in results)[
            len(results) // 2
        ] if results else 0,
    }


def main() -> None:
    args = parse_args()
    rows = stratified_sample(read_unique_rows(args.input), args.limit, args.seed)
    results = asyncio.run(crawl(args, rows))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(item) for item in results)
    summary = build_summary(results, args)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Saved: {args.output}")
    print(f"Saved: {args.summary}")


if __name__ == "__main__":
    main()
